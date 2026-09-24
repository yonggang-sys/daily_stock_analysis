# -*- coding: utf-8 -*-
"""Provider error classification for tiered LLM fallback.

Raw LiteLLM / provider exceptions are classified into a small closed set of
:class:`LLMErrorType` values so the fallback chain can decide whether to:

* retry in place (transient upstream hiccup),
* downgrade to the next provider tier (quota exhausted / auth broken),
* or abort immediately (malformed request - retrying cannot help).

The module deliberately has **no third-party imports**. Classification runs in
the hot failure path of every stock analysis and must stay importable (and
unit-testable) without ``litellm``/``openai`` installed.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class LLMErrorType(str, Enum):
    """Outcome class of a single provider call failure."""

    #: 503 / timeout / single 429 without quota wording -> retry in place.
    TRANSIENT = "transient"
    #: Hard quota exhaustion (429 quota exceeded / 402 insufficient balance) -> downgrade.
    QUOTA_EXHAUSTED = "quota"
    #: Bad request (400 and friends) -> never retry, surface the error.
    INVALID_REQUEST = "invalid"
    #: 401 / 403 / invalid key -> never retry, mark provider unavailable.
    AUTH_ERROR = "auth"


#: Log-friendly reason labels keyed by error type.
_FALLBACK_REASON_LABELS: Dict["LLMErrorType", str] = {}


class ProviderUnavailableError(Exception):
    """Raised when no fallback tier is able to serve the request.

    Callers that already handle total-chain failure (the legacy
    ``_AllModelsFailedError`` path) may translate this exception; it exists so
    the chain can report *why* every tier was skipped or failed.
    """

    def __init__(
        self,
        message: str,
        *,
        providers_tried: Optional[List[str]] = None,
        reasons: Optional[List[str]] = None,
        last_error_type: Optional["LLMErrorType"] = None,
    ) -> None:
        super().__init__(message)
        self.providers_tried: List[str] = list(providers_tried or [])
        self.reasons: List[str] = list(reasons or [])
        self.last_error_type = last_error_type

    @property
    def reason_summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no_reason_recorded"


# ---------------------------------------------------------------------------
# Marker tables (checked against lowercased, concatenated error text)
# ---------------------------------------------------------------------------

_QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "quota exceeded",
    "quota_exceeded",
    "your current quota",
    "resource_exhausted",
    "resource exhausted",
    "insufficient balance",
    "insufficient credit",
    "credit balance is too low",
    "no credits",
    "out of credits",
    "billing hard limit",
    "exceeded your credit",
    "free tier",
    "配额",
    "余额不足",
    "额度已用尽",
    "欠费",
)

_AUTH_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "api key not valid",
    "api key is invalid",
    "api key has been revoked",
    "api key expired",
    "api_key_invalid",
    "no auth credentials",
    "authentication_error",
    "authentication error",
    "invalid authentication",
    "unauthenticated",
    "not authorized",
    "unauthorized",
    "forbidden",
    "permission denied",
    "access denied",
    "invalid token",
    "令牌无效",
    "鉴权失败",
)

_INVALID_MARKERS = (
    "invalid_request_error",
    "invalid request",
    "bad request",
    "invalid parameter",
    "invalid argument",
    "missing required",
    "model not found",
    "model_not_found",
    "does not exist",
    "unsupported value",
    "context length",
    "context_length_exceeded",
    "maximum context length",
    "reduce the length",
    "content policy",
    "content_policy",
    "safety settings",
    "blocked by safety",
    "request too large",
    "payload too large",
    "参数错误",
    "无效参数",
)

_TRANSIENT_MARKERS = (
    "service unavailable",
    "overloaded",
    "temporarily unavailable",
    "server had an error",
    "internal server error",
    "bad gateway",
    "gateway timeout",
    "timeout",
    "timed out",
    "deadline exceeded",
    "connection reset",
    "connection aborted",
    "connection error",
    "connection refused",
    "remote end closed",
    "rate limit",
    "rate_limit",
    "too many requests",
    "try again",
    "retry-after",
    "retry after",
    "please retry",
    "econnreset",
    "etimedout",
    "socket hang up",
    "超时",
    "过载",
    "限流",
    "服务不可用",
)

#: Attribute names that may carry an HTTP status code on provider exceptions.
_STATUS_ATTRS = ("status_code", "http_status", "status", "code", "http_code")

_STATUS_PATTERN = re.compile(r"(?<![\d.])([1-5]\d{2})(?![\d.])")

_STRING_PATTERN = re.compile(r"['\"]([^'\"]{3,})['\"]")

_MAX_TEXT_CHARS = 8000


def _iter_error_fragments(value: Any, seen: Optional[Set[int]] = None) -> List[str]:
    """Recursively collect textual fragments from an exception-like object."""
    if seen is None:
        seen = set()
    if value is None:
        return []

    marker = id(value)
    if marker in seen:
        return []
    seen.add(marker)

    fragments: List[str] = []
    if isinstance(value, (str, bytes)):
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", "replace")
            except Exception:  # pragma: no cover - defensive
                return []
        if value.strip():
            fragments.append(value)
        return fragments

    if isinstance(value, BaseException):
        if value.args:
            fragments.extend(_iter_error_fragments(value.args, seen))
        else:
            fragments.append(f"{type(value).__name__}")
        for attr in ("message", "body", "response", "error", "detail", "msg"):
            if hasattr(value, attr):
                fragments.extend(_iter_error_fragments(getattr(value, attr), seen))
        return fragments

    if isinstance(value, dict):
        for item in value.values():
            fragments.extend(_iter_error_fragments(item, seen))
        return fragments

    if isinstance(value, (list, tuple, set)):
        for item in value:
            fragments.extend(_iter_error_fragments(item, seen))
        return fragments

    # Objects such as httpx.Response expose .text / .content but no useful repr.
    extracted = False
    for attr in ("text", "content", "reason_phrase", "get_message"):
        if not hasattr(value, attr):
            continue
        raw = getattr(value, attr)
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                continue
        if isinstance(raw, (str, bytes)):
            fragments.extend(_iter_error_fragments(raw, seen))
            extracted = True
    if extracted:
        return fragments

    try:
        rendered = str(value)
    except Exception:  # pragma: no cover - defensive
        return []
    if rendered.strip():
        fragments.append(rendered)
    return fragments


def error_text(error: Any) -> str:
    """Return a normalized, lowercased text blob for ``error``."""
    joined = " ".join(fragment for fragment in _iter_error_fragments(error) if fragment)
    return joined.lower()[:_MAX_TEXT_CHARS]


def _coerce_status(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str):
        match = _STATUS_PATTERN.search(value)
        if match:
            return int(match.group(1))
    return None


def extract_status_code(error: Any) -> Optional[int]:
    """Best-effort HTTP status extraction from a provider exception."""
    candidates: List[Any] = []
    for attr in _STATUS_ATTRS:
        if hasattr(error, attr):
            candidates.append(getattr(error, attr))

    response = getattr(error, "response", None)
    if response is not None:
        for attr in _STATUS_ATTRS:
            if hasattr(response, attr):
                candidates.append(getattr(response, attr))

    for candidate in candidates:
        status = _coerce_status(candidate)
        if status is not None:
            return status

    text = error_text(error)
    if not text:
        return None
    # Only trust a bare status token from the head of the message; scanning the
    # whole blob produces false positives (e.g. token counts, ports).
    head = text[:200]
    for pattern in (
        re.compile(r"\b(?:error code|status|http)\D{0,4}([1-5]\d{2})\b"),
        re.compile(r"^\D{0,4}([1-5]\d{2})\b"),
    ):
        match = pattern.search(head)
        if match:
            return int(match.group(1))
    return None


_QUOTA_STATUS = frozenset({402})
_AUTH_STATUS = frozenset({401, 403})
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 507, 509, 522, 524})


def _contains_any(text: str, markers: tuple) -> bool:
    return any(marker in text for marker in markers)


def _has_status_token(text: str, status: int) -> bool:
    return re.search(rf"(?<![\d.]){status}(?![\d.])", text) is not None


def classify_llm_error(error: Any) -> "LLMErrorType":
    """Classify ``error`` into an :class:`LLMErrorType`.

    Ordering matters and follows the operational policy:

    1. quota wording always wins (a 429 that mentions quota must downgrade),
    2. then auth signals,
    3. then non-retryable request errors,
    4. then transient signals,
    5. anything unknown defaults to ``TRANSIENT`` (retry once in place, then
       downgrade) because that is the least destructive assumption.
    """
    text = error_text(error)
    status = extract_status_code(error)

    if status in _QUOTA_STATUS:
        return LLMErrorType.QUOTA_EXHAUSTED
    if status in _AUTH_STATUS:
        return LLMErrorType.AUTH_ERROR

    if _contains_any(text, _QUOTA_MARKERS):
        return LLMErrorType.QUOTA_EXHAUSTED

    if _contains_any(text, _AUTH_MARKERS):
        return LLMErrorType.AUTH_ERROR

    if status == 429:
        # A bare 429 without quota wording is a per-window rate limit.
        return LLMErrorType.TRANSIENT

    if status is not None and 400 <= status < 500:
        if _contains_any(text, _TRANSIENT_MARKERS):
            return LLMErrorType.TRANSIENT
        return LLMErrorType.INVALID_REQUEST

    if _contains_any(text, _INVALID_MARKERS):
        return LLMErrorType.INVALID_REQUEST

    if _contains_any(text, _TRANSIENT_MARKERS):
        return LLMErrorType.TRANSIENT

    if status is not None and status in _TRANSIENT_STATUS:
        return LLMErrorType.TRANSIENT
    if status is not None and status >= 500:
        return LLMErrorType.TRANSIENT

    for status_code in _TRANSIENT_STATUS:
        if _has_status_token(text, status_code):
            return LLMErrorType.TRANSIENT

    return LLMErrorType.TRANSIENT


#: HTTP statuses that justify retrying the *same* provider in place.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 507, 509, 522, 524})


def transient_retry_eligible(error: Any) -> bool:
    """Whether ``error`` carries an explicit transient signal.

    :func:`classify_llm_error` defaults *unknown* failures to TRANSIENT, because
    that is the safest assumption when deciding whether to **fall back**. Retrying
    the same provider in place is a different question, and there the default must
    be conservative: a generic ``RuntimeError`` ("primary failed") has no status
    and no transient wording, so it should advance to the next model immediately
    instead of burning backoff delays.
    """
    status = extract_status_code(error)
    if status is not None and status in _RETRYABLE_STATUS:
        return True
    if retry_after_seconds(error) is not None:
        return True
    text = error_text(error)
    if not text:
        return False
    if _contains_any(text, _TRANSIENT_MARKERS):
        return True
    return any(_has_status_token(text, code) for code in _RETRYABLE_STATUS)


class ErrorClassifier:
    """Thin OO facade over :func:`classify_llm_error`.

    Kept as a class (rather than free functions only) so call sites and tests
    can inject a classifier double into the fallback chain.
    """
    def classify(self, error: Any) -> "LLMErrorType":
        return classify_llm_error(error)

    def __call__(self, error: Any) -> "LLMErrorType":  # pragma: no cover - sugar
        return classify_llm_error(error)

    @staticmethod
    def extract_status_code(error: Any) -> Optional[int]:
        return extract_status_code(error)

    @staticmethod
    def error_text(error: Any) -> str:
        return error_text(error)


DEFAULT_CLASSIFIER = ErrorClassifier()

_FALLBACK_REASON_LABELS.update(
    {
        LLMErrorType.QUOTA_EXHAUSTED: "quota_exhausted",
        LLMErrorType.AUTH_ERROR: "auth_error",
        LLMErrorType.INVALID_REQUEST: "invalid_request",
        LLMErrorType.TRANSIENT: "transient_error",
    }
)


def fallback_reason_label(error_type: "LLMErrorType") -> str:
    """Return the snake_case reason label used in ``[Fallback]`` log lines."""
    return _FALLBACK_REASON_LABELS.get(error_type, "unknown_error")


_RETRY_AFTER_PATTERN = re.compile(
    r"retry[-_ ]?after\D{0,10}(\d+(?:\.\d+)?)", re.IGNORECASE
)


def _retry_after_from_headers(headers: Any) -> Optional[float]:
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # pragma: no cover - defensive
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def retry_after_seconds(error: Any) -> Optional[float]:
    """Extract a ``Retry-After`` hint (seconds) from a provider error.

    Providers surface the hint in different places (``error.retry_after``,
    ``error.headers``, ``error.response.headers``), so all three are probed
    before falling back to a text scan.
    """
    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(error, attr, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, float(value))

    for holder in (error, getattr(error, "response", None), getattr(error, "response_headers", None)):
        if holder is None:
            continue
        parsed = _retry_after_from_headers(getattr(holder, "headers", None))
        if parsed is not None:
            return parsed
        if isinstance(holder, dict):
            parsed = _retry_after_from_headers(holder)
            if parsed is not None:
                return parsed

    match = _RETRY_AFTER_PATTERN.search(error_text(error))
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None
    return None


__all__ = [
    "DEFAULT_CLASSIFIER",
    "ErrorClassifier",
    "LLMErrorType",
    "ProviderUnavailableError",
    "classify_llm_error",
    "error_text",
    "extract_status_code",
    "fallback_reason_label",
    "retry_after_seconds",
    "transient_retry_eligible",
]

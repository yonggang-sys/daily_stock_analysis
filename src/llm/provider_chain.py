# -*- coding: utf-8 -*-
"""Tiered LLM provider fallback policy.

Provider order is fixed:

1. ``gemini``     - :mod:`src.llm.provider_error` TRANSIENT errors retry in place.
2. ``zhipu``      - GLM, retried in place on TRANSIENT errors.
3. ``openrouter`` - scarce free-tier budget, reached **only** after the first
   two tiers fail for a non-retryable reason.

The chain never burns OpenRouter budget on a single 503: transient failures are
retried in place first, and the OpenRouter tier additionally consults
:class:`~src.llm.openrouter_quota.OpenRouterQuotaLedger` before every call.

This module owns policy only. It calls into the existing analyzer loop rather
than reimplementing transport, so key rotation, Router load balancing, prompt
-cache hints and usage auditing all stay where they already live.

Standard library only.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.llm.provider_error import (
    LLMErrorType,
    ProviderUnavailableError,
    error_text,
    fallback_reason_label,
    retry_after_seconds,
)

logger = logging.getLogger(__name__)

TIER_GEMINI = "gemini"
TIER_ZHIPU = "zhipu"
TIER_OPENROUTER = "openrouter"
TIER_OTHER = "other"

#: Provider order per the fallback contract.
DEFAULT_TIER_ORDER: Tuple[str, ...] = (TIER_GEMINI, TIER_ZHIPU, TIER_OPENROUTER)

#: Tiers whose budget is treated as scarce.
PROTECTED_TIERS = frozenset({TIER_OPENROUTER})

#: Display names used in ``[Fallback]`` log lines.
TIER_DISPLAY_NAMES = {
    TIER_GEMINI: "gemini",
    TIER_ZHIPU: "glm",
    TIER_OPENROUTER: "openrouter",
    TIER_OTHER: "other",
}

#: Token bucket key per tier (matches src.llm.provider_rate_limit).
TIER_RATE_LIMIT_KEYS = {
    TIER_GEMINI: TIER_GEMINI,
    TIER_ZHIPU: TIER_ZHIPU,
    TIER_OPENROUTER: TIER_OPENROUTER,
}

DEFAULT_MAX_IN_PLACE_RETRIES = 2
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 8.0
DEFAULT_PRECHECK_TIMEOUT_SECONDS = 10.0
DEFAULT_PRECHECK_PROMPT = "ping"

_JSON_MODE_REJECTION_MARKERS = (
    "response_format",
    "response_mime_type",
    "json_object",
    "json mode",
    "json schema",
)

#: 400s that mean "this model name is not available **here**" - provider
#: specific, so the next tier may still succeed. Everything else classified as
#: INVALID_REQUEST is treated as a malformed shared request and aborts.
_MODEL_UNAVAILABLE_MARKERS = (
    "model not found",
    "model_not_found",
    "does not exist",
    "no such model",
    "unknown model",
    "unsupported model",
    "model is not available",
    "model not supported",
    "invalid model",
)


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------

def resolve_provider_tier(model: str) -> str:
    """Map a LiteLLM model string to its fallback tier.

    ``openrouter`` is matched first: OpenRouter model ids embed a second slash
    (``openrouter/anthropic/claude-...``), so a naive prefix scan would
    mis-classify them.
    """
    value = str(model or "").strip().lower()
    if not value:
        return TIER_OTHER
    if value.startswith("openrouter/") or value.startswith("openrouter"):
        return TIER_OPENROUTER
    if value.startswith("gemini/") or value.startswith("gemini-") or value.startswith("models/gemini"):
        return TIER_GEMINI
    if value.startswith("zhipu/") or value.startswith("zhipuai/") or value.startswith("glm"):
        return TIER_ZHIPU
    if value.startswith("chatglm") or "zhipu" in value:
        return TIER_ZHIPU
    return TIER_OTHER


def tier_display_name(tier: str) -> str:
    return TIER_DISPLAY_NAMES.get(tier, tier or TIER_OTHER)


def is_protected_tier(tier: str) -> bool:
    return tier in PROTECTED_TIERS


# ---------------------------------------------------------------------------
# Fallback decisions
# ---------------------------------------------------------------------------

class FallbackDecision(str, Enum):
    """What the caller should do after a provider call failed."""

    #: Retry the same model (transient upstream failure).
    RETRY_IN_PLACE = "retry_in_place"
    #: Re-issue the same model without the JSON-mode request parameters.
    RETRY_WITHOUT_JSON_MODE = "retry_without_json_mode"
    #: Move to the next provider tier.
    ADVANCE = "advance"
    #: Stop the chain and surface the error.
    ABORT = "abort"


@dataclass(frozen=True)
class FallbackOutcome:
    """Decision plus the reason label used for logging/diagnostics."""

    decision: FallbackDecision
    reason: str
    mark_unhealthy: bool = False


@dataclass
class RetryPolicy:
    """In-place retry budget for transient failures."""

    max_in_place_retries: int = DEFAULT_MAX_IN_PLACE_RETRIES
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS

    def backoff_delay(self, attempt: int, *, error: Any = None) -> float:
        """Exponential backoff for ``attempt`` (1-based), honouring Retry-After."""
        exponent = max(0, int(attempt) - 1)
        delay = self.backoff_base_seconds * (2 ** exponent)
        hinted = retry_after_seconds(error) if error is not None else None
        if hinted is not None:
            delay = max(delay, hinted)
        return float(min(max(0.0, delay), self.backoff_max_seconds))


def is_json_mode_rejection(error: Any) -> bool:
    """True when the provider rejected our JSON-output request parameters.

    This is a recoverable 400: the same provider will usually answer fine once
    the ``response_format``/``response_mime_type`` hints are dropped, so it must
    not be treated as an unrecoverable invalid request.
    """
    text = error_text(error)
    if not text:
        return False
    return any(marker in text for marker in _JSON_MODE_REJECTION_MARKERS)


def is_model_unavailable(error: Any) -> bool:
    """True when a 400 means "unknown model name", not a malformed request.

    A missing model is provider-specific: Gemini rejecting ``glm-4.6`` says
    nothing about whether GLM can serve it, so the chain advances instead of
    aborting. A genuine bad-parameter 400 is shared across providers and aborts
    (per the fallback contract), which also protects OpenRouter's budget.
    """
    text = error_text(error)
    if not text:
        return False
    return any(marker in text for marker in _MODEL_UNAVAILABLE_MARKERS)


def decide_fallback(
    error_type: LLMErrorType,
    *,
    retry_policy: RetryPolicy,
    attempt: int,
    error: Any = None,
    json_mode_active: bool = False,
) -> FallbackOutcome:
    """Apply the fallback decision table for one failed provider call.

    Args:
        error_type: classification result for the failure.
        retry_policy: in-place retry budget.
        attempt: 1-based in-place attempt counter for the current model.
        error: original exception (used for Retry-After and 400 refinements).
        json_mode_active: whether JSON-output parameters were attached.
    """
    if error_type is LLMErrorType.INVALID_REQUEST:
        if json_mode_active and is_json_mode_rejection(error):
            return FallbackOutcome(
                FallbackDecision.RETRY_WITHOUT_JSON_MODE, "json_mode_unsupported"
            )
        if is_model_unavailable(error):
            return FallbackOutcome(FallbackDecision.ADVANCE, "model_unavailable")
        return FallbackOutcome(FallbackDecision.ABORT, "invalid_request")

    if error_type is LLMErrorType.AUTH_ERROR:
        return FallbackOutcome(
            FallbackDecision.ADVANCE, "auth_error", mark_unhealthy=True
        )

    if error_type is LLMErrorType.QUOTA_EXHAUSTED:
        return FallbackOutcome(FallbackDecision.ADVANCE, "quota_exhausted")

    # TRANSIENT
    if attempt <= retry_policy.max_in_place_retries:
        return FallbackOutcome(FallbackDecision.RETRY_IN_PLACE, "transient_retry")
    return FallbackOutcome(FallbackDecision.ADVANCE, "transient_retry_exhausted")


def format_fallback_log(source_tier: str, target_tier: str, reason: str) -> str:
    """Render the mandated downgrade log line."""
    return (
        f"[Fallback] {tier_display_name(source_tier)} → "
        f"{tier_display_name(target_tier)}, reason={reason}"
    )


def log_fallback(
    source_tier: str,
    target_tier: str,
    reason: str,
    *,
    logger_: Optional[logging.Logger] = None,
) -> str:
    line = format_fallback_log(source_tier, target_tier, reason)
    (logger_ or logger).warning(line)
    return line


# ---------------------------------------------------------------------------
# Provider health
# ---------------------------------------------------------------------------

@dataclass
class ProviderHealth:
    """Health verdict for a single provider."""

    provider: str
    healthy: bool
    reason: str = ""
    checked_at: float = 0.0
    strict: bool = False


class ProviderHealthRegistry:
    """Per-run provider health registry (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._health: Dict[str, ProviderHealth] = {}

    def get(self, provider: str) -> Optional[ProviderHealth]:
        with self._lock:
            return self._health.get(str(provider or "").strip().lower())

    def is_healthy(self, provider: str) -> bool:
        entry = self.get(provider)
        return True if entry is None else entry.healthy

    def set_health(
        self,
        provider: str,
        healthy: bool,
        *,
        reason: str = "",
        strict: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> ProviderHealth:
        name = str(provider or "").strip().lower()
        entry = ProviderHealth(
            provider=name,
            healthy=healthy,
            reason=reason,
            checked_at=clock(),
            strict=strict,
        )
        with self._lock:
            self._health[name] = entry
        return entry

    def mark_unhealthy(self, provider: str, *, reason: str = "") -> ProviderHealth:
        return self.set_health(provider, False, reason=reason, strict=True)

    def mark_healthy(self, provider: str, *, reason: str = "ok") -> ProviderHealth:
        return self.set_health(provider, True, reason=reason)

    def unhealthy_providers(self) -> List[str]:
        with self._lock:
            return sorted(
                name for name, entry in self._health.items() if not entry.healthy
            )

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                name: {
                    "healthy": entry.healthy,
                    "reason": entry.reason,
                    "checked_at": entry.checked_at,
                }
                for name, entry in sorted(self._health.items())
            }


# ---------------------------------------------------------------------------
# Policy bundle
# ---------------------------------------------------------------------------

@dataclass
class ProviderFallbackPolicy:
    """Everything the analyzer loop needs to gate fallback transitions."""

    enabled: bool = True
    tier_order: Tuple[str, ...] = DEFAULT_TIER_ORDER
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    protect_openrouter: bool = True
    json_output_mode: str = "auto"
    health: ProviderHealthRegistry = field(default_factory=ProviderHealthRegistry)

    def tier_rank(self, tier: str) -> int:
        try:
            return self.tier_order.index(tier)
        except ValueError:
            return len(self.tier_order)

    def next_tier(self, tier: str) -> Optional[str]:
        """Return the successor tier in the configured order."""
        if tier == TIER_OTHER:
            return None
        rank = self.tier_rank(tier)
        if rank >= len(self.tier_order) - 1:
            return None
        return self.tier_order[rank + 1]

    def skip_reason_for(self, tier: str) -> Optional[str]:
        """Return a reason string when ``tier`` must be skipped entirely."""
        if not self.enabled:
            return None
        entry = self.health.get(tier)
        if entry is not None and not entry.healthy and entry.strict:
            return f"provider_unhealthy:{entry.reason or 'unknown'}"
        return None


def _read_config(config: Any, attr: str, env: str, default: Any = None) -> Any:
    value = getattr(config, attr, None) if config is not None else None
    if value not in (None, ""):
        return value
    env_value = os.environ.get(env)
    if env_value is not None and str(env_value).strip():
        return str(env_value).strip()
    return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _coerce_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_fallback_policy(config: Any = None) -> ProviderFallbackPolicy:
    """Build the fallback policy from runtime config / environment."""
    return ProviderFallbackPolicy(
        enabled=_coerce_bool(
            _read_config(config, "llm_fallback_chain_enabled", "LLM_FALLBACK_CHAIN_ENABLED", None),
            True,
        ),
        retry_policy=RetryPolicy(
            max_in_place_retries=_coerce_int(
                _read_config(
                    config,
                    "llm_fallback_max_in_place_retries",
                    "LLM_FALLBACK_MAX_IN_PLACE_RETRIES",
                    None,
                ),
                DEFAULT_MAX_IN_PLACE_RETRIES,
            ),
            backoff_base_seconds=_coerce_float(
                _read_config(
                    config,
                    "llm_fallback_backoff_base_seconds",
                    "LLM_FALLBACK_BACKOFF_BASE_SECONDS",
                    None,
                ),
                DEFAULT_BACKOFF_BASE_SECONDS,
            ),
            backoff_max_seconds=_coerce_float(
                _read_config(
                    config,
                    "llm_fallback_backoff_max_seconds",
                    "LLM_FALLBACK_BACKOFF_MAX_SECONDS",
                    None,
                ),
                DEFAULT_BACKOFF_MAX_SECONDS,
            ),
        ),
        protect_openrouter=_coerce_bool(
            _read_config(
                config, "openrouter_quota_guard_enabled", "OPENROUTER_QUOTA_GUARD_ENABLED", None
            ),
            True,
        ),
        json_output_mode=str(
            _read_config(config, "llm_json_output_mode", "LLM_JSON_OUTPUT_MODE", "auto")
        ).strip().lower()
        or "auto",
    )


# ---------------------------------------------------------------------------
# JSON output enforcement
# ---------------------------------------------------------------------------

def json_output_kwargs_for_model(model: str) -> Dict[str, Any]:
    """Return provider-native JSON-output request parameters.

    Gemini accepts ``response_mime_type`` (mirroring the native
    ``generationConfig.responseMimeType``); GLM/OpenRouter use the
    OpenAI-compatible ``response_format``. LiteLLM also honours
    ``response_format`` for Gemini, so both are sent for that tier.
    """
    tier = resolve_provider_tier(model)
    if tier == TIER_GEMINI:
        return {
            "response_format": {"type": "json_object"},
            "extra_body": {"response_mime_type": "application/json"},
        }
    if tier in (TIER_ZHIPU, TIER_OPENROUTER):
        return {"response_format": {"type": "json_object"}}
    if tier == TIER_OTHER:
        return {"response_format": {"type": "json_object"}}
    return {}


def merge_extra_body(call_kwargs: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Deep-merge ``extra`` into ``call_kwargs["extra_body"]`` in place."""
    if not extra:
        return
    existing = call_kwargs.get("extra_body")
    if isinstance(existing, dict):
        merged = dict(extra)
        merged.update(existing)
        call_kwargs["extra_body"] = merged
    else:
        call_kwargs["extra_body"] = dict(extra)


def apply_json_output_kwargs(
    call_kwargs: Dict[str, Any],
    model: str,
    *,
    mode: str,
    json_expected: bool,
) -> bool:
    """Attach JSON-output parameters when policy allows.

    Returns ``True`` when parameters were applied (so the caller can retry
    without them if the provider rejects them).
    """
    normalized = (mode or "auto").strip().lower()
    if normalized in ("off", "false", "0", "none", "disabled"):
        return False
    if normalized == "auto" and not json_expected:
        return False

    extra = json_output_kwargs_for_model(model)
    if not extra:
        return False

    payload = dict(extra)
    extra_body = payload.pop("extra_body", None)
    call_kwargs.update(payload)
    if extra_body:
        merge_extra_body(call_kwargs, extra_body)
    return True


def strip_json_output_kwargs(call_kwargs: Dict[str, Any]) -> None:
    """Remove JSON-output parameters previously added, in place."""
    call_kwargs.pop("response_format", None)
    extra_body = call_kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        trimmed = {
            key: value
            for key, value in extra_body.items()
            if key != "response_mime_type"
        }
        if trimmed:
            call_kwargs["extra_body"] = trimmed
        else:
            call_kwargs.pop("extra_body", None)


# ---------------------------------------------------------------------------
# Startup precheck
# ---------------------------------------------------------------------------

@dataclass
class PrecheckResult:
    """Outcome of a single provider precheck."""

    provider: str
    model: str
    healthy: bool
    reason: str = "skipped"
    elapsed_seconds: float = 0.0
    quota_consumed: bool = False

    def log_line(self) -> str:
        if self.healthy:
            return f"[Provider预检] {self.provider}: healthy"
        return f"[Provider预检] {self.provider}: unhealthy ({self.reason})"


@dataclass
class PrecheckReport:
    """Aggregate precheck outcome."""

    results: List[PrecheckResult] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def healthy_providers(self) -> List[str]:
        return [r.provider for r in self.results if r.healthy]

    @property
    def unhealthy_providers(self) -> List[str]:
        return [r.provider for r in self.results if not r.healthy]

    def log_lines(self) -> List[str]:
        lines = [r.log_line() for r in self.results]
        lines.extend(
            f"[Provider预检] {provider}: skipped ({reason})" for provider, reason in self.skipped
        )
        return lines


def precheck_providers(
    providers: Iterable[Tuple[str, str]],
    *,
    ping: Callable[[str, float], Any],
    timeout_seconds: float = DEFAULT_PRECHECK_TIMEOUT_SECONDS,
    health: Optional[ProviderHealthRegistry] = None,
    quota_ledger: Any = None,
    strict: bool = False,
    prompt: str = DEFAULT_PRECHECK_PROMPT,
    clock: Callable[[], float] = time.monotonic,
    logger_: Optional[logging.Logger] = None,
) -> PrecheckReport:
    """Ping each configured provider tier with a minimal request.

    Args:
        providers: iterable of ``(provider_tier, model)`` pairs to check.
        ping: callable ``(model, timeout) -> response``; raising means failure.
        timeout_seconds: per-provider timeout.
        health: registry to record verdicts into.
        quota_ledger: OpenRouter ledger; a precheck against OpenRouter is
            charged to the daily budget because it is a real API call.
        strict: when False (default) only *definitive* failures (auth errors)
            mark a provider unhealthy, so one network blip cannot disable the
            whole chain. When True every failure counts, matching a hard
            "all providers unhealthy -> ProviderUnavailableError" gate.
        prompt: minimal prompt to send.
    """
    log = logger_ or logger
    report = PrecheckReport()

    for provider, model in providers:
        tier = provider or resolve_provider_tier(model)
        display = tier_display_name(tier)

        if tier in PROTECTED_TIERS and quota_ledger is not None:
            decision = quota_ledger.allow(LLMErrorType.QUOTA_EXHAUSTED)
            if not decision.allowed:
                report.skipped.append((display, decision.reason))
                log.warning(
                    "[Provider预检] %s: skipped，%s", display, decision.log_line()
                )
                continue

        started = clock()
        try:
            ping(model, timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - precheck must never propagate
            elapsed = max(0.0, clock() - started)
            from src.llm.provider_error import classify_llm_error  # local import: keep hot path light

            error_type = classify_llm_error(exc)
            timed_out = "timeout" in error_text(exc) or "timed out" in error_text(exc)
            reason = "timeout" if timed_out else fallback_reason_label(error_type)
            definitive = error_type is LLMErrorType.AUTH_ERROR
            healthy = not (definitive or strict)
            report.results.append(
                PrecheckResult(display, model, healthy, reason, elapsed)
            )
            if health is not None:
                health.set_health(
                    tier, healthy, reason=reason, strict=definitive or strict
                )
            log.warning(PrecheckResult(display, model, healthy, reason).log_line())
            continue

        elapsed = max(0.0, clock() - started)
        consumed = False
        if tier in PROTECTED_TIERS and quota_ledger is not None:
            quota_ledger.record(1)
            consumed = True
        report.results.append(PrecheckResult(display, model, True, "ok", elapsed, consumed))
        if health is not None:
            health.mark_healthy(tier)
        log.info(PrecheckResult(display, model, True, "ok").log_line())

    if report.results and not report.healthy_providers:
        raise ProviderUnavailableError(
            "All configured LLM providers failed health precheck",
            providers_tried=[r.provider for r in report.results],
            reasons=[f"{r.provider}:{r.reason}" for r in report.results],
        )

    return report


def build_precheck_targets(
    model_list: Iterable[str],
    *,
    tier_order: Tuple[str, ...] = DEFAULT_TIER_ORDER,
) -> List[Tuple[str, str]]:
    """Pick the first configured model per tier, in fallback order."""
    by_tier: Dict[str, str] = {}
    for model in model_list:
        tier = resolve_provider_tier(model)
        if tier in tier_order and tier not in by_tier:
            by_tier[tier] = model
    return [(tier, by_tier[tier]) for tier in tier_order if tier in by_tier]


def resolve_json_mode_enabled(mode: str, *, json_expected: bool) -> bool:
    """Whether JSON output parameters should be attached for this call."""
    normalized = (mode or "auto").strip().lower()
    if normalized in ("off", "false", "0", "none", "disabled"):
        return False
    if normalized in ("on", "true", "1", "always", "forced"):
        return True
    return bool(json_expected)


__all__ = [
    "DEFAULT_MAX_IN_PLACE_RETRIES",
    "DEFAULT_PRECHECK_TIMEOUT_SECONDS",
    "DEFAULT_TIER_ORDER",
    "FallbackDecision",
    "FallbackOutcome",
    "PROTECTED_TIERS",
    "PrecheckReport",
    "PrecheckResult",
    "ProviderFallbackPolicy",
    "ProviderHealth",
    "ProviderHealthRegistry",
    "RetryPolicy",
    "TIER_GEMINI",
    "TIER_OPENROUTER",
    "TIER_OTHER",
    "TIER_ZHIPU",
    "apply_json_output_kwargs",
    "build_fallback_policy",
    "build_precheck_targets",
    "decide_fallback",
    "format_fallback_log",
    "is_json_mode_rejection",
    "is_model_unavailable",
    "is_protected_tier",
    "json_output_kwargs_for_model",
    "log_fallback",
    "merge_extra_body",
    "precheck_providers",
    "resolve_json_mode_enabled",
    "resolve_provider_tier",
    "strip_json_output_kwargs",
    "tier_display_name",
]

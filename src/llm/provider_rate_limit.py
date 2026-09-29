# -*- coding: utf-8 -*-
"""Per-provider rate limiting (token bucket).

Free-tier providers are sensitive to request bursts: the 16-stock concurrent
analysis burst is what pushed Gemini into 429/503 territory. Each provider tier
gets its own bucket, and GitHub Actions halves the Gemini budget because the
hosted runners share egress IPs with other workloads.

Standard library only.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

GEMINI_RPM_DEFAULT = 10
ZHIPU_RPM_DEFAULT = 60
OPENROUTER_RPM_DEFAULT = 5
GITHUB_ACTIONS_RPM_FACTOR = 0.5


def is_github_actions() -> bool:
    """True when running inside GitHub Actions.

    The repo convention is a plain truthiness check on ``GITHUB_ACTIONS``.
    """
    return bool(os.environ.get("GITHUB_ACTIONS"))


class TokenBucket:
    """Classic token bucket with lazy refill.

    ``rate_per_minute`` sets the refill rate; ``capacity`` (defaults to one
    second's worth, at least 1) bounds how much burst is allowed.
    """

    def __init__(
        self,
        rate_per_minute: float,
        *,
        capacity: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate_per_second = max(0.0, float(rate_per_minute)) / 60.0
        if capacity is None:
            capacity = max(1.0, self._rate_per_second)
        self._capacity = max(1.0, float(capacity))
        self._tokens = self._capacity
        self._clock = clock
        self._updated_at = clock()
        self._lock = threading.Lock()

    @property
    def rate_per_minute(self) -> float:
        return self._rate_per_second * 60.0

    @property
    def capacity(self) -> float:
        return self._capacity

    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        if elapsed:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
            self._updated_at = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def seconds_until(self, tokens: float = 1.0) -> float:
        with self._lock:
            self._refill()
            deficit = tokens - self._tokens
            if deficit <= 0 or self._rate_per_second <= 0:
                return 0.0
            return deficit / self._rate_per_second

    def acquire(self, tokens: float = 1.0, *, timeout: Optional[float] = None) -> bool:
        """Block until ``tokens`` are available, or until ``timeout`` elapses.

        The loop is bounded in both wall-clock (via ``deadline``) and iteration
        count, so a stalled or coarse-grained clock can never spin forever.
        """
        if self._rate_per_second <= 0:
            # A bucket that never refills can only satisfy what it already holds.
            return self.try_acquire(tokens)

        if timeout is None:
            while not self.try_acquire(tokens):
                wait = self.seconds_until(tokens)
                time.sleep(max(0.005, min(wait, 1.0)))
            return True

        budget = max(0.0, float(timeout))
        deadline = self._clock() + budget
        max_iterations = max(8, int(budget * 20) + 8)
        for _ in range(max_iterations):
            if self.try_acquire(tokens):
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            wait = min(self.seconds_until(tokens), remaining)
            time.sleep(max(0.005, min(wait, 1.0)))
        return False


@dataclass
class RateLimitOutcome:
    """Result of a rate-limit admission attempt."""

    allowed: bool
    provider: str
    waited_seconds: float = 0.0
    reason: str = ""

    def log_line(self) -> str:
        if self.allowed:
            return f"[RateLimit] {self.provider}: 已放行（等待 {self.waited_seconds:.2f}s）"
        return f"[RateLimit] {self.provider}: 超时未放行（{self.reason}）"


class ProviderRateLimiter:
    """Registry of per-provider token buckets."""

    def __init__(
        self,
        rates: Optional[Dict[str, float]] = None,
        *,
        enabled: bool = True,
        half_in_github_actions: bool = True,
        clock: Callable[[], float] = time.monotonic,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._half_in_gha = bool(half_in_github_actions)
        self._clock = clock
        self._log = logger_ or logger
        base = {
            "gemini": GEMINI_RPM_DEFAULT,
            "zhipu": ZHIPU_RPM_DEFAULT,
            "openrouter": OPENROUTER_RPM_DEFAULT,
        }
        for name, value in (rates or {}).items():
            if value in (None, ""):
                continue
            try:
                base[str(name).strip().lower()] = float(value)
            except (TypeError, ValueError):
                continue
        self._rates: Dict[str, float] = {}
        for name, rpm in base.items():
            effective = rpm
            if name == "gemini" and self._half_in_gha and is_github_actions():
                effective = rpm * GITHUB_ACTIONS_RPM_FACTOR
            self._rates[name] = effective
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def rate_for(self, provider: str) -> float:
        return self._rates.get(str(provider or "").strip().lower(), 0.0)

    def bucket_for(self, provider: str) -> TokenBucket:
        name = str(provider or "").strip().lower() or "unknown"
        with self._lock:
            bucket = self._buckets.get(name)
            if bucket is None:
                rate = self._rates.get(name, 0.0)
                if rate <= 0:
                    # Unconfigured provider: keep a conservative 10 RPM default
                    # rather than leaving the call unbounded.
                    rate = GEMINI_RPM_DEFAULT
                    self._log.debug(
                        "[RateLimit] %s 未配置 RPM，回退默认 %.0f", name, rate
                    )
                bucket = TokenBucket(rate, clock=self._clock)
                self._buckets[name] = bucket
            return bucket

    def acquire(self, provider: str, *, timeout: Optional[float] = 5.0) -> RateLimitOutcome:
        if not self._enabled:
            return RateLimitOutcome(True, provider, 0.0, "disabled")
        # Only the three tiers of the fallback chain define an RPM budget. Any
        # other provider (openai/, deepseek/, custom channels, ...) is left
        # unthrottled: applying a guessed bucket there would silently pace every
        # call and break unrelated providers.
        if self.rate_for(provider) <= 0:
            return RateLimitOutcome(True, provider, 0.0, "unconfigured")
        bucket = self.bucket_for(provider)
        started = self._clock()
        allowed = bucket.acquire(1.0, timeout=timeout)
        waited = max(0.0, self._clock() - started)
        if not allowed:
            self._log.warning(
                "[RateLimit] %s: %.2fs 内未获得令牌（RPM=%.0f）",
                provider,
                timeout or 0.0,
                bucket.rate_per_minute,
            )
            return RateLimitOutcome(False, provider, waited, "timeout")
        return RateLimitOutcome(True, provider, waited, "ok")

    def snapshot(self) -> Dict[str, Any]:
        return {
            name: {
                "rpm": round(self._rates.get(name, 0.0), 2),
                "tokens": round(self.bucket_for(name).available(), 2),
            }
            for name in sorted(self._rates)
        }


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("[RateLimit] %s=%r 非法，使用默认值 %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _read(config: Any, attr: str, default: Any) -> Any:
    value = getattr(config, attr, None) if config is not None else None
    return default if value in (None, "") else value


def create_provider_rate_limiter(config: Any = None) -> ProviderRateLimiter:
    """Build the limiter from runtime config, falling back to environment."""
    rates = {
        "gemini": _read(
            config,
            "llm_provider_rpm_gemini",
            _env_float("LLM_PROVIDER_RPM_GEMINI", GEMINI_RPM_DEFAULT),
        ),
        "zhipu": _read(
            config,
            "llm_provider_rpm_zhipu",
            _env_float("LLM_PROVIDER_RPM_ZHIPU", ZHIPU_RPM_DEFAULT),
        ),
        "openrouter": _read(
            config,
            "llm_provider_rpm_openrouter",
            _env_float("LLM_PROVIDER_RPM_OPENROUTER", OPENROUTER_RPM_DEFAULT),
        ),
    }
    enabled = _read(config, "llm_provider_rate_limit_enabled", None)
    if enabled is None:
        enabled = _env_bool("LLM_PROVIDER_RATE_LIMIT_ENABLED", True)
    half = _read(config, "llm_provider_rpm_gha_halve", None)
    if half is None:
        half = _env_bool("LLM_PROVIDER_RPM_GHA_HALVE", True)

    return ProviderRateLimiter(
        rates,
        enabled=bool(enabled) if not isinstance(enabled, str) else enabled.strip().lower() in {"1", "true", "yes", "on", "y"},
        half_in_github_actions=bool(half) if not isinstance(half, str) else half.strip().lower() in {"1", "true", "yes", "on", "y"},
    )


__all__ = [
    "GEMINI_RPM_DEFAULT",
    "OPENROUTER_RPM_DEFAULT",
    "ZHIPU_RPM_DEFAULT",
    "ProviderRateLimiter",
    "RateLimitOutcome",
    "TokenBucket",
    "create_provider_rate_limiter",
    "is_github_actions",
]

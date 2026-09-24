# -*- coding: utf-8 -*-
"""OpenRouter daily-quota ledger.

OpenRouter's free tier allows a small number of requests per day (50 by
default) and it is the **last** fallback tier, so its budget is treated as a
scarce, auditable resource.

Persistence notes (important)
-----------------------------
``.github/workflows/00-daily-analysis.yml`` only publishes
``reports/latest.md``, ``reports/latest.meta.json`` and the ``ci_emit``
artifacts. ``reports/openrouter_quota.json`` is **not** in that ``git add``
list, so the sidecar file alone cannot carry the counter across GitHub Actions
runs. The ledger therefore:

* always writes the local sidecar (local runs / Docker / WebUI), and
* optionally mirrors the counter into ``reports/latest.meta.json`` under the
  ``openrouter_usage`` key, which *is* published.

On load the ledger reads both sources and keeps the newest matching-day value,
so a published mirror is honoured when the sidecar was lost.

Standard library only - no third-party imports.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

METADATA_USAGE_KEY = "openrouter_usage"

DEFAULT_DAILY_LIMIT = 50
#: Above this count only a genuine QUOTA_EXHAUSTED downgrade may still spend
#: OpenRouter budget - the remaining calls are the emergency reserve.
DEFAULT_RESERVE_AFTER = 45
#: Hard cap on how many OpenRouter calls a single process run may issue. This
#: bounds the blast radius when the cross-run counter is unavailable.
DEFAULT_PER_RUN_CAP = 10


def _utc_date(now: Optional[float] = None) -> str:
    """Return the current UTC date as ``YYYY-MM-DD`` (quota reset boundary)."""
    stamp = time.time() if now is None else now
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> bool:
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as exc:  # pragma: no cover - filesystem dependent
        logger.warning("[OpenRouter] 额度账本写入失败: %s", exc)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False


def _read_json(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


@dataclass(frozen=True)
class QuotaDecision:
    """Result of an admissibility check for one OpenRouter call."""

    allowed: bool
    reason: str
    count: int
    limit: int
    reserve_after: int

    def log_line(self) -> str:
        if self.allowed:
            return (
                f"[OpenRouter] 今日已用 {self.count}/{self.limit} 次"
                "（前两层均失败才触发）"
            )
        return (
            f"[OpenRouter] 额度保护：跳过调用"
            f"（reason={self.reason}, 已用 {self.count}/{self.limit}）"
        )


class OpenRouterQuotaLedger:
    """Daily OpenRouter call counter with reserve-aware admission control."""

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        meta_path: Optional[Path] = None,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        reserve_after: int = DEFAULT_RESERVE_AFTER,
        per_run_cap: int = DEFAULT_PER_RUN_CAP,
        mirror_to_meta: bool = True,
        clock: Callable[[], float] = time.time,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._path = Path(path) if path else None
        self._meta_path = Path(meta_path) if meta_path else None
        self._daily_limit = max(0, int(daily_limit))
        self._reserve_after = max(0, min(int(reserve_after), self._daily_limit))
        self._per_run_cap = max(0, int(per_run_cap)) or self._daily_limit
        self._mirror_to_meta = bool(mirror_to_meta)
        self._clock = clock
        self._log = logger_ or logger

        self._date = _utc_date(self._clock())
        self._count = 0
        self._run_count = 0
        self._loaded = False

    # -- properties ---------------------------------------------------------

    @property
    def date(self) -> str:
        return self._date

    @property
    def count(self) -> int:
        return self._count

    @property
    def run_count(self) -> int:
        return self._run_count

    @property
    def limit(self) -> int:
        return self._daily_limit

    @property
    def reserve_after(self) -> int:
        return self._reserve_after

    @property
    def remaining(self) -> int:
        return max(0, self._daily_limit - self._count)

    def snapshot(self) -> Dict[str, Any]:
        """Return the serializable counter payload."""
        return {
            "date": self._date,
            "count": self._count,
            "limit": self._daily_limit,
            "reserve_after": self._reserve_after,
            "run_count": self._run_count,
            "updated_at": datetime.fromtimestamp(
                self._clock(), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # -- persistence --------------------------------------------------------

    def load(self) -> "OpenRouterQuotaLedger":
        """Load the counter, honouring the freshest source for today."""
        today = _utc_date(self._clock())
        self._date = today
        self._count = 0

        for payload in (self._read_sidecar(), self._read_meta_mirror()):
            if not payload:
                continue
            entry = self._extract_usage_entry(payload)
            if not entry or entry.get("date") != today:
                # A stale day simply means the counter rolled over at 00:00 UTC.
                continue
            try:
                value = int(entry.get("count") or 0)
            except (TypeError, ValueError):
                continue
            self._count = max(self._count, max(0, value))

        self._loaded = True
        return self

    def _read_sidecar(self) -> Optional[Dict[str, Any]]:
        return _read_json(self._path)

    def _read_meta_mirror(self) -> Optional[Dict[str, Any]]:
        meta = _read_json(self._meta_path)
        if not meta:
            return None
        entry = meta.get(METADATA_USAGE_KEY)
        return entry if isinstance(entry, dict) else None

    @staticmethod
    def _extract_usage_entry(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Normalize a sidecar or metadata payload to a ``{date,count}`` entry."""
        if not payload:
            return None
        nested = payload.get(METADATA_USAGE_KEY)
        if isinstance(nested, dict):
            payload = nested
        if "date" not in payload:
            return None
        return payload

    def save(self) -> bool:
        """Persist the counter to the sidecar and (optionally) the meta mirror."""
        payload = self.snapshot()
        ok = _atomic_write_json(self._path, payload) if self._path else True
        if self._mirror_to_meta and self._meta_path is not None:
            ok = self._merge_meta_mirror(payload) and ok
        return ok

    def _merge_meta_mirror(self, payload: Dict[str, Any]) -> bool:
        """Merge-write the counter into the published metadata file.

        Only the ``openrouter_usage`` key is touched so ``ci_emit`` fields such
        as ``reco/hotspot/holding_fundflow`` stay intact.
        """
        meta = _read_json(self._meta_path) or {}
        meta[METADATA_USAGE_KEY] = payload
        return _atomic_write_json(self._meta_path, meta)

    # -- admission control --------------------------------------------------

    def allow(self, error_type: Any) -> QuotaDecision:
        """Decide whether OpenRouter may be called for the given error type."""
        self._refresh_day()

        if self._daily_limit <= 0:
            return QuotaDecision(False, "quota_disabled", self._count, self._daily_limit, self._reserve_after)
        if self._count >= self._daily_limit:
            self._log.error(
                "[OpenRouter] 今日额度已用尽（%s/%s），拒绝调用", self._count, self._daily_limit
            )
            return QuotaDecision(False, "daily_limit_reached", self._count, self._daily_limit, self._reserve_after)
        if self._run_count >= self._per_run_cap:
            return QuotaDecision(False, "per_run_cap_reached", self._count, self._daily_limit, self._reserve_after)

        error_value = getattr(error_type, "value", error_type)
        if self._count >= self._reserve_after and error_value != "quota":
            # Emergency reserve: only a real quota downgrade justifies spending it.
            return QuotaDecision(False, "reserve_protected", self._count, self._daily_limit, self._reserve_after)

        return QuotaDecision(True, "allowed", self._count, self._daily_limit, self._reserve_after)

    def record(self, amount: int = 1) -> None:
        """Account for ``amount`` consumed OpenRouter call(s)."""
        self._refresh_day()
        self._count += max(0, int(amount))
        self._run_count += max(0, int(amount))
        self.save()

    def _refresh_day(self) -> None:
        """Reset the counter when the UTC day rolls over."""
        today = _utc_date(self._clock())
        if today != self._date:
            self._log.info(
                "[OpenRouter] 额度账本跨日重置: %s -> %s", self._date, today
            )
            self._date = today
            self._count = 0
            self._run_count = 0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("[OpenRouter] %s=%r 非法，使用默认值 %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _resolve_reports_dir() -> Path:
    """Resolve the artifacts directory used by the analysis run."""
    configured = os.environ.get("LLM_QUOTA_REPORTS_DIR") or os.environ.get("REPORTS_DIR")
    if configured and configured.strip():
        return Path(configured.strip())
    return Path("reports")


def create_openrouter_quota_ledger(
    config: Any = None,
    *,
    reports_dir: Optional[Path] = None,
    clock: Callable[[], float] = time.time,
) -> OpenRouterQuotaLedger:
    """Build the ledger from the runtime config (or environment defaults)."""
    directory = Path(reports_dir) if reports_dir else _resolve_reports_dir()
    sidecar = getattr(config, "openrouter_quota_file", "") or ""
    meta = getattr(config, "openrouter_quota_meta_file", "") or ""

    path = Path(sidecar) if str(sidecar).strip() else directory / "openrouter_quota.json"
    meta_path = Path(meta) if str(meta).strip() else directory / "latest.meta.json"

    ledger = OpenRouterQuotaLedger(
        path=path,
        meta_path=meta_path,
        daily_limit=_config_int(config, "openrouter_quota_daily_limit", "OPENROUTER_QUOTA_DAILY_LIMIT", DEFAULT_DAILY_LIMIT),
        reserve_after=_config_int(config, "openrouter_quota_reserve_after", "OPENROUTER_QUOTA_RESERVE_AFTER", DEFAULT_RESERVE_AFTER),
        per_run_cap=_config_int(config, "openrouter_quota_per_run_cap", "OPENROUTER_QUOTA_PER_RUN_CAP", DEFAULT_PER_RUN_CAP),
        mirror_to_meta=_config_bool(
            config,
            "openrouter_quota_mirror_to_meta",
            "OPENROUTER_QUOTA_MIRROR_TO_META",
            # Only meaningful when the metadata file is actually published.
            default=True,
        ),
        clock=clock,
    )
    return ledger.load()


def _config_int(config: Any, attr: str, env: str, default: int) -> int:
    value = getattr(config, attr, None) if config is not None else None
    if value in (None, ""):
        return _env_int(env, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return _env_int(env, default)


def _config_bool(config: Any, attr: str, env: str, *, default: bool) -> bool:
    value = getattr(config, attr, None) if config is not None else None
    if value in (None, ""):
        return _env_bool(env, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


__all__ = [
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_PER_RUN_CAP",
    "DEFAULT_RESERVE_AFTER",
    "METADATA_USAGE_KEY",
    "OpenRouterQuotaLedger",
    "QuotaDecision",
    "create_openrouter_quota_ledger",
]

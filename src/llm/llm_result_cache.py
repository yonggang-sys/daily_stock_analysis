# -*- coding: utf-8 -*-
"""SQLite-backed LLM result cache.

Cache key is ``sha256(stock_code + trade_date + prompt_hash)`` so a hit can only
ever return the same prompt evaluated for the same stock on the same trading
day. Entries expire after a TTL (24h by default); a hit is reported to callers
as ``data_source="llm_cache"``.

Persistence notes
-----------------
The cache database is **not** part of the ``00-daily-analysis.yml`` publish set
(only ``reports/latest.md``/``reports/latest.meta.json`` plus the ``ci_emit``
artifacts are pushed), so on GitHub Actions the cache is effectively per-run
unless ``LLM_CACHE_PATH`` points at a location that survives the run. That is
acceptable: the cache primarily removes duplicate work inside a run and gives
local/Docker/WebUI runs real cross-run savings.

Standard library only (``sqlite3`` is bundled with CPython).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 24 * 60 * 60
CACHE_DATA_SOURCE = "llm_cache"
DEFAULT_CACHE_DIR = Path("data")
DEFAULT_CACHE_FILENAME = "llm_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key    TEXT PRIMARY KEY,
    stock_code   TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    model        TEXT,
    provider     TEXT,
    response     TEXT NOT NULL,
    usage_json   TEXT,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL
)
"""

_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_expires ON llm_cache (expires_at)"
)


def build_cache_key(stock_code: str, trade_date: str, prompt_hash: str) -> str:
    """Return the canonical cache key for one analysis request."""
    raw = f"{stock_code or ''}|{trade_date or ''}|{prompt_hash or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_prompt(prompt: str, system_prompt: Optional[str] = None) -> str:
    """Hash the prompt payload (system + user) into a stable short digest."""
    digest = hashlib.sha256()
    digest.update((system_prompt or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((prompt or "").encode("utf-8"))
    return digest.hexdigest()


@dataclass
class CacheHit:
    """A cache hit, shaped for direct consumption by the analysis layer."""

    text: str
    model: str
    provider: str
    usage: Dict[str, Any]
    created_at: float
    data_source: str = CACHE_DATA_SOURCE


class LLMResultCache:
    """Thread-safe SQLite result cache with TTL expiry."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._path = Path(path) if path else None
        self._ttl = max(0, int(ttl_seconds))
        self._enabled = bool(enabled) and self._path is not None
        self._clock = clock
        self._log = logger_ or logger
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._available = False
        if self._enabled:
            self._available = self._initialize()

    # -- lifecycle ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled and self._available

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def _initialize(self) -> bool:
        try:
            assert self._path is not None
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                timeout=5.0,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            conn.commit()
            self._conn = conn
            return True
        except Exception as exc:
            self._log.warning("[LLMCache] 初始化失败（缓存已禁用）: %s", exc)
            self._conn = None
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # pragma: no cover - defensive
                    pass
                self._conn = None

    # -- read/write ---------------------------------------------------------

    def get(
        self,
        stock_code: str,
        trade_date: str,
        prompt_hash: str,
    ) -> Optional[CacheHit]:
        if not self.enabled:
            return None
        key = build_cache_key(stock_code, trade_date, prompt_hash)
        now = self._clock()
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT response, model, provider, usage_json, created_at, expires_at"
                    " FROM llm_cache WHERE cache_key = ?",
                    (key,),
                ).fetchone()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.debug("[LLMCache] 读取失败: %s", exc)
            return None

        if not row:
            return None
        response, model, provider, usage_json, created_at, expires_at = row
        if expires_at and expires_at < now:
            self.delete(stock_code, trade_date, prompt_hash)
            return None

        usage: Dict[str, Any] = {}
        if usage_json:
            try:
                parsed = json.loads(usage_json)
                if isinstance(parsed, dict):
                    usage = parsed
            except (TypeError, ValueError):
                usage = {}
        usage = dict(usage)
        usage["data_source"] = CACHE_DATA_SOURCE
        usage["cache_key"] = key
        return CacheHit(
            text=response or "",
            model=model or "",
            provider=provider or "",
            usage=usage,
            created_at=float(created_at or 0.0),
        )

    def put(
        self,
        stock_code: str,
        trade_date: str,
        prompt_hash: str,
        response: str,
        *,
        model: str = "",
        provider: str = "",
        usage: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.enabled or not response:
            return False
        key = build_cache_key(stock_code, trade_date, prompt_hash)
        now = self._clock()
        payload = None
        if usage:
            try:
                payload = json.dumps(usage, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                payload = None
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR REPLACE INTO llm_cache"
                    " (cache_key, stock_code, trade_date, prompt_hash, model, provider,"
                    "  response, usage_json, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        stock_code or "",
                        trade_date or "",
                        prompt_hash or "",
                        model or "",
                        provider or "",
                        response,
                        payload,
                        now,
                        now + self._ttl,
                    ),
                )
                self._conn.commit()
            return True
        except Exception as exc:  # pragma: no cover - defensive
            self._log.debug("[LLMCache] 写入失败: %s", exc)
            return False

    def delete(self, stock_code: str, trade_date: str, prompt_hash: str) -> None:
        if not self.enabled:
            return
        key = build_cache_key(stock_code, trade_date, prompt_hash)
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("DELETE FROM llm_cache WHERE cache_key = ?", (key,))
                self._conn.commit()
        except Exception:  # pragma: no cover - defensive
            pass

    def purge_expired(self) -> int:
        if not self.enabled:
            return 0
        try:
            with self._lock:
                assert self._conn is not None
                cursor = self._conn.execute(
                    "DELETE FROM llm_cache WHERE expires_at < ?", (self._clock(),)
                )
                self._conn.commit()
                return int(cursor.rowcount or 0)
        except Exception:  # pragma: no cover - defensive
            return 0

    def stats(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "path": str(self._path) if self._path else None}
        try:
            with self._lock:
                assert self._conn is not None
                total = self._conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
                live = self._conn.execute(
                    "SELECT COUNT(*) FROM llm_cache WHERE expires_at >= ?",
                    (self._clock(),),
                ).fetchone()[0]
            return {
                "enabled": True,
                "path": str(self._path),
                "ttl_seconds": self._ttl,
                "total": int(total or 0),
                "live": int(live or 0),
            }
        except Exception:  # pragma: no cover - defensive
            return {"enabled": True, "path": str(self._path), "error": "stats_failed"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("[LLMCache] %s=%r 非法，使用默认值 %s", name, raw, default)
        return default


def _resolve_cache_path(config: Any) -> Optional[Path]:
    explicit = getattr(config, "llm_cache_path", None) if config is not None else None
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())

    env_path = os.environ.get("LLM_CACHE_PATH", "").strip()
    if env_path:
        return Path(env_path)

    # 默认落在 ./data/ 下，与 DATABASE_PATH 的默认值（./data/stock_analysis.db）
    # 保持同一约定。刻意避开 reports/：reports/ 是产物目录，只有 latest.* 与
    # 方案A 的少数 json 在 workflow 的 git add 范围内，缓存属于运行期状态，
    # 不应与产物混放。
    return DEFAULT_CACHE_DIR / DEFAULT_CACHE_FILENAME


def _explicitly_configured_path(config: Any) -> bool:
    """Whether the operator pinned the cache location on purpose."""
    explicit = getattr(config, "llm_cache_path", None) if config is not None else None
    if explicit and str(explicit).strip():
        return True
    return bool(os.environ.get("LLM_CACHE_PATH", "").strip())


def create_llm_result_cache(config: Any = None) -> LLMResultCache:
    """Build the cache from runtime config / environment."""
    enabled_value = getattr(config, "llm_cache_enabled", None) if config is not None else None
    if enabled_value in (None, ""):
        enabled = _env_bool("LLM_CACHE_ENABLED", True)
    elif isinstance(enabled_value, bool):
        enabled = enabled_value
    else:
        enabled = str(enabled_value).strip().lower() in {"1", "true", "yes", "on", "y"}

    ttl_value = getattr(config, "llm_cache_ttl_seconds", None) if config is not None else None
    try:
        ttl = int(ttl_value) if ttl_value not in (None, "") else _env_int("LLM_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = _env_int("LLM_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS)

    path = _resolve_cache_path(config)

    # 测试密闭性：默认路径（./data/llm_cache.db）是仓库内的持久文件，若测试进程
    # 也读写它，断言「调用了 N 次 LLM」之类的用例就会依赖执行顺序与本地残留状态，
    # 在 CI 上表现为随机失败。因此在 pytest 下、且调用方没有显式指定路径时关闭
    # 运行时缓存（显式指定 LLM_CACHE_PATH / config.llm_cache_path 时仍然生效，
    # 便于专门测试缓存行为）。
    if enabled and "pytest" in sys.modules and not _explicitly_configured_path(config):
        enabled = False
        logger.debug("[LLMCache] 检测到 pytest 运行且未显式指定缓存路径，运行时缓存已禁用")

    cache = LLMResultCache(path, ttl_seconds=ttl, enabled=enabled)
    if cache.enabled and os.environ.get("GITHUB_ACTIONS") and not os.environ.get("LLM_CACHE_PATH"):
        logger.info(
            "[LLMCache] GitHub Actions 下使用默认路径 %s，该路径不会随产物发布，"
            "缓存仅在本轮运行内有效；如需跨轮复用请设置 LLM_CACHE_PATH 指向持久化位置。",
            cache.path,
        )
    return cache


__all__ = [
    "CACHE_DATA_SOURCE",
    "DEFAULT_TTL_SECONDS",
    "CacheHit",
    "LLMResultCache",
    "build_cache_key",
    "create_llm_result_cache",
    "hash_prompt",
]

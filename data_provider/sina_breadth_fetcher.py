# -*- coding: utf-8 -*-
"""Sina aggregation-based A-share breadth provider (market statistics only).

Why this provider exists
------------------------
On GitHub Actions runners the generic provider chain cannot produce market
breadth, so the daily market-review report states
"涨跌家数与成交额数据缺失（显示为0）" and ``market_light`` falls back to a neutral
50 breadth score, which understates an obviously one-sided tape.

Observed on run #249 (repo Actions, 2026-10-23 07:40 UTC):

* ``EfinanceFetcher``  - 2.36s, returns HTML instead of JSON (Eastmoney push2
  is not reachable from the runner).
* ``AkshareFetcher``   - 46.68s, ``stock_zh_a_spot_em`` dies after 10s, then the
  ``stock_zh_a_spot`` fallback times out after 30s because akshare walks all
  ~56 pages serially inside a hard 30s budget.
* ``Pytdx``/``Baostock``/``Yfinance``/``Tencent`` - 0.00s each, they do not
  implement ``get_market_stats`` at all (inherit the base ``return None``).

Sina's paging endpoint *is* reachable from Actions - ``ak.stock_zh_index_spot_sina``
(which hits ``Market_Center.getHQNodeData``) succeeds in 4.5s there.  So instead of
going through akshare we page the same endpoint directly with a small thread pool:
the whole A-share universe (≈5.6k symbols / ≈60 pages) is one consistent snapshot
in roughly 5-12s, and it yields both the advance/decline counts and the turnover.

Why a dedicated pre-flight branch instead of a normal provider
--------------------------------------------------------------
Registering this in ``DataFetcherManager._fetchers`` would either make it the
first choice for K-line/quotes/sectors (priority 0 - far too wide a blast
radius) or leave it behind the 46.68s Akshare timeout (priority 6).  It is
therefore breadth-only and is invoked by ``DataFetcherManager`` as a pre-flight
branch, see ``DataFetcherManager._get_sina_breadth_fetcher``.

Behaviour on failure is deliberately fail-open: any exception, an empty
snapshot, or more than ``_MAX_FAILED_PAGES`` missing pages returns ``None`` so
the caller falls back to the previous provider chain and behaves exactly as it
does today.

Kill switch: ``SINA_BREADTH_ENABLED=false`` disables the provider entirely.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError

logger = logging.getLogger(__name__)

# ``vip.stock.finance.sina.com.cn`` returned HTTP 456 (IP-level block) for some
# networks while ``money.finance.sina.com.cn`` kept working, so both hosts stay in
# the list and are tried in order for every page.
_SINA_BREADTH_HOSTS: Tuple[str, ...] = (
    "https://money.finance.sina.com.cn",
    "https://vip.stock.finance.sina.com.cn",
)
_SINA_BREADTH_PATH = (
    "/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    "?page={page}&num={num}&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=init"
)
_SINA_BREADTH_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

_DEFAULT_PAGE_SIZE = 100
# ~5.6k listed symbols / 100 per page ≈ 56 pages; keep headroom, an out-of-range
# page answers with an empty array and simply contributes nothing.
_DEFAULT_PAGES = 60
_MAX_PAGES = 80
_DEFAULT_WORKERS = 4
_DEFAULT_PAGE_TIMEOUT = 12.0
_DEFAULT_CACHE_TTL = 90.0
# One page is 100 symbols (<2% of the market): tolerate a few transient failures
# instead of throwing the whole snapshot away, but never let a half snapshot
# masquerade as the full market distribution.
_MAX_FAILED_PAGES = 3
# Gentle pacing between requests inside a worker: with 4 workers this keeps the
# request rate near 10/s, which stays well clear of triggering a block on the
# shared runner IP.
_REQUEST_PACING_SECONDS = 0.08

_FALSY = {"0", "false", "no", "off"}


class SinaBreadthFetcher(BaseFetcher):
    """Market-stats-only fetcher that aggregates the Sina A-share snapshot."""

    name = "SinaBreadthFetcher"
    # Never scheduled in the generic provider chain; breadth pre-flight only.
    priority = 98

    def __init__(
        self,
        *,
        pages: Optional[int] = None,
        workers: Optional[int] = None,
        page_timeout: Optional[float] = None,
        cache_ttl: Optional[float] = None,
    ) -> None:
        self._pages = max(1, min(_MAX_PAGES, pages if pages is not None else _env_int("SINA_BREADTH_PAGES", _DEFAULT_PAGES)))
        self._workers = max(1, min(8, workers if workers is not None else _env_int("SINA_BREADTH_WORKERS", _DEFAULT_WORKERS)))
        self._page_timeout = max(
            1.0,
            page_timeout if page_timeout is not None else _env_float("SINA_BREADTH_TIMEOUT", _DEFAULT_PAGE_TIMEOUT),
        )
        self._cache_ttl = max(
            0.0,
            cache_ttl if cache_ttl is not None else _env_float("SINA_BREADTH_CACHE_TTL", _DEFAULT_CACHE_TTL),
        )
        self._page_size = _DEFAULT_PAGE_SIZE
        self._hosts = _SINA_BREADTH_HOSTS
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_at = 0.0
        self._cache_lock = threading.RLock()
        # One keep-alive session per worker thread: without connection reuse every
        # page pays a fresh TLS handshake and 60 pages take ~16s instead of ~6s.
        self._tls = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._tls, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(_SINA_BREADTH_HEADERS)
            self._tls.session = session
        return session

    # ------------------------------------------------------------------ config
    @staticmethod
    def is_enabled() -> bool:
        """Return False when ``SINA_BREADTH_ENABLED`` explicitly disables the provider."""
        raw = (os.getenv("SINA_BREADTH_ENABLED", "true") or "").strip().lower()
        if not raw:
            return True
        return raw not in _FALSY

    # ------------------------------------------------------- non-breadth stubs
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise DataFetchError("SinaBreadthFetcher is breadth-only and provides no daily K-line data")

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        raise DataFetchError("SinaBreadthFetcher is breadth-only and provides no daily K-line data")

    # --------------------------------------------------------------- public API
    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """Return A-share breadth and turnover from a single Sina snapshot.

        Returns ``None`` (never a partial/empty payload) when the snapshot could
        not be built, so the caller can fall back to the legacy provider chain.
        """
        if not self.is_enabled():
            logger.info("[SinaBreadthFetcher] disabled by SINA_BREADTH_ENABLED")
            return None

        now = time.monotonic()
        with self._cache_lock:
            if self._cache is not None and (now - self._cache_at) < self._cache_ttl:
                return dict(self._cache)

        started_at = time.monotonic()
        try:
            rows, failed_pages = self._fetch_universe()
        except Exception as exc:
            logger.warning("[SinaBreadthFetcher] snapshot failed after %.2fs: %s", time.monotonic() - started_at, exc)
            return None

        elapsed = time.monotonic() - started_at
        if not rows:
            logger.warning("[SinaBreadthFetcher] empty snapshot elapsed=%.2fs", elapsed)
            return None
        if failed_pages > _MAX_FAILED_PAGES:
            logger.warning(
                "[SinaBreadthFetcher] snapshot discarded: %d/%d pages failed elapsed=%.2fs",
                failed_pages,
                self._pages,
                elapsed,
            )
            return None

        stats = _aggregate_rows(rows)
        if stats is None:
            logger.warning("[SinaBreadthFetcher] no usable rows in snapshot elapsed=%.2fs", elapsed)
            return None

        logger.info(
            "[SinaBreadthFetcher] snapshot symbols=%d pages=%d pages_failed=%d up=%d down=%d flat=%d "
            "limit_up=%d limit_down=%d amount=%.0f亿 elapsed=%.2fs",
            stats["total_symbols"],
            self._pages,
            failed_pages,
            stats["up_count"],
            stats["down_count"],
            stats["flat_count"],
            stats["limit_up_count"],
            stats["limit_down_count"],
            stats["total_amount"],
            elapsed,
        )

        payload = {key: value for key, value in stats.items() if key != "total_symbols"}
        with self._cache_lock:
            self._cache = payload
            self._cache_at = time.monotonic()
        return dict(payload)

    # ------------------------------------------------------------------ internals
    def _fetch_universe(self) -> Tuple[List[Dict[str, Any]], int]:
        """Page the whole A-share snapshot; returns ``(rows, failed_page_count)``."""
        first_page = self._fetch_page(1)
        if not first_page:
            return [], 0

        rows: List[Dict[str, Any]] = list(first_page)
        failed_pages = 0
        last_page = self._pages
        if last_page <= 1:
            return rows, failed_pages

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {
                pool.submit(self._fetch_page, page): page
                for page in range(2, last_page + 1)
            }
            for future in as_completed(futures):
                page = futures[future]
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    failed_pages += 1
                    logger.warning("[SinaBreadthFetcher] page %d failed: %s", page, exc)
        return rows, failed_pages

    def _fetch_page(self, page: int) -> List[Dict[str, Any]]:
        """Fetch one page, failing over between Sina hosts."""
        last_error: Optional[BaseException] = None
        for host in self._hosts:
            url = host + _SINA_BREADTH_PATH.format(page=page, num=self._page_size)
            try:
                response = self._session().get(url, timeout=self._page_timeout)
                if response.status_code != 200:
                    last_error = DataFetchError(
                        "sina breadth page %d returned HTTP %d (%s)" % (page, response.status_code, host)
                    )
                    continue
                text = (response.text or "").strip()
                if not text.startswith("["):
                    # Out-of-range page answers with an empty body / placeholder:
                    # the universe has ended, treat as an empty page.
                    return []
                payload = json.loads(text)
                return payload if isinstance(payload, list) else []
            except Exception as exc:
                last_error = exc
                continue
            finally:
                if _REQUEST_PACING_SECONDS > 0:
                    time.sleep(_REQUEST_PACING_SECONDS)
        raise last_error if last_error is not None else DataFetchError(
            "sina breadth page %d failed on every host" % page
        )


def _aggregate_rows(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Aggregate raw Sina snapshot rows into the upstream ``get_market_stats`` schema."""
    up = down = flat = 0
    limit_up = limit_down = 0
    amount_total = 0.0
    seen_codes = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if code:
            if code in seen_codes:
                continue
            seen_codes.add(code)

        price = _to_float(row.get("trade"))
        change = _to_float(row.get("changepercent"))
        if price is None or price <= 0 or change is None:
            # Suspended or not yet traded: excluded from the distribution, and its
            # 0 turnover must not dilute the market total either.
            continue

        amount = _to_float(row.get("amount"))
        if amount is not None and amount > 0:
            amount_total += amount

        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1

        threshold = _limit_threshold(code, str(row.get("name") or ""))
        if change >= threshold - 0.05:
            limit_up += 1
        elif change <= -(threshold - 0.05):
            limit_down += 1

    total_symbols = up + down + flat
    if total_symbols <= 0:
        return None

    return {
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        # Upstream schema carries turnover in 亿元.
        "total_amount": round(amount_total / 1e8, 2),
        "total_symbols": total_symbols,
    }


def _limit_threshold(code: str, name: str) -> float:
    """Daily price-limit percentage for one symbol.

    ST/*ST names are capped at 5% on every board; ChiNext (300/301) and STAR
    (688/689) at 20%; Beijing Stock Exchange (8x/4x) at 30%; everything else 10%.
    """
    if "st" in (name or "").lower():
        return 5.0
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("8", "4")):
        return 30.0
    return 10.0


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a valid integer; falling back to %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a valid number; falling back to %s", name, raw, default)
        return default

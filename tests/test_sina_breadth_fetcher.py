# -*- coding: utf-8 -*-
"""Offline unit tests for the Sina breadth pre-flight provider.

These tests never touch the network: every HTTP call is served by a fake
``requests.Session.get`` keyed on the ``page`` query parameter, and the
inter-request pacing is switched off for speed.

What is covered:
* aggregation math (advance/decline/flat, limit-up/down thresholds, turnover unit)
* suspended rows and duplicate codes must not pollute the distribution
* partial snapshots (too many failed pages) must return ``None`` so the caller
  falls back to the legacy provider chain instead of reporting half a market
* host failover, empty universe, kill switch, per-instance cache

The same behaviour was verified against the live endpoint (values matched the
dashboard's ``/api/breadth`` field by field); this file only guards regressions.
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetchError
from data_provider import sina_breadth_fetcher
from data_provider.sina_breadth_fetcher import (
    SinaBreadthFetcher,
    _aggregate_rows,
    _limit_threshold,
    _to_float,
)

MONEY_HOST = "money.finance.sina.com.cn"
VIP_HOST = "vip.stock.finance.sina.com.cn"


def _row(code, name="测试", trade="10.00", change="1.00", amount="100000000"):
    return {
        "code": code,
        "name": name,
        "trade": trade,
        "changepercent": change,
        "amount": amount,
    }


def _response(text, status_code=200):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = text
    return resp


class _FakeSina:
    """Serves canned JSON pages and records the requested URLs."""

    def __init__(self, pages, fail_pages=(), fail_hosts=(), empty_after=2, status_code=200):
        self.pages = pages                # {page_number: [row, ...]}
        self.fail_pages = set(fail_pages)
        self.fail_hosts = set(fail_hosts)
        self.empty_after = empty_after    # pages beyond this answer with "[]"
        self.status_code = status_code
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        page = int(url.split("page=")[1].split("&")[0])
        host = url.split("//")[1].split("/")[0]
        if host in self.fail_hosts:
            return _response("blocked", status_code=456)
        if page in self.fail_pages:
            raise OSError("simulated network error on page %d" % page)
        rows = self.pages.get(page)
        if rows is None:
            return _response("[]" if page > self.empty_after else "null")
        return _response(json.dumps(rows), status_code=self.status_code)


class TestAggregation(unittest.TestCase):
    def test_counts_thresholds_and_turnover(self):
        rows = [
            _row("600000", trade="10.00", change="10.00", amount="1000000000"),   # main board limit-up
            _row("600001", name="ST测试", change="5.00", amount="200000000"),      # ST limit-up (5%)
            _row("300001", change="20.00", amount="300000000"),                   # ChiNext limit-up (20%)
            _row("688001", change="-20.00", amount="400000000"),                  # STAR limit-down
            _row("830001", change="29.95", amount="500000000"),                   # BSE limit-up (30%)
            _row("000001", trade="9.50", change="0.00", amount="600000000"),      # flat
        ]
        stats = _aggregate_rows(rows)
        self.assertEqual(stats["up_count"], 4)
        self.assertEqual(stats["down_count"], 1)
        self.assertEqual(stats["flat_count"], 1)
        self.assertEqual(stats["limit_up_count"], 4)
        self.assertEqual(stats["limit_down_count"], 1)
        self.assertEqual(stats["total_symbols"], 6)
        # 30 亿 turnover, reported in 亿元
        self.assertAlmostEqual(stats["total_amount"], 30.0, places=2)

    def test_suspended_rows_are_excluded(self):
        rows = [_row("600000"), _row("600002", trade="0.00", change="0", amount="0")]
        stats = _aggregate_rows(rows)
        self.assertEqual(stats["total_symbols"], 1)
        self.assertAlmostEqual(stats["total_amount"], 1.0, places=2)

    def test_duplicate_codes_are_counted_once(self):
        rows = [_row("600000"), _row("600000")]
        stats = _aggregate_rows(rows)
        self.assertEqual(stats["total_symbols"], 1)
        self.assertAlmostEqual(stats["total_amount"], 1.0, places=2)

    def test_all_suspended_returns_none(self):
        self.assertIsNone(_aggregate_rows([_row("600000", trade="0.00", change="0")]))
        self.assertIsNone(_aggregate_rows([]))

    def test_limit_threshold_by_board(self):
        self.assertEqual(_limit_threshold("600000", "浦发银行"), 10.0)
        self.assertEqual(_limit_threshold("600001", "*ST测试"), 5.0)
        self.assertEqual(_limit_threshold("300001", "创业"), 20.0)
        self.assertEqual(_limit_threshold("688001", "科创"), 20.0)
        self.assertEqual(_limit_threshold("830001", "北交"), 30.0)

    def test_to_float_handles_dirty_values(self):
        self.assertEqual(_to_float("1.23"), 1.23)
        self.assertIsNone(_to_float(""))
        self.assertIsNone(_to_float(None))
        self.assertIsNone(_to_float("--"))


class TestFetcher(unittest.TestCase):
    def setUp(self):
        self._pacing = sina_breadth_fetcher._REQUEST_PACING_SECONDS
        sina_breadth_fetcher._REQUEST_PACING_SECONDS = 0
        self._env = os.environ.pop("SINA_BREADTH_ENABLED", None)

    def tearDown(self):
        sina_breadth_fetcher._REQUEST_PACING_SECONDS = self._pacing
        os.environ.pop("SINA_BREADTH_ENABLED", None)
        if self._env is not None:
            os.environ["SINA_BREADTH_ENABLED"] = self._env

    def _run(self, fake, **kwargs):
        fetcher = SinaBreadthFetcher(**kwargs)
        with mock.patch.object(sina_breadth_fetcher.requests.Session, "get", side_effect=fake):
            return fetcher.get_market_stats()

    def test_two_page_snapshot(self):
        fake = _FakeSina({
            1: [_row("600000", change="10.00"), _row("600001", change="-3.00")],
            2: [_row("600002", change="0.00")],
        })
        stats = self._run(fake, pages=2, workers=1, cache_ttl=0)
        self.assertEqual(stats["up_count"], 1)
        self.assertEqual(stats["down_count"], 1)
        self.assertEqual(stats["flat_count"], 1)
        self.assertEqual(stats["limit_up_count"], 1)
        self.assertEqual(stats["limit_down_count"], 0)
        self.assertAlmostEqual(stats["total_amount"], 3.0, places=2)
        # request contract: 100 rows per page, hs_a universe, ascending by change desc
        self.assertEqual(len(fake.urls), 2)
        self.assertTrue(all("num=100" in url and "node=hs_a" in url for url in fake.urls))
        self.assertIn("page=1&", fake.urls[0])
        self.assertIn("page=2&", fake.urls[1])

    def test_cache_avoids_second_snapshot(self):
        fake = _FakeSina({1: [_row("600000")], 2: [_row("600001")]})
        fetcher = SinaBreadthFetcher(pages=2, workers=1, cache_ttl=60)
        with mock.patch.object(sina_breadth_fetcher.requests.Session, "get", side_effect=fake):
            first = fetcher.get_market_stats()
            calls_after_first = len(fake.urls)
            second = fetcher.get_market_stats()
            self.assertEqual(first, second)
            self.assertEqual(len(fake.urls), calls_after_first)
            # returned payloads are copies, a caller cannot poison the cache
            second["up_count"] = 999
            third = fetcher.get_market_stats()
            self.assertNotEqual(third["up_count"], 999)
            self.assertEqual(len(fake.urls), calls_after_first)

    def test_empty_universe_returns_none(self):
        stats = self._run(_FakeSina({}, empty_after=0), pages=2, workers=1, cache_ttl=0)
        self.assertIsNone(stats)

    def test_too_many_failed_pages_returns_none(self):
        # 5 of 6 pages fail -> more than the tolerated 3 -> whole snapshot rejected
        fake = _FakeSina({1: [_row("600000")]}, fail_pages=(2, 3, 4, 5, 6))
        self.assertIsNone(self._run(fake, pages=6, workers=1, cache_ttl=0))

    def test_few_failed_pages_are_tolerated(self):
        fake = _FakeSina({1: [_row("600000")], 2: [_row("600001")]}, fail_pages=(3,))
        stats = self._run(fake, pages=3, workers=1, cache_ttl=0)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["up_count"] + stats["down_count"], 2)

    def test_host_failover(self):
        # Money host is blocked (HTTP 456), the vip host still answers
        fake = _FakeSina({1: [_row("600000")]}, fail_hosts=(MONEY_HOST,))
        stats = self._run(fake, pages=1, workers=1, cache_ttl=0)
        self.assertIsNotNone(stats)
        self.assertTrue(any(VIP_HOST in url for url in fake.urls))
        self.assertTrue(any(MONEY_HOST in url for url in fake.urls))

    def test_all_hosts_blocked_returns_none(self):
        fake = _FakeSina({1: [_row("600000")]}, fail_hosts=(MONEY_HOST, VIP_HOST))
        self.assertIsNone(self._run(fake, pages=1, workers=1, cache_ttl=0))

    def test_kill_switch(self):
        os.environ["SINA_BREADTH_ENABLED"] = "false"
        self.assertFalse(SinaBreadthFetcher.is_enabled())
        fake = _FakeSina({1: [_row("600000")]})
        self.assertIsNone(self._run(fake, pages=1, workers=1, cache_ttl=0))
        self.assertEqual(fake.urls, [])
        os.environ["SINA_BREADTH_ENABLED"] = "true"
        self.assertTrue(SinaBreadthFetcher.is_enabled())

    def test_breadth_only_fetcher_has_no_kline(self):
        fetcher = SinaBreadthFetcher()
        with self.assertRaises(DataFetchError):
            fetcher._fetch_raw_data("600000", "2026-01-01", "2026-02-01")
        with self.assertRaises(DataFetchError):
            fetcher._normalize_data(None, "600000")


if __name__ == "__main__":
    unittest.main()

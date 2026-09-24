# -*- coding: utf-8 -*-
"""
Unit tests for DirectNewsSearchProvider (zero-key direct news source).

Covers:
  - 东财快讯 JSONP 解析（mock HTTP）
  - 财联社电报 / 同花顺 / 新浪 解析（mock HTTP）
  - 四源并行竞速（首个 >=5 条有效源胜出 / 部分结果取最佳 / 全失败）
  - 本地缓存存取（内存 + 临时文件）
  - 个股新闻过滤（股票名 / 代码）
  - 与 7-provider 降级链协作（SearchService 仅 DirectNews 可用时仍返回结果）
  - 环境变量开关（NEWS_DIRECT_ENABLED / 各源开关）

所有网络请求均通过 unittest.mock 拦截，不触达真实外网。
每个 provider 实例使用独立临时缓存文件，避免用例间相互污染。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Mock newspaper before search_service import（可选依赖）
if "newspaper" not in sys.modules:
    mock_np = MagicMock()
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np

from src.search_service import SearchResponse, SearchResult, SearchService, DirectNewsSearchProvider


def _now_iso(offset_days: int = 0) -> str:
    dt = datetime.now(tz=timezone.utc).astimezone() + timedelta(days=offset_days)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _item(title, snippet="摘要", url="https://example.com/1", source="src", pub=None):
    return SearchResult(
        title=title, snippet=snippet, url=url, source=source,
        published_date=pub or _now_iso(0),
    )


class _Resp:
    def __init__(self, text, status=200):
        self.status_code = status
        self.text = text


class DNTestBase(unittest.TestCase):
    """为每个用例分配独立临时缓存文件并清理，避免跨用例文件缓存污染。"""

    def setUp(self):
        self._cache_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name

    def tearDown(self):
        if os.path.exists(self._cache_file):
            os.remove(self._cache_file)

    def _provider(self, mocks=None):
        p = DirectNewsSearchProvider()
        p._cache_path = self._cache_file
        p._mem_cache = {}
        for attr, val in (mocks or {}).items():
            setattr(p, attr, val)
        return p


class TestEastmoneyParsing(DNTestBase):
    def test_eastmoney_jsonp_parse(self):
        payload = json.dumps({
            "code": 1,
            "data": {
                "list": [
                    {"unique_id": "123", "title": "央行宣布降准", "content": "全面降准0.5个百分点", "lash_time": _now_iso(0)},
                    {"unique_id": "124", "title": "北向资金净流入", "content": "今日净流入超50亿", "datetime": _now_iso(-1)},
                ]
            },
        })
        with patch("src.search_service.requests.get", return_value=_Resp("jsonp(" + payload + ")")) as mg:
            items = self._provider()._fetch_eastmoney(3)
        self.assertEqual(mg.call_count, 1)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "央行宣布降准")
        self.assertIn("降准", items[0].snippet)
        self.assertIn("kuaixun.eastmoney.com/123", items[0].url)
        self.assertEqual(items[0].source, "东方财富快讯")
        self.assertTrue(items[0].published_date.startswith("20"))

    def test_eastmoney_empty(self):
        with patch("src.search_service.requests.get", return_value=_Resp("jsonp({})")):
            items = self._provider()._fetch_eastmoney(3)
        self.assertEqual(items, [])

    def test_eastmoney_http_error(self):
        with patch("src.search_service.requests.get", return_value=_Resp("{}", status=500)):
            items = self._provider()._fetch_eastmoney(3)
        self.assertEqual(items, [])


class TestOtherSourceParsing(DNTestBase):
    def test_cls_telegram_parse(self):
        payload = json.dumps({"data": {"telegram_list": [
            {"id": "999", "title": "工信部发文", "content": "促进算力发展", "time": int(time.time())},
        ]}})
        with patch("src.search_service.requests.get", return_value=_Resp(payload)):
            items = self._provider()._fetch_cls(3)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "工信部发文")
        self.assertEqual(items[0].source, "财联社电报")

    def test_10jqka_parse(self):
        payload = json.dumps({"data": {"list": [
            {"title": "新能源利好", "summary": "补贴延续", "url": "https://news.10jqka.com.cn/x", "time": int(time.time()) * 1000},
        ]}})
        with patch("src.search_service.requests.get", return_value=_Resp(payload)):
            items = self._provider()._fetch_10jqka(3)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "新能源利好")
        self.assertEqual(items[0].source, "同花顺快讯")

    def test_sina_roll_parse(self):
        payload = json.dumps({"result": {"data": [
            {"title": "A股三大指数收涨", "intro": "沪指涨1%", "url": "https://news.sina.com.cn/a", "ctime": int(time.time())},
        ]}})
        with patch("src.search_service.requests.get", return_value=_Resp(payload)):
            items = self._provider()._fetch_sina(3)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "A股三大指数收涨")
        self.assertEqual(items[0].source, "新浪滚动新闻")


class TestRace(DNTestBase):
    def test_first_source_with_5_wins(self):
        p = self._provider({
            "_fetch_eastmoney": lambda days: [_item(f"东财{i}") for i in range(5)],
            "_fetch_cls": lambda days: [_item(f"财联社{i}") for i in range(2)],
            "_fetch_10jqka": lambda days: [],
            "_fetch_sina": lambda days: [],
        })
        resp = p.search("A股 大盘", max_results=5, days=3)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 5)
        self.assertTrue(resp.results[0].title.startswith("东财"))
        self.assertEqual(resp.provider, "DirectNews")

    def test_all_partial_returns_best(self):
        p = self._provider({
            "_fetch_eastmoney": lambda days: [_item(f"东财{i}") for i in range(2)],
            "_fetch_cls": lambda days: [_item(f"财联社{i}") for i in range(3)],
            "_fetch_10jqka": lambda days: [_item(f"同花顺{i}") for i in range(1)],
            "_fetch_sina": lambda days: [],
        })
        resp = p.search("A股 大盘", max_results=5, days=3)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 3)
        self.assertTrue(resp.results[0].title.startswith("财联社"))

    def test_all_fail_no_cache_returns_failure(self):
        p = self._provider({
            "_fetch_eastmoney": lambda days: [],
            "_fetch_cls": lambda days: [],
            "_fetch_10jqka": lambda days: [],
            "_fetch_sina": lambda days: [],
        })
        resp = p.search("A股 大盘", max_results=5, days=3)
        self.assertFalse(resp.success)
        self.assertEqual(resp.results, [])


class TestCacheFallback(DNTestBase):
    def test_all_fail_file_cache_fallback(self):
        seeded = [_item("缓存新闻A", pub=_now_iso(0)), _item("缓存新闻B", pub=_now_iso(0))]
        with open(self._cache_file, "w", encoding="utf-8") as fh:
            json.dump({
                "A股 大盘": {
                    "saved_at": time.time(), "query": "A股 大盘",
                    "provider": "DirectNews", "success": True,
                    "results": [vars(r) for r in seeded],
                }
            }, fh, ensure_ascii=False)
        p = self._provider({
            "_fetch_eastmoney": lambda days: [],
            "_fetch_cls": lambda days: [],
            "_fetch_10jqka": lambda days: [],
            "_fetch_sina": lambda days: [],
        })
        resp = p.search("A股 大盘", max_results=5, days=3)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 2)
        self.assertEqual(resp.results[0].title, "缓存新闻A")

    def test_cache_file_expiry_not_used(self):
        with open(self._cache_file, "w", encoding="utf-8") as fh:
            json.dump({
                "A股 大盘": {
                    "saved_at": time.time() - (25 * 3600), "query": "A股 大盘",
                    "provider": "DirectNews", "success": True,
                    "results": [vars(_item("过期缓存"))],
                }
            }, fh, ensure_ascii=False)
        p = self._provider({
            "_fetch_eastmoney": lambda days: [],
            "_fetch_cls": lambda days: [],
            "_fetch_10jqka": lambda days: [],
            "_fetch_sina": lambda days: [],
        })
        resp = p.search("A股 大盘", max_results=5, days=3)
        self.assertFalse(resp.success)
        self.assertEqual(resp.results, [])

    def test_success_writes_cache_then_served(self):
        # 第一次成功写入缓存；第二次所有源失败应回退到刚写入的缓存
        p = self._provider({
            "_fetch_eastmoney": lambda days: [_item("直连新闻1"), _item("直连新闻2")],
            "_fetch_cls": lambda days: [],
            "_fetch_10jqka": lambda days: [],
            "_fetch_sina": lambda days: [],
        })
        first = p.search("新能源", max_results=5, days=3)
        self.assertTrue(first.success)
        # 现在所有源失败
        p2 = self._provider({
            "_fetch_eastmoney": lambda days: [],
            "_fetch_cls": lambda days: [],
            "_fetch_10jqka": lambda days: [],
            "_fetch_sina": lambda days: [],
        })
        # 复用同一缓存文件
        p2._cache_path = self._cache_file
        second = p2.search("新能源", max_results=5, days=3)
        self.assertTrue(second.success)
        self.assertEqual(len(second.results), 2)


class TestStockFiltering(DNTestBase):
    def _provider(self, items_all_market):
        return super()._provider({
            "_fetch_eastmoney": lambda days: items_all_market,
            "_fetch_cls": lambda days: [],
            "_fetch_10jqka": lambda days: [],
            "_fetch_sina": lambda days: [],
        })

    def test_filter_by_stock_name(self):
        items = [_item("长江电力获机构增持"), _item("贵州茅台发布年报"), _item("大盘今日震荡")]
        resp = self._provider(items).search("长江电力", max_results=5, days=3)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 1)
        self.assertIn("长江电力", resp.results[0].title)

    def test_filter_by_stock_code(self):
        items = [_item("600900 长江电力盘中拉升"), _item("其他无关新闻")]
        resp = self._provider(items).search("600900", max_results=5, days=3)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 1)
        self.assertIn("600900", resp.results[0].title)

    def test_non_stock_topic_not_filtered(self):
        items = [_item("央行降准"), _item("新能源补贴")]
        resp = self._provider(items).search("A股 大盘", max_results=5, days=3)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 2)


class TestEnvToggles(DNTestBase):
    def test_all_sources_disabled_is_unavailable(self):
        with patch.dict(os.environ, {
            "NEWS_EASTMONEY_ENABLED": "false", "NEWS_10JQKA_ENABLED": "false",
            "NEWS_CLS_ENABLED": "false", "NEWS_SINA_ENABLED": "false",
        }):
            self.assertFalse(DirectNewsSearchProvider().is_available)

    def test_default_is_available(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in ("NEWS_EASTMONEY_ENABLED", "NEWS_10JQKA_ENABLED", "NEWS_CLS_ENABLED", "NEWS_SINA_ENABLED"):
                os.environ.pop(k, None)
            self.assertTrue(DirectNewsSearchProvider().is_available)

    def test_master_switch_excludes_from_chain(self):
        with patch.dict(os.environ, {"NEWS_DIRECT_ENABLED": "false"}):
            self.assertNotIn("DirectNews", [pr.name for pr in SearchService()._providers])

    def test_inserted_at_chain_front(self):
        self.assertEqual(SearchService()._providers[0].name, "DirectNews")


class TestIntegrationWithChain(DNTestBase):
    def test_search_topic_news_uses_directnews(self):
        svc = SearchService()
        self.assertEqual(svc._providers[0].name, "DirectNews")
        provider = svc._providers[0]
        provider._cache_path = self._cache_file
        provider._mem_cache = {}
        provider._fetch_eastmoney = lambda days: [_item(f"直连新闻{i}") for i in range(6)]
        provider._fetch_cls = lambda days: []
        provider._fetch_10jqka = lambda days: []
        provider._fetch_sina = lambda days: []

        resp = svc.search_topic_news("A股 大盘", max_results=5)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 5)
        self.assertEqual(resp.provider, "DirectNews")


if __name__ == "__main__":
    unittest.main()

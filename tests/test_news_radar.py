from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from stock_alert.news_radar import NewsRadarError, NewsRadarService, normalize_settings


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class NewsRadarServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        config["news_radar"]["platforms"] = ["cls-hot", "xueqiu"]
        config["news_radar"]["keywords"] = ["A股", "机器人"]
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_get(self, url: str, **_: object) -> FakeResponse:
        platform_id = parse_qs(urlparse(url).query)["id"][0]
        self.calls.append(platform_id)
        title = "浦发银行发布A股机器人产业报告" if platform_id == "cls-hot" else "市场午后震荡"
        return FakeResponse(
            {
                "status": "success",
                "updatedTime": 1_788_000_000_000,
                "items": [
                    {"title": title, "url": f"https://example.com/{platform_id}"},
                    {"title": "危险链接", "url": "javascript:alert(1)"},
                ],
            }
        )

    def test_fetch_matches_watchlist_and_keywords_and_uses_cache(self) -> None:
        service = NewsRadarService(self.config_path, request_get=self.fake_get)

        first = service.fetch()
        second = service.fetch()

        self.assertTrue(first["ok"])
        self.assertEqual(len(first["sources"]), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(self.calls), 2)
        top = first["items"][0]
        self.assertIn("浦发银行", top["matched_stocks"])
        self.assertEqual(top["matched_keywords"], ["A股", "机器人"])
        unsafe = next(item for item in first["items"] if item["title"] == "危险链接")
        self.assertEqual(unsafe["url"], "")

    def test_one_failed_platform_does_not_hide_other_results(self) -> None:
        def partial_get(url: str, **kwargs: object) -> FakeResponse:
            if "id=xueqiu" in url:
                raise TimeoutError("timed out")
            return self.fake_get(url, **kwargs)

        result = NewsRadarService(self.config_path, request_get=partial_get).fetch(force=True)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 2)
        failed = next(source for source in result["sources"] if source["id"] == "xueqiu")
        self.assertFalse(failed["ok"])
        self.assertIn("timed out", failed["error"])

    def test_direct_stock_hotlists_expose_rank_and_market_fields(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["news_radar"]["platforms"] = ["ths-hot", "eastmoney-hot"]
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        def direct_get(url: str, **_: object) -> FakeResponse:
            if "10jqka.com.cn" in url:
                return FakeResponse(
                    {
                        "status_code": 0,
                        "data": {
                            "stock_list": [
                                {
                                    "market": 33,
                                    "code": "000001",
                                    "rate": "1234567",
                                    "rise_and_fall": 2.35,
                                    "name": "平安银行",
                                    "hot_rank_chg": 3,
                                    "order": 1,
                                    "tag": {
                                        "popularity_tag": "持续上榜",
                                        "concept_tag": ["银行"],
                                    },
                                }
                            ]
                        },
                    }
                )
            if "push2" in url and "eastmoney.com" in url:
                return FakeResponse(
                    {
                        "data": {
                            "diff": [
                                {"f12": "600000", "f14": "浦发银行", "f2": 10.25, "f3": -1.2}
                            ]
                        }
                    }
                )
            raise AssertionError(f"unexpected GET {url}")

        def direct_post(url: str, **_: object) -> FakeResponse:
            self.assertIn("emappdata.eastmoney.com", url)
            return FakeResponse(
                {
                    "code": 0,
                    "message": "OK",
                    "data": [{"sc": "SH600000", "rk": 1, "hisRc": 5}],
                }
            )

        result = NewsRadarService(
            self.config_path,
            request_get=direct_get,
            request_post=direct_post,
        ).fetch(force=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["item_count"], 2)
        ths = next(item for item in result["items"] if item["source_id"] == "ths-hot")
        eastmoney = next(
            item for item in result["items"] if item["source_id"] == "eastmoney-hot"
        )
        self.assertEqual(ths["stock_code"], "000001")
        self.assertEqual(ths["heat"], 1_234_567)
        self.assertEqual(ths["hot_tag"], "持续上榜")
        self.assertEqual(eastmoney["title"], "浦发银行")
        self.assertEqual(eastmoney["price"], 10.25)
        self.assertEqual(eastmoney["change_pct"], -1.2)
        self.assertIn("浦发银行", eastmoney["matched_stocks"])

    def test_settings_reject_unknown_platform(self) -> None:
        with self.assertRaises(NewsRadarError):
            normalize_settings({"platforms": ["unknown"], "keywords": []})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_alert.news_agent import NewsAgentError, NewsAgentService, normalize_settings


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def raise_for_status(self) -> None:
        return None


class NewsAgentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        self.config_path.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_settings_hide_api_key_and_blank_update_preserves_it(self) -> None:
        service = NewsAgentService(self.config_path)
        public = service.save_settings(
            {
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "relation-model",
                "api_key": "secret-value",
            }
        )

        self.assertNotIn("api_key", public)
        self.assertTrue(public["api_key_configured"])
        self.assertTrue(public["configured"])
        stored = json.loads(service.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["api_key"], "secret-value")

        service.save_settings({"model": "relation-model-v2", "api_key": ""})
        stored = json.loads(service.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["api_key"], "secret-value")
        self.assertEqual(stored["model"], "relation-model-v2")

        cleared = service.save_settings({"clear_api_key": True})
        self.assertFalse(cleared["api_key_configured"])
        stored = json.loads(service.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["api_key"], "")

    def test_analyze_sends_filtered_news_hotlists_and_watchlist(self) -> None:
        captured: dict = {}
        model_result = {
            "overview": "机器人新闻与热榜股票存在主题关联",
            "themes": [],
            "news_to_market": [],
            "hot_stock_to_news": [],
            "watchlist_impacts": [],
            "risks": ["标题信息有限"],
        }

        def fake_post(url: str, **kwargs: object) -> FakeResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse(
                {"choices": [{"message": {"content": json.dumps(model_result, ensure_ascii=False)}}]}
            )

        service = NewsAgentService(self.config_path, request_post=fake_post)
        service.save_settings(
            {
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "relation-model",
                "api_key": "secret-value",
                "max_news_items": 20,
            }
        )
        radar = {
            "items": [
                {
                    "id": "hot-ths-1",
                    "source_id": "ths-hot",
                    "source_name": "同花顺热榜",
                    "title": "机器人股份",
                    "stock_code": "000001",
                    "rank": 1,
                },
                {
                    "id": "hot-east-1",
                    "source_id": "eastmoney-hot",
                    "source_name": "东方财富人气榜",
                    "title": "算力科技",
                    "stock_code": "600001",
                    "rank": 2,
                },
                {
                    "id": "news-selected",
                    "source_id": "cls-hot",
                    "source_name": "财联社",
                    "title": "机器人产业获得政策支持",
                    "category": "财经",
                },
                {
                    "id": "news-hidden",
                    "source_id": "wallstreetcn-latest",
                    "source_name": "华尔街见闻",
                    "title": "未被当前筛选选中的新闻",
                },
            ]
        }

        result = service.analyze(
            {"question": "分析机器人链条", "item_ids": ["news-selected"]},
            radar,
            [{"code": "000001", "name": "机器人股份", "cost": 10.2}],
        )

        self.assertTrue(result["structured"])
        self.assertEqual(result["analysis"]["overview"], model_result["overview"])
        self.assertEqual(captured["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-value")
        request_payload = captured["json"]
        self.assertEqual(request_payload["model"], "relation-model")
        context = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual([item["id"] for item in context["news"]], ["news-selected"])
        self.assertEqual(len(context["hot_stocks"]), 2)
        self.assertEqual(context["watchlist"][0]["code"], "000001")
        self.assertEqual(result["metadata"]["news_count"], 1)
        self.assertEqual(result["metadata"]["hot_stock_count"], 2)

    def test_responses_payload_and_array_chat_content_are_supported(self) -> None:
        settings = {
            "api_url": "http://127.0.0.1:11434/v1/responses",
            "model": "local-model",
            "temperature": 0.2,
        }
        request = NewsAgentService._request_payload(settings, {"news": []})

        self.assertIn("instructions", request)
        self.assertIn("input", request)
        self.assertNotIn("messages", request)
        content = NewsAgentService._extract_content(
            {"choices": [{"message": {"content": [{"type": "text", "text": "结果"}]}}]}
        )
        self.assertEqual(content, "结果")

    def test_invalid_settings_and_unconfigured_analysis_are_rejected(self) -> None:
        with self.assertRaises(NewsAgentError):
            normalize_settings({"api_url": "file:///tmp/model", "model": "x"})
        with self.assertRaises(NewsAgentError):
            NewsAgentService(self.config_path).analyze({}, {"items": []}, [])


if __name__ == "__main__":
    unittest.main()

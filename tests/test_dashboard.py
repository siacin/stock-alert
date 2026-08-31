from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

from stock_alert.dashboard import DashboardController, DashboardError, DashboardHTTPServer


ROOT = Path(__file__).resolve().parents[1]


class DashboardControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        self.config_path.write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.controller = DashboardController(self.config_path)

    def tearDown(self) -> None:
        self.controller.shutdown()
        self.temporary.cleanup()

    def test_save_valid_config(self) -> None:
        payload = self.controller.get_config()
        payload["poll_interval_seconds"] = 3.5
        payload["stocks"][0]["cost"] = 12.34

        saved = self.controller.save_config(payload)

        self.assertEqual(saved["poll_interval_seconds"], 3.5)
        self.assertEqual(saved["stocks"][0]["cost"], 12.34)

    def test_invalid_config_does_not_replace_file(self) -> None:
        original = self.config_path.read_text(encoding="utf-8")
        payload = self.controller.get_config()
        payload["stocks"] = []

        with self.assertRaises(DashboardError):
            self.controller.save_config(payload)

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_invalid_stock_monitor_items_do_not_replace_file(self) -> None:
        original = self.config_path.read_text(encoding="utf-8")
        payload = self.controller.get_config()
        payload["stocks"][0]["monitor_items"] = ["unknown-rule"]

        with self.assertRaises(DashboardError):
            self.controller.save_config(payload)

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_local_api_ping_and_status(self) -> None:
        server = DashboardHTTPServer(("127.0.0.1", 0), self.controller, ROOT / "web")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as response:
                ping = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as response:
                status = json.loads(response.read().decode("utf-8"))
            self.assertTrue(ping["ok"])
            self.assertEqual(status["app"], "stock-alert-dashboard")
            self.assertEqual(status["stock_count"], 2)
            self.assertIn("market_phase", status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_analysis_api_is_limited_to_watchlist(self) -> None:
        self.controller.analysis_service.get = Mock(
            return_value={"code": "600000", "name": "浦发银行", "technical_score": 42}
        )
        server = DashboardHTTPServer(("127.0.0.1", 0), self.controller, ROOT / "web")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analysis?code=600000", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["technical_score"], 42)
            self.controller.analysis_service.get.assert_called_once()
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analysis?code=600001", timeout=2)
            self.assertEqual(context.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_news_radar_settings_can_be_saved_while_monitor_is_running(self) -> None:
        self.controller._worker = Mock()
        self.controller._worker.is_alive.return_value = True
        payload = self.controller.news_radar_settings()["settings"]
        payload["keywords"] = ["浦发银行", "机器人"]

        saved = self.controller.save_news_radar_settings(payload)

        self.assertEqual(saved["settings"]["keywords"], ["浦发银行", "机器人"])
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["news_radar"]["keywords"], ["浦发银行", "机器人"])
        self.controller._worker = None

    def test_news_agent_settings_never_return_the_api_key(self) -> None:
        saved = self.controller.save_news_agent_settings(
            {
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "relation-model",
                "api_key": "private-key",
            }
        )

        self.assertNotIn("api_key", saved["settings"])
        self.assertTrue(saved["settings"]["api_key_configured"])
        read_back = self.controller.news_agent_settings()["settings"]
        self.assertNotIn("api_key", read_back)
        self.assertTrue(read_back["api_key_configured"])


if __name__ == "__main__":
    unittest.main()

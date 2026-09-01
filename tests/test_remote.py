from __future__ import annotations

import concurrent.futures
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

from remote import TARGET, empty_serve, owned_serve
from stock_alert.dashboard import DashboardController, DashboardError, DashboardHTTPServer
from stock_alert.remote_access import RemoteAccessPolicy, remote_config_view

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://stock.example.ts.net"
OWNER = "owner@example.com"


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.path.write_text((ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.controller = DashboardController(self.path)
        self.policy = self.controller.remote_policy
        self.policy.write({"enabled": True, "origin": ORIGIN, "owner_login": OWNER})
        self.servers = []
        self.remote = self.start_server(remote=True)
        self.local = self.start_server(remote=False)

    def start_server(self, remote):
        server = DashboardHTTPServer(("127.0.0.1", 0), self.controller, ROOT / "web", remote=remote)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_address[1]}"

    def tearDown(self):
        self.controller._worker = None
        self.controller.shutdown()
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        self.temp.cleanup()

    def request(self, path, *, method="GET", payload=None, authorized=True, headers=None, local=False):
        merged = {} if local or not authorized else {"Tailscale-User-Login": OWNER, "Origin": ORIGIN}
        merged.update(headers or {})
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            merged["Content-Type"] = "application/json"
        req = urllib.request.Request((self.local if local else self.remote) + path, data=data, headers=merged, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if response.headers.get_content_type() == "application/json" else body

    def test_unauthenticated_html_static_and_api_are_denied(self):
        for route in ("/", "/app.js", "/styles.css", "/market.js", "/market.css", "/api/market-monitor", "/api/config", "/api/alerts", "/api/news-agent/result"):
            self.assertEqual(self.request(route, authorized=False)[0], 403, route)

    def test_market_monitor_shared_settings_require_owner_and_revision(self):
        status, data = self.request("/api/market-monitor")
        self.assertEqual(status, 200)
        settings = data["settings"]
        settings["interval_seconds"] = 30
        self.assertEqual(self.request("/api/market-monitor/settings", method="PUT", payload=settings, authorized=False)[0], 403)
        self.assertEqual(self.request("/api/market-monitor/settings", method="PUT", payload=settings)[0], 200)
        self.assertEqual(self.request("/api/market-monitor/settings", method="PUT", payload=settings)[0], 409)
        self.assertEqual(self.request("/api/market-monitor", local=True)[1]["settings"]["interval_seconds"], 30)
        self.assertEqual(self.request("/api/market-monitor/start", method="POST", headers={"Origin":"https://evil.example"})[0], 403)
        self.assertFalse(self.controller.market_monitor.status()["enabled"])

    def test_wrong_owner_and_public_funnel_requests_fail_closed(self):
        self.assertEqual(self.request("/api/status", headers={"Tailscale-User-Login": "other@example.com"})[0], 403)
        self.assertEqual(self.request("/api/status", authorized=False, headers={"X-Forwarded-For": "100.1.1.1"})[0], 403)

    def test_owner_can_read_but_not_manage_local_secrets(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/api/status")[0], 200)
        for method in ("GET", "PUT"):
            self.assertEqual(self.request("/api/news-agent/settings", method=method, payload={} if method == "PUT" else None)[0], 403)
        self.assertEqual(self.request("/api/remote/disable", method="POST")[0], 403)
        self.assertEqual(self.request("/api/news-agent/test", method="POST", payload={})[0], 403)

    def test_local_agent_probe_does_not_fetch_personal_context(self):
        self.controller.news_agent_service.test_connection = Mock(return_value={"ok":True,"saved":False})
        self.controller.news_radar = Mock(side_effect=AssertionError("probe must not fetch news"))
        status,result=self.request("/api/news-agent/test",method="POST",payload={},local=True)
        self.assertEqual(status,200)
        self.assertFalse(result["saved"])
        self.controller.news_radar.assert_not_called()
        self.controller.news_agent_service.test_connection.assert_called_once_with({})

    def test_probe_shares_analysis_lock_and_requires_same_origin(self):
        self.controller.news_agent_service.test_connection=Mock(return_value={"ok":True})
        self.controller._agent_run_lock.acquire()
        try:
            self.assertEqual(self.request("/api/news-agent/test",method="POST",payload={},local=True)[0],409)
        finally:
            self.controller._agent_run_lock.release()
        self.assertEqual(self.request("/api/news-agent/test",method="POST",payload={},local=True,headers={"Origin":"https://evil.example"})[0],403)
        self.controller.news_agent_service.test_connection.assert_not_called()

    def test_host_localhost_does_not_bypass_remote_authorization(self):
        self.assertEqual(self.request("/api/config", authorized=False, headers={"Host": "localhost:8765"})[0], 403)
        self.assertEqual(self.request("/api/news-agent/settings", headers={"Host": "localhost:8765"})[0], 403)

    def test_local_host_and_proxy_misrouting_protected(self):
        self.assertEqual(self.request("/api/config", local=True, headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/config", local=True, headers={"Tailscale-User-Login": OWNER})[0], 403)

    def test_remote_writes_require_exact_origin(self):
        self.controller.stop = Mock(return_value={"state": "stopped"})
        for origin in ("", "null", "http://127.0.0.1", "https://evil.example", ORIGIN + ":123"):
            self.assertEqual(self.request("/api/monitor/stop", method="POST", headers={"Origin": origin})[0], 403)
        self.controller.stop.assert_not_called()
        self.assertEqual(self.request("/api/monitor/stop", method="POST")[0], 202)

    def test_local_cross_origin_and_cross_site_rejected(self):
        self.assertEqual(self.request("/api/monitor/stop", local=True, method="POST", headers={"Origin": "http://127.0.0.1:9999"})[0], 403)
        self.assertEqual(self.request("/api/status", local=True, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_remote_config_redacts_and_preserves_webhook_and_paths(self):
        full = self.controller.get_config()
        full["notifications"]["webhooks"] = [{"url": "https://example.com/private-token"}]
        self.controller.save_config(full)
        code, view = self.request("/api/config")
        self.assertEqual(code, 200)
        self.assertNotIn("database_path", view)
        self.assertNotIn("log_path", view)
        self.assertNotIn("private-token", json.dumps(view))
        view["stocks"][0]["cost"] = 18.50
        code, saved = self.request("/api/config", method="PUT", payload=view)
        self.assertEqual(code, 200, saved)
        full_after = self.controller.get_config()
        self.assertEqual(full_after["notifications"]["webhooks"], full["notifications"]["webhooks"])
        self.assertEqual(full_after["database_path"], full["database_path"])
        self.assertEqual(full_after["stocks"][0]["cost"], 18.50)

    def test_remote_cannot_inject_paths_or_webhooks(self):
        for extras in ({"database_path": "evil.db"}, {"notifications": {"webhooks": []}}, {"news_agent": {"api_url": "evil"}}):
            view = remote_config_view(self.controller.get_config())
            view.update(extras)
            self.assertEqual(self.request("/api/config", method="PUT", payload=view)[0], 403)

    def test_two_devices_saving_same_revision_only_one_wins(self):
        view = self.controller.get_config()
        def save(cost):
            payload = json.loads(json.dumps(view))
            payload["stocks"][0]["cost"] = cost
            try:
                self.controller.save_config(payload, require_revision=True)
                return 200
            except DashboardError as exc:
                return exc.status
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            results = list(executor.map(save, [10, 11]))
        self.assertEqual(sorted(results), [200, 409])
        self.assertNotIn("_revision", json.loads(self.path.read_text(encoding="utf-8")))

    def test_stale_radar_save_and_missing_revision_rejected(self):
        radar = self.controller.news_radar_settings()
        view = self.controller.get_config()
        view["stocks"][0]["cost"] = 17
        self.controller.save_config(view)
        self.assertEqual(self.request("/api/news-radar/settings", method="PUT", payload={**radar["settings"], "_revision": radar["_revision"]})[0], 409)
        self.assertEqual(self.request("/api/config", method="PUT", payload={"stocks": []})[0], 409)

    def test_disable_takes_effect_without_restart(self):
        self.assertEqual(self.request("/api/ping")[0], 200)
        self.assertEqual(self.request("/api/remote/disable", local=True, method="POST")[0], 202)
        self.assertEqual(self.request("/api/ping")[0], 403)
        self.assertEqual(self.request("/api/ping", local=True)[0], 200)

    def test_malformed_or_deleted_policy_fails_closed(self):
        for value in ("invalid-json", "[]", '{"enabled":true}'):
            self.policy.path.write_text(value, encoding="utf-8")
            self.assertEqual(self.request("/api/ping")[0], 403)
        self.policy.path.unlink()
        self.assertEqual(self.request("/api/ping")[0], 403)

    def test_duplicate_identity_header_fails_closed(self):
        headers = Message()
        headers["Tailscale-User-Login"] = OWNER
        headers["Tailscale-User-Login"] = OWNER
        self.assertFalse(self.policy.authorize(headers))

    def test_agent_results_survive_restart_and_are_shared(self):
        self.controller.news_radar = Mock(return_value={})
        self.controller.news_agent_service.analyze = Mock(return_value={"structured": True, "analysis": {"overview": "test"}})
        saved = self.controller.news_agent_analyze({})
        self.assertEqual(self.request("/api/news-agent/result")[1]["result"]["result_id"], saved["result_id"])
        restarted = DashboardController(self.path)
        self.assertEqual(restarted.news_agent_result()["result"], saved)
        restarted.shutdown()

    def test_manual_news_can_skip_radar_fetch(self):
        self.controller.news_radar = Mock(side_effect=AssertionError("manual-only analysis must not fetch radar"))
        self.controller.news_agent_service.analyze = Mock(return_value={"structured": True, "analysis": {"overview": "manual"}})
        result = self.controller.news_agent_analyze({"user_news": "模拟新闻", "include_radar": False})
        self.assertEqual(result["analysis"]["overview"], "manual")
        self.controller.news_radar.assert_not_called()
        radar = self.controller.news_agent_service.analyze.call_args.args[1]
        self.assertEqual(radar, {"items": []})
        self.assertEqual(self.controller.news_agent_service.analyze.call_args.args[2], [])

    def test_parallel_agent_run_rejected_without_extra_api_cost(self):
        self.controller._agent_run_lock.acquire()
        try:
            self.assertEqual(self.request("/api/news-agent/analyze", method="POST", payload={})[0], 409)
        finally:
            self.controller._agent_run_lock.release()

    def test_server_cannot_listen_on_public_interface(self):
        with self.assertRaises(ValueError):
            DashboardHTTPServer(("0.0.0.0", 0), self.controller, ROOT / "web", remote=True)


class ServeSetupTests(unittest.TestCase):
    def test_only_exact_private_proxy_is_owned(self):
        value = {"TCP": {"443": {"HTTPS": True}}, "Web": {"stock.example.ts.net:443": {"Handlers": {"/": {"Proxy": TARGET}}}}}
        self.assertTrue(owned_serve(value, "stock.example.ts.net"))
        value["AllowFunnel"] = {"stock.example.ts.net:443": True}
        self.assertFalse(owned_serve(value, "stock.example.ts.net"))
        self.assertTrue(empty_serve({}))
        self.assertFalse(empty_serve(value))


if __name__ == "__main__":
    unittest.main()

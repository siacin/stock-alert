from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from stock_alert.market_data import (INDUSTRIES, MarketDataClient, TZ, normalize_pool_row,
                                     normalize_row, number, pool_time)
from stock_alert.market_monitor import (MarketMonitorError, MarketMonitorService, assess,
                                        classify_first_board, cycle_label, find_baseline,
                                        hot_attention, leader_impacts, position_features,
                                        score_market_leaders, score_sector_leaders,
                                        sentiment, validate_settings)
from stock_alert.dashboard import DashboardController, DashboardHTTPServer

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 31, 10, 0, tzinfo=TZ)


def fixture(now=NOW, change=.5):
    def row(code, name, value):
        return {"code": code, "name": name, "change_pct": value, "last": 10,
                "amount": 100000, "timestamp": now.isoformat(), "up": 60,
                "down": 40, "leader": "测试股", "leader_pct": 2}
    stocks = [row(f"{600000+i:06}", "测试", change if i % 4 else -.5) for i in range(3600)]
    sectors = [row(f"BK{i:04}", name, (30-i)/10) for i, name in enumerate(sorted(INDUSTRIES))]
    def feed(rows):
        return {"source": "eastmoney", "rows": rows, "received": len(rows), "expected": len(rows), "complete": True}
    return {"stocks": feed(stocks), "sectors": feed(sectors),
            **{k: {"ok": True, "count": n, "date": now.strftime("%Y%m%d")} for k, n in [("up", 30), ("down", 3), ("broken", 10)]}}


class MarketDataTests(unittest.TestCase):
    @staticmethod
    def history(values, sources=("eastmoney", "tencent")):
        start = NOW.date() - timedelta(days=len(values) + 5)
        return {"closes": [{"date": (start + timedelta(days=index)).isoformat(), "close": value}
                           for index, value in enumerate(values)], "sources": list(sources)}

    def classify(self, values, sector_values=None, **updates):
        row = {"code":"600000", "name":"测试股份", "last":values[-1] * 1.1,
               "streak":1, "first_limit_time":"10:00:00", "seal_amount":2e7,
               "float_market_cap":10e8, **updates}
        sector_values = sector_values or [100] * len(values)
        sector = {"last":sector_values[-1] * 1.02}
        featured = position_features(row, self.history(values), sector, self.history(sector_values, ("eastmoney",)))
        return classify_first_board(featured)

    def test_first_board_position_classification_uses_pre_event_relative_returns(self):
        low = self.classify([10] * 12, [100,101,102,103,104,105,106,107,108,109,110,111])
        self.assertEqual(low["position_label"], "低位补涨")
        self.assertLess(low["pre_excess_5d"], 0)
        self.assertEqual(low["prior_limit_count_10d"], 0)
        self.assertTrue(low["history_verified"])

        rebound = self.classify([10,10,10,10,10,10,10,11,10.7,10.6,10.5,10.4])
        self.assertEqual(rebound["position_label"], "高位反包")
        self.assertGreaterEqual(rebound["prior_limit_count_10d"], 1)

        acceleration = self.classify([10,10.2,10.4,10.6,10.8,11,11.3,11.6,12,12.4,12.8,13.2])
        self.assertEqual(acceleration["position_label"], "趋势加速")
        self.assertGreater(acceleration["pre_return_5d"], 8)

    def test_missing_history_never_guesses_catch_up(self):
        row = {"code":"600000", "name":"测试", "last":11, "streak":1}
        result = classify_first_board(position_features(row, {}, {"last":102}, {}))
        self.assertEqual(result["position_label"], "历史不足待核验")

    def test_sector_and_market_leaders_use_independent_explainable_scores(self):
        rows = [
            {"code":"600001", "name":"换手高标", "streak":3, "first_limit_time":"09:35:00",
             "seal_amount":5e7, "float_market_cap":30e8, "open_count":2, "amount":8e8,
             "turnover_pct":18, "industry":"细分甲", "change_pct":10, "prior_limit_count_10d":2},
            {"code":"600002", "name":"跟风一", "streak":1, "first_limit_time":"09:39:00",
             "seal_amount":2e7, "float_market_cap":20e8, "open_count":0, "amount":3e8,
             "turnover_pct":10, "industry":"细分甲", "change_pct":10},
            {"code":"600003", "name":"跟风二", "streak":1, "first_limit_time":"09:42:00",
             "seal_amount":1e7, "float_market_cap":20e8, "open_count":0, "amount":2e8,
             "turnover_pct":8, "industry":"细分乙", "change_pct":10},
        ]
        impacts = {"600001":{"observations":2, "sector_score":70, "market_score":72}}
        sector = score_sector_leaders(rows, Counter({"细分甲":2, "细分乙":1}), impacts)
        self.assertEqual(sector[0]["code"], "600001")
        self.assertEqual(sector[0]["sector_leader_role"], "板块龙已确认")
        self.assertEqual(sector[0]["followers_after_limit"], 2)
        self.assertIn("板块带动代理", sector[0]["sector_leader_components"])

        attention = hot_attention([
            {"stock_code":"600001", "source_name":"同花顺", "rank":1},
            {"stock_code":"600001", "source_name":"东方财富", "rank":3},
        ])
        market = score_market_leaders(rows, attention, impacts)
        self.assertEqual(market[0]["code"], "600001")
        self.assertEqual(market[0]["market_leader_role"], "市场投机龙已确认")
        self.assertEqual(market[0]["attention_best_rank"], 1)
        self.assertGreaterEqual(market[0]["limit_utilization_pct"], 99)

    def test_leader_event_impacts_require_consecutive_short_snapshots(self):
        old = {"at":NOW.isoformat(), "leader_context":{
            "up_codes":[], "broken_codes":[], "code_sector":{"600001":"BK1"},
            "score":50, "up_count":10, "broken_rate":20,
            "sectors":{"BK1":{"change_pct":1, "up_ratio":50, "rank":5}}}}
        event = {"at":(NOW+timedelta(minutes=1)).isoformat(), "leader_context":{
                   "up_codes":["600001"], "broken_codes":[], "code_sector":{"600001":"BK1"},
                   "score":51, "up_count":11, "broken_rate":19,
                   "sectors":{"BK1":{"change_pct":1.2, "up_ratio":52, "rank":5}}}}
        current = {"at":(NOW+timedelta(minutes=2)).isoformat(),
                   "up_codes":["600001"], "broken_codes":[], "code_sector":{"600001":"BK1"},
                   "score":54, "up_count":14, "broken_rate":16,
                   "sectors":{"BK1":{"change_pct":2.2, "up_ratio":62, "rank":3}}}
        self.assertNotIn("600001", leader_impacts([old], event["leader_context"] | {"at":event["at"]}))
        result = leader_impacts([old, event], current)["600001"]
        self.assertEqual(result["observations"], 1)
        self.assertGreater(result["sector_score"], 50)
        self.assertGreater(result["market_score"], 50)

    def test_numeric_missing_is_not_zero(self):
        for value in (None, "-", "nan", "inf"):
            self.assertIsNone(number(value))
        self.assertEqual(number(0), 0)
        self.assertIsNone(normalize_row({"f12":"600000", "f2":"-", "f3":0, "f18":10}))

    def test_position_cache_retries_missing_second_stock_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MarketDataClient(Path(tmp))
            closes = [{"date":(NOW.date()-timedelta(days=index+1)).isoformat(), "close":10}
                      for index in range(20)]
            client._position_history["stock:600000"] = {
                "quote_date":NOW.date().isoformat(), "closes":closes,
                "sources":["eastmoney"], "attempted_at":time.time()}
            self.assertTrue(client._history_is_ready("stock:600000", NOW.date()))
            client._position_history["stock:600000"]["attempted_at"] = 0
            self.assertFalse(client._history_is_ready("stock:600000", NOW.date()))
            client._position_history["stock:600000"]["sources"].append("tencent")
            self.assertTrue(client._history_is_ready("stock:600000", NOW.date()))

    def test_a_share_scope_and_no_price_rescaling(self):
        raw = {"f12":"920000", "f14":"测试", "f2":10.5, "f3":5, "f18":10, "f6":1e6}
        self.assertEqual(normalize_row(raw)["last"], 10.5)
        self.assertEqual(normalize_row(raw)["amount"], 1e6)
        self.assertIsNone(normalize_row(dict(raw, f12="900901")))
        self.assertIsNone(normalize_row(dict(raw, f12="510300")))
        self.assertIsNone(normalize_row(dict(raw, f14="半导体"), sector=True))

    def test_limit_pool_fields_preserve_board_time_seal_and_streak(self):
        row = normalize_pool_row({"c":"600000", "n":"测试股份", "zdp":10, "amount":3e8,
                                  "hs":12.5, "ltsz":25e8, "lbc":3, "fbt":93105,
                                  "lbt":145502, "fund":1.2e8, "zbc":2, "hybk":"光模块",
                                  "zttj":"3/3"}, "up")
        self.assertEqual(pool_time(93105), "09:31:05")
        self.assertIsNone(pool_time(0))
        self.assertEqual(row["first_limit_time"], "09:31:05")
        self.assertEqual(row["streak"], 3)
        self.assertEqual(row["seal_amount"], 1.2e8)
        self.assertEqual(row["industry"], "光模块")

    def test_pagination_complete_missing_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MarketDataClient(Path(tmp))
            raw = [{"f12":str(600000+i), "f14":"测试", "f2":10, "f18":10, "f3":0} for i in range(205)]
            client._page = Mock(side_effect=lambda p, *a: (raw[(p-1)*100:p*100], 205))
            result = client.clist()
            self.assertTrue(result["complete"])
            self.assertEqual(result["received"], 205)
            self.assertEqual(client._page.call_count, 3)
            client._page = Mock(side_effect=lambda p, *a: (raw[:100], 205))
            self.assertFalse(client.clist()["complete"])
            client._page = Mock(side_effect=lambda p, *a: (raw[(p-1)*100:p*100], 205 if p == 1 else None))
            self.assertFalse(client.clist()["complete"])

    def test_pool_null_partial_wrong_date_vs_true_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MarketDataClient(Path(tmp))
            response = Mock()
            client._get = Mock(return_value=response)
            for data in (None, {"qdate":20260831,"pool":[],"tc":2}, {"qdate":20260828,"pool":[],"tc":0}):
                response.json.return_value = {"data":data}
                self.assertIsNone(client.pool("down", "20260831")["count"])
            response.json.return_value = {"data":{"qdate":20260831,"pool":[],"tc":0}}
            self.assertEqual(client.pool("down", "20260831")["count"], 0)

    def test_quality_rejects_stale_missing_future_and_partial(self):
        feed = fixture()["stocks"]
        self.assertTrue(assess(feed, NOW, True)[1]["usable"])
        self.assertFalse(assess(feed, NOW + timedelta(minutes=4), True)[1]["usable"])
        self.assertFalse(assess(feed, NOW - timedelta(minutes=1), True)[1]["usable"])
        feed["complete"] = False
        self.assertFalse(assess(feed, NOW, True)[1]["usable"])
        feed["complete"] = True
        for row in feed["rows"]:
            row["timestamp"] = None
        self.assertFalse(assess(feed, NOW, True)[1]["usable"])

    def test_old_day_is_not_live_even_with_recent_fetch(self):
        feed = fixture()["stocks"]
        self.assertFalse(assess(feed, NOW + timedelta(days=1), False)[1]["usable"])

    def test_score_formula_and_labels(self):
        rows = fixture()["stocks"]["rows"]
        self.assertEqual(sentiment(rows)["score"], 68.8)
        self.assertEqual(sentiment(rows)["up"], 2700)
        self.assertEqual(cycle_label(50, None), "积累基线中")
        self.assertEqual(cycle_label(35, -8), "退潮")
        self.assertEqual(cycle_label(40, 8), "低位修复")
        self.assertEqual(cycle_label(80, 0), "亢奋观察")

    def test_baseline_no_lunch_or_source_bridge(self):
        point = {"at": NOW.isoformat(), "signature":"a"}
        self.assertEqual(find_baseline([point], NOW + timedelta(minutes=5), 5, "a"), point)
        self.assertIsNone(find_baseline([point], NOW + timedelta(minutes=5), 5, "b"))
        self.assertIsNone(find_baseline([point], NOW + timedelta(minutes=9), 5, "a"))
        point["at"] = NOW.replace(hour=11, minute=29).isoformat()
        self.assertIsNone(find_baseline([point], NOW.replace(hour=13), 90, "a"))


class MarketServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"config.json"
        self.path.write_text((ROOT/"config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.now = NOW
        self.service = MarketMonitorService(self.path, clock=lambda:self.now)
        self.service._running = True
        self.service._persist = Mock()

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def ingest(self, minute, change=.5, feeds=None, phase="continuous"):
        self.now = NOW + timedelta(minutes=minute)
        self.service.ingest(feeds or fixture(self.now, change), self.now, phase)
        return self.service.status()

    def test_warmup_then_baseline_and_no_duplicate_quotes(self):
        first = self.ingest(0)
        self.assertEqual(first["snapshot"]["sentiment"]["cycle"], "积累基线中")
        self.assertFalse(first["snapshot"]["signal_eligible"])
        for minute in range(1,6):
            status = self.ingest(minute)
        self.assertEqual(status["snapshot"]["sentiment"]["delta"], 0)
        self.assertEqual(status["snapshot"]["sectors"][0]["rank_changes"]["5"], 0)
        before = len(self.service._history)
        self.ingest(5)
        self.assertEqual(len(self.service._history), before)
        self.assertFalse(self.service.status()["snapshot"]["signal_eligible"])

    def test_cycle_requires_three_confirmations_and_dedupes(self):
        for minute in range(6): self.ingest(minute)
        for minute in (6,7):
            self.assertEqual(self.ingest(minute, change=-2)["events"], [])
        result = self.ingest(8, change=-2)
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("退潮", result["events"][0]["message"])
        self.assertEqual(len(self.ingest(9, change=-2)["events"]), 1)

    def test_rank_rotation_confirmed(self):
        for minute in range(6): self.ingest(minute)
        for minute in (6,7,8):
            feeds = fixture(NOW + timedelta(minutes=minute))
            feeds["sectors"]["rows"][-1]["change_pct"] = 6
            result = self.ingest(minute, feeds=feeds)
        self.assertTrue(any("走强" in e["message"] for e in result["events"]))
        self.assertGreater(result["snapshot"]["sectors"][0]["rank_changes"]["5"], 5)

    def test_top_five_sector_ladder_uses_members_and_limit_pool_evidence(self):
        feeds = fixture()
        top = max(feeds["sectors"]["rows"], key=lambda row: row["change_pct"])
        members = [
            {"code":"600001", "name":"空间龙", "change_pct":10, "amount":2e8,
             "turnover_pct":8, "float_market_cap":30e8, "industry":"细分甲", "timestamp":NOW.isoformat()},
            {"code":"600002", "name":"首板股", "change_pct":10, "amount":1e8,
             "turnover_pct":12, "float_market_cap":15e8, "industry":"细分乙", "timestamp":NOW.isoformat()},
            {"code":"600003", "name":"容量股", "change_pct":5, "amount":9e8,
             "turnover_pct":4, "float_market_cap":100e8, "industry":"细分甲", "timestamp":NOW.isoformat()},
        ]
        feeds["sector_members"] = {top["code"]: {"complete":True, "rows":members}}
        feeds["up"].update({"count":2, "rows":[
            {**members[0], "kind":"up", "streak":3, "first_limit_time":"09:35:00", "seal_amount":5e7},
            {**members[1], "kind":"up", "streak":1, "first_limit_time":"09:31:00", "seal_amount":8e7},
        ]})
        feeds["broken"].update({"count":1, "rows":[
            {**members[2], "kind":"broken", "streak":0, "first_limit_time":"10:20:00", "seal_amount":0},
        ]})
        result = self.ingest(0, feeds=feeds)
        ladders = result["snapshot"]["sector_ladders"]
        self.assertEqual(len(ladders), 5)
        first = ladders[0]
        self.assertEqual(first["name"], top["name"])
        self.assertEqual(first["emotion_leader"]["name"], "空间龙")
        self.assertEqual(first["earliest_limit"]["name"], "首板股")
        self.assertEqual(first["max_seal"]["name"], "首板股")
        self.assertEqual(first["capacity_core"]["name"], "容量股")
        self.assertEqual(first["main_directions"][0]["name"], "细分甲")
        self.assertEqual(first["limit_up_count"], 2)
        self.assertEqual(first["promotion_count"], 1)
        self.assertEqual(first["promoted_stocks"][0]["name"], "空间龙")
        self.assertEqual(first["broken_count"], 1)
        self.assertIn("sector_leader_score", first["sector_leader"])
        self.assertTrue(first["sector_leader_candidates"])
        self.assertTrue(result["snapshot"]["market_speculation_leaders"])
        self.assertIn("market_leader_score", result["snapshot"]["market_speculation_leaders"][0])

    def test_partial_stale_disabled_and_offhours_never_alert(self):
        for minute in range(6): self.ingest(minute)
        for minute in (6,7,8):
            feeds = fixture(NOW+timedelta(minutes=minute), -2)
            feeds["stocks"]["complete"] = False
            result = self.ingest(minute, feeds=feeds)
        self.assertEqual(result["events"], [])
        self.assertIsNone(result["snapshot"]["sentiment"])
        for minute in range(9,13): self.ingest(minute, change=-2, phase="closed")
        self.assertEqual(self.service.status()["events"], [])
        self.service._settings["alerts_enabled"] = False
        for minute in range(13,17): self.ingest(minute, change=4)
        self.assertEqual(self.service.status()["events"], [])

    def test_industry_failure_does_not_erase_breadth(self):
        feeds = fixture()
        feeds["sectors"]["complete"] = False
        result = self.ingest(0, feeds=feeds)
        self.assertIsNotNone(result["snapshot"]["sentiment"])
        self.assertFalse(result["snapshot"]["rotation_eligible"])
        self.assertTrue(all(r["label"] == "数据不足" for r in result["snapshot"]["sectors"]))

    def test_status_stops_advertising_signals_on_lunch_or_stale_snapshot(self):
        for minute in range(6): self.ingest(minute)
        self.assertTrue(self.service.status()["snapshot"]["signal_eligible"])
        self.now = NOW.replace(hour=11, minute=30)
        status = self.service.status()["snapshot"]
        self.assertEqual(status["phase"], "lunch_break")
        self.assertFalse(status["signal_eligible"])
        self.assertFalse(status["rotation_eligible"])

    def test_few_fresh_stocks_cannot_generate_a_market_score(self):
        feeds = fixture()
        for row in feeds["stocks"]["rows"][10:]: row["timestamp"] = (NOW-timedelta(minutes=10)).isoformat()
        self.assertIsNone(self.ingest(0, feeds=feeds)["snapshot"]["sentiment"])

    def test_midday_and_next_day_reset(self):
        for minute in range(6): self.ingest(minute)
        result = self.ingest(181)
        self.assertIsNone(result["snapshot"]["sentiment"]["delta"])
        result = self.ingest(1440)
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(result["events"], [])

    def test_settings_revision_validation_and_persistence(self):
        settings = self.service.settings()
        settings["interval_seconds"] = 30
        saved = self.service.save_settings(settings)
        with self.assertRaises(MarketMonitorError): self.service.save_settings(settings)
        self.assertEqual(saved["interval_seconds"], 30)
        other = MarketMonitorService(self.path)
        self.assertEqual(other.settings()["interval_seconds"], 30)
        self.assertFalse(other.status()["enabled"])
        for value in (0, 1, True, 999, float("nan")):
            with self.assertRaises(MarketMonitorError): validate_settings({"interval_seconds":value})

    def test_history_survives_restart_but_not_enabled_state(self):
        self.service._persist = MarketMonitorService._persist.__get__(self.service)
        self.ingest(0)
        restored = MarketMonitorService(self.path, clock=lambda:self.now)
        self.assertEqual(len(restored.status()["history"]), 1)
        self.assertTrue(restored.status()["snapshot"]["stale"])
        self.assertFalse(restored.status()["enabled"])
        self.assertFalse(restored.status()["snapshot"]["signal_eligible"])

    def test_worker_start_idempotent_stop_during_fetch(self):
        self.service._running = False
        entered, release = threading.Event(), threading.Event()
        def fetch(now):
            entered.set()
            release.wait(3)
            return fixture(now)
        self.service.client = Mock()
        self.service.client.fetch.side_effect = fetch
        self.service.start()
        self.assertTrue(entered.wait(2))
        worker = self.service._worker
        self.service.start()
        self.assertIs(worker, self.service._worker)
        self.service.stop()
        release.set()
        worker.join(3)
        self.assertEqual(self.service.client.fetch.call_count, 1)
        self.assertFalse(self.service.status()["enabled"])
        self.assertEqual(self.service.status()["events"], [])

    def test_api_status_settings_static_and_cross_origin_security(self):
        controller = DashboardController(self.path)
        server = DashboardHTTPServer(("127.0.0.1",0), controller, ROOT/"web")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base+"/api/market-monitor") as response:
                data = json.load(response)
            self.assertFalse(data["enabled"])
            self.assertIsNone(data["snapshot"])
            for path in ("/market.js", "/market.css"):
                with urllib.request.urlopen(base+path) as response:
                    self.assertEqual(response.status, 200)
            req = urllib.request.Request(base+"/api/market-monitor/settings", data=json.dumps(data["settings"]).encode(), method="PUT", headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status, 200)
            req = urllib.request.Request(base+"/api/market-monitor/start", method="POST", headers={"Origin":"https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
            self.assertEqual(error.exception.code, 403)
            self.assertFalse(controller.market_monitor.status()["enabled"])
        finally:
            server.shutdown(); server.server_close(); controller.shutdown(); thread.join(2)


if __name__ == "__main__":
    unittest.main()

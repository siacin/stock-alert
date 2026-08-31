from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_alert.config import AlertConfig
from stock_alert.detector import AlertDetector
from stock_alert.models import DEFAULT_MONITOR_ITEMS, ConsensusQuote, Quote, WatchStock
from stock_alert.storage import StateStore


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)


def source_quote(source: str, last: float, *, high: float, bid_volume: float = 1000) -> Quote:
    return Quote(
        source=source,
        code="600000",
        name="测试股票",
        timestamp=NOW,
        fetched_at=NOW,
        last=last,
        prev_close=10.0,
        open=10.0,
        high=high,
        low=9.8,
        volume_shares=100.0,
        amount=1000.0,
        bid1_price=last,
        bid1_volume=bid_volume,
        limit_up=11.0,
    )


def aggregate(*quotes: Quote) -> ConsensusQuote:
    last = sum(quote.last for quote in quotes) / len(quotes)
    return ConsensusQuote(
        code="600000",
        name="测试股票",
        timestamp=NOW,
        last=last,
        prev_close=10.0,
        open=10.0,
        high=max(quote.high or 0 for quote in quotes),
        low=9.8,
        volume_shares=100,
        amount=1000,
        average_price=10.0,
        limit_up=11.0,
        sources=tuple(quote.source for quote in quotes),
        source_quotes=quotes,
        price_spread_bps=0,
    )


class DetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        config = AlertConfig(
            line_hold_polls=2,
            line_confirmations=2,
            allow_single_source_fallback=True,
            notify_initial_bomb=False,
            database_path=Path(self.temp.name) / "state.db",
            stocks=(WatchStock("600000", "测试股票"),),
        )
        self.store = StateStore(config.database_path)
        self.detector = AlertDetector(config, self.store)
        self.watch = WatchStock("600000", "测试股票")
        self.watches = {self.watch.code: self.watch}

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_two_sources_combine_bomb_alert(self) -> None:
        sealed = aggregate(
            source_quote("tencent", 11.0, high=11.0),
            source_quote("sina", 11.0, high=11.0),
        )
        self.assertEqual(self.detector.process({"600000": sealed}, self.watches, NOW), [])

        opened = aggregate(
            source_quote("tencent", 10.98, high=11.0),
            source_quote("sina", 10.98, high=11.0),
        )
        events = self.detector.process({"600000": opened}, self.watches, NOW)
        bombs = [event for event in events if event.event_type == "bomb"]
        self.assertEqual(len(bombs), 1)
        self.assertEqual(set(bombs[0].sources), {"tencent", "sina"})

    def test_average_break_requires_hold_polls(self) -> None:
        above = aggregate(
            source_quote("tencent", 10.10, high=10.10),
            source_quote("sina", 10.10, high=10.10),
        )
        self.detector.process({"600000": above}, self.watches, NOW)

        below = aggregate(
            source_quote("tencent", 9.90, high=10.10),
            source_quote("sina", 9.90, high=10.10),
        )
        first = self.detector.process({"600000": below}, self.watches, NOW)
        self.assertFalse(any(event.event_type == "break_average" for event in first))
        second = self.detector.process({"600000": below}, self.watches, NOW)
        self.assertTrue(any(event.event_type == "break_average" for event in second))

    def test_reseal_after_bomb(self) -> None:
        sealed = aggregate(source_quote("tencent", 11.0, high=11.0))
        opened = aggregate(source_quote("tencent", 10.98, high=11.0))
        self.detector.process({"600000": sealed}, self.watches, NOW)
        self.detector.process({"600000": opened}, self.watches, NOW)
        events = self.detector.process({"600000": sealed}, self.watches, NOW)
        self.assertTrue(any(event.event_type == "reseal" for event in events))

    def test_initial_bomb_can_be_reported(self) -> None:
        config = self.detector.config
        config.notify_initial_bomb = True
        opened = aggregate(source_quote("sina", 10.80, high=11.0))
        events = self.detector.process({"600000": opened}, self.watches, NOW)
        self.assertTrue(any(event.event_type == "bomb" and event.metadata["initial"] for event in events))

    def test_rapid_rise_within_twenty_seconds_emits_once(self) -> None:
        start = aggregate(
            source_quote("tencent", 10.00, high=10.00),
            source_quote("sina", 10.00, high=10.00),
        )
        risen = aggregate(
            source_quote("tencent", 10.31, high=10.31),
            source_quote("sina", 10.31, high=10.31),
        )
        self.detector.process({"600000": start}, self.watches, NOW)
        events = self.detector.process({"600000": risen}, self.watches, NOW + timedelta(seconds=10))
        rapid = [event for event in events if event.event_type == "rapid_rise"]
        self.assertEqual(len(rapid), 1)
        self.assertAlmostEqual(rapid[0].metadata["move_pct"], 3.1, places=2)
        repeated = self.detector.process({"600000": risen}, self.watches, NOW + timedelta(seconds=12))
        self.assertFalse(any(event.event_type == "rapid_rise" for event in repeated))

    def test_rapid_fall_ignores_samples_outside_window(self) -> None:
        start = aggregate(
            source_quote("tencent", 10.00, high=10.00),
            source_quote("sina", 10.00, high=10.00),
        )
        fallen = aggregate(
            source_quote("tencent", 9.69, high=10.00),
            source_quote("sina", 9.69, high=10.00),
        )
        self.detector.process({"600000": start}, self.watches, NOW)
        too_late = self.detector.process({"600000": fallen}, self.watches, NOW + timedelta(seconds=21))
        self.assertFalse(any(event.event_type == "rapid_fall" for event in too_late))

        self.detector = AlertDetector(self.detector.config, self.store)
        self.detector.process({"600000": start}, self.watches, NOW)
        events = self.detector.process({"600000": fallen}, self.watches, NOW + timedelta(seconds=8))
        rapid = [event for event in events if event.event_type == "rapid_fall"]
        self.assertEqual(len(rapid), 1)
        self.assertAlmostEqual(rapid[0].metadata["move_pct"], -3.1, places=2)

    def test_monitor_items_default_to_every_rule(self) -> None:
        self.assertEqual(self.watch.monitor_items, DEFAULT_MONITOR_ITEMS)

    def test_board_events_are_suppressed_when_only_rapid_rise_is_selected(self) -> None:
        watch = WatchStock("600000", "测试股票", monitor_items=("rapid_rise",))
        watches = {watch.code: watch}
        sealed = aggregate(source_quote("tencent", 11.0, high=11.0))
        opened = aggregate(source_quote("tencent", 10.80, high=11.0))

        self.detector.process({"600000": sealed}, watches, NOW)
        events = self.detector.process({"600000": opened}, watches, NOW + timedelta(seconds=2))

        self.assertFalse(any(event.event_type in {"open_board_warning", "bomb", "reseal"} for event in events))

    def test_only_selected_cost_line_can_emit(self) -> None:
        watch = WatchStock("600000", "测试股票", cost=10.0, monitor_items=("cost",))
        watches = {watch.code: watch}
        above = aggregate(
            source_quote("tencent", 10.10, high=10.10),
            source_quote("sina", 10.10, high=10.10),
        )
        below = aggregate(
            source_quote("tencent", 9.90, high=10.10),
            source_quote("sina", 9.90, high=10.10),
        )

        self.detector.process({"600000": above}, watches, NOW)
        self.detector.process({"600000": below}, watches, NOW + timedelta(seconds=2))
        events = self.detector.process({"600000": below}, watches, NOW + timedelta(seconds=4))

        self.assertTrue(any(event.event_type == "break_cost" for event in events))
        self.assertFalse(any(event.event_type == "break_average" for event in events))

    def test_unknown_or_empty_monitor_items_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WatchStock("600000", monitor_items=())
        with self.assertRaises(ValueError):
            WatchStock("600000", monitor_items=("not-a-rule",))


if __name__ == "__main__":
    unittest.main()

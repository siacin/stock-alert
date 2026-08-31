from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from stock_alert.analysis import (
    DailyBar,
    HistoryBundle,
    build_analysis,
    apply_sina_qfq,
    parse_eastmoney_bars,
    parse_sina_bars,
    parse_sina_qfq_factors,
    parse_tencent_bars,
)
from stock_alert.app import is_market_open, market_phase, seconds_until_next_session
from stock_alert.config import AlertConfig
from stock_alert.models import fallback_limit_pct


TZ = ZoneInfo("Asia/Shanghai")


class AnalysisParserTests(unittest.TestCase):
    def test_three_history_parsers(self) -> None:
        tencent = {
            "data": {"sh600000": {"qfqday": [["2026-08-28", "9.01", "9.00", "9.04", "8.95", "587868"]]}}
        }
        eastmoney = {"data": {"klines": ["2026-08-28,9.01,9.00,9.04,8.95,587868,528817735"]}}
        sina = [{"day": "2026-08-28", "open": "9.01", "high": "9.04", "low": "8.95", "close": "9.00", "volume": "587868"}]

        for rows in (
            parse_tencent_bars(tencent, "600000"),
            parse_eastmoney_bars(eastmoney),
            parse_sina_bars(sina),
        ):
            self.assertEqual(rows[0].trade_date, date(2026, 8, 28))
            self.assertEqual(rows[0].close, 9.0)
            self.assertEqual(rows[0].high, 9.04)

    def test_sina_forward_adjustment(self) -> None:
        bars = [
            DailyBar(date(2026, 7, 15), 9.31, 9.40, 9.20, 9.31),
            DailyBar(date(2026, 7, 16), 8.85, 9.00, 8.80, 8.85),
        ]
        payload = 'var sh600000qfq={"data":[{"d":"2026-07-16","f":"1.0"},{"d":"2025-07-16","f":"1.0472440944882"}]};'
        adjusted = apply_sina_qfq(bars, parse_sina_qfq_factors(payload))
        self.assertAlmostEqual(adjusted[0].close, 8.89, places=2)
        self.assertEqual(adjusted[1].close, 8.85)


class IndicatorAnalysisTests(unittest.TestCase):
    def test_rising_series_produces_complete_evidence(self) -> None:
        first = date(2026, 1, 1)
        bars = []
        for index in range(100):
            close = 10.0 + index * 0.08
            bars.append(DailyBar(first + timedelta(days=index), close - 0.03, close + 0.10, close - 0.12, close, 1000 + index))
        history = HistoryBundle(
            bars=tuple(bars),
            sources=("eastmoney", "tencent"),
            source_status=(),
            adjusted=True,
            expires_at=0,
        )
        quote = {"name": "测试股票", "last": bars[-1].close, "timestamp": datetime(2026, 4, 10, 14, 0, tzinfo=TZ).isoformat()}

        result = build_analysis("600000", bars, quote, history)

        self.assertEqual(result["label"], "偏强")
        self.assertGreater(result["technical_score"], 0)
        self.assertGreater(result["indicators"]["ma5"], result["indicators"]["ma20"])
        self.assertIsNotNone(result["indicators"]["rsi14"])
        self.assertEqual(len(result["chart"]), 80)
        self.assertTrue(any(item["method"] == "MACD" for item in result["signals"]))

    def test_current_main_board_st_fallback_is_ten_percent(self) -> None:
        self.assertEqual(fallback_limit_pct("600000", "*ST 测试"), 0.10)
        self.assertEqual(fallback_limit_pct("300001", "ST 测试"), 0.20)
        self.assertEqual(fallback_limit_pct("830001", "ST 测试"), 0.30)


class MarketPhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AlertConfig()

    def at(self, hour: int, minute: int) -> datetime:
        return datetime(2026, 8, 28, hour, minute, tzinfo=TZ)

    def test_auction_gap_is_not_live(self) -> None:
        self.assertEqual(market_phase(self.at(9, 20), self.config), "open_auction")
        self.assertEqual(market_phase(self.at(9, 26), self.config), "auction_gap")
        self.assertFalse(is_market_open(self.at(9, 26), self.config))
        self.assertEqual(market_phase(self.at(9, 30), self.config), "continuous")
        self.assertEqual(seconds_until_next_session(self.at(9, 26), self.config), 240.0)

    def test_lunch_and_closing_auction(self) -> None:
        self.assertEqual(market_phase(self.at(11, 45), self.config), "lunch_break")
        self.assertFalse(is_market_open(self.at(11, 45), self.config))
        self.assertEqual(market_phase(self.at(14, 58), self.config), "closing_auction")
        self.assertTrue(is_market_open(self.at(14, 58), self.config))


if __name__ == "__main__":
    unittest.main()

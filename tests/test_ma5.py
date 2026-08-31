from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from stock_alert.ma5 import (
    AutomaticMA5Service,
    DailyClose,
    HistoryCacheEntry,
    parse_eastmoney_daily,
    parse_sina_daily,
    parse_tencent_daily,
)
from stock_alert.models import ConsensusQuote, WatchStock


TZ = ZoneInfo("Asia/Shanghai")


class AutomaticMA5Tests(unittest.TestCase):
    def test_tencent_history_parser(self) -> None:
        payload = {
            "data": {
                "sh600000": {
                    "day": [
                        ["2026-08-27", "9.18", "9.07", "9.20", "8.98", "958101"],
                        ["2026-08-28", "9.01", "9.00", "9.04", "8.95", "587868"],
                    ]
                }
            }
        }
        rows = parse_tencent_daily(payload, "600000")
        self.assertEqual(rows[-1], DailyClose(date(2026, 8, 28), 9.0))

    def test_eastmoney_history_parser(self) -> None:
        payload = {
            "data": {
                "klines": [
                    "2026-08-27,9.18,9.07,9.20,8.98,958101",
                    "2026-08-28,9.01,9.00,9.04,8.95,587868",
                ]
            }
        }
        rows = parse_eastmoney_daily(payload)
        self.assertEqual(rows[-1], DailyClose(date(2026, 8, 28), 9.0))

    def test_sina_history_parser(self) -> None:
        payload = [
            {"day": "2026-08-27", "close": "9.070"},
            {"day": "2026-08-28", "close": "9.000"},
        ]
        rows = parse_sina_daily(payload)
        self.assertEqual(rows[-1], DailyClose(date(2026, 8, 28), 9.0))

    def test_dynamic_ma5_uses_live_price_and_four_closes(self) -> None:
        service = AutomaticMA5Service(3.0)
        quote_date = date(2026, 8, 28)
        service._cache[("600000", quote_date)] = HistoryCacheEntry(
            closes=(
                DailyClose(date(2026, 8, 24), 10.0),
                DailyClose(date(2026, 8, 25), 11.0),
                DailyClose(date(2026, 8, 26), 12.0),
                DailyClose(date(2026, 8, 27), 13.0),
            ),
            sources=("eastmoney", "tencent"),
            errors=(),
            attempted_at=0.0,
        )
        watch = WatchStock("600000", "测试股票", ma5=99.0, auto_ma5=True)
        quote = ConsensusQuote(
            code="600000",
            name="测试股票",
            timestamp=datetime(2026, 8, 28, 10, 0, tzinfo=TZ),
            last=14.0,
            prev_close=13.0,
            open=13.5,
            high=14.1,
            low=13.4,
            volume_shares=1000,
            amount=14000,
            average_price=14.0,
            limit_up=14.3,
            sources=("eastmoney", "tencent"),
            source_quotes=(),
            price_spread_bps=0.0,
        )

        effective, details = service.resolve({"600000": watch}, {"600000": quote})

        self.assertEqual(effective["600000"].ma5, 12.0)
        self.assertEqual(details["600000"]["mode"], "auto")
        self.assertEqual(details["600000"]["sources"], ["eastmoney", "tencent"])

    def test_manual_mode_keeps_configured_value(self) -> None:
        service = AutomaticMA5Service(3.0)
        watch = WatchStock("600000", ma5=9.15, auto_ma5=False)
        effective, details = service.resolve({"600000": watch}, {})
        self.assertEqual(effective["600000"].ma5, 9.15)
        self.assertEqual(details["600000"]["mode"], "manual")


if __name__ == "__main__":
    unittest.main()

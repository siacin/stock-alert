from __future__ import annotations

import unittest
from datetime import date

from stock_alert.trend import (
    IntradayTrendService,
    TrendPoint,
    TrendSeries,
    parse_sina_trend,
    parse_tencent_trend,
)


class IntradayTrendTests(unittest.TestCase):
    def test_tencent_minute_parser(self) -> None:
        payload = {
            "data": {
                "sh600000": {
                    "data": {
                        "date": "20260828",
                        "data": ["0930 9.01 100 90100.00", "0931 9.02 200 180300.00"],
                    }
                }
            }
        }
        series = parse_tencent_trend(payload, "600000")
        self.assertIsNotNone(series)
        assert series is not None
        self.assertEqual(series.trade_date, date(2026, 8, 28))
        self.assertEqual(series.points[-1].time, "09:31")
        self.assertEqual(series.points[-1].price, 9.02)

    def test_sina_five_minute_parser(self) -> None:
        payload = [
            {"day": "2026-08-28 14:55:00", "close": "9.01"},
            {"day": "2026-08-28 15:00:00", "close": "9.00"},
        ]
        series = parse_sina_trend(payload)
        self.assertIsNotNone(series)
        assert series is not None
        self.assertEqual(series.points[-1], TrendPoint("15:00", 9.0))

    def test_combine_uses_median_at_matching_minute(self) -> None:
        series = [
            TrendSeries("tencent", date(2026, 8, 28), (TrendPoint("09:30", 10.0), TrendPoint("09:31", 11.0))),
            TrendSeries("sina", date(2026, 8, 28), (TrendPoint("09:30", 12.0),)),
        ]
        payload = IntradayTrendService._combine("600000", series, [])
        self.assertEqual(payload["sources"], ["sina", "tencent"])
        self.assertEqual(payload["points"][0]["price"], 11.0)
        self.assertEqual(payload["points"][1]["price"], 11.0)


if __name__ == "__main__":
    unittest.main()

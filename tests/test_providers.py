from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_alert.providers import EastMoneyProvider, SinaProvider, TencentProvider


TZ = ZoneInfo("Asia/Shanghai")
FETCHED = datetime(2026, 8, 28, 11, 19, tzinfo=TZ)


class ProviderParserTests(unittest.TestCase):
    def test_tencent_parser(self) -> None:
        raw = (
            'v_sh600000="1~浦发银行~600000~9.00~9.07~9.01~587868~224488~363380~'
            '8.99~9804~8.98~10304~8.97~4961~8.96~12245~8.95~21070~9.00~1274~'
            '9.01~5415~9.02~5191~9.03~7488~9.04~4771~~20260828161452~-0.07~-0.77~'
            '9.04~8.95~9.00/587868/528817735~587868~52882~0.18~5.85~~9.04~8.95~0.99~'
            '2997.53~2997.53~0.40~9.98~8.16~0.72~34245~9.00~4.84~5.99~";'
        )
        quotes = TencentProvider.parse(raw, FETCHED)
        quote = quotes["600000"]
        self.assertEqual(quote.name, "浦发银行")
        self.assertEqual(quote.last, 9.00)
        self.assertEqual(quote.limit_up, 9.98)
        self.assertEqual(quote.bid1_price, 8.99)
        self.assertEqual(quote.bid1_volume, 980400)
        self.assertEqual(quote.volume_shares, 58_786_800)
        self.assertEqual(quote.amount, 528_817_735)

    def test_sina_parser(self) -> None:
        raw = (
            'var hq_str_sh600000="浦发银行,9.010,9.070,9.000,9.040,8.950,8.990,9.000,'
            '58786810,528817735.000,980400,8.990,1030400,8.980,496100,8.970,1224500,'
            '8.960,2107000,8.950,127400,9.000,541500,9.010,519100,9.020,748829,9.030,'
            '477133,9.040,2026-08-28,15:34:58,00";'
        )
        quotes = SinaProvider.parse(raw, FETCHED)
        quote = quotes["600000"]
        self.assertEqual(quote.last, 9.00)
        self.assertEqual(quote.prev_close, 9.07)
        self.assertEqual(quote.ask1_price, 9.00)
        self.assertEqual(quote.ask1_volume, 127400)
        self.assertAlmostEqual(quote.average_price or 0, 8.9955, places=4)

    def test_eastmoney_parser(self) -> None:
        payload = {
            "data": {
                "diff": [
                    {
                        "f2": 900,
                        "f5": 587868,
                        "f6": 528817735.0,
                        "f12": "600000",
                        "f14": "浦发银行",
                        "f15": 904,
                        "f16": 895,
                        "f17": 901,
                        "f18": 907,
                        "f31": 899,
                        "f32": 900,
                        "f124": 1787900000,
                    }
                ]
            }
        }
        quotes = EastMoneyProvider.parse(payload, FETCHED)
        quote = quotes["600000"]
        self.assertEqual(quote.last, 9.00)
        self.assertEqual(quote.prev_close, 9.07)
        self.assertEqual(quote.bid1_price, 8.99)
        self.assertEqual(quote.volume_shares, 58_786_800)


if __name__ == "__main__":
    unittest.main()

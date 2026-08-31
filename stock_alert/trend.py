from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

import requests

from .models import normalize_code, symbol_for


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class TrendPoint:
    time: str
    price: float
    average_price: float | None = None


@dataclass(slots=True, frozen=True)
class TrendSeries:
    source: str
    trade_date: date
    points: tuple[TrendPoint, ...]


def parse_tencent_trend(payload: dict, code: str) -> TrendSeries | None:
    symbol = symbol_for(code)
    minute_data = ((((payload.get("data") or {}).get(symbol) or {}).get("data") or {}))
    date_text = str(minute_data.get("date", ""))
    try:
        trade_date = datetime.strptime(date_text, "%Y%m%d").date()
    except ValueError:
        return None
    points: list[TrendPoint] = []
    for row in minute_data.get("data") or []:
        fields = str(row).split()
        if len(fields) < 2 or len(fields[0]) != 4:
            continue
        try:
            price = float(fields[1])
            volume_lots = float(fields[2]) if len(fields) > 2 else 0.0
            amount = float(fields[3]) if len(fields) > 3 else 0.0
        except ValueError:
            continue
        if price <= 0:
            continue
        average = amount / (volume_lots * 100) if volume_lots > 0 and amount > 0 else None
        points.append(TrendPoint(f"{fields[0][:2]}:{fields[0][2:]}", price, average))
    return TrendSeries("tencent", trade_date, tuple(points)) if points else None


def parse_sina_trend(payload: list | dict) -> TrendSeries | None:
    rows = payload if isinstance(payload, list) else []
    dated_points: list[tuple[date, TrendPoint]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = datetime.strptime(str(row.get("day", "")), "%Y-%m-%d %H:%M:%S")
            price = float(row.get("close", 0))
        except (TypeError, ValueError):
            continue
        if price > 0:
            dated_points.append((timestamp.date(), TrendPoint(timestamp.strftime("%H:%M"), price)))
    if not dated_points:
        return None
    latest_date = max(item[0] for item in dated_points)
    points = tuple(point for item_date, point in dated_points if item_date == latest_date)
    return TrendSeries("sina", latest_date, points)


class IntradayTrendService:
    tencent_endpoint = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    sina_endpoint = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

    def __init__(self, timeout_seconds: float = 4.0, cache_seconds: float = 20.0) -> None:
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _fetch_tencent(self, code: str) -> TrendSeries | None:
        response = requests.get(
            self.tencent_endpoint,
            params={"code": symbol_for(code)},
            headers={"User-Agent": "Mozilla/5.0 StockAlert/1.1", "Accept": "application/json,*/*"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return parse_tencent_trend(response.json(), code)

    def _fetch_sina(self, code: str) -> TrendSeries | None:
        response = requests.get(
            self.sina_endpoint,
            params={"symbol": symbol_for(code), "scale": "5", "ma": "no", "datalen": "60"},
            headers={
                "User-Agent": "Mozilla/5.0 StockAlert/1.1",
                "Accept": "application/json,*/*",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return parse_sina_trend(response.json())

    @staticmethod
    def _combine(code: str, series: list[TrendSeries], errors: list[str]) -> dict:
        if not series:
            return {"code": code, "trade_date": None, "sources": [], "points": [], "error": "; ".join(errors)}
        trade_date = max(item.trade_date for item in series)
        current = [item for item in series if item.trade_date == trade_date]
        prices: dict[str, list[float]] = {}
        averages: dict[str, list[float]] = {}
        for item in current:
            for point in item.points:
                prices.setdefault(point.time, []).append(point.price)
                if point.average_price:
                    averages.setdefault(point.time, []).append(point.average_price)
        points = [
            {
                "time": minute,
                "price": float(median(values)),
                "average_price": float(median(averages[minute])) if averages.get(minute) else None,
            }
            for minute, values in sorted(prices.items())
        ]
        return {
            "code": code,
            "trade_date": trade_date.isoformat(),
            "sources": sorted(item.source for item in current),
            "points": points,
            "error": None if points else "; ".join(errors),
        }

    def get(self, code: str) -> dict:
        code = normalize_code(code)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(code)
            if cached and now - cached[0] < self.cache_seconds:
                return cached[1]

        series: list[TrendSeries] = []
        errors: list[str] = []
        fetchers = (("tencent", self._fetch_tencent), ("sina", self._fetch_sina))
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="intraday-trend") as executor:
            future_map = {executor.submit(fetcher, code): source for source, fetcher in fetchers}
            for future in as_completed(future_map):
                source = future_map[future]
                try:
                    result = future.result()
                    if result:
                        series.append(result)
                    else:
                        errors.append(f"{source}: 无有效分时数据")
                except Exception as exc:  # noqa: BLE001 - one trend source may fail independently
                    errors.append(f"{source}: {type(exc).__name__}: {exc}")
        payload = self._combine(code, series, errors)
        with self._lock:
            self._cache[code] = (time.monotonic(), payload)
        if not payload["points"]:
            LOGGER.warning("分时走势获取失败 code=%s error=%s", code, payload["error"])
        return payload

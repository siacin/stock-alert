from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date
from statistics import median
from typing import Callable

import requests

from .analysis import apply_sina_qfq, parse_sina_bars, parse_sina_qfq_factors
from .models import ConsensusQuote, WatchStock, secid_for, symbol_for


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DailyClose:
    trade_date: date
    close: float


@dataclass(slots=True)
class HistoryCacheEntry:
    closes: tuple[DailyClose, ...]
    sources: tuple[str, ...]
    errors: tuple[str, ...]
    attempted_at: float


def parse_tencent_daily(payload: dict, code: str) -> list[DailyClose]:
    symbol = symbol_for(code)
    block = ((payload.get("data") or {}).get(symbol) or {})
    rows = block.get("qfqday") or block.get("day") or []
    result: list[DailyClose] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            parsed_date = date.fromisoformat(str(row[0]))
            close = float(row[2])
        except (TypeError, ValueError):
            continue
        if close > 0:
            result.append(DailyClose(parsed_date, close))
    return result


def parse_eastmoney_daily(payload: dict) -> list[DailyClose]:
    rows = ((payload.get("data") or {}).get("klines") or [])
    result: list[DailyClose] = []
    for row in rows:
        fields = row.split(",") if isinstance(row, str) else row
        if not isinstance(fields, (list, tuple)) or len(fields) < 3:
            continue
        try:
            parsed_date = date.fromisoformat(str(fields[0]))
            close = float(fields[2])
        except (TypeError, ValueError):
            continue
        if close > 0:
            result.append(DailyClose(parsed_date, close))
    return result


def parse_sina_daily(payload: list | dict) -> list[DailyClose]:
    rows = payload if isinstance(payload, list) else []
    result: list[DailyClose] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            parsed_date = date.fromisoformat(str(row.get("day", "")))
            close = float(row.get("close", 0))
        except (TypeError, ValueError):
            continue
        if close > 0:
            result.append(DailyClose(parsed_date, close))
    return result


class AutomaticMA5Service:
    """Resolve a dynamic MA5 from the live price and four completed daily closes."""

    tencent_endpoint = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    eastmoney_endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    sina_endpoint = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

    def __init__(self, timeout_seconds: float, retry_seconds: float = 300.0) -> None:
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self.retry_seconds = retry_seconds
        self._cache: dict[tuple[str, date], HistoryCacheEntry] = {}

    def _fetch_tencent(self, code: str) -> list[DailyClose]:
        response = requests.get(
            self.tencent_endpoint,
            params={"param": f"{symbol_for(code)},day,,,12,qfq"},
            headers={"User-Agent": "Mozilla/5.0 StockAlert/1.1", "Accept": "application/json,*/*"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return parse_tencent_daily(response.json(), code)

    def _fetch_eastmoney(self, code: str) -> list[DailyClose]:
        response = requests.get(
            self.eastmoney_endpoint,
            params={
                "secid": secid_for(code),
                "klt": "101",
                "fqt": "1",
                "lmt": "12",
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
            headers={
                "User-Agent": "Mozilla/5.0 StockAlert/1.1",
                "Accept": "application/json,*/*",
                "Referer": "https://quote.eastmoney.com/",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return parse_eastmoney_daily(response.json())

    def _fetch_sina(self, code: str) -> list[DailyClose]:
        response = requests.get(
            self.sina_endpoint,
            params={"symbol": symbol_for(code), "scale": "240", "ma": "no", "datalen": "12", "fq": "1"},
            headers={
                "User-Agent": "Mozilla/5.0 StockAlert/1.1",
                "Accept": "application/json,*/*",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        bars = parse_sina_bars(response.json())
        factor_response = requests.get(
            f"https://finance.sina.com.cn/realstock/company/{symbol_for(code)}/qfq.js",
            headers={
                "User-Agent": "Mozilla/5.0 StockAlert/1.1",
                "Accept": "application/json,*/*",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=self.timeout_seconds,
        )
        factor_response.raise_for_status()
        adjusted = apply_sina_qfq(bars, parse_sina_qfq_factors(factor_response.text))
        return [DailyClose(item.trade_date, item.close) for item in adjusted]

    def _needs_refresh(self, key: tuple[str, date], now_monotonic: float) -> bool:
        cached = self._cache.get(key)
        if cached is None:
            return True
        if len(cached.closes) >= 4:
            return False
        return now_monotonic - cached.attempted_at >= self.retry_seconds

    def _load_histories(self, keys: list[tuple[str, date]]) -> None:
        fetchers: tuple[tuple[str, Callable[[str], list[DailyClose]]], ...] = (
            ("tencent", self._fetch_tencent),
            ("eastmoney", self._fetch_eastmoney),
            ("sina", self._fetch_sina),
        )
        grouped: dict[tuple[str, date], dict[str, list[DailyClose]]] = defaultdict(dict)
        errors: dict[tuple[str, date], list[str]] = defaultdict(list)
        tasks = []
        for code, quote_date in keys:
            for source, fetcher in fetchers:
                tasks.append((code, quote_date, source, fetcher))
        if not tasks:
            return

        with ThreadPoolExecutor(max_workers=min(8, len(tasks)), thread_name_prefix="ma5-history") as executor:
            future_map = {
                executor.submit(fetcher, code): (code, quote_date, source)
                for code, quote_date, source, fetcher in tasks
            }
            for future in as_completed(future_map):
                code, quote_date, source = future_map[future]
                key = (code, quote_date)
                try:
                    rows = [row for row in future.result() if row.trade_date < quote_date]
                    if rows:
                        grouped[key][source] = rows
                    else:
                        errors[key].append(f"{source}: 未返回已完成日线")
                except Exception as exc:  # noqa: BLE001 - one history source may fail independently
                    errors[key].append(f"{source}: {type(exc).__name__}: {exc}")

        attempted_at = time.monotonic()
        for key in keys:
            by_date: dict[date, list[float]] = defaultdict(list)
            source_rows = grouped.get(key, {})
            for rows in source_rows.values():
                for row in rows:
                    by_date[row.trade_date].append(row.close)
            selected_dates = sorted(by_date)[-4:]
            closes = tuple(DailyClose(day, float(median(by_date[day]))) for day in selected_dates)
            entry = HistoryCacheEntry(
                closes=closes,
                sources=tuple(sorted(source_rows)),
                errors=tuple(errors.get(key, ())),
                attempted_at=attempted_at,
            )
            self._cache[key] = entry
            if len(closes) >= 4:
                LOGGER.info(
                    "自动 MA5 日线就绪 code=%s sources=%s dates=%s",
                    key[0],
                    ",".join(entry.sources),
                    ",".join(item.trade_date.isoformat() for item in closes),
                )
            else:
                LOGGER.warning("自动 MA5 日线不足 code=%s errors=%s", key[0], "; ".join(entry.errors))

    def resolve(
        self,
        watches: dict[str, WatchStock],
        consensus: dict[str, ConsensusQuote],
    ) -> tuple[dict[str, WatchStock], dict[str, dict]]:
        now_monotonic = time.monotonic()
        pending: list[tuple[str, date]] = []
        for code, quote in consensus.items():
            watch = watches[code]
            key = (code, quote.timestamp.date())
            if watch.auto_ma5 and self._needs_refresh(key, now_monotonic):
                pending.append(key)
        self._load_histories(pending)

        effective = dict(watches)
        details: dict[str, dict] = {}
        for code, watch in watches.items():
            quote = consensus.get(code)
            if not watch.auto_ma5:
                details[code] = {
                    "mode": "manual",
                    "value": watch.ma5,
                    "sources": [],
                    "completed_closes": [],
                    "error": None,
                }
                continue
            if quote is None:
                continue
            entry = self._cache.get((code, quote.timestamp.date()))
            if entry and len(entry.closes) >= 4:
                value = (sum(item.close for item in entry.closes[-4:]) + quote.last) / 5
                effective[code] = replace(watch, ma5=value)
                details[code] = {
                    "mode": "auto",
                    "value": value,
                    "sources": list(entry.sources),
                    "completed_closes": [
                        {"date": item.trade_date.isoformat(), "close": item.close}
                        for item in entry.closes[-4:]
                    ],
                    "error": None,
                }
            else:
                error = "; ".join(entry.errors) if entry and entry.errors else "历史日线不足"
                details[code] = {
                    "mode": "fallback" if watch.ma5 else "unavailable",
                    "value": watch.ma5,
                    "sources": list(entry.sources) if entry else [],
                    "completed_closes": [],
                    "error": error,
                }
        return effective, details

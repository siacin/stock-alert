from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Iterable
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

import requests

from .models import ProviderResult, Quote, WatchStock, secid_for, symbol_for


TZ = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)


def _float(value: object, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _chunks(items: list[WatchStock], size: int) -> Iterable[list[WatchStock]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _parse_time(value: str, formats: tuple[str, ...], fallback: datetime) -> datetime:
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=TZ)
        except (TypeError, ValueError):
            continue
    return fallback


class BaseProvider:
    name = "base"

    def __init__(self, timeout_seconds: float, max_batch_size: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_batch_size = max_batch_size
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockAlert/1.0",
                "Accept": "*/*",
                "Connection": "keep-alive",
            }
        )

    def fetch(self, stocks: list[WatchStock]) -> ProviderResult:
        started = time.perf_counter()
        quotes: dict[str, Quote] = {}
        errors: list[str] = []
        for batch in _chunks(stocks, self.max_batch_size):
            try:
                quotes.update(self.fetch_batch(batch))
            except Exception as exc:  # noqa: BLE001 - one failed chunk must not erase completed chunks
                codes = ",".join(stock.code for stock in batch)
                errors.append(f"codes={codes} {type(exc).__name__}: {exc}")
        error = "; ".join(errors) if errors else None
        if not quotes and not error:
            error = "数据源未返回任何有效行情"
        latency_ms = round((time.perf_counter() - started) * 1000)
        return ProviderResult(source=self.name, quotes=quotes, latency_ms=latency_ms, error=error)

    def fetch_batch(self, stocks: list[WatchStock]) -> dict[str, Quote]:
        raise NotImplementedError


class TencentProvider(BaseProvider):
    name = "tencent"
    endpoint = "https://qt.gtimg.cn/q="
    _line_re = re.compile(r'v_[^=]+="(.*?)";')

    def fetch_batch(self, stocks: list[WatchStock]) -> dict[str, Quote]:
        symbols = ",".join(symbol_for(stock.code) for stock in stocks)
        response = self.session.get(
            self.endpoint + url_quote(symbols, safe=","),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        text = response.content.decode("gb18030", errors="replace")
        return self.parse(text, datetime.now(TZ))

    @classmethod
    def parse(cls, text: str, fetched_at: datetime) -> dict[str, Quote]:
        result: dict[str, Quote] = {}
        for match in cls._line_re.finditer(text):
            fields = match.group(1).split("~")
            if len(fields) < 49:
                continue
            code = fields[2]
            last = _float(fields[3])
            prev_close = _float(fields[4])
            if not code or not last or not prev_close:
                continue
            composite = fields[35].split("/") if len(fields) > 35 else []
            amount = _float(composite[2]) if len(composite) >= 3 else None
            update_time = _parse_time(fields[30], ("%Y%m%d%H%M%S",), fetched_at)
            result[code] = Quote(
                source=cls.name,
                code=code,
                name=fields[1],
                timestamp=update_time,
                fetched_at=fetched_at,
                last=last,
                prev_close=prev_close,
                open=_float(fields[5]),
                high=_float(fields[33]),
                low=_float(fields[34]),
                volume_shares=(_float(fields[6]) or 0) * 100,
                amount=amount,
                bid1_price=_float(fields[9]),
                bid1_volume=(_float(fields[10]) or 0) * 100,
                ask1_price=_float(fields[19]),
                ask1_volume=(_float(fields[20]) or 0) * 100,
                limit_up=_float(fields[47]),
                raw={"turnover_rate": _float(fields[38])},
            )
        return result


class SinaProvider(BaseProvider):
    name = "sina"
    endpoint = "https://hq.sinajs.cn/list="
    _line_re = re.compile(r'hq_str_(?:sh|sz|bj)(\d{6})="(.*?)";')

    def __init__(self, timeout_seconds: float, max_batch_size: int) -> None:
        super().__init__(timeout_seconds, max_batch_size)
        self.session.headers.update({"Referer": "https://finance.sina.com.cn/"})

    def fetch_batch(self, stocks: list[WatchStock]) -> dict[str, Quote]:
        symbols = ",".join(symbol_for(stock.code) for stock in stocks)
        response = self.session.get(
            self.endpoint + url_quote(symbols, safe=","),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        text = response.content.decode("gb18030", errors="replace")
        return self.parse(text, datetime.now(TZ))

    @classmethod
    def parse(cls, text: str, fetched_at: datetime) -> dict[str, Quote]:
        result: dict[str, Quote] = {}
        for match in cls._line_re.finditer(text):
            code = match.group(1)
            fields = match.group(2).split(",")
            if len(fields) < 32:
                continue
            last = _float(fields[3])
            prev_close = _float(fields[2])
            if not code or not last or not prev_close:
                continue
            update_time = _parse_time(f"{fields[30]} {fields[31]}", ("%Y-%m-%d %H:%M:%S",), fetched_at)
            result[code] = Quote(
                source=cls.name,
                code=code,
                name=fields[0],
                timestamp=update_time,
                fetched_at=fetched_at,
                last=last,
                prev_close=prev_close,
                open=_float(fields[1]),
                high=_float(fields[4]),
                low=_float(fields[5]),
                volume_shares=_float(fields[8]),
                amount=_float(fields[9]),
                bid1_price=_float(fields[11]),
                bid1_volume=_float(fields[10]),
                ask1_price=_float(fields[21]),
                ask1_volume=_float(fields[20]),
                limit_up=None,
            )
        return result


class EastMoneyProvider(BaseProvider):
    name = "eastmoney"
    endpoints = (
        "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
    )
    fields = "f2,f5,f6,f12,f14,f15,f16,f17,f18,f31,f32,f124"

    def __init__(self, timeout_seconds: float, max_batch_size: int) -> None:
        super().__init__(timeout_seconds, max_batch_size)
        self.session.headers.update({"Referer": "https://quote.eastmoney.com/"})

    def fetch_batch(self, stocks: list[WatchStock]) -> dict[str, Quote]:
        params = {
            "secids": ",".join(secid_for(stock.code) for stock in stocks),
            "fields": self.fields,
            "_": str(int(time.time() * 1000)),
        }
        last_error: Exception | None = None
        for endpoint in self.endpoints:
            try:
                response = self.session.get(endpoint, params=params, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = json.loads(response.content.decode("utf-8"))
                return self.parse(payload, datetime.now(TZ))
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
        if last_error:
            raise last_error
        return {}

    @classmethod
    def parse(cls, payload: dict, fetched_at: datetime) -> dict[str, Quote]:
        result: dict[str, Quote] = {}
        rows = ((payload.get("data") or {}).get("diff") or [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows:
            code = str(row.get("f12", ""))
            last_raw = _float(row.get("f2"))
            prev_raw = _float(row.get("f18"))
            if not code or not last_raw or not prev_raw:
                continue
            timestamp_value = _float(row.get("f124"))
            timestamp = (
                datetime.fromtimestamp(timestamp_value, TZ)
                if timestamp_value and timestamp_value > 1_000_000_000
                else fetched_at
            )
            scale = 100.0
            result[code] = Quote(
                source=cls.name,
                code=code,
                name=str(row.get("f14", "")),
                timestamp=timestamp,
                fetched_at=fetched_at,
                last=last_raw / scale,
                prev_close=prev_raw / scale,
                open=(_float(row.get("f17")) or 0) / scale,
                high=(_float(row.get("f15")) or 0) / scale,
                low=(_float(row.get("f16")) or 0) / scale,
                volume_shares=(_float(row.get("f5")) or 0) * 100,
                amount=_float(row.get("f6")),
                bid1_price=(_float(row.get("f31")) or 0) / scale,
                bid1_volume=None,
                ask1_price=(_float(row.get("f32")) or 0) / scale,
                ask1_volume=None,
                limit_up=None,
            )
        return result


PROVIDER_TYPES = {
    "tencent": TencentProvider,
    "eastmoney": EastMoneyProvider,
    "sina": SinaProvider,
}


class MultiSourceClient:
    def __init__(self, provider_names: tuple[str, ...], timeout_seconds: float, max_batch_size: int) -> None:
        self.providers = [PROVIDER_TYPES[name](timeout_seconds, max_batch_size) for name in provider_names]

    def fetch_all(self, stocks: tuple[WatchStock, ...]) -> list[ProviderResult]:
        enabled = [stock for stock in stocks if stock.enabled]
        results: list[ProviderResult] = []
        with ThreadPoolExecutor(max_workers=len(self.providers), thread_name_prefix="quote-source") as executor:
            future_to_provider = {executor.submit(provider.fetch, enabled): provider for provider in self.providers}
            for future in as_completed(future_to_provider):
                provider = future_to_provider[future]
                try:
                    result = future.result()
                except Exception as exc:  # defensive: BaseProvider already isolates normal failures
                    result = ProviderResult(provider.name, {}, 0, f"{type(exc).__name__}: {exc}")
                if result.error:
                    LOGGER.warning("数据源失败 source=%s latency=%dms error=%s", result.source, result.latency_ms, result.error)
                else:
                    LOGGER.debug("数据源完成 source=%s quotes=%d latency=%dms", result.source, len(result.quotes), result.latency_ms)
                results.append(result)
        return sorted(results, key=lambda item: item.source)

from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime
from statistics import fmean, median, pstdev
from typing import Any, Callable, Mapping, Sequence

import requests

from .models import normalize_code, secid_for, symbol_for


@dataclass(slots=True, frozen=True)
class DailyBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    amount: float | None = None


@dataclass(slots=True)
class HistoryBundle:
    bars: tuple[DailyBar, ...]
    sources: tuple[str, ...]
    source_status: tuple[dict[str, Any], ...]
    adjusted: bool
    expires_at: float


def _date(value: Any) -> date:
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10])


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bar(
    trade_date: Any,
    open_price: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any = None,
    amount: Any = None,
) -> DailyBar | None:
    try:
        parsed_date = _date(trade_date)
    except ValueError:
        return None
    values = [_number(item) for item in (open_price, high, low, close)]
    if any(item is None or item <= 0 for item in values):
        return None
    opened, highest, lowest, closed = (float(item) for item in values)
    highest = max(highest, opened, closed)
    lowest = min(lowest, opened, closed)
    return DailyBar(parsed_date, opened, highest, lowest, closed, _number(volume), _number(amount))


def parse_tencent_bars(payload: Mapping[str, Any], code: str) -> list[DailyBar]:
    block = ((payload.get("data") or {}).get(symbol_for(code)) or {})
    rows = block.get("qfqday") or block.get("day") or []
    result: list[DailyBar] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        item = _bar(row[0], row[1], row[3], row[4], row[2], row[5], row[6] if len(row) > 6 else None)
        if item:
            result.append(item)
    return sorted(result, key=lambda item: item.trade_date)


def parse_eastmoney_bars(payload: Mapping[str, Any]) -> list[DailyBar]:
    rows = ((payload.get("data") or {}).get("klines") or [])
    result: list[DailyBar] = []
    for row in rows:
        fields = row.split(",") if isinstance(row, str) else row
        if not isinstance(fields, (list, tuple)) or len(fields) < 7:
            continue
        item = _bar(fields[0], fields[1], fields[3], fields[4], fields[2], fields[5], fields[6])
        if item:
            result.append(item)
    return sorted(result, key=lambda item: item.trade_date)


def parse_sina_bars(payload: list | Mapping[str, Any]) -> list[DailyBar]:
    rows = payload if isinstance(payload, list) else []
    result: list[DailyBar] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = _bar(
            row.get("day"), row.get("open"), row.get("high"), row.get("low"),
            row.get("close"), row.get("volume"), row.get("amount"),
        )
        if item:
            result.append(item)
    return sorted(result, key=lambda item: item.trade_date)


def parse_sina_qfq_factors(payload: str) -> list[tuple[date, float]]:
    """Parse Sina's corporate-action factor file used for forward adjustment."""
    start = payload.find("{")
    end = payload.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("新浪复权因子格式无效")
    decoded = json.loads(payload[start : end + 1])
    result: list[tuple[date, float]] = []
    for item in decoded.get("data", []):
        try:
            trade_date = _date(item.get("d"))
            factor = float(item.get("f"))
        except (AttributeError, TypeError, ValueError):
            continue
        if factor > 0:
            result.append((trade_date, factor))
    return sorted(result, key=lambda item: item[0])


def apply_sina_qfq(bars: Sequence[DailyBar], factors: Sequence[tuple[date, float]]) -> list[DailyBar]:
    """Convert Sina raw OHLC to a current-basis forward-adjusted price series.

    Sina's factor file provides the theoretical ex-price ratio at each action.
    Cash distributions are applied as an additive gap (matching common Chinese
    charting clients); large share-ratio actions use multiplicative adjustment.
    """
    if not factors:
        return list(bars)
    raw = sorted(bars, key=lambda item: item.trade_date)
    adjusted = list(raw)
    for index in range(1, len(factors)):
        effective_date, after_factor = factors[index]
        _previous_date, before_factor = factors[index - 1]
        if after_factor <= 0 or before_factor <= 0:
            continue
        previous_rows = [item for item in raw if item.trade_date < effective_date]
        if not previous_rows:
            continue
        ratio = before_factor / after_factor
        if ratio <= 1.0000001:
            continue
        previous_close = previous_rows[-1].close
        cash_gap = previous_close - previous_close / ratio

        def transform(value: float) -> float:
            # Cash dividends normally produce a small theoretical-price ratio;
            # bonus/split actions produce a much larger one and must preserve
            # percentage returns through division.
            return value - cash_gap if ratio < 1.25 else value / ratio

        adjusted = [
            replace(
                item,
                open=transform(item.open), high=transform(item.high),
                low=transform(item.low), close=transform(item.close),
            ) if item.trade_date < effective_date else item
            for item in adjusted
        ]
    return adjusted


def sma(values: Sequence[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if period <= 0:
        return output
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            output[index] = running / period
    return output


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    output = [float(values[0])]
    for value in values[1:]:
        output.append(alpha * value + (1.0 - alpha) * output[-1])
    return output


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return output
    gains = [max(values[index] - values[index - 1], 0.0) for index in range(1, len(values))]
    losses = [max(values[index - 1] - values[index], 0.0) for index in range(1, len(values))]
    average_gain = fmean(gains[:period])
    average_loss = fmean(losses[:period])

    def score() -> float:
        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

    output[period] = score()
    for index in range(period + 1, len(values)):
        gain = gains[index - 1]
        loss = losses[index - 1]
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        output[index] = score()
    return output


def atr(bars: Sequence[DailyBar], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None] * len(bars)
    if not bars:
        return output
    ranges = [bars[0].high - bars[0].low]
    for index in range(1, len(bars)):
        current = bars[index]
        previous_close = bars[index - 1].close
        ranges.append(max(current.high - current.low, abs(current.high - previous_close), abs(current.low - previous_close)))
    if len(ranges) < period:
        return output
    current_atr = fmean(ranges[:period])
    output[period - 1] = current_atr
    for index in range(period, len(ranges)):
        current_atr = (current_atr * (period - 1) + ranges[index]) / period
        output[index] = current_atr
    return output


def _last(values: Sequence[float | None]) -> float | None:
    return values[-1] if values and values[-1] is not None else None


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None


def _median_optional(values: Sequence[float | None]) -> float | None:
    valid = [float(value) for value in values if value is not None and math.isfinite(value)]
    return float(median(valid)) if valid else None


def merge_bars(source_rows: Mapping[str, Sequence[DailyBar]]) -> list[DailyBar]:
    by_date: dict[date, list[DailyBar]] = {}
    for rows in source_rows.values():
        for item in rows:
            by_date.setdefault(item.trade_date, []).append(item)
    merged: list[DailyBar] = []
    for trade_date in sorted(by_date):
        rows = by_date[trade_date]
        opened = float(median(item.open for item in rows))
        closed = float(median(item.close for item in rows))
        highest = max(float(median(item.high for item in rows)), opened, closed)
        lowest = min(float(median(item.low for item in rows)), opened, closed)
        merged.append(
            DailyBar(
                trade_date, opened, highest, lowest, closed,
                _median_optional([item.volume for item in rows]),
                _median_optional([item.amount for item in rows]),
            )
        )
    return merged


class TechnicalAnalysisService:
    """Multi-source, read-only technical analysis for one watched A-share."""

    tencent_endpoint = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    eastmoney_endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    sina_endpoint = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

    def __init__(self, timeout_seconds: float = 5.0, cache_seconds: float = 300.0, bars: int = 260) -> None:
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self.cache_seconds = max(10.0, float(cache_seconds))
        self.bar_limit = max(120, int(bars))
        self._cache: dict[str, HistoryBundle] = {}
        self._lock = threading.Lock()

    @property
    def _headers(self) -> dict[str, str]:
        return {"User-Agent": "Mozilla/5.0 StockAlert/1.2", "Accept": "application/json,*/*"}

    def _fetch_tencent(self, code: str) -> list[DailyBar]:
        response = requests.get(
            self.tencent_endpoint,
            params={"param": f"{symbol_for(code)},day,,,{self.bar_limit},qfq"},
            headers=self._headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return parse_tencent_bars(response.json(), code)

    def _fetch_eastmoney(self, code: str) -> list[DailyBar]:
        response = requests.get(
            self.eastmoney_endpoint,
            params={
                "secid": secid_for(code), "klt": "101", "fqt": "1", "lmt": str(self.bar_limit),
                "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
            headers={**self._headers, "Referer": "https://quote.eastmoney.com/"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return parse_eastmoney_bars(response.json())

    def _fetch_sina(self, code: str) -> list[DailyBar]:
        response = requests.get(
            self.sina_endpoint,
            params={"symbol": symbol_for(code), "scale": "240", "ma": "no", "datalen": str(self.bar_limit)},
            headers={**self._headers, "Referer": "https://finance.sina.com.cn/"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        bars = parse_sina_bars(response.json())
        factor_response = requests.get(
            f"https://finance.sina.com.cn/realstock/company/{symbol_for(code)}/qfq.js",
            headers={**self._headers, "Referer": "https://finance.sina.com.cn/"},
            timeout=self.timeout_seconds,
        )
        factor_response.raise_for_status()
        factors = parse_sina_qfq_factors(factor_response.text)
        return apply_sina_qfq(bars, factors)

    def _load(self, code: str) -> HistoryBundle:
        fetchers: tuple[tuple[str, bool, Callable[[str], list[DailyBar]]], ...] = (
            ("tencent", True, self._fetch_tencent),
            ("eastmoney", True, self._fetch_eastmoney),
            ("sina", True, self._fetch_sina),
        )
        fetched: dict[str, list[DailyBar]] = {}
        status: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="technical-history") as executor:
            future_map = {executor.submit(fetcher, code): (name, adjusted) for name, adjusted, fetcher in fetchers}
            for future in as_completed(future_map):
                name, adjusted = future_map[future]
                try:
                    rows = future.result()
                    if not rows:
                        raise ValueError("未返回日线")
                    fetched[name] = rows
                    status[name] = {"name": name, "ok": True, "bars": len(rows), "adjusted": adjusted, "error": None}
                except Exception as exc:  # noqa: BLE001 - every free source degrades independently
                    status[name] = {
                        "name": name, "ok": False, "bars": 0, "adjusted": adjusted,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        adjusted_rows = dict(fetched)
        selected = adjusted_rows
        if not selected:
            details = "; ".join(f"{name}: {item['error']}" for name, item in sorted(status.items()))
            raise RuntimeError(f"三路历史日线均不可用（{details}）")
        bars = tuple(merge_bars(selected)[-self.bar_limit :])
        return HistoryBundle(
            bars=bars,
            sources=tuple(sorted(selected)),
            source_status=tuple(status[name] for name in ("tencent", "eastmoney", "sina")),
            adjusted=bool(adjusted_rows),
            expires_at=time.monotonic() + self.cache_seconds,
        )

    def _history(self, code: str) -> HistoryBundle:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(code)
            if cached and cached.expires_at > now:
                return cached
        loaded = self._load(code)
        with self._lock:
            self._cache[code] = loaded
        return loaded

    @staticmethod
    def _with_live_bar(bars: Sequence[DailyBar], quote: Mapping[str, Any] | None) -> list[DailyBar]:
        output = list(bars)
        if not quote or _number(quote.get("last")) is None:
            return output
        timestamp = str(quote.get("timestamp") or datetime.now().isoformat())
        try:
            trade_date = _date(timestamp)
        except ValueError:
            trade_date = datetime.now().date()
        close = float(quote["last"])
        opened = _number(quote.get("open")) or close
        highest = max(_number(quote.get("high")) or close, opened, close)
        lowest = min(_number(quote.get("low")) or close, opened, close)
        current = DailyBar(
            trade_date, opened, highest, lowest, close,
            _number(quote.get("volume_shares")), _number(quote.get("amount")),
        )
        if output and output[-1].trade_date == trade_date:
            output[-1] = current
        elif not output or output[-1].trade_date < trade_date:
            output.append(current)
        return output

    def get(self, code: str, live_quote: Mapping[str, Any] | None = None) -> dict[str, Any]:
        code = normalize_code(code)
        history = self._history(code)
        bars = self._with_live_bar(history.bars, live_quote)
        if len(bars) < 60:
            raise RuntimeError(f"有效日线仅 {len(bars)} 根，至少需要 60 根")
        return build_analysis(code, bars, live_quote, history)


def build_analysis(
    code: str,
    bars: Sequence[DailyBar],
    live_quote: Mapping[str, Any] | None = None,
    history: HistoryBundle | None = None,
) -> dict[str, Any]:
    closes = [item.close for item in bars]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    dif = [fast - slow for fast, slow in zip(ema12, ema26)]
    dea = ema(dif, 9)
    macd_hist = [(left - right) * 2.0 for left, right in zip(dif, dea)]
    rsi14 = rsi(closes, 14)
    atr14 = atr(bars, 14)
    close = closes[-1]
    boll_mid = _last(ma20)
    boll_std = pstdev(closes[-20:]) if len(closes) >= 20 else None
    boll_upper = boll_mid + 2 * boll_std if boll_mid is not None and boll_std is not None else None
    boll_lower = boll_mid - 2 * boll_std if boll_mid is not None and boll_std is not None else None
    previous_20 = bars[-21:-1] if len(bars) >= 21 else bars[:-1]
    channel_high = max((item.high for item in previous_20), default=None)
    channel_low = min((item.low for item in previous_20), default=None)
    ma20_slope = None
    if len(ma20) >= 6 and ma20[-1] is not None and ma20[-6] is not None:
        ma20_slope = (float(ma20[-1]) / float(ma20[-6]) - 1.0) * 100.0
    return20 = (close / closes[-21] - 1.0) * 100.0 if len(closes) >= 21 else None
    daily_returns = [(closes[index] / closes[index - 1] - 1.0) * 100.0 for index in range(max(1, len(closes) - 19), len(closes))]
    volatility20 = pstdev(daily_returns) if len(daily_returns) >= 10 else None
    current_atr = _last(atr14)
    natr14 = current_atr / close * 100.0 if current_atr else None

    score = 0
    current_ma20 = _last(ma20)
    current_ma60 = _last(ma60)
    current_rsi = _last(rsi14)
    if current_ma20:
        score += 18 if close >= current_ma20 else -18
    if current_ma20 and current_ma60:
        score += 15 if current_ma20 >= current_ma60 else -15
    if ma20_slope is not None:
        score += 12 if ma20_slope > 0.3 else -12 if ma20_slope < -0.3 else 0
    score += 15 if dif[-1] >= dea[-1] else -15
    if current_rsi is not None:
        score += 10 if 50 <= current_rsi <= 70 else -8 if 30 <= current_rsi < 50 else 4 if current_rsi > 70 else -4
    if boll_mid:
        score += 8 if close >= boll_mid else -8
    breakout = bool(channel_high and close > channel_high)
    breakdown = bool(channel_low and close < channel_low)
    score += 12 if breakout else -12 if breakdown else 0
    score = max(-100, min(100, score))
    label = "偏强" if score >= 35 else "偏弱" if score <= -35 else "中性"

    signals: list[dict[str, str]] = []

    def signal(method: str, title: str, direction: str, detail: str) -> None:
        signals.append({"method": method, "title": title, "direction": direction, "detail": detail})

    if _last(ma5) and _last(ma10) and current_ma20:
        if close > float(_last(ma5)) > float(_last(ma10)) > current_ma20:
            signal("均线系统", "短期多头排列", "positive", "现价 > MA5 > MA10 > MA20，短中期趋势同向。")
        elif close < float(_last(ma5)) < float(_last(ma10)) < current_ma20:
            signal("均线系统", "短期空头排列", "negative", "现价 < MA5 < MA10 < MA20，短中期趋势偏弱。")
        else:
            signal("均线系统", "均线交织", "neutral", "MA5、MA10、MA20 尚未形成一致排列。")
    if len(dif) >= 2 and len(dea) >= 2:
        crossed_up = dif[-2] <= dea[-2] and dif[-1] > dea[-1]
        crossed_down = dif[-2] >= dea[-2] and dif[-1] < dea[-1]
        if crossed_up:
            signal("MACD", "DIF 上穿 DEA", "positive", "日线动量刚转强，仍需结合价格与成交验证。")
        elif crossed_down:
            signal("MACD", "DIF 下穿 DEA", "negative", "日线动量刚转弱，警惕趋势延续。")
        else:
            direction = "positive" if dif[-1] >= dea[-1] else "negative"
            signal("MACD", "红柱区" if direction == "positive" else "绿柱区", direction, "DIF 位于 DEA 上方。" if direction == "positive" else "DIF 位于 DEA 下方。")
    if current_rsi is not None:
        if current_rsi >= 70:
            signal("RSI(14)", "进入超买区", "warning", "动量强但短线拥挤，超买不等于立即反转。")
        elif current_rsi <= 30:
            signal("RSI(14)", "进入超卖区", "warning", "跌势可能过度，但超卖不等于已经见底。")
        else:
            signal("RSI(14)", "动量偏强" if current_rsi >= 50 else "动量偏弱", "positive" if current_rsi >= 50 else "negative", f"RSI(14) 为 {current_rsi:.1f}。")
    if breakout:
        signal("唐奇安通道", "突破前 20 日高点", "positive", f"现价高于前 20 日最高价 {channel_high:.2f}。")
    elif breakdown:
        signal("唐奇安通道", "跌破前 20 日低点", "negative", f"现价低于前 20 日最低价 {channel_low:.2f}。")
    else:
        signal("唐奇安通道", "仍在 20 日区间", "neutral", f"区间 {channel_low:.2f}–{channel_high:.2f}。" if channel_low and channel_high else "历史数据不足。")
    average_price = _number((live_quote or {}).get("average_price"))
    cost_price = _number((live_quote or {}).get("cost"))
    if average_price:
        signal(
            "盘中均价线", "位于分时均价上方" if close >= average_price else "跌至分时均价下方",
            "positive" if close >= average_price else "negative",
            f"现价相对分时均价偏离 {(close / average_price - 1.0) * 100.0:+.2f}%。",
        )
    if cost_price:
        signal(
            "持仓成本线", "位于成本线上方" if close >= cost_price else "位于成本线下方",
            "positive" if close >= cost_price else "warning",
            f"现价相对用户成本偏离 {(close / cost_price - 1.0) * 100.0:+.2f}%；成本线不参与技术强弱评分。",
        )
    board_state = str((live_quote or {}).get("board_state") or "")
    if board_state == "sealed":
        signal("涨停状态", "当前封板", "warning", "封板状态依赖免费快照和盘口字段，应继续用多源确认。")
    elif board_state == "opened":
        signal("涨停状态", "当日曾开板", "warning", "日内最高价触及涨停价，但当前已不在涨停价。")

    levels: list[dict[str, Any]] = []
    for name, value in (
        ("MA5", _last(ma5)), ("MA10", _last(ma10)), ("MA20", current_ma20), ("MA60", current_ma60),
        ("布林上轨", boll_upper), ("布林下轨", boll_lower), ("前20日高", channel_high), ("前20日低", channel_low),
        ("分时均价", average_price),
        ("持仓成本", cost_price),
    ):
        if value is None:
            continue
        levels.append({
            "name": name, "value": _rounded(value, 3),
            "relation": "上方" if close >= value else "下方",
            "distance_pct": _rounded((close / value - 1.0) * 100.0, 2),
        })

    risks: list[str] = []
    source_count = len(history.sources) if history else 0
    if source_count < 2:
        risks.append("当前只有一个历史日线来源参与计算，交叉校验能力下降。")
    if history and not history.adjusted:
        risks.append("前复权主链路不可用，当前使用未复权降级日线；除权附近的均线和动量指标可能失真。")
    if natr14 is not None and natr14 >= 4:
        risks.append(f"ATR(14) 占现价 {natr14:.1f}%，近期波动较大，固定阈值容易产生噪声。")
    if current_rsi is not None and (current_rsi >= 70 or current_rsi <= 30):
        risks.append("RSI 处于极值区，趋势可能延续，也可能快速回摆，需要等待价格确认。")
    if live_quote is None:
        risks.append("尚无本轮实时快照，最后一根 K 线可能不是当前盘中价格。")
    if not risks:
        risks.append("未发现突出的指标风险；免费行情仍可能延迟、缺失或短时中断。")

    trend_text = "中期趋势向上" if current_ma20 and current_ma60 and close > current_ma20 > current_ma60 else "中期趋势向下" if current_ma20 and current_ma60 and close < current_ma20 < current_ma60 else "中期趋势震荡"
    momentum_text = "动量偏强" if dif[-1] >= dea[-1] and (current_rsi or 50) >= 50 else "动量偏弱" if dif[-1] < dea[-1] and (current_rsi or 50) < 50 else "动量分歧"
    summary = f"{trend_text}，{momentum_text}；技术强弱评分 {score:+d}（{label}）。"

    chart = []
    start = max(0, len(bars) - 80)
    for index in range(start, len(bars)):
        chart.append({
            "date": bars[index].trade_date.isoformat(), "close": _rounded(closes[index]),
            "ma5": _rounded(ma5[index]), "ma20": _rounded(ma20[index]), "ma60": _rounded(ma60[index]),
        })

    return {
        "code": code,
        "name": str((live_quote or {}).get("name") or code),
        "price": _rounded(close),
        "change_pct": _rounded(_number((live_quote or {}).get("change_pct")), 2),
        "data_date": bars[-1].trade_date.isoformat(),
        "updated_at": str((live_quote or {}).get("timestamp") or datetime.now().isoformat()),
        "bars_count": len(bars),
        "adjusted": history.adjusted if history else True,
        "sources": list(history.sources) if history else [],
        "source_status": list(history.source_status) if history else [],
        "technical_score": score,
        "label": label,
        "summary": summary,
        "indicators": {
            "ma5": _rounded(_last(ma5)), "ma10": _rounded(_last(ma10)),
            "ma20": _rounded(current_ma20), "ma60": _rounded(current_ma60),
            "ma20_slope_5d_pct": _rounded(ma20_slope, 2),
            "macd": {"dif": _rounded(dif[-1], 4), "dea": _rounded(dea[-1], 4), "histogram": _rounded(macd_hist[-1], 4)},
            "rsi14": _rounded(current_rsi, 2),
            "boll": {"upper": _rounded(boll_upper), "middle": _rounded(boll_mid), "lower": _rounded(boll_lower)},
            "atr14": _rounded(current_atr), "natr14_pct": _rounded(natr14, 2),
            "donchian20": {"high": _rounded(channel_high), "low": _rounded(channel_low)},
            "return20_pct": _rounded(return20, 2), "volatility20_pct": _rounded(volatility20, 2),
        },
        "levels": levels,
        "signals": signals,
        "risks": risks,
        "chart": chart,
        "disclaimer": "技术指标只描述历史价格与当前状态，不预测必然结果，不构成投资建议。",
    }

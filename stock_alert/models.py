from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any, Iterable


PRICE_TICK = 0.01
MONITOR_ITEM_LABELS: dict[str, str] = {
    "open_board": "开板预警",
    "bomb": "炸板",
    "reseal": "回封",
    "rapid_rise": "快速拉升",
    "rapid_fall": "快速下跌",
    "average": "分时均价穿越",
    "cost": "成本线穿越",
    "ma5": "MA5 穿越",
}
DEFAULT_MONITOR_ITEMS = tuple(MONITOR_ITEM_LABELS)


def normalize_monitor_items(values: Any) -> tuple[str, ...]:
    """Validate and normalize per-stock alert switches in a stable UI order."""
    if values is None:
        return DEFAULT_MONITOR_ITEMS
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("monitor_items 必须是监控项目列表")
    requested = {str(value).strip() for value in values if str(value).strip()}
    unknown = requested - set(DEFAULT_MONITOR_ITEMS)
    if unknown:
        raise ValueError(f"不支持的监控项目: {', '.join(sorted(unknown))}")
    normalized = tuple(item for item in DEFAULT_MONITOR_ITEMS if item in requested)
    if not normalized:
        raise ValueError("启用的股票至少需要选择一个监控项目")
    return normalized


def normalize_code(value: str) -> str:
    """Normalize common A-share code formats to six digits."""
    raw = str(value).strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    if raw.startswith(("0.", "1.")):
        raw = raw.split(".", 1)[1]
    if "." in raw:
        left, right = raw.split(".", 1)
        if left.isdigit():
            raw = left
        elif right.isdigit():
            raw = right
    if not (raw.isdigit() and len(raw) == 6):
        raise ValueError(f"无效的 A 股代码: {value!r}")
    return raw


def market_of(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("4", "8")) or code.startswith("92"):
        return "BJ"
    return "SZ"


def symbol_for(code: str) -> str:
    market = market_of(code).lower()
    return f"{market}{normalize_code(code)}"


def secid_for(code: str) -> str:
    code = normalize_code(code)
    return f"{1 if market_of(code) == 'SH' else 0}.{code}"


def round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fallback_limit_pct(code: str, name: str = "") -> float:
    """Best-effort fallback; a configured/provider limit price always takes precedence."""
    code = normalize_code(code)
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if market_of(code) == "BJ":
        return 0.30
    # From 2026-07-06, risk-warning (ST/*ST) shares on the Shanghai and
    # Shenzhen main boards use the same 10% limit as other main-board shares.
    # Provider/configured limit prices still take precedence over this fallback.
    return 0.10


def compute_limit_up(prev_close: float | None, code: str, name: str = "", limit_pct: float | None = None) -> float | None:
    if not prev_close or prev_close <= 0:
        return None
    pct = fallback_limit_pct(code, name) if limit_pct is None else float(limit_pct)
    return round_price(prev_close * (1 + pct))


@dataclass(slots=True, frozen=True)
class WatchStock:
    code: str
    name: str = ""
    cost: float | None = None
    ma5: float | None = None
    auto_ma5: bool = True
    widget_enabled: bool = True
    limit_pct: float | None = None
    enabled: bool = True
    monitor_items: tuple[str, ...] = DEFAULT_MONITOR_ITEMS

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", normalize_code(self.code))
        for field_name in ("cost", "ma5"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{self.code} 的 {field_name} 必须大于 0")
        if self.limit_pct is not None and not 0 < self.limit_pct < 1:
            raise ValueError(f"{self.code} 的 limit_pct 必须位于 0 与 1 之间")
        object.__setattr__(self, "monitor_items", normalize_monitor_items(self.monitor_items))

    def monitors(self, item: str) -> bool:
        return item in self.monitor_items


@dataclass(slots=True)
class Quote:
    source: str
    code: str
    name: str
    timestamp: datetime
    fetched_at: datetime
    last: float
    prev_close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume_shares: float | None = None
    amount: float | None = None
    bid1_price: float | None = None
    bid1_volume: float | None = None
    ask1_price: float | None = None
    ask1_volume: float | None = None
    limit_up: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.code = normalize_code(self.code)

    @property
    def average_price(self) -> float | None:
        if self.amount is None or self.volume_shares is None or self.volume_shares <= 0:
            return None
        return self.amount / self.volume_shares

    def resolved_limit_up(self, watch: WatchStock) -> float | None:
        return self.limit_up or compute_limit_up(self.prev_close, self.code, self.name or watch.name, watch.limit_pct)

    def is_at_limit(self, watch: WatchStock) -> bool:
        limit_up = self.resolved_limit_up(watch)
        return bool(limit_up and self.last >= limit_up - PRICE_TICK / 2)

    def is_sealed(self, watch: WatchStock) -> bool:
        if not self.is_at_limit(watch):
            return False
        limit_up = self.resolved_limit_up(watch)
        if self.bid1_price is not None and self.bid1_volume is not None:
            return self.bid1_price >= float(limit_up) - PRICE_TICK / 2 and self.bid1_volume > 0
        return True


@dataclass(slots=True)
class ProviderResult:
    source: str
    quotes: dict[str, Quote]
    latency_ms: int
    error: str | None = None


def median_optional(values: Iterable[float | None]) -> float | None:
    cleaned = [float(value) for value in values if value is not None and value > 0]
    return float(median(cleaned)) if cleaned else None


@dataclass(slots=True)
class ConsensusQuote:
    code: str
    name: str
    timestamp: datetime
    last: float
    prev_close: float
    open: float | None
    high: float | None
    low: float | None
    volume_shares: float | None
    amount: float | None
    average_price: float | None
    limit_up: float | None
    sources: tuple[str, ...]
    source_quotes: tuple[Quote, ...]
    price_spread_bps: float


@dataclass(slots=True)
class AlertEvent:
    event_type: str
    code: str
    name: str
    occurred_at: datetime
    price: float
    line_price: float | None
    sources: tuple[str, ...]
    severity: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"{self.code}:{self.event_type}"

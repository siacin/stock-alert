from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import WatchStock


DEFAULT_SESSIONS = (("09:15", "11:30"), ("13:00", "15:00"))


@dataclass(slots=True)
class WebhookConfig:
    url: str
    kind: str = "generic"
    timeout_seconds: float = 3.0


@dataclass(slots=True)
class AlertConfig:
    poll_interval_seconds: float = 2.0
    request_timeout_seconds: float = 2.5
    providers: tuple[str, ...] = ("tencent", "eastmoney", "sina")
    max_batch_size: int = 50
    max_quote_age_seconds: float = 15.0
    max_price_spread_bps: float = 20.0
    line_confirmations: int = 2
    allow_single_source_fallback: bool = True
    hysteresis_bps: float = 3.0
    line_hold_polls: int = 2
    notify_initial_bomb: bool = True
    notify_recovery: bool = True
    rapid_move_window_seconds: float = 20.0
    rapid_move_threshold_pct: float = 3.0
    beep: bool = True
    database_path: Path = Path("data/stock-alert.db")
    log_path: Path = Path("data/stock-alert.log")
    sessions: tuple[tuple[str, str], ...] = DEFAULT_SESSIONS
    holidays: frozenset[str] = frozenset()
    cooldown_seconds: dict[str, int] = field(
        default_factory=lambda: {
            "open_board_warning": 20,
            "bomb": 20,
            "reseal": 20,
            "break_average": 60,
            "recover_average": 60,
            "break_cost": 60,
            "recover_cost": 60,
            "break_ma5": 60,
            "recover_ma5": 60,
            "rapid_rise": 30,
            "rapid_fall": 30,
            "data_degraded": 300,
        }
    )
    webhooks: tuple[WebhookConfig, ...] = ()
    stocks: tuple[WatchStock, ...] = ()


def _number(data: dict[str, Any], key: str, default: float, minimum: float) -> float:
    value = float(data.get(key, default))
    if value < minimum:
        raise ValueError(f"{key} 不能小于 {minimum}")
    return value


def load_config(path: str | Path) -> AlertConfig:
    config_path = Path(path).resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.parent

    stocks = tuple(WatchStock(**item) for item in data.get("stocks", ()) if item.get("enabled", True))
    if not stocks:
        raise ValueError("配置文件至少需要一只 enabled=true 的自选股")
    seen: set[str] = set()
    for stock in stocks:
        if stock.code in seen:
            raise ValueError(f"自选股代码重复: {stock.code}")
        seen.add(stock.code)

    supported = {"tencent", "eastmoney", "sina"}
    providers = tuple(str(item).lower() for item in data.get("providers", ("tencent", "eastmoney", "sina")))
    unknown = set(providers) - supported
    if unknown:
        raise ValueError(f"不支持的数据源: {', '.join(sorted(unknown))}")
    if not providers:
        raise ValueError("至少需要配置一个数据源")
    if len(set(providers)) != len(providers):
        raise ValueError("providers 中的数据源不能重复")

    notification = data.get("notifications", {})
    webhooks = tuple(WebhookConfig(**item) for item in notification.get("webhooks", ()))
    sessions = tuple(tuple(item) for item in data.get("sessions", DEFAULT_SESSIONS))
    if any(len(item) != 2 for item in sessions):
        raise ValueError("sessions 中每项必须是 [开始时间, 结束时间]")

    database_path = Path(data.get("database_path", "data/stock-alert.db"))
    log_path = Path(data.get("log_path", "data/stock-alert.log"))
    if not database_path.is_absolute():
        database_path = base / database_path
    if not log_path.is_absolute():
        log_path = base / log_path

    config = AlertConfig(
        poll_interval_seconds=_number(data, "poll_interval_seconds", 2.0, 0.5),
        request_timeout_seconds=_number(data, "request_timeout_seconds", 2.5, 0.5),
        providers=providers,
        max_batch_size=int(_number(data, "max_batch_size", 50, 1)),
        max_quote_age_seconds=_number(data, "max_quote_age_seconds", 15.0, 1.0),
        max_price_spread_bps=_number(data, "max_price_spread_bps", 20.0, 1.0),
        line_confirmations=int(_number(data, "line_confirmations", 2, 1)),
        allow_single_source_fallback=bool(data.get("allow_single_source_fallback", True)),
        hysteresis_bps=_number(data, "hysteresis_bps", 3.0, 0.0),
        line_hold_polls=int(_number(data, "line_hold_polls", 2, 1)),
        notify_initial_bomb=bool(data.get("notify_initial_bomb", True)),
        notify_recovery=bool(data.get("notify_recovery", True)),
        rapid_move_window_seconds=_number(data, "rapid_move_window_seconds", 20.0, 5.0),
        rapid_move_threshold_pct=_number(data, "rapid_move_threshold_pct", 3.0, 0.1),
        beep=bool(notification.get("beep", True)),
        database_path=database_path.resolve(),
        log_path=log_path.resolve(),
        sessions=sessions,
        holidays=frozenset(str(item) for item in data.get("holidays", ())),
        cooldown_seconds={**AlertConfig().cooldown_seconds, **data.get("cooldown_seconds", {})},
        webhooks=webhooks,
        stocks=stocks,
    )
    return config

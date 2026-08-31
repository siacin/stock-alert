"""Private Tailscale Serve access policy. Never trust these headers on a LAN listener."""
from __future__ import annotations

import json
import re
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any


REMOTE_PORT = 8766
REMOTE_CONFIG_KEYS = frozenset({
    "poll_interval_seconds", "request_timeout_seconds", "providers", "max_batch_size",
    "max_quote_age_seconds", "max_price_spread_bps", "line_confirmations",
    "allow_single_source_fallback", "hysteresis_bps", "line_hold_polls",
    "notify_initial_bomb", "notify_recovery", "rapid_move_window_seconds",
    "rapid_move_threshold_pct", "sessions", "holidays", "cooldown_seconds",
    "news_radar", "stocks", "_revision",
})
STOCK_KEYS = frozenset({
    "code", "name", "cost", "ma5", "auto_ma5", "widget_enabled", "monitor_items",
    "limit_pct", "enabled", "limit_up",
})


def remote_config_view(config: dict[str, Any]) -> dict[str, Any]:
    view = {key: value for key, value in config.items() if key in REMOTE_CONFIG_KEYS}
    view["stocks"] = [{k: v for k, v in stock.items() if k in STOCK_KEYS} for stock in config.get("stocks", [])]
    view["notifications"] = {"beep": config.get("notifications", {}).get("beep", True)}
    return view


def merge_remote_config(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if incoming.keys() - REMOTE_CONFIG_KEYS - {"notifications"}:
        raise ValueError("手机端不能修改文件路径、密钥或推送地址")
    notifications = incoming.get("notifications", {})
    if not isinstance(notifications, dict) or notifications.keys() - {"beep"}:
        raise ValueError("手机端只能修改电脑提示音开关")
    stocks = incoming.get("stocks", current.get("stocks", []))
    if not isinstance(stocks, list) or any(not isinstance(s, dict) or s.keys() - STOCK_KEYS for s in stocks):
        raise ValueError("自选股字段无效")
    result = {**current, **incoming}
    result["notifications"] = {**current.get("notifications", {}), **notifications}
    return result


class RemoteAccessPolicy:
    def __init__(self, config_path: str | Path) -> None:
        self.path = Path(config_path).resolve().parent / "data" / "remote-access.json"

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {"enabled": False}
            origin, login = value.get("origin", ""), value.get("owner_login", "")
            valid = (
                isinstance(origin, str)
                and re.fullmatch(r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)+\.ts\.net", origin)
                and isinstance(login, str) and bool(login.strip())
            )
            if value.get("enabled") is True and valid:
                return {"enabled": True, "origin": origin, "owner_login": login}
        except (OSError, ValueError):
            pass
        return {"enabled": False}

    def write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".pending")
        pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(self.path)

    def authorize(self, headers) -> bool:
        policy = self.read()
        if not policy["enabled"]:
            return False
        # Duplicate identity headers and malformed encoded values fail closed.
        values = headers.get_all("Tailscale-User-Login", [])
        if len(values) != 1:
            return False
        try:
            login = str(make_header(decode_header(values[0])))
        except (ValueError, LookupError, UnicodeError):
            return False
        return login == policy["owner_login"]

"""Explainable intraday rules; scores describe observations, not price forecasts."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
from statistics import mean, median
from pathlib import Path

from .app import market_phase
from .config import load_config
from .market_data import MarketDataClient, TZ
from .models import fallback_limit_pct

LOG = logging.getLogger(__name__)
DEFAULTS = {"interval_seconds": 60, "lookback_minutes": 5, "confirmations": 3,
            "rank_jump": 5, "alerts_enabled": True}
LIVE_PHASES = {"continuous", "closing_auction"}


class MarketMonitorError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def revised(settings):
    result = dict(settings)
    result["_revision"] = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    return result


def validate_settings(payload):
    if not isinstance(payload, dict):
        raise MarketMonitorError("设置必须是 JSON 对象")
    settings = {}
    ranges = {"interval_seconds": {30, 60, 120, 300}, "lookback_minutes": {5, 15, 30},
              "confirmations": set(range(2, 6)), "rank_jump": set(range(3, 16))}
    for key, choices in ranges.items():
        value = payload.get(key, DEFAULTS[key])
        if type(value) is not int or value not in choices:
            raise MarketMonitorError(f"{key} 超出允许范围")
        settings[key] = value
    if type(payload.get("alerts_enabled", True)) is not bool:
        raise MarketMonitorError("提醒开关必须为布尔值")
    settings["alerts_enabled"] = payload.get("alerts_enabled", True)
    return settings


def assess(feed, now, live, *, sectors=False):
    rows = feed.get("rows", [])
    dated = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row.get("timestamp") or "")
            if stamp.tzinfo and stamp <= now + timedelta(seconds=5):
                dated.append((row, stamp))
        except (ValueError, TypeError):
            pass
    day = Counter(stamp.date().isoformat() for _, stamp in dated).most_common(1)
    day = day[0][0] if day else None
    same_day = [(r, t) for r, t in dated if t.date().isoformat() == day]
    fresh = [(r, t) for r, t in same_day if -5 <= (now - t).total_seconds() <= 180]
    freshness = len(fresh) / len(same_day) if same_day else 0
    # Stopped/delisted instruments can retain old quotes: exclude, never count flat.
    coverage = feed.get("received", 0) / feed.get("expected", 1) if feed.get("expected") else 0
    sufficient = bool(feed.get("complete") and len(same_day) >= (31 if sectors else 3000)
                      and len(same_day) / max(len(rows), 1) >= .90)
    usable = sufficient and day == now.date().isoformat() and (not live or freshness >= .95)
    selected = fresh if live else same_day
    times = sorted(t for _, t in same_day)
    quote_time = times[len(times) // 2].isoformat() if times else None
    return [r for r, _ in selected], {
        "source": feed.get("source", "eastmoney"), "complete": bool(feed.get("complete")),
        "received": feed.get("received", 0), "expected": feed.get("expected", 0),
        "coverage_pct": round(100 * min(coverage, 1), 1), "valid": len(selected),
        "excluded": max(0, feed.get("received", len(rows)) - len(selected)), "fresh_pct": round(freshness * 100, 1),
        "date": day, "quote_time": quote_time, "usable": usable, "sufficient": sufficient,
        "error": feed.get("error"), "universe_date": feed.get("universe_date"),
        "universe_age_days": feed.get("universe_age_days", 0)}


def sentiment(rows):
    if not rows:
        return None
    changes = [r["change_pct"] for r in rows]
    n = len(changes)
    up, down = sum(v > 0 for v in changes), sum(v < 0 for v in changes)
    strong, weak = sum(v >= 3 for v in changes), sum(v <= -3 for v in changes)
    breadth = (up - down) / n
    middle = median(changes)
    parts = {"breadth": round(30 * breadth, 2),
             "median": round(15 * max(-1, min(1, middle / 2)), 2),
             "tails": round(5 * (strong - weak) / n, 2)}
    amounts = [r["amount"] for r in rows if r.get("amount") is not None]
    return {"score": round(50 + sum(parts.values()), 1), "up": up, "down": down,
            "flat": n - up - down, "strong": strong, "weak": weak,
            "breadth_pct": round(breadth * 100, 2), "median_pct": round(middle, 3),
            "equal_weight_pct": round(mean(changes), 4), "parts": parts,
            "amount": sum(amounts) if len(amounts) >= n * .95 else None,
            "amount_coverage_pct": round(100 * len(amounts) / n, 1)}


def cycle_label(score, delta):
    if delta is None:
        return "积累基线中"
    if delta <= -5:
        return "退潮" if score < 45 else "高位分歧" if score >= 65 else "转弱"
    if delta >= 5:
        return "低位修复" if score < 50 else "升温"
    if score <= 25:
        return "低迷观察"
    if score >= 75:
        return "亢奋观察"
    return "偏强震荡" if score >= 55 else "偏弱震荡" if score <= 45 else "中性震荡"


def find_baseline(history, now, minutes, signature):
    target = now - timedelta(minutes=minutes)
    for point in reversed(history):
        at = datetime.fromisoformat(point["at"])
        if at <= target:
            # Never bridge overnight, lunch, long outages or changed provider/universe.
            if (at.date() == now.date() and (at.hour < 12) == (now.hour < 12)
                    and (target - at).total_seconds() <= 150 and point["signature"] == signature):
                return point
            return None
    return None


def _ladder_stock(row):
    return {key: row.get(key) for key in (
        "code", "name", "change_pct", "amount", "turnover_pct", "float_market_cap",
        "streak", "first_limit_time", "last_limit_time", "seal_amount", "open_count",
        "industry", "limit_statistics", "return_3d", "return_5d", "return_10d",
        "pre_return_5d", "pre_return_10d", "sector_return_5d", "excess_5d",
        "pre_sector_return_5d", "pre_excess_5d",
        "distance_20d_high_pct", "prior_limit_count_10d", "days_since_prior_limit",
        "history_sources", "history_verified", "sector_history_mode",
        "price_position_level", "price_position_reason",
        "position_level", "position_label",
        "position_reason", "catch_up_score", "catch_up_confidence",
        "sector_leader_score", "sector_leader_components", "sector_leader_role",
        "market_leader_score", "market_leader_components", "market_leader_role",
        "followers_after_limit", "seal_float_ratio_pct", "limit_utilization_pct",
        "attention_sources", "attention_best_rank", "influence_observations",
        "sector_influence_score", "market_influence_score", "dual_leader") if row.get(key) is not None}


def _pct_change(latest, base):
    return round((latest / base - 1) * 100, 2) if latest and base and latest > 0 and base > 0 else None


def _history_values(bundle):
    values = []
    for item in bundle.get("closes", []) if isinstance(bundle, dict) else []:
        try:
            close = float(item["close"])
            if close > 0:
                values.append(close)
        except (KeyError, TypeError, ValueError):
            continue
    return values


def _price_position(result):
    pre5, pre10, excess = result.get("pre_return_5d"), result.get("pre_return_10d"), result.get("pre_excess_5d")
    if pre5 is None or pre10 is None or excess is None:
        result["price_position_level"] = "未知"
        result["price_position_reason"] = "历史日线或行业基线不足"
    elif pre5 >= 8 or pre10 >= 15 or (result.get("prior_limit_count_10d") or 0) >= 1:
        result["price_position_level"] = "高位"
        result["price_position_reason"] = "封板前多日涨幅、近期涨停记忆或高点距离显示位置偏高"
    elif pre5 <= 3 and pre10 <= 10 and excess <= 0:
        result["price_position_level"] = "低位"
        result["price_position_reason"] = "封板前涨幅温和且近5日未跑赢行业基线"
    else:
        result["price_position_level"] = "中位"
        result["price_position_reason"] = "多日涨幅与行业超额处于低位和高位条件之间"
    return result


def _apply_sector_proxy(row, proxy):
    result = dict(row)
    result["pre_sector_return_5d"] = round(proxy, 2)
    result["pre_excess_5d"] = round(result["pre_return_5d"] - proxy, 2) if result.get("pre_return_5d") is not None else None
    result["sector_history_mode"] = "大市值成分股样本估算"
    return _price_position(result)


def position_features(row, stock_history, sector_row, sector_history):
    """Derive transparent price-position evidence from completed adjusted daily closes."""
    result = dict(row)
    closes = _history_values(stock_history)
    sector_closes = _history_values(sector_history)
    last = row.get("last")
    sector_last = sector_row.get("last")
    for sessions in (3, 5, 10):
        result[f"return_{sessions}d"] = _pct_change(last, closes[-sessions]) if len(closes) >= sessions else None
    result["pre_return_5d"] = _pct_change(closes[-1], closes[-6]) if len(closes) >= 6 else None
    result["pre_return_10d"] = _pct_change(closes[-1], closes[-11]) if len(closes) >= 11 else None
    sector_5d = _pct_change(sector_last, sector_closes[-5]) if len(sector_closes) >= 5 else None
    pre_sector_5d = _pct_change(sector_closes[-1], sector_closes[-6]) if len(sector_closes) >= 6 else None
    result["sector_return_5d"] = sector_5d
    result["pre_sector_return_5d"] = pre_sector_5d
    result["excess_5d"] = round(result["return_5d"] - sector_5d, 2) if (
        result["return_5d"] is not None and sector_5d is not None) else None
    result["pre_excess_5d"] = round(result["pre_return_5d"] - pre_sector_5d, 2) if (
        result["pre_return_5d"] is not None and pre_sector_5d is not None) else None
    high20 = max([last, *closes[-20:]]) if last and closes else None
    result["distance_20d_high_pct"] = _pct_change(last, high20) if high20 else None
    threshold = fallback_limit_pct(str(row.get("code")), str(row.get("name") or "")) * 100 - .5
    daily = [_pct_change(closes[index], closes[index - 1])
             for index in range(max(1, len(closes) - 10), len(closes))]
    limit_offsets = [len(daily) - index for index, value in enumerate(daily)
                     if value is not None and value >= threshold]
    result["prior_limit_count_10d"] = len(limit_offsets) if len(closes) >= 11 else None
    result["days_since_prior_limit"] = min(limit_offsets) if limit_offsets else None
    sources = stock_history.get("sources", []) if isinstance(stock_history, dict) else []
    result["history_sources"] = list(sources)
    result["history_verified"] = len(sources) >= 2 and len(closes) >= 11
    if pre_sector_5d is not None:
        result["sector_history_mode"] = "行业指数"
    return _price_position(result)


def classify_first_board(row):
    """Classify a first board; missing history stays unverified instead of being guessed."""
    result = dict(row)
    pre5, pre10 = row.get("pre_return_5d"), row.get("pre_return_10d")
    excess5, distance = row.get("pre_excess_5d"), row.get("distance_20d_high_pct")
    prior, since = row.get("prior_limit_count_10d"), row.get("days_since_prior_limit")
    score = 50
    if excess5 is not None:
        score += 15 if excess5 <= 0 else 5 if excess5 <= 3 else -15 if excess5 >= 10 else -5
    if pre5 is not None:
        score += 12 if pre5 <= 3 else 5 if pre5 <= 6 else -12 if pre5 >= 12 else 0
    if prior is not None:
        score += 10 if prior == 0 else -12
    if row.get("first_limit_time") and row["first_limit_time"] <= "10:30:00":
        score += 6
    float_cap, seal = row.get("float_market_cap"), row.get("seal_amount")
    if float_cap and seal is not None and seal / float_cap >= .01:
        score += 7
    score = max(0, min(100, score))
    result["catch_up_score"] = score
    result["catch_up_confidence"] = "高" if score >= 75 else "中" if score >= 55 else "低"

    enough = pre5 is not None and pre10 is not None and excess5 is not None and prior is not None
    if not enough:
        label, level = "历史不足待核验", "未知"
        reason = "缺少至少10个已完成交易日日线或行业指数基线，不推断补涨/反包。"
    elif since is not None and 2 <= since <= 6 and (pre5 >= 4 or (distance is not None and distance >= -3)):
        label, level = "高位反包", "高位"
        reason = f"近10日已有{prior}次估算涨停，距上次约{since}个交易日，当前首板属于断板后再度封板。"
    elif (pre5 >= 8 or pre10 >= 15) and distance is not None and distance >= -3:
        label, level = "趋势加速", "高位"
        reason = f"封板前5/10日涨幅约{pre5:+.1f}%/{pre10:+.1f}%，且距20日高点{distance:.1f}%。"
    elif pre5 <= 6 and pre10 <= 15 and excess5 <= 3 and prior == 0 and score >= 55:
        label, level = "低位补涨", "低位"
        reason = f"封板前5日涨幅约{pre5:+.1f}%，封板前5日相对板块{excess5:+.1f}pp，近10日无估算涨停。"
    else:
        label, level = "中位跟随", "中位"
        reason = f"历史位置不满足低位补涨、高位反包或趋势加速的完整条件；5日超额{excess5:+.1f}pp。"
    result.update({"position_label": label, "position_level": level, "position_reason": reason})
    return result


def _clock_seconds(value):
    try:
        hour, minute, second = (int(part) for part in str(value).split(":"))
        return hour * 3600 + minute * 60 + second
    except (TypeError, ValueError):
        return None


def _percentile(value, values):
    valid = sorted(float(item) for item in values if item is not None)
    if value is None or not valid:
        return 0.0
    return 100 * sum(item <= float(value) for item in valid) / len(valid)


def hot_attention(items):
    """Combine already-cached stock hotlists without triggering another network request."""
    grouped = {}
    for item in items or []:
        code = str(item.get("stock_code") or "")
        if not code:
            continue
        entry = grouped.setdefault(code, {"sources": set(), "best_rank": None})
        entry["sources"].add(str(item.get("source_name") or item.get("source_id") or "热榜"))
        try:
            rank = int(item.get("rank"))
            entry["best_rank"] = rank if entry["best_rank"] is None else min(entry["best_rank"], rank)
        except (TypeError, ValueError):
            pass
    result = {}
    for code, entry in grouped.items():
        rank_score = max(0, 10 - max(0, (entry["best_rank"] or 31) - 1) / 3)
        result[code] = {"sources": sorted(entry["sources"]), "best_rank": entry["best_rank"],
                        "score": round(min(15, len(entry["sources"]) * 3 + rank_score), 1)}
    return result


def leader_context(feeds, industry_rows, metrics, broken_rate):
    code_sector = {}
    for sector in sorted(industry_rows, key=lambda row: row["rank"])[:5]:
        for member in feeds.get("sector_members", {}).get(sector["code"], {}).get("rows", []):
            if member.get("code"):
                code_sector[member["code"]] = sector["code"]
    up_codes = {row.get("code") for row in feeds.get("up", {}).get("rows", []) if row.get("code")}
    broken_codes = {row.get("code") for row in feeds.get("broken", {}).get("rows", []) if row.get("code")}
    return {
        "up_codes": sorted(up_codes), "broken_codes": sorted(broken_codes),
        "code_sector": {code: sector for code, sector in code_sector.items() if code in up_codes or code in broken_codes},
        "score": metrics.get("score") if metrics else None,
        "up_count": feeds.get("up", {}).get("count"), "broken_rate": broken_rate,
        "sectors": {row["code"]: {"change_pct": row.get("change_pct"), "up_ratio": row.get("up_ratio"),
                                   "rank": row.get("rank")} for row in industry_rows},
    }


def leader_impacts(history, current):
    """Estimate influence in the snapshot *after* a seal/reseal/break event."""
    contexts = [(point.get("at"), point.get("leader_context")) for point in history[-120:]
                if point.get("leader_context")]
    contexts.append((current.get("at"), current))
    observations = {}
    for (before_at, before), (event_at, event_state), (after_at, after) in zip(
            contexts, contexts[1:], contexts[2:]):
        try:
            event_gap = (datetime.fromisoformat(event_at) - datetime.fromisoformat(before_at)).total_seconds()
            response_gap = (datetime.fromisoformat(after_at) - datetime.fromisoformat(event_at)).total_seconds()
        except (TypeError, ValueError):
            continue
        if not 20 <= event_gap <= 180 or not 20 <= response_gap <= 180:
            continue
        before_up, event_up = set(before.get("up_codes", [])), set(event_state.get("up_codes", []))
        event_broken = set(event_state.get("broken_codes", []))
        transitions = [(code, 1, "封板/回封") for code in event_up if code not in before_up]
        transitions += [(code, -1, "炸板") for code in event_broken if code in before_up and code not in event_up]
        for code, sign, event in transitions:
            sector_code = (event_state.get("code_sector", {}).get(code)
                           or before.get("code_sector", {}).get(code)
                           or after.get("code_sector", {}).get(code))
            event_sector = event_state.get("sectors", {}).get(sector_code, {})
            after_sector = after.get("sectors", {}).get(sector_code, {})
            def oriented(after_value, event_value, weight=1):
                return sign * (after_value - event_value) * weight if after_value is not None and event_value is not None else None
            item = {"event": event,
                    "market_score": oriented(after.get("score"), event_state.get("score")),
                    "limit_count": oriented(after.get("up_count"), event_state.get("up_count")),
                    "broken_rate": oriented(event_state.get("broken_rate"), after.get("broken_rate")),
                    "sector_change": oriented(after_sector.get("change_pct"), event_sector.get("change_pct")),
                    "sector_breadth": oriented(after_sector.get("up_ratio"), event_sector.get("up_ratio")),
                    "sector_rank": oriented(event_sector.get("rank"), after_sector.get("rank"))}
            observations.setdefault(code, []).append(item)
    result = {}
    for code, rows in observations.items():
        average = lambda key: mean(values) if (values := [row[key] for row in rows if row[key] is not None]) else None
        sector_change, breadth, rank = average("sector_change"), average("sector_breadth"), average("sector_rank")
        market_score, limit_count, broken = average("market_score"), average("limit_count"), average("broken_rate")
        sector_score = max(0, min(100, 50 + (sector_change or 0) * 15 + (breadth or 0) * .5 + (rank or 0) * 4))
        market_influence = max(0, min(100, 50 + (market_score or 0) * 4 + (limit_count or 0) * 2 + (broken or 0)))
        result[code] = {"observations": len(rows), "sector_score": round(sector_score, 1),
                        "market_score": round(market_influence, 1),
                        "events": Counter(row["event"] for row in rows)}
    return result


def score_sector_leaders(rows, direction_counts, impacts):
    if not rows:
        return []
    max_streak = max(row.get("streak", 0) for row in rows) or 1
    earliest_time = min((_clock_seconds(row.get("first_limit_time")) for row in rows
                         if _clock_seconds(row.get("first_limit_time")) is not None), default=None)
    amounts = [row.get("amount") for row in rows]
    top_direction = direction_counts.most_common(1)[0][0] if direction_counts else None
    scored = []
    for row in rows:
        result = dict(row)
        first = _clock_seconds(row.get("first_limit_time"))
        followers = sum(1 for other in rows if first is not None and (other_time := _clock_seconds(other.get("first_limit_time")))
                        is not None and 0 < other_time - first <= 600)
        event_position = 12 * row.get("streak", 0) / max_streak
        event_position += 4 if first is not None and first == earliest_time else 0
        event_position += 4 if first is not None and first <= 10 * 3600 else 0
        influence = min(12, followers * 3) + (5 if row.get("industry") == top_direction else 0)
        impact = impacts.get(row.get("code"), {})
        influence += min(8, impact.get("sector_score", 0) * .08) if impact.get("observations") else 0
        logic = 15 if row.get("industry") == top_direction else 10 if row.get("industry") else 5
        float_cap, seal = row.get("float_market_cap"), row.get("seal_amount")
        seal_ratio = 100 * seal / float_cap if float_cap and seal is not None else None
        seal_quality = min(15, (seal_ratio or 0) * 5) if seal_ratio is not None else 4
        opens = row.get("open_count", 0)
        divergence = 10 if 1 <= opens <= 3 else 5 if opens == 0 else 6 if opens <= 6 else 2
        liquidity = 5 * _percentile(row.get("amount"), amounts) / 100
        turnover = row.get("turnover_pct")
        liquidity += 5 if turnover is not None and 5 <= turnover <= 35 else 2 if turnover is not None else 0
        history_score = min(5, row.get("streak", 0) + (row.get("prior_limit_count_10d") or 0))
        components = {"板块带动代理": round(min(25, influence), 1), "事件身位": round(min(20, event_position), 1),
                      "逻辑关联": logic, "封板质量": round(seal_quality, 1), "分歧承接": divergence,
                      "流动性": round(liquidity, 1), "历史记忆": history_score}
        total = round(min(100, sum(components.values())), 1)
        confirmed = impact.get("observations", 0) >= 2 and total >= 70 and impact.get("sector_score", 0) >= 55
        role = "板块龙已确认" if confirmed else "板块龙候选" if total >= 60 else (
            "空间板但带动待验证" if row.get("streak", 0) == max_streak else "板块强势股")
        result.update({"sector_leader_score": total, "sector_leader_components": components,
                       "sector_leader_role": role, "followers_after_limit": followers,
                       "seal_float_ratio_pct": round(seal_ratio, 3) if seal_ratio is not None else None,
                       "influence_observations": impact.get("observations", 0),
                       "sector_influence_score": impact.get("sector_score")})
        scored.append(result)
    return sorted(scored, key=lambda row: (-row["sector_leader_score"], -row.get("streak", 0),
                                           row.get("first_limit_time") or "99:99:99"))


def score_market_leaders(rows, attention, impacts):
    if not rows:
        return []
    max_streak = max(row.get("streak", 0) for row in rows) or 1
    max_count = sum(row.get("streak", 0) == max_streak for row in rows)
    amounts = [row.get("amount") for row in rows]
    result = []
    for row in rows:
        candidate = dict(row)
        streak = row.get("streak", 0)
        space = 20 * streak / max_streak + (5 if streak == max_streak and max_count == 1 else 0)
        impact = impacts.get(row.get("code"), {})
        influence = 8 + (8 if streak == max_streak else 0)
        if impact.get("observations"):
            influence = min(25, 8 + impact.get("market_score", 0) * .17)
        hot = attention.get(row.get("code"), {})
        attention_score = hot.get("score", 0)
        opens = row.get("open_count", 0)
        divergence = 15 if 1 <= opens <= 3 else 7 if opens == 0 else 9 if opens <= 6 else 3
        liquidity = 10 * _percentile(row.get("amount"), amounts) / 100
        turnover = row.get("turnover_pct")
        liquidity += 5 if turnover is not None and 8 <= turnover <= 40 else 2 if turnover is not None else 0
        independence = 5 if streak >= 2 and row.get("industry") else 2
        components = {"市场空间": round(min(25, space), 1), "全市场带动代理": round(min(25, influence), 1),
                      "跨平台热度": round(min(15, attention_score), 1), "分歧生存": divergence,
                      "流动性": round(min(15, liquidity), 1), "独立辨识": independence}
        total = round(min(100, sum(components.values())), 1)
        confirmed = impact.get("observations", 0) >= 2 and total >= 70 and impact.get("market_score", 0) >= 55
        role = "市场投机龙已确认" if confirmed else "市场投机龙候选" if total >= 60 else (
            "空间板但市场带动待验证" if streak == max_streak else "高辨识度观察")
        limit_pct = fallback_limit_pct(str(row.get("code")), str(row.get("name") or "")) * 100
        seal_ratio = 100 * row["seal_amount"] / row["float_market_cap"] if row.get("float_market_cap") and row.get("seal_amount") is not None else None
        candidate.update({"market_leader_score": total, "market_leader_components": components,
                          "market_leader_role": role, "attention_sources": hot.get("sources", []),
                          "attention_best_rank": hot.get("best_rank"),
                          "limit_utilization_pct": round(100 * row.get("change_pct", 0) / limit_pct, 1) if limit_pct else None,
                          "seal_float_ratio_pct": round(seal_ratio, 3) if seal_ratio is not None else None,
                          "influence_observations": impact.get("observations", 0),
                          "market_influence_score": impact.get("market_score")})
        result.append(candidate)
    return sorted(result, key=lambda row: (-row["market_leader_score"], -row.get("streak", 0),
                                           row.get("first_limit_time") or "99:99:99"))


def sector_ladders(industry_rows, feeds, now, live, impacts=None):
    """Build evidence-backed ladders for the five strongest first-level industries."""
    impacts = impacts or {}
    up_pool = feeds.get("up", {})
    broken_pool = feeds.get("broken", {})
    up_index = {row["code"]: row for row in (up_pool.get("rows", []) if up_pool.get("ok") else [])
                if isinstance(row, dict) and row.get("code")}
    broken_index = {row["code"]: row for row in (broken_pool.get("rows", []) if broken_pool.get("ok") else [])
                    if isinstance(row, dict) and row.get("code")}
    member_feeds = feeds.get("sector_members", {})
    position_history = feeds.get("position_history", {})
    stock_histories = position_history.get("stocks", {}) if isinstance(position_history, dict) else {}
    sector_histories = position_history.get("sectors", {}) if isinstance(position_history, dict) else {}
    result = []
    for sector in sorted(industry_rows, key=lambda row: row["rank"])[:5]:
        feed = member_feeds.get(sector["code"], {}) if isinstance(member_feeds, dict) else {}
        members = []
        for row in feed.get("rows", []):
            try:
                stamp = datetime.fromisoformat(row.get("timestamp") or "")
            except (ValueError, TypeError):
                continue
            if stamp.date() != now.date() or stamp > now + timedelta(seconds=5):
                continue
            if live and (now - stamp).total_seconds() > 180:
                continue
            members.append(row)
        members = [position_features(row, stock_histories.get(row["code"], {}), sector,
                                     sector_histories.get(sector["code"], {})) for row in members]
        sector_index_ready = len(_history_values(sector_histories.get(sector["code"], {}))) >= 6
        proxy_samples = [(row.get("pre_return_5d"), row.get("total_market_cap")) for row in members
                         if row.get("pre_return_5d") is not None and row.get("total_market_cap")]
        proxy = (sum(value * cap for value, cap in proxy_samples) / sum(cap for _, cap in proxy_samples)) if (
            not sector_index_ready and len(proxy_samples) >= 3 and sum(cap for _, cap in proxy_samples) > 0) else None
        if proxy is not None:
            members = [_apply_sector_proxy(row, proxy) for row in members]
        codes = {row["code"] for row in members}
        member_index = {row["code"]: row for row in members}

        def enrich(pool_row):
            member = member_index.get(pool_row["code"], {})
            merged = {**member, **pool_row}
            merged["name"] = member.get("name") or pool_row.get("name")
            merged["industry"] = member.get("industry") or pool_row.get("industry")
            return merged

        limit_rows = [position_features(enrich(up_index[code]), stock_histories.get(code, {}),
                                       sector, sector_histories.get(sector["code"], {}))
                      for code in codes if code in up_index]
        broken_rows = [position_features(enrich(broken_index[code]), stock_histories.get(code, {}),
                                        sector, sector_histories.get(sector["code"], {}))
                       for code in codes if code in broken_index]
        if proxy is not None:
            limit_rows = [_apply_sector_proxy(row, proxy) for row in limit_rows]
            broken_rows = [_apply_sector_proxy(row, proxy) for row in broken_rows]
        limit_rows = [classify_first_board(row) if row.get("streak", 0) <= 1 else {
            **row, "position_label": f"{max(2, row.get('streak', 0))}连板身位",
            "position_level": "高位" if row.get("streak", 0) >= 3 else "中高位",
            "position_reason": "连板高度优先定义事件身位；历史涨幅仅作价格位置补充。",
        } for row in limit_rows]
        limit_rows.sort(key=lambda row: (-row.get("streak", 0), row.get("first_limit_time") or "99:99:99",
                                         -(row.get("seal_amount") or 0), row["code"]))
        earliest = min((row for row in limit_rows if row.get("first_limit_time")),
                       key=lambda row: row["first_limit_time"], default=None)
        max_seal = max((row for row in limit_rows if row.get("seal_amount") is not None),
                       key=lambda row: row["seal_amount"], default=None)
        capacity = max((row for row in members if row.get("amount") is not None),
                       key=lambda row: row["amount"], default=None)
        trend = max(members, key=lambda row: (row.get("change_pct") or -999, row.get("amount") or 0), default=None)
        emotion = limit_rows[0] if limit_rows else None
        directions = Counter(row.get("industry") for row in limit_rows if row.get("industry"))
        direction_rows = []
        for name, count in directions.most_common(3):
            candidates = [row for row in limit_rows if row.get("industry") == name]
            direction_rows.append({"name": name, "limit_up_count": count,
                                   "leader": _ladder_stock(candidates[0]) if candidates else None})
        if not direction_rows:
            mover_directions = Counter(row.get("industry") for row in sorted(
                members, key=lambda item: -(item.get("change_pct") or -999))[:10] if row.get("industry"))
            direction_rows = [{"name": name, "limit_up_count": 0, "leader": None}
                              for name, _ in mover_directions.most_common(3)]
        limit_rows = score_sector_leaders(limit_rows, directions, impacts)
        scored_index = {row["code"]: row for row in limit_rows}
        for item in direction_rows:
            candidates = [row for row in limit_rows if row.get("industry") == item["name"]]
            if candidates:
                item["leader"] = _ladder_stock(candidates[0])
        sector_leader = limit_rows[0] if limit_rows else None
        # Rebind the role rows so their displayed evidence includes the leader scores.
        emotion = scored_index.get(emotion.get("code")) if emotion else None
        earliest = scored_index.get(earliest.get("code")) if earliest else None
        max_seal = scored_index.get(max_seal.get("code")) if max_seal else None
        grouped = {}
        for row in limit_rows:
            grouped.setdefault(max(1, row.get("streak", 0)), []).append(_ladder_stock(row))
        ladder = [{"streak": streak, "label": f"{streak}连板" if streak > 1 else "首板",
                   "stocks": rows} for streak, rows in sorted(grouped.items(), reverse=True)]
        promoted = [_ladder_stock(row) for row in limit_rows if row.get("streak", 0) >= 2]
        first_board_rows = [row for row in limit_rows if row.get("streak", 0) <= 1]
        first_boards = [_ladder_stock(row) for row in first_board_rows]
        position_groups = {
            "low_catch_up_candidates": [_ladder_stock(row) for row in first_board_rows
                                        if row.get("position_label") == "低位补涨"],
            "high_rebound_candidates": [_ladder_stock(row) for row in first_board_rows
                                         if row.get("position_label") == "高位反包"],
            "trend_acceleration_candidates": [_ladder_stock(row) for row in first_board_rows
                                               if row.get("position_label") == "趋势加速"],
            "follow_candidates": [_ladder_stock(row) for row in first_board_rows
                                  if row.get("position_label") in ("中位跟随", "历史不足待核验")],
        }
        broken_focus = [_ladder_stock(row) for row in sorted(
            broken_rows, key=lambda item: (-(item.get("change_pct") or -999), item.get("first_limit_time") or "99:99:99"))[:6]]
        max_streak = max((row.get("streak", 0) for row in limit_rows), default=0)
        if len(limit_rows) >= 4 and max_streak >= 2 and first_boards:
            analysis = "空间板、首板与封板数量同时存在，涨停梯队相对完整。"
        elif len(limit_rows) >= 2:
            analysis = "存在多只封板股，但高度或后排补涨仍不足，梯队完整性一般。"
        elif len(limit_rows) == 1:
            analysis = "目前以单只封板股带动为主，尚未形成清晰板块梯队。"
        else:
            analysis = "板块涨幅居前，但当前完整涨停池未匹配到封板股，属于趋势走强或数据待补。"
        if first_board_rows:
            counts = Counter(row.get("position_label") for row in first_board_rows)
            analysis += " 首板位置分类：" + "、".join(
                f"{label}{count}只" for label, count in counts.items() if label) + "。"
        if sector_leader:
            analysis += f" 板块龙候选为{sector_leader['name']}（{sector_leader['sector_leader_score']}分），"
            analysis += ("已有事件响应确认。" if sector_leader.get("sector_leader_role") == "板块龙已确认"
                         else "仍需更多封板/炸板后的板块响应样本确认。")
        missing = []
        if not feed.get("complete"):
            missing.append(feed.get("error") or "成分股列表不完整")
        if not up_pool.get("ok"):
            missing.append("涨停池不可用")
        if not broken_pool.get("ok"):
            missing.append("炸板池不可用")
        history_covered = sum(bool(row.get("history_sources")) for row in limit_rows)
        if limit_rows and history_covered < len(limit_rows):
            missing.append(f"身位日线覆盖 {history_covered}/{len(limit_rows)}")
        if position_history.get("truncated_count", 0):
            missing.append(f"全局身位候选超出保护上限，{position_history['truncated_count']}只本轮未补抓日线")
        if proxy is not None:
            missing.append(f"行业指数日线不可用，使用{len(proxy_samples)}只大市值成分股样本加权估算")
        elif not sector_index_ready and limit_rows:
            missing.append("行业历史基线不可用")
        verified_count = sum(bool(row.get("history_verified")) for row in limit_rows)
        result.append({
            "rank": sector["rank"], "code": sector["code"], "name": sector["name"],
            "change_pct": sector.get("change_pct"), "excess_pct": sector.get("excess_pct"),
            "up_ratio": sector.get("up_ratio"), "amount_share": sector.get("amount_share"),
            "rotation_label": sector.get("label"), "constituent_count": len(members),
            "limit_up_count": len(limit_rows), "broken_count": len(broken_rows),
            "promotion_count": len(promoted),
            "max_streak": max_streak, "sector_feed_leader": sector.get("leader"),
            "main_directions": direction_rows, "emotion_leader": _ladder_stock(emotion) if emotion else None,
            "sector_leader": _ladder_stock(sector_leader) if sector_leader else None,
            "sector_leader_candidates": [_ladder_stock(row) for row in limit_rows[:3]],
            "trend_core": _ladder_stock(trend) if trend else None,
            "capacity_core": _ladder_stock(capacity) if capacity else None,
            "earliest_limit": _ladder_stock(earliest) if earliest else None,
            "max_seal": _ladder_stock(max_seal) if max_seal else None,
            "ladder": ladder, "promoted_stocks": promoted, "first_board_candidates": first_boards,
            **position_groups, "position_history_covered": history_covered,
            "position_history_verified": verified_count,
            "position_sector_baseline": "行业指数" if sector_index_ready else (
                f"{len(proxy_samples)}只大市值成分股样本" if proxy is not None else "缺失"),
            "broken_focus": broken_focus,
            "analysis": analysis, "data_complete": not missing, "missing_data": missing,
        })
    return result


class MarketMonitorService:
    def __init__(self, config_path, client=None, clock=None, hotlist_provider=None):
        self.config_path = Path(config_path)
        self.directory = self.config_path.parent / "data"
        self.settings_path = self.directory / "market-monitor-settings.json"
        self.database = self.directory / "market-monitor.db"
        self.client = client or MarketDataClient(self.directory)
        self.hotlist_provider = hotlist_provider or (lambda: [])
        self.clock = clock or (lambda: datetime.now(TZ))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._worker = None
        self._running = self._busy = self._closed = self._requested = False
        self._last_attempt = 0.0
        self._last_error = None
        self._failures = 0
        self._settings = dict(DEFAULTS)
        self._snapshot = None
        self._history = []
        self._events = []
        self._candidates = {}
        self._stable_cycle = None
        self._last_signature = None
        self._last_quote_time = None
        self._load()

    def _load(self):
        try:
            self._settings = validate_settings(json.loads(self.settings_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        if not self.database.exists():
            return
        try:
            with closing(sqlite3.connect(self.database)) as db:
                row = db.execute("SELECT payload FROM latest WHERE id=1").fetchone()
                self._snapshot = json.loads(row[0]) if row else None
                today = self.clock().date().isoformat()
                self._history = [json.loads(r[0]) for r in db.execute("SELECT payload FROM points WHERE at>=? ORDER BY at", (today,))]
                self._events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events ORDER BY at DESC LIMIT 100")]
            if self._snapshot:
                self._snapshot["restored"] = True
                self._snapshot["signal_eligible"] = False
        except (sqlite3.Error, ValueError, OSError):
            self._last_error = "历史记录读取失败；新的采集仍可运行"

    def settings(self):
        with self._lock:
            return revised(self._settings)

    def save_settings(self, payload):
        settings = validate_settings(payload)
        with self._lock:
            if payload.get("_revision") != self.settings()["_revision"]:
                raise MarketMonitorError("设置已在其他设备修改，请刷新后重新设置", 409)
            self.directory.mkdir(parents=True, exist_ok=True)
            pending = self.settings_path.with_suffix(".pending")
            try:
                pending.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
                pending.replace(self.settings_path)
            except OSError as exc:
                raise MarketMonitorError("无法保存市场监控设置", 500) from exc
            self._settings = settings
            self._candidates.clear()
            self._stable_cycle = None
            self._wake.set()
            return self.settings()

    def status(self):
        now = self.clock()
        try:
            current_phase = market_phase(now, load_config(self.config_path))
        except (OSError, ValueError, TypeError):
            current_phase = "closed"
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
            if snapshot:
                age = (now - datetime.fromisoformat(snapshot["captured_at"])).total_seconds()
                snapshot["phase"] = current_phase
                snapshot["live"] = current_phase in LIVE_PHASES
                snapshot["stale"] = bool(snapshot.get("restored") or age > max(180, self._settings["interval_seconds"] * 3))
                if snapshot["stale"] or not self._running or not snapshot["live"]:
                    snapshot["signal_eligible"] = False
                    snapshot["rotation_eligible"] = False
            return {"enabled": self._running, "busy": self._busy, "error": self._last_error,
                    "server_time": now.isoformat(), "settings": self.settings(), "snapshot": snapshot,
                    "history": [{k: p[k] for k in ("at", "score", "cycle")} for p in self._history[-600:]],
                    "events": copy.deepcopy(self._events[:100]),
                    "next_interval_seconds": min(300, self._settings["interval_seconds"] * 2 ** min(self._failures, 3))}

    def _launch(self):
        if self._closed:
            raise MarketMonitorError("服务正在关闭", 409)
        if not self._worker or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="market-monitor", daemon=True)
            self._worker.start()
        self._wake.set()

    def start(self):
        with self._lock:
            if self._running:
                return self.status()
            self._running = True
            self._requested = True
            self._launch()
        return self.status()

    def stop(self):
        with self._lock:
            self._running = self._requested = False
            self._candidates.clear()
            self._wake.set()
        return self.status()

    def refresh(self):
        with self._lock:
            if not self._busy and time.monotonic() - self._last_attempt >= 20:
                self._requested = True
                self._launch()
        return self.status()

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._running = self._requested = False
            self._wake.set()
        if self._worker:
            self._worker.join(timeout=2)

    def _loop(self):
        while True:
            with self._lock:
                if self._closed or (not self._running and not self._requested):
                    self._worker = None
                    return
                requested = self._requested
                self._requested = False
                self._wake.clear()
            try:
                now = self.clock()
                phase = market_phase(now, load_config(self.config_path))
                if requested or phase in LIVE_PHASES:
                    with self._lock:
                        self._busy = True
                        self._last_attempt = time.monotonic()
                    feeds = self.client.fetch(now)
                    with self._lock:
                        if not self._closed:
                            finished = self.clock()
                            self.ingest(feeds, finished, market_phase(finished, load_config(self.config_path)))
                with self._lock:
                    wait = min(300, self._settings["interval_seconds"] * 2 ** min(self._failures, 3)) if phase in LIVE_PHASES else 30
            except Exception:
                LOG.exception("Market monitor collection failed")
                with self._lock:
                    self._last_error = "市场监控本轮失败，保留旧快照；稍后自动重试"
                    self._failures += 1
                wait = 60
            finally:
                with self._lock:
                    self._busy = False
            with self._lock:
                if not self._running and not self._requested:
                    self._worker = None
                    return
            self._wake.wait(wait)

    def _confirm(self, key, value, message, now, events):
        old_value, count = self._candidates.get(key, (None, 0))
        count = count + 1 if old_value == value else 1
        self._candidates[key] = (value, count)
        if count < self._settings["confirmations"]:
            return False
        cutoff = (now - timedelta(minutes=10)).isoformat()
        if not any(e["key"] == key and e["at"] > cutoff for e in self._events):
            events.append({"id": f"{now.isoformat()}:{key}", "key": key, "at": now.isoformat(), "message": message})
        return True

    def ingest(self, feeds, now, phase):
        """Single-writer entry, also used by deterministic offline tests."""
        live = phase in LIVE_PHASES
        stocks, market_quality = assess(feeds["stocks"], now, live)
        sectors, sector_quality = assess(feeds["sectors"], now, live, sectors=True)
        metrics = sentiment(stocks) if market_quality["sufficient"] and stocks and (not live or market_quality["usable"]) else None
        # Both use the same trading date; an old sector table cannot be paired with new breadth.
        matched = market_quality["date"] == sector_quality["date"]
        rotation_ok = sector_quality["usable"] and market_quality["usable"] and matched
        signature = "|".join([market_quality["source"], sector_quality["source"],
                              ",".join(sorted(r["code"] for r in sectors))])
        self._history = [p for p in self._history if p["at"][:10] == now.date().isoformat()]
        previous = self._history[-1] if self._history else None
        continuity = bool(previous and signature == previous["signature"]
                          and abs(len(stocks) - previous["population"]) <= max(1, previous["population"] * .02)
                          and (now - datetime.fromisoformat(previous["at"])).total_seconds() <= 360)
        if not continuity or self._last_signature != signature:
            self._candidates.clear()
            self._stable_cycle = None
        self._last_signature = signature
        baselines = {minutes: find_baseline(self._history, now, minutes, signature) if continuity else None for minutes in (5, 15, 30)}
        base = baselines[self._settings["lookback_minutes"]]
        delta = round(metrics["score"] - base["score"], 1) if metrics and base else None
        cycle = cycle_label(metrics["score"], delta) if metrics else "数据不足"
        if not market_quality["usable"] and live:
            cycle = "数据不足"
        if metrics:
            metrics.update({"delta": delta, "cycle": cycle})
        industry_rows = []
        all_amount = sum(r["amount"] or 0 for r in sectors)
        for rank, row in enumerate(sorted(sectors, key=lambda r: (-r["change_pct"], r["code"])), 1):
            up, down = row.get("up"), row.get("down")
            breadth = 100 * up / (up + down) if up is not None and down is not None and up + down > 0 else None
            excess = row["change_pct"] - metrics["equal_weight_pct"] if metrics and matched else None
            changes = {}
            for minutes, b in baselines.items():
                old = b["sectors"].get(row["code"]) if b else None
                changes[str(minutes)] = old["rank"] - rank if old else None
            old = base["sectors"].get(row["code"]) if base else None
            momentum = round(excess - old["excess_pct"], 3) if old and excess is not None and old["excess_pct"] is not None else None
            label = "积累基线中" if momentum is None else ("领先" if excess >= 0 and momentum >= 0 else
                    "改善" if excess < 0 and momentum >= 0 else "转弱" if excess >= 0 else "落后")
            if not rotation_ok:
                label = "数据不足"
            share = 100 * row["amount"] / all_amount if all(r["amount"] is not None for r in sectors) and all_amount > 0 and len(sectors) == 31 else None
            industry_rows.append({**row, "rank": rank, "up_ratio": round(breadth, 1) if breadth is not None else None,
                                  "excess_pct": round(excess, 3) if excess is not None else None,
                                  "rank_changes": changes, "momentum": momentum, "label": label,
                                  "amount_share": round(share, 2) if share is not None else None})
        new_quote = market_quality["quote_time"] != self._last_quote_time
        eligible = bool(live and market_quality["usable"] and metrics and continuity and base
                        and market_quality["universe_age_days"] == 0
                        and new_quote)
        events = []
        active_keys = set()
        if eligible and self._running and self._settings["alerts_enabled"]:
            if delta is not None:
                active_keys.add("cycle")
                if self._stable_cycle is None:
                    self._stable_cycle = cycle
                elif cycle != self._stable_cycle:
                    if self._confirm("cycle", cycle, f"情绪状态：{self._stable_cycle} → {cycle}；评分 {metrics['score']}，窗口变化 {delta:+.1f}", now, events):
                        self._stable_cycle = cycle
                else:
                    self._candidates.pop("cycle", None)
            if rotation_ok:
                for row in industry_rows:
                    jump = row["rank_changes"][str(self._settings["lookback_minutes"])]
                    momentum, breadth = row["momentum"], row["up_ratio"]
                    if jump is None or momentum is None or breadth is None:
                        continue
                    direction = "走强" if jump >= self._settings["rank_jump"] and momentum >= .5 and breadth >= 55 else (
                        "走弱" if jump <= -self._settings["rank_jump"] and momentum <= -.5 and breadth <= 45 else None)
                    if direction:
                        key = row["code"] + direction
                        active_keys.add(key)
                        self._confirm(key, direction, f"{row['name']}{direction}：排名变化 {jump:+d}，相对强度变化 {momentum:+.2f} 个百分点", now, events)
        self._candidates = {k: v for k, v in self._candidates.items() if k in active_keys}
        self._last_quote_time = market_quality["quote_time"]
        pools = {kind: feeds[kind] for kind in ("up", "down", "broken")}
        for pool in pools.values():
            if pool.get("date") != (market_quality["date"] or "").replace("-", ""):
                pool.update({"ok": False, "count": None})
        up, broken = pools["up"].get("count"), pools["broken"].get("count")
        broken_rate = round(100 * broken / (up + broken), 1) if up is not None and broken is not None and up + broken > 0 else None
        context = leader_context(feeds, industry_rows, metrics, broken_rate)
        context["at"] = now.isoformat()
        impacts = leader_impacts(self._history, context)
        try:
            hot_items = self.hotlist_provider() or []
        except Exception:  # noqa: BLE001 - optional cached radar must not stop market monitoring
            hot_items = []
        attention = hot_attention(hot_items)
        ladders = sector_ladders(industry_rows, feeds, now, live, impacts)
        market_leaders = score_market_leaders(
            feeds.get("up", {}).get("rows", []) if feeds.get("up", {}).get("ok") else [], attention, impacts)
        sector_leaders = {ladder["sector_leader"]["code"]: ladder for ladder in ladders if ladder.get("sector_leader")}
        for candidate in market_leaders:
            ladder = sector_leaders.get(candidate.get("code"))
            if not ladder or candidate.get("market_leader_score", 0) < 60 or ladder["sector_leader"].get("sector_leader_score", 0) < 60:
                continue
            confirmed = (candidate.get("market_leader_role") == "市场投机龙已确认"
                         and ladder["sector_leader"].get("sector_leader_role") == "板块龙已确认")
            candidate["dual_leader"] = True
            candidate["market_leader_role"] = "双重龙头已确认" if confirmed else "双重龙头候选"
            ladder["sector_leader"].update({"dual_leader": True,
                                             "market_leader_score": candidate["market_leader_score"],
                                             "market_leader_role": candidate["market_leader_role"]})
        self._snapshot = {"captured_at": now.isoformat(), "phase": phase, "live": live,
                          "market_quality": market_quality, "sector_quality": sector_quality,
                          "sentiment": metrics, "sectors": industry_rows, "sector_ladders": ladders,
                          "market_speculation_leaders": [_ladder_stock(row) for row in market_leaders[:5]],
                          "leader_analysis": {"hotlist_stock_count": len(attention),
                                              "influence_stock_count": len(impacts),
                                              "influence_requires_observations": 2,
                                              "method": "事件后下一张30–180秒快照响应；无逐笔时仅作候选"},
                          "pools": pools,
                          "broken_rate": broken_rate, "signal_eligible": eligible,
                          "rotation_eligible": bool(eligible and rotation_ok and len(base["sectors"]) == 31), "restored": False}
        self._last_error = None if market_quality["usable"] and sector_quality["usable"] else "部分数据不足或非当日快照；相关信号暂停，请查看数据质量"
        self._failures = 0 if market_quality["usable"] else self._failures + 1
        self._events = (list(reversed(events)) + self._events)[:100]
        point = None
        if live and market_quality["usable"] and metrics and new_quote:
            point = {"at": now.isoformat(), "score": metrics["score"], "cycle": cycle,
                     "population": len(stocks), "signature": signature,
                     "sectors": {r["code"]: {"rank": r["rank"], "excess_pct": r["excess_pct"]} for r in industry_rows} if rotation_ok else {},
                     "leader_context": context}
            self._history.append(point)
            self._history = self._history[-1000:]
        try:
            self._persist(point, events, now)
        except (sqlite3.Error, OSError):
            self._last_error = "行情已更新，但本地历史保存失败，请检查磁盘空间/权限"

    def _persist(self, point, events, now):
        self.directory.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS latest(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS points(at TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("INSERT OR REPLACE INTO latest VALUES(1,?)", (json.dumps(self._snapshot, ensure_ascii=False),))
            if point:
                db.execute("INSERT OR REPLACE INTO points VALUES(?,?)", (point["at"], json.dumps(point, ensure_ascii=False)))
            for event in events:
                db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?)", (event["id"], event["at"], json.dumps(event, ensure_ascii=False)))
            cutoff = (now - timedelta(days=30)).date().isoformat()
            db.execute("DELETE FROM points WHERE at<?", (cutoff,))
            db.execute("DELETE FROM events WHERE at<?", (cutoff,))

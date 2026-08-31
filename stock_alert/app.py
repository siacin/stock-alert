from __future__ import annotations

import copy
import logging
import threading
import time
from datetime import datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

from .aggregate import aggregate_results
from .config import AlertConfig
from .detector import AlertDetector
from .ma5 import AutomaticMA5Service
from .models import AlertEvent, WatchStock
from .notifier import Notifier
from .providers import MultiSourceClient
from .storage import StateStore


TZ = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)

ACTIVE_MARKET_PHASES = {"open_auction", "continuous", "closing_auction", "custom"}


def _clock(value: str) -> clock_time:
    return datetime.strptime(value, "%H:%M").time()


def market_phase(now: datetime, config: AlertConfig) -> str:
    """Return the A-share trading phase while respecting configured sessions.

    The 09:25-09:30 auction-to-continuous gap is deliberately not considered
    live trading.  This prevents stale auction quotes from triggering line or
    rapid-move alerts.
    """
    if now.weekday() >= 5 or now.date().isoformat() in config.holidays:
        return "closed"
    current = now.time().replace(tzinfo=None)
    within_config = any(_clock(start) <= current < _clock(end) for start, end in config.sessions)
    canonical = "closed"
    if clock_time(9, 15) <= current < clock_time(9, 25):
        canonical = "open_auction"
    elif clock_time(9, 25) <= current < clock_time(9, 30):
        canonical = "auction_gap"
    elif clock_time(9, 30) <= current < clock_time(11, 30):
        canonical = "continuous"
    elif clock_time(11, 30) <= current < clock_time(13, 0):
        canonical = "lunch_break"
    elif clock_time(13, 0) <= current < clock_time(14, 57):
        canonical = "continuous"
    elif clock_time(14, 57) <= current < clock_time(15, 0):
        canonical = "closing_auction"

    if canonical in {"auction_gap", "lunch_break"}:
        return canonical
    if canonical in ACTIVE_MARKET_PHASES:
        return canonical if within_config else "closed"

    # Preserve explicitly configured non-standard sessions for testing or
    # special instruments, without broadening the default A-share schedule.
    return "custom" if within_config else "closed"


def is_market_open(now: datetime, config: AlertConfig) -> bool:
    return market_phase(now, config) in ACTIVE_MARKET_PHASES


def seconds_until_next_session(now: datetime, config: AlertConfig) -> float:
    candidates: list[datetime] = []
    phase_starts = ("09:15", "09:30", "13:00", "14:57")
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() >= 5 or day.isoformat() in config.holidays:
            continue
        configured_starts = {start for start, _end in config.sessions}
        for start in sorted(set(phase_starts) | configured_starts):
            hour, minute = (int(part) for part in start.split(":"))
            candidate = datetime.combine(day, clock_time(hour, minute), tzinfo=TZ)
            if candidate > now and is_market_open(candidate, config):
                candidates.append(candidate)
    if not candidates:
        return 60.0
    return max(1.0, (min(candidates) - now).total_seconds())


class AlertApplication:
    def __init__(self, config: AlertConfig, notifications_disabled: bool = False) -> None:
        self.config = config
        self.watches: dict[str, WatchStock] = {stock.code: stock for stock in config.stocks}
        self.client = MultiSourceClient(config.providers, config.request_timeout_seconds, config.max_batch_size)
        self.store = StateStore(config.database_path)
        self.detector = AlertDetector(config, self.store)
        self.ma5_service = AutomaticMA5Service(config.request_timeout_seconds)
        self.notifier = Notifier(config.beep, config.webhooks, disabled=notifications_disabled)
        self._last_off_hours_message = 0.0
        self._snapshot_lock = threading.Lock()
        self._snapshot: dict = {
            "updated_at": None,
            "ok": None,
            "message": "尚未获取行情",
            "sources": [],
            "quotes": [],
            "events": [],
        }

    def _publish_snapshot(
        self,
        now: datetime,
        results: list,
        consensus: dict,
        events: list[AlertEvent],
        *,
        ok: bool,
        message: str,
        effective_watches: dict[str, WatchStock] | None = None,
        ma5_details: dict[str, dict] | None = None,
    ) -> None:
        source_rows = [
            {
                "name": result.source,
                "ok": bool(result.quotes),
                "quote_count": len(result.quotes),
                "latency_ms": result.latency_ms,
                "error": result.error,
            }
            for result in results
        ]
        quote_rows = []
        active_watches = effective_watches or self.watches
        ma5_details = ma5_details or {}
        for code, quote in sorted(consensus.items()):
            watch = active_watches[code]
            ma5_detail = ma5_details.get(code, {})
            change_pct = ((quote.last - quote.prev_close) / quote.prev_close * 100) if quote.prev_close else 0.0
            at_limit = bool(quote.limit_up and quote.last >= quote.limit_up - 0.005)
            hit_limit = bool(quote.limit_up and quote.high and quote.high >= quote.limit_up - 0.005)
            board_state = "sealed" if at_limit else ("opened" if hit_limit else "normal")
            quote_rows.append(
                {
                    "code": code,
                    "name": quote.name or watch.name,
                    "timestamp": quote.timestamp.isoformat(),
                    "last": quote.last,
                    "prev_close": quote.prev_close,
                    "change_pct": change_pct,
                    "open": quote.open,
                    "high": quote.high,
                    "low": quote.low,
                    "average_price": quote.average_price,
                    "limit_up": quote.limit_up,
                    "volume_shares": quote.volume_shares,
                    "amount": quote.amount,
                    "cost": watch.cost,
                    "ma5": watch.ma5,
                    "ma5_mode": ma5_detail.get("mode", "manual"),
                    "ma5_sources": ma5_detail.get("sources", []),
                    "ma5_completed_closes": ma5_detail.get("completed_closes", []),
                    "ma5_error": ma5_detail.get("error"),
                    "widget_enabled": watch.widget_enabled,
                    "monitor_items": list(watch.monitor_items),
                    "sources": list(quote.sources),
                    "price_spread_bps": quote.price_spread_bps,
                    "board_state": board_state,
                    "below_average": bool(quote.average_price and quote.last < quote.average_price),
                    "below_cost": bool(watch.cost and quote.last < watch.cost),
                    "below_ma5": bool(watch.ma5 and quote.last < watch.ma5),
                }
            )
        event_rows = [
            {
                "event_type": event.event_type,
                "code": event.code,
                "name": event.name,
                "occurred_at": event.occurred_at.isoformat(),
                "price": event.price,
                "line_price": event.line_price,
                "sources": list(event.sources),
                "severity": event.severity,
                "message": event.message,
            }
            for event in events
        ]
        with self._snapshot_lock:
            self._snapshot = {
                "updated_at": now.isoformat(),
                "ok": ok,
                "message": message,
                "sources": source_rows,
                "quotes": quote_rows,
                "events": event_rows,
            }

    def get_snapshot(self) -> dict:
        with self._snapshot_lock:
            return copy.deepcopy(self._snapshot)

    def run_once(self, *, enforce_freshness: bool, print_quotes: bool = False) -> bool:
        now = datetime.now(TZ)
        results = self.client.fetch_all(self.config.stocks)
        good_results = [result for result in results if result.quotes]
        status = " ".join(
            f"{result.source}={'OK:'+str(len(result.quotes)) if result.quotes else 'FAIL'}({result.latency_ms}ms)"
            for result in results
        )
        LOGGER.info("多源行情完成 %s", status)
        if not good_results:
            LOGGER.error("全部数据源失败，本轮不更新状态、不产生提醒")
            self._publish_snapshot(now, results, {}, [], ok=False, message="全部数据源获取失败")
            return False
        if len(good_results) == 1:
            LOGGER.warning("仅一个数据源可用，本轮属于单源降级模式 source=%s", good_results[0].source)

        consensus, _grouped = aggregate_results(
            results,
            self.watches,
            now,
            self.config.max_quote_age_seconds,
            enforce_freshness,
        )
        missing = sorted(set(self.watches) - set(consensus))
        if missing:
            LOGGER.warning("部分自选股无新鲜行情 codes=%s", ",".join(missing))
        if not consensus:
            LOGGER.error("数据源有响应，但没有可用的新鲜行情，本轮不更新状态")
            self._publish_snapshot(now, results, {}, [], ok=False, message="没有可用的新鲜行情")
            return False

        effective_watches, ma5_details = self.ma5_service.resolve(self.watches, consensus)

        if print_quotes:
            for quote in consensus.values():
                avg = f"{quote.average_price:.2f}" if quote.average_price else "-"
                limit_up = f"{quote.limit_up:.2f}" if quote.limit_up else "-"
                ma5 = effective_watches[quote.code].ma5
                ma5_text = f"{ma5:.2f}" if ma5 else "-"
                print(
                    f"{quote.name or quote.code}({quote.code}) 现价={quote.last:.2f} "
                    f"均价={avg} MA5={ma5_text} 涨停={limit_up} 来源={'+'.join(quote.sources)} "
                    f"价差={quote.price_spread_bps:.1f}bps",
                    flush=True,
                )

        for quote in consensus.values():
            if quote.price_spread_bps > self.config.max_price_spread_bps:
                LOGGER.warning(
                    "多源价格分歧 code=%s spread=%.1fbps sources=%s",
                    quote.code,
                    quote.price_spread_bps,
                    ",".join(quote.sources),
                )

        events = self.detector.process(consensus, effective_watches, now)
        emitted_events: list[AlertEvent] = []
        for event in events:
            cooldown = int(self.config.cooldown_seconds.get(event.event_type, 60))
            if not self.store.can_emit(event, cooldown):
                LOGGER.debug("提醒处于冷却期 key=%s", event.dedupe_key)
                continue
            self.store.record_event(event)
            self.notifier.send(event)
            emitted_events.append(event)
        self._publish_snapshot(
            now,
            results,
            consensus,
            emitted_events,
            ok=True,
            message=f"已更新 {len(consensus)} 只股票",
            effective_watches=effective_watches,
            ma5_details=ma5_details,
        )
        LOGGER.info("本轮完成 stocks=%d events=%d", len(consensus), len(emitted_events))
        return True

    def run_forever(self, *, ignore_market_hours: bool = False, print_quotes: bool = False) -> None:
        LOGGER.info(
            "提醒器启动 stocks=%d providers=%s interval=%.1fs",
            len(self.watches),
            ",".join(self.config.providers),
            self.config.poll_interval_seconds,
        )
        while True:
            now = datetime.now(TZ)
            market_open = is_market_open(now, self.config)
            if not market_open and not ignore_market_hours:
                wait = min(seconds_until_next_session(now, self.config), 300.0)
                monotonic_now = time.monotonic()
                if monotonic_now - self._last_off_hours_message > 300:
                    LOGGER.info("当前不在监控时段，%.0f 秒后再次检查", wait)
                    self._last_off_hours_message = monotonic_now
                time.sleep(wait)
                continue

            started = time.monotonic()
            self.run_once(enforce_freshness=market_open and not ignore_market_hours, print_quotes=print_quotes)
            elapsed = time.monotonic() - started
            time.sleep(max(0.1, self.config.poll_interval_seconds - elapsed))

    def close(self) -> None:
        self.notifier.close()
        self.store.close()

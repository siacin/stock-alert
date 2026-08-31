from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from statistics import median
from typing import Callable

from .config import AlertConfig
from .models import AlertEvent, ConsensusQuote, PRICE_TICK, Quote, WatchStock
from .storage import StateStore


EVENT_LABELS = {
    "open_board_warning": "开板预警",
    "bomb": "炸板",
    "reseal": "回封",
    "break_average": "跌破分时均价",
    "recover_average": "收复分时均价",
    "break_cost": "跌破成本线",
    "recover_cost": "收复成本线",
    "break_ma5": "跌破 MA5",
    "recover_ma5": "收复 MA5",
    "rapid_rise": "快速拉升",
    "rapid_fall": "快速下跌",
}
EVENT_MONITOR_ITEMS = {
    "open_board_warning": "open_board",
    "bomb": "bomb",
    "reseal": "reseal",
    "break_average": "average",
    "recover_average": "average",
    "break_cost": "cost",
    "recover_cost": "cost",
    "break_ma5": "ma5",
    "recover_ma5": "ma5",
    "rapid_rise": "rapid_rise",
    "rapid_fall": "rapid_fall",
}


class AlertDetector:
    def __init__(self, config: AlertConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.trade_date = ""
        self.states: dict[tuple[str, str], dict] = {}
        self._rapid_samples: dict[str, deque[tuple[datetime, float]]] = defaultdict(deque)
        self._rapid_active: dict[str, set[str]] = defaultdict(set)

    def _ensure_date(self, now: datetime) -> None:
        trade_date = now.date().isoformat()
        if trade_date != self.trade_date:
            self.trade_date = trade_date
            self.states = self.store.load_states(trade_date)
            self._rapid_samples.clear()
            self._rapid_active.clear()

    def process(
        self,
        consensus: dict[str, ConsensusQuote],
        watches: dict[str, WatchStock],
        now: datetime,
    ) -> list[AlertEvent]:
        self._ensure_date(now)
        events: list[AlertEvent] = []
        for code, aggregate in consensus.items():
            watch = watches[code]
            if any(watch.monitors(item) for item in ("open_board", "bomb", "reseal")):
                board_events = self._process_board_states(aggregate, watch, now)
                events.extend(
                    event
                    for event in board_events
                    if watch.monitors(EVENT_MONITOR_ITEMS[event.event_type])
                )
            events.extend(self._process_lines(aggregate, watch, now))
            if watch.monitors("rapid_rise") or watch.monitors("rapid_fall"):
                events.extend(self._process_rapid_move(aggregate, watch, now))
        self.store.save_states(self.trade_date, self.states, now)
        return self._combine(events)

    def _process_board_states(self, aggregate: ConsensusQuote, watch: WatchStock, now: datetime) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for quote in aggregate.source_quotes:
            limit_up = quote.resolved_limit_up(watch)
            if not limit_up:
                continue
            scope = f"source:{quote.source}"
            key = (watch.code, scope)
            stored_previous = self.states.get(key)
            previous = stored_previous
            if previous and previous.get("updated_at"):
                try:
                    previous_age = (now - datetime.fromisoformat(previous["updated_at"])).total_seconds()
                    continuity_limit = max(self.config.max_quote_age_seconds * 2, self.config.poll_interval_seconds * 5)
                    if previous_age > continuity_limit:
                        previous = None
                except ValueError:
                    previous = None
            sealed = quote.is_sealed(watch)
            at_limit = quote.is_at_limit(watch)
            high_hit = bool(quote.high and quote.high >= limit_up - PRICE_TICK / 2)
            hit_limit = bool((stored_previous or {}).get("hit_limit")) or at_limit or high_hit

            if previous is None:
                if (
                    stored_previous is None
                    and self.config.notify_initial_bomb
                    and hit_limit
                    and not sealed
                    and quote.last < limit_up - PRICE_TICK / 2
                ):
                    events.append(self._event("bomb", aggregate, now, limit_up, (quote.source,), "critical", initial=True))
            else:
                was_sealed = bool(previous.get("sealed"))
                if was_sealed and not sealed:
                    event_type = "bomb" if quote.last < limit_up - PRICE_TICK / 2 else "open_board_warning"
                    severity = "critical" if event_type == "bomb" else "warning"
                    events.append(self._event(event_type, aggregate, now, limit_up, (quote.source,), severity))
                elif not was_sealed and sealed and bool(previous.get("hit_limit")):
                    events.append(self._event("reseal", aggregate, now, limit_up, (quote.source,), "info"))

            self.states[key] = {
                "sealed": sealed,
                "at_limit": at_limit,
                "hit_limit": hit_limit,
                "last": quote.last,
                "limit_up": limit_up,
                "quote_time": quote.timestamp.isoformat(),
                "updated_at": now.isoformat(),
            }
        return events

    def _process_rapid_move(
        self,
        aggregate: ConsensusQuote,
        watch: WatchStock,
        now: datetime,
    ) -> list[AlertEvent]:
        samples = self._rapid_samples[aggregate.code]
        samples.append((now, aggregate.last))
        cutoff = now - timedelta(seconds=self.config.rapid_move_window_seconds)
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        if len(samples) < 2:
            return []

        low_time, low_price = min(samples, key=lambda item: item[1])
        high_time, high_price = max(samples, key=lambda item: item[1])
        rise_pct = (aggregate.last / low_price - 1) * 100 if low_price > 0 else 0.0
        fall_pct = (1 - aggregate.last / high_price) * 100 if high_price > 0 else 0.0
        threshold = self.config.rapid_move_threshold_pct
        rearm = threshold * 0.5
        active = self._rapid_active[aggregate.code]
        if rise_pct < rearm:
            active.discard("rapid_rise")
        if fall_pct < rearm:
            active.discard("rapid_fall")

        event_type: str | None = None
        move_pct = 0.0
        reference_price = aggregate.last
        elapsed_seconds = 0.0
        if (
            watch.monitors("rapid_rise")
            and rise_pct >= threshold
            and rise_pct >= fall_pct
            and "rapid_rise" not in active
        ):
            event_type = "rapid_rise"
            move_pct = rise_pct
            reference_price = low_price
            elapsed_seconds = max(0.0, (now - low_time).total_seconds())
        elif watch.monitors("rapid_fall") and fall_pct >= threshold and "rapid_fall" not in active:
            event_type = "rapid_fall"
            move_pct = -fall_pct
            reference_price = high_price
            elapsed_seconds = max(0.0, (now - high_time).total_seconds())
        if not event_type:
            return []

        active.add(event_type)
        label = EVENT_LABELS[event_type]
        sources = tuple(sorted(set(aggregate.sources)))
        message = (
            f"[{label}] {aggregate.name or aggregate.code}({aggregate.code}) "
            f"{elapsed_seconds:.0f}秒波动={move_pct:+.2f}% 现价={aggregate.last:.2f} "
            f"起点={reference_price:.2f} 来源={'+'.join(sources)}"
        )
        return [
            AlertEvent(
                event_type=event_type,
                code=aggregate.code,
                name=aggregate.name,
                occurred_at=now,
                price=aggregate.last,
                line_price=reference_price,
                sources=sources,
                severity="warning",
                message=message,
                metadata={
                    "source_count": len(sources),
                    "move_pct": round(move_pct, 3),
                    "elapsed_seconds": round(elapsed_seconds, 3),
                    "window_seconds": self.config.rapid_move_window_seconds,
                },
            )
        ]

    def _process_lines(self, aggregate: ConsensusQuote, watch: WatchStock, now: datetime) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        definitions: list[tuple[str, Callable[[Quote], float | None], str, str]] = []
        if watch.monitors("average"):
            definitions.append(
                ("average", lambda quote: quote.average_price, "break_average", "recover_average")
            )
        if watch.monitors("cost") and watch.cost:
            definitions.append(("cost", lambda _quote: watch.cost, "break_cost", "recover_cost"))
        if watch.monitors("ma5") and watch.ma5:
            definitions.append(("ma5", lambda _quote: watch.ma5, "break_ma5", "recover_ma5"))

        for scope, line_getter, break_type, recover_type in definitions:
            valid: list[tuple[Quote, float]] = []
            for quote in aggregate.source_quotes:
                line = line_getter(quote)
                if line and line > 0:
                    valid.append((quote, float(line)))
            if not valid:
                continue

            required = self.config.line_confirmations
            single_source = len(valid) == 1
            if single_source and self.config.allow_single_source_fallback:
                required = 1
            required = min(required, len(valid)) if len(valid) >= self.config.line_confirmations else required
            if len(valid) < required:
                continue

            below_sources: list[str] = []
            above_sources: list[str] = []
            for quote, line in valid:
                band = max(PRICE_TICK, line * self.config.hysteresis_bps / 10000)
                if quote.last < line - band:
                    below_sources.append(quote.source)
                elif quote.last > line + band:
                    above_sources.append(quote.source)

            target: str | None = None
            target_sources: list[str] = []
            if len(below_sources) >= required:
                target, target_sources = "below", below_sources
            elif len(above_sources) >= required:
                target, target_sources = "above", above_sources

            key = (watch.code, f"line:{scope}")
            state = self.states.get(key)
            if state is None:
                self.states[key] = {
                    "status": target or "neutral",
                    "pending": None,
                    "pending_count": 0,
                    "last": aggregate.last,
                }
                continue
            if target is None or target == state.get("status"):
                state.update({"pending": None, "pending_count": 0, "last": aggregate.last})
                continue

            if state.get("pending") == target:
                state["pending_count"] = int(state.get("pending_count", 0)) + 1
            else:
                state["pending"] = target
                state["pending_count"] = 1
            state["last"] = aggregate.last

            if state["pending_count"] < self.config.line_hold_polls:
                continue
            previous_status = state.get("status")
            state.update({"status": target, "pending": None, "pending_count": 0})
            if previous_status in {"above", "neutral"} and target == "below":
                line_price = float(median(line for _quote, line in valid))
                severity = "warning" if len(target_sources) > 1 else "single-source"
                events.append(self._event(break_type, aggregate, now, line_price, tuple(target_sources), severity))
            elif previous_status == "below" and target == "above" and self.config.notify_recovery:
                line_price = float(median(line for _quote, line in valid))
                events.append(self._event(recover_type, aggregate, now, line_price, tuple(target_sources), "info"))
        return events

    def _event(
        self,
        event_type: str,
        aggregate: ConsensusQuote,
        now: datetime,
        line_price: float | None,
        sources: tuple[str, ...],
        severity: str,
        initial: bool = False,
    ) -> AlertEvent:
        label = EVENT_LABELS[event_type]
        source_text = "+".join(sorted(set(sources)))
        line_text = f" 参考线={line_price:.2f}" if line_price else ""
        initial_text = "（启动时发现当日已发生）" if initial else ""
        message = (
            f"[{label}] {aggregate.name or aggregate.code}({aggregate.code}) "
            f"现价={aggregate.last:.2f}{line_text} 来源={source_text}{initial_text}"
        )
        return AlertEvent(
            event_type=event_type,
            code=aggregate.code,
            name=aggregate.name,
            occurred_at=now,
            price=aggregate.last,
            line_price=line_price,
            sources=tuple(sorted(set(sources))),
            severity=severity,
            message=message,
            metadata={
                "source_count": len(set(sources)),
                "price_spread_bps": round(aggregate.price_spread_bps, 3),
                "initial": initial,
            },
        )

    @staticmethod
    def _combine(events: list[AlertEvent]) -> list[AlertEvent]:
        grouped: dict[tuple[str, str], list[AlertEvent]] = defaultdict(list)
        for event in events:
            grouped[(event.code, event.event_type)].append(event)
        combined: list[AlertEvent] = []
        for (_code, _event_type), group in grouped.items():
            base = group[0]
            sources = tuple(sorted({source for event in group for source in event.sources}))
            if len(group) > 1:
                base.sources = sources
                base.metadata["source_count"] = len(sources)
                base.message = base.message.rsplit("来源=", 1)[0] + f"来源={'+'.join(sources)}"
            combined.append(base)
        return combined

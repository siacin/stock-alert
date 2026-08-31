from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median

from .models import ConsensusQuote, ProviderResult, Quote, WatchStock, median_optional


def _is_fresh(quote: Quote, now: datetime, max_age_seconds: float) -> bool:
    age = (now - quote.timestamp).total_seconds()
    return -5 <= age <= max_age_seconds


def aggregate_results(
    results: list[ProviderResult],
    watches: dict[str, WatchStock],
    now: datetime,
    max_quote_age_seconds: float,
    enforce_freshness: bool,
) -> tuple[dict[str, ConsensusQuote], dict[str, list[Quote]]]:
    grouped: dict[str, list[Quote]] = defaultdict(list)
    for result in results:
        for code, quote in result.quotes.items():
            if code not in watches or quote.last <= 0:
                continue
            if enforce_freshness and not _is_fresh(quote, now, max_quote_age_seconds):
                continue
            grouped[code].append(quote)

    consensus: dict[str, ConsensusQuote] = {}
    for code, quotes in grouped.items():
        if not quotes:
            continue
        watch = watches[code]
        last = float(median([quote.last for quote in quotes]))
        prev_close = float(median([quote.prev_close for quote in quotes]))
        limit_values = [quote.resolved_limit_up(watch) for quote in quotes]
        average_values = [quote.average_price for quote in quotes]
        spread = ((max(quote.last for quote in quotes) - min(quote.last for quote in quotes)) / last * 10000) if last else 0
        newest = max(quotes, key=lambda quote: quote.timestamp)
        consensus[code] = ConsensusQuote(
            code=code,
            name=watch.name or newest.name,
            timestamp=newest.timestamp,
            last=last,
            prev_close=prev_close,
            open=median_optional(quote.open for quote in quotes),
            high=median_optional(quote.high for quote in quotes),
            low=median_optional(quote.low for quote in quotes),
            volume_shares=median_optional(quote.volume_shares for quote in quotes),
            amount=median_optional(quote.amount for quote in quotes),
            average_price=median_optional(average_values),
            limit_up=median_optional(limit_values),
            sources=tuple(sorted(quote.source for quote in quotes)),
            source_quotes=tuple(sorted(quotes, key=lambda quote: quote.source)),
            price_spread_bps=spread,
        )
    return consensus, grouped

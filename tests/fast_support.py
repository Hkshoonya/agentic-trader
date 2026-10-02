"""Shared builders for the fast-engine tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agentic_trading.fast.bars import Bar
from agentic_trading.venues.model import Tick

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # a Monday


def quote(symbol: str, bid, ask, at: datetime, *, venue: str = "alpaca") -> Tick:
    return Tick(venue, symbol, Decimal(str(bid)), Decimal(str(ask)), None, None, at, at)


def trade(symbol: str, price, at: datetime, *, venue: str = "alpaca") -> Tick:
    return Tick(venue, symbol, None, None, Decimal(str(price)), Decimal("0.01"), at, at)


def bars_from(closes, *, start: datetime = T0, wick: float = 0.0) -> list[Bar]:
    """One bar per close; each opens at the previous close."""
    out, previous = [], float(closes[0])
    for i, close in enumerate(closes):
        close = float(close)
        out.append(Bar(start + timedelta(minutes=i), previous,
                       max(previous, close) + wick, min(previous, close) - wick, close))
        previous = close
    return out

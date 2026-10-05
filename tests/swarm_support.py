"""Daily bars for swarm tests: deterministic, no network, no files."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from agentic_trading.history import Bar

D0 = date(2024, 1, 1)


def daily(symbol: str, closes: list[float], *, start: date = D0, weekdays_only: bool = False) -> list[Bar]:
    bars, day = [], start
    for close in closes:
        while weekdays_only and day.weekday() >= 5:
            day += timedelta(days=1)
        price = Decimal(str(round(close, 6)))
        when = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        bars.append(Bar(symbol, when, price, price, price, price, Decimal("1000")))
        day += timedelta(days=1)
    return bars


def path(n: int, *, start: float = 100.0, step: float = 0.004, wiggle: float = 0.01, phase: float = 0.0) -> list[float]:
    """A drifting price with a deterministic wobble (never a straight line)."""
    return [start * (1 + step) ** i * (1 + wiggle * math.sin(i / 3 + phase)) for i in range(n)]


def universe(n: int = 420) -> dict[str, list[Bar]]:
    """Four equities (weekdays) and three coins (every day), all rising at different speeds."""
    return {
        "SPY": daily("SPY", path(n, step=0.001), weekdays_only=True),
        "QQQ": daily("QQQ", path(n, step=0.0012, phase=1), weekdays_only=True),
        "AAPL": daily("AAPL", path(n, step=0.002, phase=2, wiggle=0.03), weekdays_only=True),
        "MSFT": daily("MSFT", path(n, step=-0.0005, phase=3, wiggle=0.03), weekdays_only=True),
        "BTCUSD": daily("BTCUSD", path(n, start=30000, step=0.002, phase=4, wiggle=0.04)),
        "ETHUSD": daily("ETHUSD", path(n, start=2000, step=0.003, phase=5, wiggle=0.05)),
        "SOLUSD": daily("SOLUSD", path(n, start=50, step=-0.001, phase=6, wiggle=0.06)),
    }

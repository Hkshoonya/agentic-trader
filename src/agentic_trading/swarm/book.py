"""One day of the swarm's book, the record the desk judges.

The book is marked at the day's closes. That closes the previous day's sample,
just as the desk's own books sample. The book then trades toward the blend:
- a position more than 5 points over its target is sold down;
- one under its target by more than 5 points is bought up;
- a new target of any size is entered (the $1 minimum still applies).

A symbol with no close that day (a stock on a Saturday) waits. Missed days are
replayed one by one by the caller, so a machine that was off still gets one
sample per day.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from agentic_trading.desk.book import MIN_NOTIONAL, MemberBook
from agentic_trading.desk.member import broker_symbol
from agentic_trading.history import Bar

REBALANCE_GAP = Decimal("0.05")
ZERO = Decimal("0")


def closes_on(series: dict[str, list[Bar]], day: date) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for symbol, bars in series.items():
        for bar in reversed(bars):
            if bar.start.date() == day:
                if bar.close > 0:
                    out[broker_symbol(symbol)] = Decimal(str(bar.close))
                break
            if bar.start.date() < day:
                break
    return out


def advance(book: MemberBook, day: date, weights: dict[str, float], closes: dict[str, Decimal],
            costs: Any) -> dict[str, int]:
    book.mark(closes, datetime.combine(day, time(23, 59), tzinfo=timezone.utc))
    targets = {broker_symbol(s): Decimal(str(w)) for s, w in weights.items() if w > 0}
    sold = bought = 0
    if book.equity <= 0:
        return {"sold": sold, "bought": bought}
    current = book.weights()
    for symbol in sorted(book.positions):
        price = closes.get(symbol)
        have = Decimal(str(current.get(symbol, 0.0)))
        want = targets.get(symbol, ZERO)
        if price is None or have <= 0:
            continue
        if want == 0 or have - want > REBALANCE_GAP:
            quantity = book.positions[symbol] * (have - want) / have
            if book.sell(symbol, quantity, price, costs) > 0:
                sold += 1
    equity, current = book.equity, book.weights()
    for symbol, want in sorted(targets.items()):
        price = closes.get(symbol)
        have = Decimal(str(current.get(symbol, 0.0)))
        if price is None:
            continue
        if symbol in book.positions and want - have <= REBALANCE_GAP:
            continue  # the gap stops churn on held positions; it never keeps a new target out
        if book.buy(symbol, (want - have) * equity, price, costs, MIN_NOTIONAL) > 0:
            bought += 1
    return {"sold": sold, "bought": bought}

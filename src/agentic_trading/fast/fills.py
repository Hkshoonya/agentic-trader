"""Paper fills that do not flatter the strategy.

A decision fills at the first usable quote that arrives at least ``delay``
(250 ms) after it, which models an order's trip to the venue. Buys pay the ask
and sells get the bid, plus the venue fee. A trade print, or a crossed or
one-sided quote, never fills. An order the book cannot afford (under the $1
minimum) comes back as a fill of quantity 0, so the caller knows it failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from agentic_trading.desk.book import MIN_NOTIONAL, MemberBook
from agentic_trading.fast.costs import FeeOnlyCosts
from agentic_trading.venues.model import Tick

ZERO = Decimal("0")


def usable_quote(tick: Tick) -> bool:
    return (tick.bid is not None and tick.ask is not None
            and tick.bid > 0 and tick.ask > 0 and tick.ask >= tick.bid)


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # "buy" or "sell"
    decided_at: datetime
    notional: Decimal = ZERO
    quantity: Decimal = ZERO


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    at: datetime


class PaperFiller:
    def __init__(self, book: MemberBook, fee: Decimal, *, delay: timedelta,
                 minimum: Decimal = MIN_NOTIONAL) -> None:
        self.book = book
        self.costs = FeeOnlyCosts(fee)
        self.delay = delay
        self.minimum = minimum
        self.pending: list[Order] = []

    def submit(self, order: Order) -> None:
        self.pending.append(order)

    def on_tick(self, tick: Tick) -> list[Fill]:
        if not usable_quote(tick):
            return []
        fills: list[Fill] = []
        waiting: list[Order] = []
        for order in self.pending:
            if order.symbol != tick.symbol or tick.received_at - order.decided_at < self.delay:
                waiting.append(order)
                continue
            if order.side == "buy":
                price = tick.ask
                quantity = self.book.buy(order.symbol, order.notional, price, self.costs, self.minimum)
            else:
                price = tick.bid
                quantity = self.book.sell(order.symbol, order.quantity, price, self.costs)
            fills.append(Fill(order.symbol, order.side, price, quantity, tick.received_at))
        self.pending = waiting
        return fills

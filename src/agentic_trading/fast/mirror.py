"""The same trades, priced at Coinbase: a comparison book the desk never judges.

Each Alpaca-book fill is copied at Coinbase's bid or ask at that moment, with
Coinbase's fee. A copy is **unpriced** (counted, not guessed) when Coinbase has
no quote or its quote is older than ``max_age``, or when the book cannot
afford the buy.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.costs import FeeOnlyCosts
from agentic_trading.fast.fills import Fill, usable_quote
from agentic_trading.venues.model import Tick

ZERO = Decimal("0")


class CoinbaseMirror:
    def __init__(self, book: MemberBook, fee: Decimal, *, max_age: timedelta = timedelta(seconds=5)) -> None:
        self.book = book
        self.costs = FeeOnlyCosts(fee)
        self.max_age = max_age
        self.quotes: dict[str, Tick] = {}
        self.unpriced = 0

    @staticmethod
    def product(symbol: str) -> str:
        return symbol.replace("/", "-")

    def on_tick(self, tick: Tick) -> None:
        if usable_quote(tick):
            self.quotes[tick.symbol] = tick

    def mark(self, symbol: str, fallback: Decimal, now: datetime) -> None:
        quote = self.quotes.get(self.product(symbol))
        price = (quote.bid + quote.ask) / 2 if quote is not None else fallback
        self.book.mark({symbol: price}, now)

    def copy(self, fill: Fill, now: datetime) -> bool:
        if fill.side == "sell" and self.book.positions.get(fill.symbol.upper(), ZERO) <= 0:
            return False  # its buy was never copied, and that was counted already
        quote = self.quotes.get(self.product(fill.symbol))
        if quote is None or abs(now - quote.received_at) > self.max_age:
            self.unpriced += 1
            return False
        if fill.side == "buy":
            quantity = self.book.buy(fill.symbol, fill.price * fill.quantity, quote.ask, self.costs)
        else:
            held = self.book.positions.get(fill.symbol.upper(), ZERO)
            quantity = self.book.sell(fill.symbol, held, quote.bid, self.costs)
        if quantity <= 0:
            self.unpriced += 1
            return False
        return True

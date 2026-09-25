"""A desk member: an existing strategy, trading its own paper book in full.

The member converts the strategy's intents into paper fills at the live quote
(buys at the ask, sells at the bid) and reports each fill back to the strategy,
exactly as the runtime reports shadow fills, so the strategy's own ledger stays
true. A strategy that raises is disabled; the rest of the desk carries on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from agentic_trading.desk.book import ZERO, MemberBook
from agentic_trading.orders import is_crypto_symbol
from agentic_trading.types import Side


def broker_symbol(symbol: str) -> str:
    """BTCUSD -> BTC-USD; equities unchanged. The desk keys everything this way."""
    text = str(symbol).upper()
    if is_crypto_symbol(text) and "-" not in text and text.endswith("USD"):
        return f"{text[:-3]}-USD"
    return text


def _price(raw: Any, fallback: Any) -> Optional[Decimal]:
    for value in (raw, fallback):
        try:
            price = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if price.is_finite() and price > 0:
            return price
    return None


class Member:
    def __init__(
        self, name: str, strategy: Any, book: MemberBook, *, order_pct: Decimal
    ) -> None:
        self.name = name
        self.strategy = strategy
        self.book = book
        self.order_pct = Decimal(str(order_pct))
        self.failed = ""
        seed = getattr(strategy, "seed_positions", None)
        if callable(seed):
            seed({symbol: str(qty) for symbol, qty in book.positions.items()})

    def on_quote(
        self, quote: dict[str, Any], quotes: dict[str, dict[str, Any]], costs: Any
    ) -> list[dict[str, Any]]:
        if self.failed:
            return []
        try:
            intents = list(self.strategy.on_quote(quote) or [])
        except Exception as exc:  # noqa: BLE001 — one member must not stop the desk
            self.failed = f"{type(exc).__name__}: {exc}"[:200]
            return [
                {"event": "desk_member_failed", "member": self.name, "error": self.failed}
            ]
        events: list[dict[str, Any]] = []
        for intent in intents:
            symbol = broker_symbol(intent.symbol)
            side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))
            seen = quotes.get(symbol) or {}
            if side is Side.BUY:
                price = _price(seen.get("ask"), intent.ref_price)
                if price is None:
                    continue
                share = (intent.metadata or {}).get("target_share")
                if share is not None:
                    notional = self.book.equity * Decimal(str(share))
                else:
                    weight = Decimal(str(intent.weight)) if intent.weight is not None else Decimal("1")
                    notional = self.book.equity * self.order_pct * min(Decimal("1"), max(ZERO, weight))
                filled = self.book.buy(symbol, notional, price, costs)
                signed = filled
            else:
                price = _price(seen.get("bid"), intent.ref_price)
                if price is None or intent.quantity is None:
                    continue
                filled = self.book.sell(symbol, Decimal(str(intent.quantity)), price, costs)
                signed = -filled
            if filled <= 0:
                continue
            note = getattr(self.strategy, "note_fill", None)
            if callable(note):
                note(symbol, signed)
            events.append(
                {
                    "event": "member_fill",
                    "member": self.name,
                    "symbol": symbol,
                    "side": side.value,
                    "quantity": str(filled),
                    "price": str(price),
                    "reason": intent.reason,
                }
            )
        return events

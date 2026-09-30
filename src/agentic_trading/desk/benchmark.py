"""The benchmark member: 60% QQQ / 40% BTC, bought once and held.

It is the desk's "do nothing smart" option. Every other member has to beat it
on live results to be given capital, and it holds whatever capital nobody
earned.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from agentic_trading.orders import is_crypto_symbol
from agentic_trading.types import OrderIntent, Side, new_decision_id

BENCHMARK_SHARES: dict[str, Decimal] = {"QQQ": Decimal("0.6"), "BTC-USD": Decimal("0.4")}


class BenchmarkStrategy:
    last_decided_day = ""

    def __init__(self, shares: Optional[dict[str, Decimal]] = None) -> None:
        self.shares = {s.upper(): Decimal(str(v)) for s, v in (shares or BENCHMARK_SHARES).items()}
        self._held: set[str] = set()

    def on_quote(self, quote: dict[str, Any]) -> list[OrderIntent]:
        symbol = str(quote.get("symbol") or "").upper()
        if symbol not in self.shares or symbol in self._held:
            return []
        if not is_crypto_symbol(symbol) and quote.get("market_session") != "regular":
            return []
        try:
            ask = Decimal(str(quote.get("ask")))
        except (ArithmeticError, TypeError, ValueError):
            return []
        if not ask.is_finite() or ask <= 0:
            return []
        return [
            OrderIntent(
                decision_id=new_decision_id(),
                symbol=symbol,
                side=Side.BUY,
                reason="benchmark_hold",
                created_at=_stamp(quote.get("observed_at")),
                quantity=Decimal("1"),  # placeholder; the member sizes by target_share
                ref_price=ask,
                metadata={"target_share": str(self.shares[symbol])},
            )
        ]

    def seed_positions(self, positions: dict[str, Any]) -> int:
        self._held = {
            s.upper() for s, q in (positions or {}).items() if Decimal(str(q)) > 0
        }
        return len(self._held)

    def note_fill(self, symbol: str, quantity: Any) -> None:
        if Decimal(str(quantity)) > 0:
            self._held.add(symbol.upper())

    def release_decision(self, day: str) -> bool:
        return False

    def reload_history(self) -> int:
        return 0


def _stamp(raw: Any) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

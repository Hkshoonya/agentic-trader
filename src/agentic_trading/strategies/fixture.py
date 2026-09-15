"""Deterministic fixture strategy for shadow soak tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from agentic_trading.types import OrderIntent, Side, new_decision_id

DEFAULT_QUANTITY = Decimal("0.01")


def _parse_observed_at(quote: dict) -> datetime:
    value = quote.get("observed_at")
    if not isinstance(value, str):
        return datetime.now(timezone.utc)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


class FixtureStrategy:
    """Emit preloaded intents or a single buy then sell cycle for shadow soak."""

    def __init__(
        self,
        intents: Optional[list[OrderIntent]] = None,
        *,
        quantity: Decimal = DEFAULT_QUANTITY,
    ) -> None:
        self._preloaded = list(intents) if intents is not None else None
        self._preload_index = 0
        self._tick = 0
        self._quantity = quantity

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        if self._preloaded is not None:
            if self._preload_index >= len(self._preloaded):
                return []
            intent = self._preloaded[self._preload_index]
            self._preload_index += 1
            return [intent]

        if self._tick >= 2:
            return []

        created_at = _parse_observed_at(quote)
        symbol = quote["symbol"]
        self._tick += 1

        if self._tick == 1:
            return [
                OrderIntent(
                    decision_id=new_decision_id(),
                    symbol=symbol,
                    side=Side.BUY,
                    quantity=self._quantity,
                    ref_price=quote["ask"],
                    reason="fixture:buy",
                    created_at=created_at,
                )
            ]

        return [
            OrderIntent(
                decision_id=new_decision_id(),
                symbol=symbol,
                side=Side.SELL,
                quantity=self._quantity,
                ref_price=quote["bid"],
                reason="fixture:sell",
                created_at=created_at,
            )
        ]

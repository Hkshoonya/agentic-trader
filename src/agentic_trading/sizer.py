"""Position sizing: make strategy intents fit the account's risk budget.

Strategies express *what* they want to trade; they do not know the account
equity. A $50 account with a 5% cap can hold at most $2.50 of exposure, so a
fixed 0.01-share SPY intent (~$6.60) would be refused by RiskGuard forever and
the bot would never trade.

This module rescales **entries** down to the cap. Exits are never rescaled: a
close must be able to sell exactly what is held, otherwise a position could be
opened and never flattened.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from agentic_trading.types import OrderIntent, Side

QUANTITY_STEP = Decimal("0.000001")  # broker allows 6 decimal places


def size_intent(
    intent: OrderIntent,
    *,
    equity: Decimal,
    max_order_pct: Decimal,
    min_notional: Decimal = Decimal("1.00"),
) -> Optional[OrderIntent]:
    """Return an intent whose notional fits the per-order cap.

    Returns ``None`` when the intent cannot be sized to at least
    ``min_notional``, which means the account is too small for this trade.
    """
    if equity <= 0:
        return None

    cap = equity * Decimal(str(max_order_pct))
    notional = intent.resolved_notional()
    side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))

    # Exits pass through untouched: they must match the held quantity.
    if side is Side.SELL:
        return intent if notional >= min_notional or intent.quantity is not None else None

    if notional <= cap:
        return intent if notional >= min_notional else None

    if intent.quantity is None or intent.ref_price is None:
        return None

    resized = (cap / intent.ref_price).quantize(
        QUANTITY_STEP, rounding=ROUND_DOWN
    )
    if resized <= 0:
        return None
    if resized * intent.ref_price < min_notional:
        return None
    return replace(intent, quantity=resized)

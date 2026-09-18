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

# A floor-sized order has to clear the broker's minimum *after* the quantity is
# rounded down to six decimals and after the price has moved a little between
# the decision and the fill, so the target carries a small margin. Without it a
# $1.00 target becomes a $0.9997 order and is refused for being a hair short.
FLOOR_MARGIN = Decimal("0.02")


def floor_cap(
    *,
    equity: Decimal,
    policy_cap: Decimal,
    min_notional: Decimal,
    max_cap: Decimal,
    margin: Decimal = FLOOR_MARGIN,
) -> Decimal:
    """The per-order fraction needed to place an order at all.

    Returns ``policy_cap`` when the account is big enough for that cap to clear
    ``min_notional``. Otherwise it returns the fraction that *does* clear it,
    never above ``max_cap`` — a ceiling the operator sets separately, because
    this rule deliberately trades outside the size the evidence supports.

    ``max_cap <= 0`` disables the rule and returns ``policy_cap`` unchanged.
    """
    if equity <= 0 or max_cap <= 0:
        return policy_cap
    needed = (min_notional * (Decimal(1) + margin)) / equity
    if needed <= policy_cap:
        return policy_cap
    return min(needed, max_cap)


def size_intent(
    intent: OrderIntent,
    *,
    equity: Decimal,
    max_order_pct: Decimal,
    min_notional: Decimal = Decimal("1.00"),
    proportional: bool = False,
) -> Optional[OrderIntent]:
    """Return an intent whose notional fits the per-order cap.

    ``max_order_pct`` is the ceiling. With ``proportional=True`` and a weight on
    the intent, the ceiling is scaled by that weight — a 60%-vol pair gets a
    third of the budget a 20%-vol name gets, which is what the strategy asked
    for. The weight is capped at 1.0: a quiet asset does not get *more* than the
    operator's per-order ceiling for being quiet.

    Returns ``None`` when the intent cannot be sized to at least
    ``min_notional``, which means the account is too small for this trade.
    """
    if equity <= 0:
        return None

    budget = Decimal(str(max_order_pct))
    if proportional and intent.weight is not None:
        try:
            weight = Decimal(str(intent.weight))
        except (ArithmeticError, TypeError, ValueError):
            weight = Decimal("1")
        if weight > 0:
            budget = budget * min(Decimal("1"), weight)
    cap = equity * budget
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

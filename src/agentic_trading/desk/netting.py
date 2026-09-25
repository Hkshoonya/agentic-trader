"""Netting: the desk's allocations become one quantity target per symbol.

Targets are quantities, fixed when an event (allocation change, member fill,
seeding) recomputes them. Holding a quantity through price drift is the whole
point: a dollar target recomputed on every quote would pay a rebalance charge
each time a crypto price moved 5%.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from agentic_trading.desk.member import broker_symbol
from agentic_trading.orders import is_crypto_symbol
from agentic_trading.types import OrderIntent, Side, new_decision_id

MIN_USD = Decimal("1.00")
GAP_PCT = Decimal("0.05")
STEP = Decimal("0.000001")
ZERO = Decimal("0")


def target_quantities(
    allocations: dict[str, float],
    member_weights: dict[str, dict[str, float]],
    account_equity: Decimal,
    prices: dict[str, Decimal],
) -> tuple[dict[str, Decimal], set[str]]:
    dollars: dict[str, Decimal] = {}
    for member, allocation in allocations.items():
        if allocation <= 0:
            continue
        for symbol, weight in member_weights.get(member, {}).items():
            key = broker_symbol(symbol)
            dollars[key] = dollars.get(key, ZERO) + (
                Decimal(str(allocation)) * Decimal(str(weight)) * account_equity
            )
    targets: dict[str, Decimal] = {}
    unpriced: set[str] = set()
    for symbol, usd in dollars.items():
        price = prices.get(symbol)
        if price is None or price <= 0:
            unpriced.add(symbol)
            continue
        targets[symbol] = (usd / price).quantize(STEP, rounding=ROUND_DOWN)
    return targets, unpriced


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def gap_intent(
    symbol: str,
    *,
    target: Decimal,
    held: Decimal,
    quote: dict[str, Any],
    created_at: datetime,
    min_buy_usd: Decimal = MIN_USD,
) -> Optional[OrderIntent]:
    """The order that closes ``symbol``'s gap, or None when none should be sent.

    ``min_buy_usd`` is the smallest buy the runtime will accept for this
    symbol (the broker minimum, or the measured-cost floor for crypto). A buy
    under it would be refused every retry window forever, so it is not sent.
    """
    symbol = broker_symbol(symbol)
    if not is_crypto_symbol(symbol) and quote.get("market_session") != "regular":
        return None
    gap = target - held
    if gap == 0:
        return None
    price = _decimal(quote.get("ask") if gap > 0 else quote.get("bid"))
    if price is None:
        return None
    selling_out = target <= 0 and held > 0
    if not selling_out:
        threshold = max(MIN_USD, GAP_PCT * target * price)
        if abs(gap) * price <= threshold:
            return None
    if gap > 0:
        side, quantity = Side.BUY, gap.quantize(STEP, rounding=ROUND_DOWN)
    else:
        side = Side.SELL
        quantity = held if selling_out else min(-gap, held).quantize(STEP, rounding=ROUND_DOWN)
    if quantity <= 0:
        return None
    if side is Side.BUY and quantity * price < min_buy_usd:
        return None
    return OrderIntent(
        decision_id=new_decision_id(),
        symbol=symbol,
        side=side,
        reason="desk_rebalance",
        created_at=created_at,
        quantity=quantity,
        ref_price=price,
    )

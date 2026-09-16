"""Schema-safe equity order requests for Robinhood Trading MCP.

Encodes the documented parameter rules for ``review_equity_order`` /
``place_equity_order`` so invalid orders fail locally, before a broker round
trip — and, in live mode, before any real submission.

Rules encoded here (from the live tool schema):

- exactly one of ``quantity`` or ``dollar_amount``
- ``dollar_amount`` requires ``type=market`` (server derives shares)
- ``limit_price`` required for ``limit`` / ``stop_limit``; omitted otherwise
- ``stop_price`` required for ``stop_market`` / ``stop_limit``; omitted otherwise
- market orders must be ``gfd``
- extended-hours and overnight sessions execute **limit orders only**
- fractional quantity and dollar-based orders only in
  ``regular_hours`` + ``market``, at most 6 decimal places
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from agentic_trading.types import Side

REGULAR_HOURS = "regular_hours"
EXTENDED_HOURS = "extended_hours"
ALL_DAY_HOURS = "all_day_hours"

MARKET = "market"
LIMIT = "limit"
STOP_MARKET = "stop_market"
STOP_LIMIT = "stop_limit"

_ORDER_TYPES = (MARKET, LIMIT, STOP_MARKET, STOP_LIMIT)
_MARKET_HOURS = (REGULAR_HOURS, EXTENDED_HOURS, ALL_DAY_HOURS)
_MAX_FRACTIONAL_PLACES = 6


class OrderValidationError(ValueError):
    """Raised when a request would be rejected by the broker schema."""


def _as_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 — surface as validation error
        raise OrderValidationError(f"{field} is not a decimal: {value!r}") from exc
    if not result.is_finite():
        raise OrderValidationError(f"{field} must be finite")
    return result


def _is_fractional(quantity: Decimal) -> bool:
    return quantity != quantity.to_integral_value()


def _fractional_places(quantity: Decimal) -> int:
    exponent = quantity.as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= 0:
        return 0
    return -exponent


@dataclass(frozen=True)
class EquityOrderRequest:
    """A single equity order expressed in broker-schema terms."""

    account_number: str
    symbol: str
    side: Side
    order_type: str
    quantity: Optional[Decimal] = None
    dollar_amount: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    time_in_force: str = "gfd"
    market_hours: str = REGULAR_HOURS
    ref_id: Optional[str] = None

    def __post_init__(self) -> None:
        account = str(self.account_number or "").strip()
        if not account:
            raise OrderValidationError("account_number required")
        object.__setattr__(self, "account_number", account)

        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            raise OrderValidationError("symbol required")
        object.__setattr__(self, "symbol", symbol)

        if not isinstance(self.side, Side):
            object.__setattr__(self, "side", Side(str(self.side).lower()))

        if self.order_type not in _ORDER_TYPES:
            raise OrderValidationError(f"unknown order_type: {self.order_type!r}")
        if self.market_hours not in _MARKET_HOURS:
            raise OrderValidationError(
                f"unknown market_hours: {self.market_hours!r}"
            )
        if self.time_in_force not in ("gfd", "gtc"):
            raise OrderValidationError(
                f"unknown time_in_force: {self.time_in_force!r}"
            )

        has_qty = self.quantity is not None
        has_dollars = self.dollar_amount is not None
        if has_qty == has_dollars:
            raise OrderValidationError(
                "provide exactly one of quantity or dollar_amount"
            )

        if has_qty:
            qty = _as_decimal(self.quantity, "quantity")
            if qty <= 0:
                raise OrderValidationError("quantity must be positive")
            object.__setattr__(self, "quantity", qty)
        if has_dollars:
            dollars = _as_decimal(self.dollar_amount, "dollar_amount")
            if dollars <= 0:
                raise OrderValidationError("dollar_amount must be positive")
            object.__setattr__(self, "dollar_amount", dollars)

        if self.limit_price is not None:
            limit = _as_decimal(self.limit_price, "limit_price")
            if limit <= 0:
                raise OrderValidationError("limit_price must be positive")
            object.__setattr__(self, "limit_price", limit)
        if self.stop_price is not None:
            stop = _as_decimal(self.stop_price, "stop_price")
            if stop <= 0:
                raise OrderValidationError("stop_price must be positive")
            object.__setattr__(self, "stop_price", stop)

        needs_limit = self.order_type in (LIMIT, STOP_LIMIT)
        if needs_limit and self.limit_price is None:
            raise OrderValidationError(
                f"limit_price required for {self.order_type}"
            )
        if not needs_limit and self.limit_price is not None:
            raise OrderValidationError(
                f"limit_price must be omitted for {self.order_type}"
            )

        needs_stop = self.order_type in (STOP_MARKET, STOP_LIMIT)
        if needs_stop and self.stop_price is None:
            raise OrderValidationError(f"stop_price required for {self.order_type}")
        if not needs_stop and self.stop_price is not None:
            raise OrderValidationError(
                f"stop_price must be omitted for {self.order_type}"
            )

        if self.order_type in (MARKET, STOP_MARKET) and self.time_in_force != "gfd":
            raise OrderValidationError(
                f"{self.order_type} orders must use gfd (market orders cannot rest)"
            )

        # Extended-hours and overnight sessions execute limit orders only.
        if self.market_hours != REGULAR_HOURS and self.order_type != LIMIT:
            raise OrderValidationError(
                "outside regular_hours only limit orders execute; "
                f"got {self.order_type} for {self.market_hours}"
            )

        if self.dollar_amount is not None:
            if self.order_type != MARKET or self.market_hours != REGULAR_HOURS:
                raise OrderValidationError(
                    "dollar_amount requires type=market and market_hours=regular_hours"
                )

        if self.quantity is not None and _is_fractional(self.quantity):
            if self.order_type != MARKET or self.market_hours != REGULAR_HOURS:
                raise OrderValidationError(
                    "fractional quantity requires type=market and "
                    "market_hours=regular_hours"
                )
            if _fractional_places(self.quantity) > _MAX_FRACTIONAL_PLACES:
                raise OrderValidationError(
                    f"fractional quantity limited to {_MAX_FRACTIONAL_PLACES} "
                    "decimal places"
                )

    def to_mcp_args(self, *, include_ref_id: bool = True) -> dict[str, Any]:
        """Arguments accepted by ``review_equity_order`` / ``place_equity_order``.

        ``ref_id`` is an idempotency key for *placement only*; the live review
        tool rejects it as an unexpected property.
        """
        # Crypto is a separate namespace: it wants the numeric rhs account id and
        # rejects market_hours outright. Detected inline (not imported) because
        # marketdata imports broker, which imports orders.
        symbol = self.symbol.upper()
        crypto = "-" in symbol or symbol.endswith("USD")
        args: dict[str, Any] = {
            ("rhs_account_number" if crypto else "account_number"): self.account_number,
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.order_type,
        }
        if not crypto:
            args["time_in_force"] = self.time_in_force
            args["market_hours"] = self.market_hours
        elif self.order_type != "market":
            # Crypto market orders reject gfd ("use gtc or omit"); resting
            # crypto orders still carry a time in force.
            args["time_in_force"] = self.time_in_force
        if self.quantity is not None:
            args["quantity"] = _fmt(self.quantity)
        if self.dollar_amount is not None:
            args["dollar_amount"] = _fmt(self.dollar_amount, places=2)
        if self.limit_price is not None:
            args["limit_price"] = _fmt(self.limit_price)
        if self.stop_price is not None:
            args["stop_price"] = _fmt(self.stop_price)
        if self.ref_id and include_ref_id:
            args["ref_id"] = self.ref_id
        return args

    @property
    def notional_bound(self) -> Decimal:
        """Worst-case notional for a pre-trade cap check."""
        if self.dollar_amount is not None:
            return self.dollar_amount
        assert self.quantity is not None
        reference = self.limit_price or self.stop_price
        if reference is None:
            raise OrderValidationError("quantity orders need a reference price")
        return self.quantity * reference


def _fmt(value: Decimal, *, places: Optional[int] = None) -> str:
    if places is not None:
        quantized = value.quantize(Decimal(1).scaleb(-places))
        return format(quantized, "f")
    # Preserve the caller's scale (95.00 stays "95.00") for readable journals.
    return format(value, "f")

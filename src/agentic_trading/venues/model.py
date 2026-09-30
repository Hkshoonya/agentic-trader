"""Venue-neutral shapes: every broker's data is turned into these."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

Side = Literal["buy", "sell"]


def _text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


@dataclass(frozen=True)
class Tick:
    venue: str
    symbol: str
    bid: Optional[Decimal]
    ask: Optional[Decimal]
    last: Optional[Decimal]
    size: Optional[Decimal]
    exchange_at: datetime
    received_at: datetime

    def to_row(self) -> dict[str, Optional[str]]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "bid": _text(self.bid),
            "ask": _text(self.ask),
            "last": _text(self.last),
            "size": _text(self.size),
            "exchange_at": self.exchange_at.isoformat(),
            "received_at": self.received_at.isoformat(),
        }


@dataclass(frozen=True)
class AccountView:
    venue: str
    mode: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    day_trades: int = 0
    pattern_day_trader: bool = False


@dataclass(frozen=True)
class PositionView:
    symbol: str
    qty: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class VenueOrder:
    client_order_id: str
    symbol: str
    side: Side
    type: Literal["market", "limit"] = "market"
    notional: Optional[Decimal] = None
    qty: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be buy or sell, not {self.side!r}")
        if self.type not in ("market", "limit"):
            raise ValueError(f"type must be market or limit, not {self.type!r}")
        if (self.notional is None) == (self.qty is None):
            raise ValueError("give exactly one of notional or qty")
        size = self.notional if self.notional is not None else self.qty
        if size is None or size <= 0:
            raise ValueError("order size must be positive")
        if self.type == "limit" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("a limit order needs a positive limit_price")

    def estimated_notional(self, price: Decimal) -> Decimal:
        if self.notional is not None:
            return self.notional
        return (self.qty or Decimal("0")) * price


@dataclass(frozen=True)
class VenueAck:
    client_order_id: str
    venue_order_id: str
    status: str
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Optional[Decimal] = None


def new_client_order_id(prefix: str = "at") -> str:
    """Our own id on every order, so a retried submit cannot fill twice."""
    return f"{prefix}-{uuid.uuid4().hex[:24]}"

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderIntent:
    decision_id: str
    symbol: str
    side: Side
    reason: str
    created_at: datetime
    notional_usd: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    ref_price: Optional[Decimal] = None
    metadata: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        if not self.decision_id:
            raise ValueError("decision_id required")
        # force notional resolution at construction
        self.resolved_notional()

    def resolved_notional(self) -> Decimal:
        if self.notional_usd is not None:
            n = Decimal(str(self.notional_usd))
        elif self.quantity is not None and self.ref_price is not None:
            n = Decimal(str(self.quantity)) * Decimal(str(self.ref_price))
        else:
            raise ValueError("need notional_usd or quantity+ref_price")
        if n <= 0 or not n.is_finite():
            raise ValueError("notional must be positive and finite")
        return n


def new_decision_id() -> str:
    return str(uuid4())

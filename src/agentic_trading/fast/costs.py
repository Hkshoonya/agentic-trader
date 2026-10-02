"""A ``MemberBook`` cost model that charges only a venue's fee.

The switchboard fills at the real ask (buys) or bid (sells), so the spread is
already in the price. The book must add the fee and nothing else; the desk's
usual cost model would charge an estimated spread a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class _FeeModel:
    per_side_bps: Decimal
    fee_per_order: Decimal = Decimal("0")


class FeeOnlyCosts:
    def __init__(self, per_side_fee: Decimal) -> None:
        self._model = _FeeModel(Decimal(str(per_side_fee)) * Decimal("10000"))

    def for_symbol(self, symbol: str) -> _FeeModel:
        return self._model

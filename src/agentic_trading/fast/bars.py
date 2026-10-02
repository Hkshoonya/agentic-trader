"""One-minute OHLC bars per symbol, built from tick prices.

Bars are keyed on the exchange's time. A late tick (its minute is older than the
open bar's) folds into the open bar; it never creates a past bar. A minute
with no ticks has no bar; gaps are not filled.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Bar:
    minute: datetime
    open: float
    high: float
    low: float
    close: float


class MinuteBars:
    def __init__(self, keep: int = 1440) -> None:
        self.keep = keep
        self._closed: dict[str, deque[Bar]] = {}
        self._open: dict[str, Bar] = {}

    def add(self, symbol: str, price: float, at: datetime) -> Optional[Bar]:
        minute = at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        current = self._open.get(symbol)
        if current is None:
            self._open[symbol] = Bar(minute, price, price, price, price)
            return None
        if minute <= current.minute:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
            return None
        self._closed.setdefault(symbol, deque(maxlen=self.keep)).append(current)
        self._open[symbol] = Bar(minute, price, price, price, price)
        return current

    def closed(self, symbol: str) -> list[Bar]:
        return list(self._closed.get(symbol, ()))

"""OHLC bars per symbol, built from tick prices: one bar per ``minutes`` (1 by default).

``minutes`` divides 60, so bars line up with the hour (a 15-minute bar opens
at :00, :15, :30 or :45). Bars are keyed on the exchange's time. A late tick
(its bar is older than the open one) folds into the open bar; it never creates
a past bar. A period with no ticks has no bar; gaps are not filled.
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


BAR_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 60)


def bar_start(at: datetime, minutes: int = 1) -> datetime:
    """The opening time of the ``minutes``-long bar that holds ``at`` (UTC)."""
    start = at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return start.replace(minute=start.minute - start.minute % minutes)


class MinuteBars:
    def __init__(self, keep: int = 1440, minutes: int = 1) -> None:
        if minutes not in BAR_MINUTES:
            raise ValueError(f"bar minutes must be one of {BAR_MINUTES}")
        self.keep = keep
        self.minutes = minutes
        self._closed: dict[str, deque[Bar]] = {}
        self._open: dict[str, Bar] = {}

    def add(self, symbol: str, price: float, at: datetime) -> Optional[Bar]:
        minute = bar_start(at, self.minutes)
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

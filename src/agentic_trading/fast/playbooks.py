"""Three long-only playbooks: what to buy, where to get out.

* **breakout** (trending): price clears the 30-bar high. It exits on a
  trailing stop 1.5 x ATR under the highest price since entry.
* **pullback** (trending): a dip to the 20-bar EMA while the close holds above
  the 60-bar EMA, bought when price clears the dip bar's high. It targets the
  60-bar high, with a stop under the dip.
* **squeeze break** (squeeze): price clears a quiet 60-bar range. The stop is
  the range middle and the target one range height higher.

``expected_move`` feeds the switchboard's cost gate. A trade stays with the
playbook that opened it until that playbook's exit fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from agentic_trading.fast.bars import Bar
from agentic_trading.fast.regime import SQUEEZE, TRENDING

BREAKOUT, PULLBACK, SQUEEZE_BREAK, RECOVERED = "breakout", "pullback", "squeeze_break", "recovered"
ATR_BARS = 30
BREAKOUT_BARS = 30
RANGE_BARS = 60
TRAIL_ATR = 1.5
BREAKOUT_MOVE_ATR = 2.0
PULLBACK_STOP_ATR = 0.25


@dataclass(frozen=True)
class Plan:
    playbook: str
    stop: float
    target: Optional[float]
    expected_move: float


@dataclass
class Trade:
    symbol: str
    playbook: str
    entry: float
    stop: float
    target: Optional[float]
    quantity: Decimal
    opened_at: datetime
    high: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "playbook": self.playbook, "entry": self.entry,
            "stop": self.stop, "target": self.target, "quantity": str(self.quantity),
            "opened_at": self.opened_at.isoformat(), "high": self.high,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Trade":
        return cls(
            str(raw["symbol"]), str(raw["playbook"]), float(raw["entry"]), float(raw["stop"]),
            None if raw.get("target") is None else float(raw["target"]),
            Decimal(str(raw["quantity"])), datetime.fromisoformat(str(raw["opened_at"])),
            float(raw["high"]),
        )


def atr(bars: Sequence[Bar], n: int = ATR_BARS) -> float:
    window = list(bars)[-(n + 1):]
    if len(window) < 2:
        return 0.0
    ranges = [
        max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close))
        for prior, bar in zip(window, window[1:])
    ]
    return sum(ranges) / len(ranges)


def ema(values: Sequence[float], n: int) -> float:
    k = 2.0 / (n + 1)
    average = float(values[0])
    for value in values[1:]:
        average = float(value) * k + average * (1 - k)
    return average


def breakout_entry(bars: Sequence[Bar], price: float) -> Optional[Plan]:
    if len(bars) < BREAKOUT_BARS:
        return None
    level = max(bar.high for bar in bars[-BREAKOUT_BARS:])
    size = atr(bars)
    if price <= level or size <= 0:
        return None
    return Plan(BREAKOUT, price - TRAIL_ATR * size, None, BREAKOUT_MOVE_ATR * size)


def pullback_entry(bars: Sequence[Bar], price: float) -> Optional[Plan]:
    if len(bars) < RANGE_BARS:
        return None
    closes = [bar.close for bar in bars[-240:]]
    last = bars[-1]
    if last.close <= ema(closes, 60) or last.low > ema(closes, 20) or price <= last.high:
        return None
    target = max(bar.high for bar in bars[-RANGE_BARS:])
    stop = last.low - PULLBACK_STOP_ATR * atr(bars)
    if target <= price or stop >= price:
        return None
    return Plan(PULLBACK, stop, target, target - price)


def squeeze_entry(bars: Sequence[Bar], price: float) -> Optional[Plan]:
    if len(bars) < RANGE_BARS:
        return None
    window = bars[-RANGE_BARS:]
    high, low = max(bar.high for bar in window), min(bar.low for bar in window)
    if price <= high or high <= low:
        return None
    height = high - low
    return Plan(SQUEEZE_BREAK, (high + low) / 2, price + height, height)


ENTRIES: dict[str, tuple[tuple[str, Callable[[Sequence[Bar], float], Optional[Plan]]], ...]] = {
    TRENDING: ((BREAKOUT, breakout_entry), (PULLBACK, pullback_entry)),
    SQUEEZE: ((SQUEEZE_BREAK, squeeze_entry),),
}


def exit_reason(trade: Trade, bars: Sequence[Bar], price: float) -> Optional[str]:
    """Why the trade should close at ``price`` now, or None. Ratchets trailing stops."""
    trade.high = max(trade.high, price)
    if trade.playbook == BREAKOUT:
        size = atr(bars)
        if size > 0:
            trade.stop = max(trade.stop, trade.high - TRAIL_ATR * size)
        return "trailing stop" if price <= trade.stop else None
    if price <= trade.stop:
        return "stop"
    if trade.target is not None and price >= trade.target:
        return "target"
    return None

"""Each coin's market, read once a minute: trending, squeeze, choppy or unclear.

* **efficiency ratio** (Kaufman): net move / total path over 30 bars.
  At least 0.35 is trending; at most 0.20 is choppy.
* **squeeze**: the 30-bar realized volatility sits in the bottom 20% of the
  day's rolling values (needs at least 240 bars of history).
* Precedence: squeeze, then trending, then choppy; anything else is unclear.
  Fewer than 60 bars is warming.

Choppy, unclear and warming all mean "stand aside". In chop, a ~0.5% round-trip
cost eats every small win.
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

from agentic_trading.fast.bars import Bar

WARMING, TRENDING, SQUEEZE, CHOPPY, UNCLEAR = "warming", "trending", "squeeze", "choppy", "unclear"
STAND_ASIDE = frozenset({WARMING, CHOPPY, UNCLEAR})
ER_BARS = 30
ER_TREND = 0.35
ER_CHOP = 0.20
MIN_BARS = 60
SQUEEZE_HISTORY = 240
SQUEEZE_SHARE = 0.20
VOL_STEP = 10


def efficiency_ratio(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        return 0.0
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return 0.0 if path == 0 else abs(closes[-1] - closes[0]) / path


def realized_vol(closes: Sequence[float]) -> float:
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return statistics.pstdev(returns) if len(returns) >= 2 else 0.0


def read_regime(bars: Sequence[Bar]) -> str:
    if len(bars) < MIN_BARS:
        return WARMING
    closes = [bar.close for bar in bars]
    window = ER_BARS + 1  # 30 moves need 31 closes
    if len(closes) >= SQUEEZE_HISTORY:
        now = realized_vol(closes[-window:])
        history = [realized_vol(closes[i - window:i]) for i in range(len(closes), window - 1, -VOL_STEP)]
        if now > 0 and sum(1 for v in history if v < now) / len(history) < SQUEEZE_SHARE:
            return SQUEEZE
    ratio = efficiency_ratio(closes[-window:])
    if ratio >= ER_TREND:
        return TRENDING
    if ratio <= ER_CHOP:
        return CHOPPY
    return UNCLEAR

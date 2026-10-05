"""Each coin's market, read as each bar closes: trending, squeeze, choppy or unclear.

* **efficiency ratio** (Kaufman): net move / total path over 30 bars.
  At least 0.35 is trending; at most 0.20 is choppy.
* **squeeze**: the 30-bar realized volatility sits in the bottom 20% of its
  rolling values over the bars kept: the last 24 h, or the last 240 bars when
  a day holds fewer (``history_bars``). Needs at least 240 bars of history.
* Precedence: squeeze, then trending, then choppy; anything else is unclear.
  Fewer than 60 bars is warming.

Choppy, unclear and warming all mean "stand aside". In chop, a ~0.5% round-trip
cost eats every small win.
"""

from __future__ import annotations

import math
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


def history_bars(bar_minutes: int) -> int:
    """How many bars to keep: a day's worth, and never fewer than the squeeze needs."""
    return max(SQUEEZE_HISTORY, 1440 // bar_minutes)


def efficiency_ratio(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        return 0.0
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return 0.0 if path == 0 else abs(closes[-1] - closes[0]) / path


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(b / a) if a > 0 and b > 0 else 0.0 for a, b in zip(closes, closes[1:])]


def realized_vol(closes: Sequence[float]) -> float:
    """Population standard deviation of log returns, in floats.

    ``statistics.pstdev`` computes with exact fractions: about 0.3 ms a window,
    which made each minute's reading ~15 ms on the venues event loop and a
    90-day replay take hours.
    """
    returns = _log_returns(closes)
    if len(returns) < 2:
        return 0.0
    mean = math.fsum(returns) / len(returns)
    return math.sqrt(math.fsum((r - mean) ** 2 for r in returns) / len(returns))


def _rolling_vols(closes: Sequence[float], window: int) -> list[float]:
    """``realized_vol`` of each ``window``-close slice ending at len, len-10, ... (prefix sums)."""
    returns = _log_returns(closes)
    s1, s2 = [0.0], [0.0]
    for r in returns:
        s1.append(s1[-1] + r)
        s2.append(s2[-1] + r * r)
    vols = []
    for end in range(len(closes), window - 1, -VOL_STEP):
        lo, hi = end - window, end - 1  # the window's returns are [lo, hi)
        n = hi - lo
        mean = (s1[hi] - s1[lo]) / n
        variance = (s2[hi] - s2[lo]) / n - mean * mean
        vols.append(math.sqrt(variance) if variance > 0 else 0.0)
    return vols


def read_regime(bars: Sequence[Bar]) -> str:
    if len(bars) < MIN_BARS:
        return WARMING
    closes = [bar.close for bar in bars]
    window = ER_BARS + 1  # 30 moves need 31 closes
    if len(closes) >= SQUEEZE_HISTORY:
        history = _rolling_vols(closes, window)
        now = history[0]
        if now > 0 and sum(1 for v in history if v < now) / len(history) < SQUEEZE_SHARE:
            return SQUEEZE
    ratio = efficiency_ratio(closes[-window:])
    if ratio >= ER_TREND:
        return TRENDING
    if ratio <= ER_CHOP:
        return CHOPPY
    return UNCLEAR

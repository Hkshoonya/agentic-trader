"""Weekly dip reversal (long-only, equities).

Each week, while SPY is above its 200-day average, the book holds the three
equities with the most negative 5-bar return among those still above their own
200-day average: last week's pullbacks in names that are rising. Crypto is left
out because weekly turnover at its spread costs more than a bounce pays.

Why it exists (2026-09-29): the desk's other members follow momentum, so the
allocator had no genuinely different candidate to fund. Short-horizon reversal
bets the other way. Its specification was fixed on design grounds before any
backtest of it was run, and it earns capital only through its live paper book.

Honest status (2026-09-29, 19% orders, modeled costs, 6 folds): +26 bps per
trade after costs, 15.7% max drawdown, p = 0.26; by year +3.8%, +16.5%, -4.1%,
-0.8% (2023-2026), trailing buy-and-hold in three of four. It is therefore an
opt-in member (``desk_members``), not in the default line-up: each extra member
raises every member's allocator bar (see ``desk.allocator.min_t``).

The ranking lives in :func:`agentic_trading.walkforward.rank_reversal`; books,
exit cadence, fill bookkeeping and restart state are the trend strategy's.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from agentic_trading.strategies.trend_crypto import TrendCryptoStrategy


class DipReversalStrategy(TrendCryptoStrategy):
    """Hold the week's deepest pullbacks in rising names; rotate on Mondays."""

    def target_weights(self, *, as_of: Optional[datetime] = None) -> dict[str, Decimal]:
        from agentic_trading.walkforward import targets_as_of

        weights = targets_as_of(
            self.history,
            as_of or datetime.now(timezone.utc),
            max_positions=self.max_positions,
            rule="reversal",
        )
        return {symbol: Decimal(str(weight)) for symbol, weight in weights.items()}

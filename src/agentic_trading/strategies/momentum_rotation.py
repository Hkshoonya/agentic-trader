"""Weekly momentum rotation (long-only, two books).

Each week the equity book holds the three equities with the strongest 63-bar
return, and the crypto book the two strongest coins by 60-bar return — only
names whose return is positive, and only while the book's regime symbol (SPY
over its 200-day average, BTC over its 100-day average) says risk is on.

Honest status (2026-09-23 research): over 2021-09..2026-09 the equity book beat
holding SPY in total but trailed it by ~27 points over the latest ~20 months;
the crypto book mainly cut BTC's bear-market losses. Neither beat buy-and-hold
with statistical confidence, and ~40 configurations were examined before this
one was fixed. It runs in shadow as a 30-day trial against buy-and-hold.

The ranking lives in :func:`agentic_trading.walkforward.rank_rotation`, so the
evidence gate and the console read the same arithmetic this strategy trades.
Everything else — books, exit cadence, fill bookkeeping, restart state — is the
trend strategy's, unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from agentic_trading.strategies.trend_crypto import TrendCryptoStrategy


class MomentumRotationStrategy(TrendCryptoStrategy):
    """Hold each book's strongest recent names; rotate on the weekly boundary."""

    def target_weights(self, *, as_of: Optional[datetime] = None) -> dict[str, Decimal]:
        from agentic_trading.walkforward import targets_as_of

        weights = targets_as_of(
            self.history,
            as_of or datetime.now(timezone.utc),
            max_positions=self.max_positions,
            rule="rotation",
        )
        return {symbol: Decimal(str(weight)) for symbol, weight in weights.items()}

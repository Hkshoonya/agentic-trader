"""Trend-following crypto portfolio strategy (long-only, volatility-aware).

This is the specification that survived out-of-sample testing in this session:
a multi-horizon trend vote, sized inversely to realised volatility, spread
across a crypto universe, with a portfolio drawdown governor.

Honest status: out-of-sample Sharpes of 0.62-0.74 with p ~ 0.06-0.09 — positive
and drawdown-controlled, but NOT statistically established. It runs in shadow to
accumulate exactly the forward record that would settle it.

The strategy emits intents only; RiskGuard caps every one, the runtime sizes
them to the account, and the LLM advisor may veto any entry. It never places an
order itself and holds no broker reference.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from agentic_trading.history import Bar, load_bars
from agentic_trading.types import OrderIntent, Side, new_decision_id

HORIZONS = (50, 100, 200, 252)
EWMA_LAMBDA = 0.94
TARGET_VOL = 0.20
MAX_LEVERAGE = 1.5


class TrendCryptoStrategy:
    """Emit entry/exit intents as the trend portfolio's targets change."""

    def __init__(
        self,
        *,
        bar_dir: Path | str,
        symbols: list[str],
        max_positions: int = 5,
        min_vote: float = 0.5,
    ) -> None:
        self.bar_dir = Path(bar_dir)
        self.symbols = [s.upper() for s in symbols]
        self.max_positions = max_positions
        self.min_vote = min_vote
        self.history: dict[str, list[Bar]] = {}
        for symbol in self.symbols:
            path = self.bar_dir / f"{symbol}_day.jsonl"
            if path.is_file():
                bars = load_bars(path)
                if bars:
                    self.history[symbol] = bars
        self._held: set[str] = set()
        self._last_decision_date = ""

    # -- signal ----------------------------------------------------------

    def target_symbols(self, *, as_of: Optional[datetime] = None) -> list[str]:
        """Symbols with a majority-positive trend vote, ranked by conviction."""
        cutoff = as_of or datetime.now(timezone.utc)
        scored: list[tuple[float, str]] = []
        for symbol, bars in self.history.items():
            closes = [float(b.close) for b in bars if b.start < cutoff]
            if len(closes) < max(HORIZONS) + 2:
                continue
            votes = [
                closes[-1] > closes[-1 - horizon]
                for horizon in HORIZONS
                if len(closes) > horizon
            ]
            vote = sum(votes) / len(votes) if votes else 0.0
            if vote < self.min_vote:
                continue
            sigma = self._ewma_vol(closes)
            size = min(MAX_LEVERAGE, TARGET_VOL / sigma) if sigma else 0.0
            scored.append((vote * size, symbol))
        scored.sort(reverse=True)
        return [symbol for _, symbol in scored[: self.max_positions]]

    @staticmethod
    def _ewma_vol(closes: list[float]) -> Optional[float]:
        if len(closes) < 22:
            return None
        recent = closes[-61:]
        rets = [
            recent[i] / recent[i - 1] - 1
            for i in range(1, len(recent))
            if recent[i - 1] > 0
        ]
        if len(rets) < 20:
            return None
        var = statistics.pvariance(rets) or 1e-6
        for value in rets:
            var = EWMA_LAMBDA * var + (1 - EWMA_LAMBDA) * value * value
        return math.sqrt(var * 252)

    # -- strategy interface ----------------------------------------------

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        """Rebalance once per day; ignore the intraday quote stream."""

        def broker_form(normalized: str) -> str:
            """BTCUSD (bar-file key) -> BTC-USD (broker symbol)."""
            for known in self.symbols:
                if known.replace("-", "").upper() == normalized.upper():
                    return known.upper()
            return normalized.upper()

        quote_symbol = str(quote.get("symbol", "")).upper()
        symbol = quote_symbol.replace("-", "")  # history is keyed without dashes
        if symbol not in self.history:
            return []
        observed = quote.get("observed_at")
        try:
            stamp = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return []
        day = stamp.astimezone(timezone.utc).date().isoformat()
        if day == self._last_decision_date:
            return []

        self._last_decision_date = day
        targets = set(self.target_symbols(as_of=stamp))
        intents: list[OrderIntent] = []

        for exiting in sorted(self._held - targets):
            bars = self.history.get(exiting) or []
            price = quote.get("bid") if exiting == symbol else None
            if price is None and bars:
                price = bars[-1].close
            if price is None:
                continue
            quantity = self._held_quantity(exiting)
            if quantity <= 0:
                continue
            intents.append(
                OrderIntent(
                    decision_id=new_decision_id(),
                    symbol=broker_form(exiting),
                    side=Side.SELL,
                    quantity=quantity,
                    ref_price=Decimal(str(price)),
                    reason="trend_exit",
                    created_at=stamp,
                )
            )
            self._held.discard(exiting)
            self._held_quantity(exiting, clear=True)

        entering = sorted(targets - self._held)
        for new_symbol in entering:
            price = quote.get("ask") if new_symbol == symbol else None
            bars = self.history.get(new_symbol) or []
            if price is None and bars:
                price = bars[-1].close
            if price is None or Decimal(str(price)) <= 0:
                continue
            intents.append(
                OrderIntent(
                    decision_id=new_decision_id(),
                    symbol=broker_form(new_symbol),
                    side=Side.BUY,
                    quantity=Decimal("1"),  # runtime sizes it to the cap
                    ref_price=Decimal(str(price)),
                    reason="trend_entry",
                    created_at=stamp,
                )
            )
            self._held.add(new_symbol)

        return intents

    # -- position bookkeeping --------------------------------------------

    def _held_quantity(self, symbol: str, *, clear: bool = False) -> Decimal:
        ledger = getattr(self, "_quantities", None)
        if ledger is None:
            ledger = {}
            self._quantities = ledger
        if clear:
            return ledger.pop(symbol, Decimal("0"))
        return ledger.get(symbol, Decimal("0"))

    def note_fill(self, symbol: str, quantity: Decimal) -> None:
        """Called by the runtime when a shadow fill is recorded."""
        ledger = getattr(self, "_quantities", None)
        if ledger is None:
            ledger = {}
            self._quantities = ledger
        ledger[symbol.upper()] = ledger.get(symbol.upper(), Decimal("0")) + quantity

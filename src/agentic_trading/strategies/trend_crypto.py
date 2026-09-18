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

import json
import math
import statistics
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from agentic_trading.history import Bar, load_bars
from agentic_trading.orders import is_crypto_symbol
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
        state_path: Path | str | None = None,
    ) -> None:
        self.bar_dir = Path(bar_dir)
        self.symbols = [s.upper() for s in symbols]
        self.max_positions = max_positions
        self.min_vote = min_vote
        self.state_path = Path(state_path) if state_path else None
        self.history: dict[str, list[Bar]] = {}
        for symbol in self.symbols:
            # Bar files are named without the dash (BTCUSD_day.jsonl) while the
            # broker symbol keeps it (BTC-USD); history is keyed by the former.
            key = symbol.replace("-", "").upper()
            path = self.bar_dir / f"{key}_day.jsonl"
            if path.is_file():
                bars = load_bars(path)
                if bars:
                    self.history[key] = bars
        # Holdings are keyed the way the bar files are (BTCUSD), and are only
        # ever changed by a *reported fill* — never by emitting an intent.
        self._quantities: dict[str, Decimal] = {}
        self._last_decision_date = ""
        # The day the *runtime* last saw a rebalance from this strategy. The
        # runtime reads it to ask for a retry when the day's intents could not
        # be acted on for a technical reason (see ``release_decision``).
        self.last_decided_day = ""
        self._load_state()

    # -- position bookkeeping --------------------------------------------

    def _key(self, symbol: str) -> str:
        """Normalise any spelling (BTC-USD, BTCUSD, btc-usd) to the bar key."""
        return str(symbol).replace("-", "").upper()

    @property
    def _held(self) -> set[str]:
        """Positions actually held. Derived, so it cannot drift from the ledger."""
        return {key for key, quantity in self._quantities.items() if quantity > 0}

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(payload, dict):
            return
        quantities = payload.get("quantities")
        if isinstance(quantities, dict):
            for symbol, value in quantities.items():
                try:
                    quantity = Decimal(str(value))
                except (TypeError, ValueError, ArithmeticError):
                    continue
                if quantity > 0:
                    self._quantities[self._key(symbol)] = quantity
        self._last_decision_date = str(payload.get("last_decision_date", ""))

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        from agentic_trading import jsonio

        payload = {
            "last_decision_date": self._last_decision_date,
            "quantities": {key: str(value) for key, value in self._quantities.items()},
        }
        try:
            jsonio.write_text(
                self.state_path, jsonio.dumps(payload, indent=2) + "\n"
            )
        except OSError:
            return

    def seed_positions(self, positions: dict[str, Any]) -> int:
        """Adopt the runtime's view of what is held (it owns the truth).

        Called at start-up so a restart — or a fresh state file — still knows
        which positions need exits, and so the strategy cannot believe it holds
        something that was vetoed or never filled.

        An **empty** view is treated as "no information" rather than "you hold
        nothing": the runtime's shadow book is day-scoped, so it is legitimately
        empty at the start of a new day, and wiping the strategy's remembered
        positions on that basis would strand exactly the exits we need.
        """
        if not positions:
            return 0
        seeded = 0
        reconciled: dict[str, Decimal] = {}
        for symbol, quantity in (positions or {}).items():
            try:
                value = Decimal(str(quantity))
            except (TypeError, ValueError, ArithmeticError):
                continue
            if value > 0:
                reconciled[self._key(symbol)] = value
                seeded += 1
        self._quantities = reconciled
        self._save_state()
        return seeded

    # -- signal ----------------------------------------------------------

    def target_weights(
        self, *, as_of: Optional[datetime] = None
    ) -> dict[str, Decimal]:
        """The book the rule wants: ``{bar-file symbol: inverse-vol weight}``.

        The weight is ``min(MAX_LEVERAGE, TARGET_VOL / sigma)`` — 1.0 for a
        20%-vol asset, lower for wilder ones. Ranking uses vote × weight, and the
        runtime sizes each entry by the weight, so a 60%-vol pair ends up with a
        third of the dollars a 20%-vol name gets instead of the same amount.
        """
        cutoff = as_of or datetime.now(timezone.utc)
        scored: list[tuple[float, str, float]] = []
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
            if size <= 0:
                continue
            scored.append((vote * size, symbol, size))
        scored.sort(reverse=True)
        return {
            symbol: Decimal(str(round(size, 6)))
            for _, symbol, size in scored[: self.max_positions]
        }

    def target_symbols(self, *, as_of: Optional[datetime] = None) -> list[str]:
        """Symbols with a majority-positive trend vote, ranked by conviction."""
        return list(self.target_weights(as_of=as_of))

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
        """Exits every cycle, entries once a day per book.

        The two sides of a rebalance do not deserve the same clock. An entry
        spends risk budget on a signal that the daily bars produced, and doing
        that repeatedly through the day would turn one decision into many. An
        exit *removes* risk, and the position it closes is a position the rule
        no longer wants — so it is re-emitted on every quote until the fill
        actually arrives. Previously an exit was emitted once and then never
        again: if that single sell was refused or cancelled, the book sat in a
        position the strategy had already decided to leave, until the next
        day's rebalance.

        The crypto book and the equity book also do not share a clock, because
        they do not share a market. Crypto trades around the clock and decides
        at the UTC day roll. An equity can only be ordered *fractionally* while
        the regular session is open — outside it the broker refuses a fractional
        order, and a position that cannot be exited must not be opened — so the
        equity book decides on the first quote of its own session. That is what
        keeps a sell aligned with the hours in which the sell can execute.

        The runtime suppresses a repeat while the first sell is still working,
        so "every quote" does not mean "an order every quote".
        """

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
        is_crypto = is_crypto_symbol(quote_symbol)

        def in_same_book(key: str) -> bool:
            return is_crypto_symbol(key) == is_crypto

        targets = set(self.target_symbols(as_of=stamp))
        intents: list[OrderIntent] = []

        held = {key for key in self._held if in_same_book(key)}
        wanted = {key for key in targets if in_same_book(key)}

        for exiting in sorted(held - wanted):
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

        day = stamp.astimezone(timezone.utc).date().isoformat()
        book = "crypto" if is_crypto else "equity"
        decision_key = f"{book}:{day}"
        if not is_crypto and str(quote.get("market_session") or "") != "regular":
            # Nothing to decide: this book can only be ordered while its session
            # is open, and the exits above have already been considered.
            return intents
        if decision_key == self._last_decision_date:
            return intents

        self._last_decision_date = decision_key
        self.last_decided_day = decision_key
        self._save_state()  # a restart must not re-run today's entries
        entering = sorted(wanted - held)
        weights = self.target_weights(as_of=stamp)
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
                    # The same weight the ranking used, so the dollars match the
                    # conviction instead of every symbol getting the same size.
                    weight=weights.get(new_symbol),
                )
            )

        return intents

    # -- position bookkeeping --------------------------------------------

    def release_decision(self, day: str) -> bool:
        """Un-spend a day's rebalance so it can be attempted again.

        Only the runtime calls this, and only after every intent the rebalance
        produced was refused for a *technical* reason — the account value was
        not known yet, the runtime's position book could not be read, the
        operator had not armed the session, or the broker review call failed.

        A rebalance that a **risk** rule refused is never released: regime
        blocks, model vetoes, position-count and correlation limits are
        decisions, and retrying until a control changes its mind is not a
        control. It is also never released once anything was placed, so a retry
        cannot buy the same symbol twice.
        """
        if not day or day != self._last_decision_date:
            return False
        self._last_decision_date = ""
        self.last_decided_day = ""
        self._save_state()
        return True

    def _held_quantity(self, symbol: str, *, clear: bool = False) -> Decimal:
        key = self._key(symbol)
        if clear:
            return self._quantities.pop(key, Decimal("0"))
        return self._quantities.get(key, Decimal("0"))

    def note_fill(self, symbol: str, quantity: Any) -> Decimal:
        """Record an actual fill. Signed: sells are negative.

        Called by the runtime for accepted shadow fills. Emitting an intent does
        *not* change holdings — a vetoed or capped intent must not leave the
        strategy believing it holds something it never bought.
        """
        key = self._key(symbol)
        try:
            delta = Decimal(str(quantity))
        except (TypeError, ValueError, ArithmeticError):
            return self._quantities.get(key, Decimal("0"))
        updated = self._quantities.get(key, Decimal("0")) + delta
        if updated <= 0:
            self._quantities.pop(key, None)
        else:
            self._quantities[key] = updated
        self._save_state()
        return self._quantities.get(key, Decimal("0"))

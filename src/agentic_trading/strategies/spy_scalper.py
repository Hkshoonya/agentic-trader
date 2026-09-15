"""Streaming SPY scalper: paper_scalper rules → OrderIntent list."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import paper_scalper
from agentic_trading.types import OrderIntent, Side, new_decision_id

BPS = paper_scalper.BPS
QUANTITY_STEP = paper_scalper.QUANTITY_STEP
ROUND_DOWN = paper_scalper.ROUND_DOWN
MIN_SUPPORTED = paper_scalper.MIN_SUPPORTED
MAX_SUPPORTED = paper_scalper.MAX_SUPPORTED


def _parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


class SpyScalperStrategy:
    """Emit buy/sell OrderIntents from streaming quotes using paper scalper rules."""

    def __init__(self, config: paper_scalper.Config) -> None:
        self.config = config
        self._cash = config.initial_cash
        self._position: Optional[dict[str, Any]] = None
        self._mids: deque[Decimal] = deque(maxlen=3)
        self._last_observed: Optional[datetime] = None
        self._last_quote: Optional[datetime] = None
        self._last_exit: Optional[datetime] = None
        self._closed_trades = 0
        self._loss_halted = False

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        try:
            bid, ask, observed, quoted = self._validate(quote)
        except (ValueError, KeyError, TypeError, InvalidOperation, OverflowError):
            self._mids.clear()
            return []

        if (
            self._last_observed is not None
            and (observed - self._last_observed).total_seconds()
            > self.config.max_signal_gap_seconds
        ):
            self._mids.clear()

        self._last_observed = observed
        self._last_quote = quoted

        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * BPS
        modeled_buy = ask * (1 + self.config.slippage_bps / BPS)
        modeled_sell = bid * (1 - self.config.slippage_bps / BPS)
        exited = False

        if self._position is not None:
            exit_intent = self._maybe_exit(
                observed=observed,
                bid=bid,
                ask=ask,
                modeled_sell=modeled_sell,
            )
            if exit_intent is not None:
                self._mids.clear()
                return [exit_intent]
            exited = False

        can_enter = (
            self._position is None
            and not exited
            and not self._loss_halted
            and self._closed_trades < self.config.max_trades
        )
        if can_enter and self._last_exit is not None:
            can_enter = (
                observed - self._last_exit
            ).total_seconds() >= self.config.cooldown_seconds

        if can_enter and spread_bps <= self.config.max_spread_bps:
            self._mids.append(mid)
            if len(self._mids) == 3 and self._mids[0] < self._mids[1] < self._mids[2]:
                buy = self._try_enter(
                    observed=observed, ask=ask, modeled_buy=modeled_buy
                )
                if buy is not None:
                    self._mids.clear()
                    return [buy]
        else:
            self._mids.clear()

        return []

    def _validate(self, quote: dict) -> tuple[Decimal, Decimal, datetime, datetime]:
        if not isinstance(quote, dict):
            raise ValueError("quote must be an object")
        if quote.get("symbol") != self.config.symbol:
            raise ValueError("wrong_symbol")
        observed = _parse_time(quote["observed_at"])
        quoted = _parse_time(quote["quote_at"])
        bid = Decimal(str(quote["bid"]))
        ask = Decimal(str(quote["ask"]))
        if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
            raise ValueError("invalid_or_crossed_prices")
        if bid < MIN_SUPPORTED or ask > MAX_SUPPORTED:
            raise ValueError("unsupported_price_magnitude")
        age = (observed - quoted).total_seconds()
        if age < 0:
            raise ValueError("future_quote")
        if age > self.config.max_quote_age_seconds:
            raise ValueError("stale_quote")
        if self._last_quote is not None and quoted <= self._last_quote:
            raise ValueError("duplicate_or_out_of_order_quote")
        return bid, ask, observed, quoted

    def _maybe_exit(
        self,
        *,
        observed: datetime,
        bid: Decimal,
        ask: Decimal,
        modeled_sell: Decimal,
    ) -> Optional[OrderIntent]:
        assert self._position is not None
        quantity = self._position["quantity"]
        liquidation = quantity * modeled_sell - self.config.fee_per_order
        equity = self._cash + liquidation
        pnl = liquidation - self._position["entry_cost"]
        return_bps = pnl / self._position["entry_cost"] * BPS
        age_seconds = (observed - self._position["entry_at"]).total_seconds()

        reason: Optional[str] = None
        if self.config.initial_cash - equity >= self.config.daily_loss_limit:
            reason = "loss_limit"
            self._loss_halted = True
        elif return_bps <= -self.config.stop_loss_bps:
            reason = "stop_loss"
        elif return_bps >= self.config.take_profit_bps:
            reason = "take_profit"
        elif age_seconds >= self.config.max_hold_seconds:
            reason = "timeout"

        if reason is None:
            return None

        self._cash += liquidation
        self._closed_trades += 1
        self._position = None
        self._last_exit = observed

        return OrderIntent(
            decision_id=new_decision_id(),
            symbol=self.config.symbol,
            side=Side.SELL,
            quantity=quantity,
            ref_price=bid,
            reason=reason,
            created_at=observed,
        )

    def _try_enter(
        self,
        *,
        observed: datetime,
        ask: Decimal,
        modeled_buy: Decimal,
    ) -> Optional[OrderIntent]:
        budget = min(self._cash, self.config.initial_cash)
        spendable = budget - self.config.fee_per_order * 2
        quantity = (
            (spendable / modeled_buy).quantize(QUANTITY_STEP, rounding=ROUND_DOWN)
            if spendable > 0
            else Decimal("0")
        )
        if quantity <= 0:
            return None

        cost = quantity * modeled_buy + self.config.fee_per_order
        self._cash -= cost
        self._position = {
            "quantity": quantity,
            "entry_at": observed,
            "entry_price": modeled_buy,
            "entry_cost": cost,
        }

        return OrderIntent(
            decision_id=new_decision_id(),
            symbol=self.config.symbol,
            side=Side.BUY,
            quantity=quantity,
            ref_price=ask,
            reason="three_rising_midquotes",
            created_at=observed,
        )

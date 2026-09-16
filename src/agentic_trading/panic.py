"""Panic-reversion: buy capitulation, exit on recovery or time. No stop-loss.

A deliberately different bet from every family tested so far. The economic
rationale: crypto liquidations cascade — forced sellers push price far below
fair value within hours, and the reversion is fast. Trend/momentum rules lose
there; so do tight stops, because crypto noise is wide enough to hit them
before the recovery arrives. So this rule:

  1. entry  — today's return is <= mean - k*sigma over a rolling window
              (a capitulation bar, not merely a down day)
  2. exit   — price recovers to the pre-dip level, or max_hold bars elapse
  3. no stop-loss

Removing the stop is a real risk increase, which is why it is only defensible
with the drawdown limit enforced downstream: the promotion gate rejects any
candidate whose out-of-sample drawdown exceeds the operator's cap, so an
unbounded downside shows up as a refusal rather than a loss.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any, Optional

from agentic_trading.backtest import CostModel, Metrics, Trade
from agentic_trading.families import metrics_from_trades
from agentic_trading.history import Bar

BPS = Decimal("10000")


def _returns(bars: list[Bar]) -> list[Optional[float]]:
    out: list[Optional[float]] = [None]
    for index in range(1, len(bars)):
        previous = bars[index - 1].close
        if previous <= 0:
            out.append(None)
            continue
        out.append(float((bars[index].close - previous) / previous))
    return out


def sigma_dip_trades(
    bars: list[Bar],
    *,
    sigma_window: int = 20,
    sigma_k: float = 2.0,
    max_hold: int = 10,
    recovery: bool = True,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
) -> list[Trade]:
    """Simulate the capitulation-buy rule on one symbol."""
    costs = costs or CostModel()
    if len(bars) <= sigma_window + 2:
        return []

    returns = _returns(bars)
    trades: list[Trade] = []
    index = sigma_window
    while index < len(bars) - 1:
        window = [value for value in returns[index - sigma_window : index] if value is not None]
        today = returns[index]
        if len(window) < 5 or today is None:
            index += 1
            continue
        mean = statistics.fmean(window)
        sigma = statistics.pstdev(window)
        if sigma <= 0 or today > mean - sigma_k * sigma:
            index += 1
            continue

        # Capitulation bar: enter at the next open.
        entry_index = index + 1
        raw_entry = bars[entry_index].open
        entry_price = costs.buy_price(raw_entry)
        pre_dip = bars[index - 1].close
        budget = starting_cash
        quantity = (budget - costs.fee_per_order) / entry_price if entry_price > 0 else Decimal("0")
        if quantity <= 0:
            index += 1
            continue

        exit_index = None
        exit_price = None
        reason = ""
        for offset in range(entry_index, min(entry_index + max_hold, len(bars))):
            bar = bars[offset]
            if recovery and bar.close >= pre_dip:
                exit_index, exit_price, reason = offset, costs.sell_price(bar.close), "recovery"
                break
            if offset == entry_index + max_hold - 1:
                exit_index, exit_price, reason = offset, costs.sell_price(bar.close), "timeout"
                break
        if exit_index is None or exit_price is None:
            break

        pnl = (exit_price - entry_price) * quantity - costs.fee_per_order
        basis = entry_price * quantity
        trades.append(
            Trade(
                entry_time=bars[entry_index].start,
                exit_time=bars[exit_index].start,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl=pnl,
                return_bps=float(pnl / basis * BPS) if basis > 0 else 0.0,
                bars_held=exit_index - entry_index,
                exit_reason=reason,
            )
        )
        index = exit_index + 1
    return trades


CANDIDATES: tuple[dict[str, Any], ...] = tuple(
    {
        "sigma_window": window,
        "sigma_k": k,
        "max_hold": hold,
        "recovery": recovery,
    }
    for window in (10, 20, 40)
    for k in (1.5, 2.0, 2.5, 3.0)
    for hold in (5, 10, 20)
    for recovery in (True, False)
)


def pooled_metrics_for(
    symbol_bars: dict[str, list[Bar]],
    params: dict[str, Any],
    *,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
    bootstrap_samples: int = 0,
    seed: int = 7,
) -> Metrics:
    trades: list[Trade] = []
    for bars in symbol_bars.values():
        trades.extend(
            sigma_dip_trades(
                bars, costs=costs, starting_cash=starting_cash, **params
            )
        )
    trades.sort(key=lambda trade: trade.exit_time)
    return metrics_from_trades(
        trades, starting_cash=starting_cash, bootstrap_samples=bootstrap_samples, seed=seed
    )


def _split(
    symbol_bars: dict[str, list[Bar]], train_fraction: float
) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]]]:
    train: dict[str, list[Bar]] = {}
    test: dict[str, list[Bar]] = {}
    for symbol, bars in symbol_bars.items():
        cut = max(1, int(len(bars) * train_fraction))
        train[symbol] = bars[:cut]
        test[symbol] = bars[cut:]
    return train, test

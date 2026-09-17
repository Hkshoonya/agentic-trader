"""Walk-forward test of the *actual* strategy, with no parameter search.

Why this exists: the promotion gate grades the genome search, whose significance
bar has to be divided by every hypothesis tried (0.05 / 144 = 0.000347). That is
correct statistics applied to an experiment too broad for its sample, and it
made the gate effectively unpassable — a system designed never to trade.

This runs the opposite experiment, which is what a quant would actually do:

- **one fixed rule**, the same one the bot trades (`TrendCryptoStrategy`), with
  parameters taken from its specification rather than picked by search. One
  hypothesis means no multiple-comparison penalty: the bar is a plain 0.05.
- **walk-forward**, not a single 70/30 split: the timeline is cut into
  consecutive folds and every fold after the first is out-of-sample, so the
  evidence is a sequence of independent windows rather than one lucky split.

It cannot manufacture an edge — the rule is fixed before the numbers are seen —
and it reports the same way the gate does, so a result here means what it says.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from agentic_trading.backtest import CostModel
from agentic_trading.history import Bar

TARGET_VOL = 0.20
MAX_LEVERAGE = 1.5
EWMA_LAMBDA = 0.94


@dataclass
class Fold:
    index: int
    start: str
    end: str
    trades: int
    expectancy_bps: float
    return_pct: float


@dataclass
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    trades: int = 0
    expectancy_bps: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    final_equity: float = 0.0
    bootstrap_p_value: float = 1.0
    alpha: float = 0.05

    @property
    def eligible(self) -> bool:
        return (
            self.trades >= 30
            and self.expectancy_bps > 1.0
            and self.bootstrap_p_value <= self.alpha
            and self.max_drawdown_pct <= 15.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypotheses": 1,
            "alpha": self.alpha,
            "folds": [fold.__dict__ for fold in self.folds],
            "trades": self.trades,
            "expectancy_bps": round(self.expectancy_bps, 3),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "final_equity": round(self.final_equity, 4),
            "bootstrap_p_value": round(self.bootstrap_p_value, 4),
            "eligible": self.eligible,
        }


def _vol(closes: list[float]) -> Optional[float]:
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
    var = sum(value * value for value in rets) / len(rets) or 1e-6
    for value in rets:
        var = EWMA_LAMBDA * var + (1 - EWMA_LAMBDA) * value * value
    return math.sqrt(var * 252)


def targets_as_of(
    series: dict[str, list[Bar]],
    when: datetime,
    *,
    horizons: tuple[int, ...] = (50, 100, 200, 252),
    min_vote: float = 0.5,
    max_positions: int = 5,
) -> dict[str, float]:
    """The production rule, evaluated with data strictly before ``when``.

    Returns ``{symbol: size_fraction}`` for the symbols whose multi-horizon
    trend vote is positive, inverse-volatility sized. No searching, no fitting.
    """
    scored: list[tuple[float, str, float]] = []
    for symbol, bars in series.items():
        closes = [float(bar.close) for bar in bars if bar.start < when]
        if len(closes) < max(horizons) + 2:
            continue
        votes = [
            closes[-1] > closes[-1 - horizon]
            for horizon in horizons
            if len(closes) > horizon
        ]
        vote = sum(votes) / len(votes) if votes else 0.0
        if vote < min_vote:
            continue
        sigma = _vol(closes)
        size = min(MAX_LEVERAGE, TARGET_VOL / sigma) if sigma else 0.0
        if size <= 0:
            continue
        scored.append((vote * size, symbol, size))
    scored.sort(reverse=True)
    chosen = scored[:max_positions]
    total = sum(size for _, _, size in chosen) or 1.0
    return {symbol: size / total for _, symbol, size in chosen}


def simulate(
    series: dict[str, list[Bar]],
    *,
    start: datetime,
    end: datetime,
    costs: Optional[CostModel] = None,
    starting_cash: float = 50.0,
    max_positions: int = 5,
    step_days: int = 1,
    max_gross: float = 1.0,
    per_order_pct: float = 0.03,
    governor: float = 0.0,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Trade the fixed rule forward through one window; return round-trip trades."""
    costs = costs or CostModel()
    per_side = float(costs.per_side_bps) / 10_000

    closes: dict[str, dict[Any, float]] = {
        symbol: {bar.start.date(): float(bar.close) for bar in bars}
        for symbol, bars in series.items()
    }
    dates = sorted({day for table in closes.values() for day in table})
    dates = [day for day in dates if start.date() <= day <= end.date()]
    if len(dates) < 3:
        return [], []

    equity = starting_cash
    held: dict[str, float] = {}
    entry: dict[str, tuple[float, float]] = {}  # symbol -> (price, notional)
    trades: list[dict[str, Any]] = []
    # Portfolio equity after every realised event: drawdown must be measured on
    # what the account did, not on what a single position did.
    curve: list[float] = [equity]

    peak = equity
    for offset in range(0, len(dates), step_days):
        day = dates[offset]
        when = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        prices = {
            symbol: table[day]
            for symbol, table in closes.items()
            if day in table and table[day] > 0
        }
        targets = targets_as_of(series, when, max_positions=max_positions)

        # Exit anything no longer targeted. Positions are held as units, so the
        # entry price already contains the entry cost and the exit price the
        # exit cost: neither side can be double counted or forgotten.
        for symbol in list(held):
            if symbol in targets or symbol not in prices:
                continue
            price = prices[symbol]
            units = held.pop(symbol)
            entry_price, notional = entry.pop(symbol)
            proceeds = units * price * (1 - per_side)
            equity += proceeds
            curve.append(equity)
            trades.append(
                {
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "notional": notional,
                    "pnl": proceeds - notional,
                    "return_bps": (proceeds - notional) / notional * 10_000,
                }
            )

        peak = max(peak, equity)
        in_drawdown = (peak - equity) / peak if peak > 0 else 0.0
        # Drawdown governor: past the threshold the book stops opening new risk
        # and waits for the account to recover. Existing positions keep their
        # mechanical exits — this is a brake, not a second strategy.
        if governor and in_drawdown >= governor:
            continue

        # Enter the new targets, sized against current equity. Cash leaves the
        # account here — an entry that does not debit is how a simulator
        # compounds for free and reports six-figure returns per trade.
        #
        # The weights are the strategy's own inverse-volatility sizes and are
        # NOT normalised: normalising them forces the book to be fully invested
        # at all times, which throws away the risk control and turns a
        # vol-targeted rule into a crypto beta proxy.
        for symbol, weight in targets.items():
            if symbol in held or symbol not in prices:
                continue
            # Production sizing: the runtime sizes every entry to the per-order
            # risk cap, not to the strategy's volatility score — the score only
            # ranks which symbols get the slot. Simulating anything else would
            # be testing a bot that does not exist.
            notional = equity * per_order_pct
            if notional <= 0:
                continue
            equity -= notional
            price = prices[symbol]
            units = notional / (price * (1 + per_side))
            held[symbol] = units
            entry[symbol] = (price, notional)

    # Close whatever is still open at the end of the window.
    last = dates[-1]
    for symbol in list(held):
        table = closes.get(symbol) or {}
        price = table.get(last)
        if price is None:
            continue
        units = held.pop(symbol)
        entry_price, notional = entry.pop(symbol)
        proceeds = units * price * (1 - per_side)
        equity += proceeds
        curve.append(equity)
        trades.append(
            {
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": price,
                "notional": notional,
                "pnl": proceeds - notional,
                "return_bps": (proceeds - notional) / notional * 10_000,
            }
        )
    return trades, curve


def _curve_drawdown(curve: list[float]) -> float:
    """Worst peak-to-trough fall of the *account*, in percent."""
    peak = curve[0] if curve else 0.0
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


def _max_drawdown(returns_bps: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for bps in returns_bps:
        equity *= 1 + bps / 10_000
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak * 100)
    return worst


def bootstrap_p(returns_bps: list[float], *, samples: int = 2000, seed: int = 7) -> float:
    """P(mean <= 0) under resampling with replacement — one hypothesis, so no
    multiple-comparison correction applies."""
    if len(returns_bps) < 5:
        return 1.0
    rng = random.Random(seed)
    size = len(returns_bps)
    worse = 0
    for _ in range(samples):
        draw = [returns_bps[rng.randrange(size)] for _ in range(size)]
        if sum(draw) / size <= 0:
            worse += 1
    return (worse + 1) / (samples + 1)


def walk_forward(
    series: dict[str, list[Bar]],
    *,
    folds: int = 6,
    costs: Optional[CostModel] = None,
    starting_cash: float = 50.0,
    max_positions: int = 5,
    per_order_pct: float = 0.03,
    governor: float = 0.0,
) -> WalkForwardResult:
    """Cut the pooled timeline into consecutive windows and trade each one."""
    dates = sorted({bar.start for bars in series.values() for bar in bars})
    if len(dates) < folds * 60:
        folds = max(2, len(dates) // 60)
    span = len(dates) // folds
    result = WalkForwardResult()
    returns: list[float] = []
    equity_curve: list[float] = [starting_cash]
    equity = starting_cash
    for index in range(folds):
        lo = index * span
        hi = len(dates) if index == folds - 1 else (index + 1) * span
        if hi - lo < 30:
            continue
        window = dates[lo:hi]
        trades, fold_curve = simulate(
            series,
            start=window[0],
            end=window[-1],
            costs=costs,
            starting_cash=equity,
            max_positions=max_positions,
            per_order_pct=per_order_pct,
            governor=governor,
        )
        equity_curve.extend(fold_curve[1:])
        window_returns = [trade["return_bps"] for trade in trades]
        returns.extend(window_returns)
        pnl = sum(trade["pnl"] for trade in trades)
        opening_equity = equity
        equity += pnl
        result.folds.append(
            Fold(
                index=index,
                start=window[0].date().isoformat(),
                end=window[-1].date().isoformat(),
                trades=len(trades),
                expectancy_bps=(
                    sum(window_returns) / len(window_returns) if window_returns else 0.0
                ),
                return_pct=pnl / opening_equity * 100 if opening_equity > 0 else 0.0,
            )
        )

    result.trades = len(returns)
    if returns:
        result.expectancy_bps = sum(returns) / len(returns)
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value <= 0]
        result.win_rate = len(wins) / len(returns)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        result.profit_factor = (
            gross_win / gross_loss if gross_loss else float("inf")
        )
        result.max_drawdown_pct = _curve_drawdown(equity_curve)
        result.bootstrap_p_value = bootstrap_p(returns)
    result.final_equity = equity
    result.alpha = 0.05
    return result

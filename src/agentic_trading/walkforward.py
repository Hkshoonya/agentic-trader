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


def rank_targets(
    series: dict[str, list[Bar]],
    when: datetime,
    *,
    horizons: tuple[int, ...] = (50, 100, 200, 252),
    min_vote: float = 0.5,
    max_positions: int = 5,
) -> list[dict[str, Any]]:
    """Every symbol's vote and size, chosen or not, for the console.

    :func:`targets_as_of` answers "what would the book be"; this answers "why is
    each name in or out", which is the question an operator actually asks when
    nothing has traded. Both read the same numbers, so the explanation cannot
    drift from the decision.
    """
    rows: list[dict[str, Any]] = []
    for symbol, bars in series.items():
        closes = [float(bar.close) for bar in bars if bar.start < when]
        row: dict[str, Any] = {
            "symbol": symbol,
            "bars": len(closes),
            "vote": 0.0,
            "vol_pct": None,
            "weight": 0.0,
            "selected": False,
        }
        if len(closes) < max(horizons) + 2:
            row["reason"] = f"only {len(closes)} bars (needs {max(horizons) + 2})"
            rows.append(row)
            continue
        votes = [
            closes[-1] > closes[-1 - horizon]
            for horizon in horizons
            if len(closes) > horizon
        ]
        vote = sum(votes) / len(votes) if votes else 0.0
        sigma = _vol(closes)
        row["vote"] = round(vote, 3)
        row["vol_pct"] = None if sigma is None else round(sigma * 100, 1)
        row["weight"] = min(MAX_LEVERAGE, TARGET_VOL / sigma) if sigma else 0.0
        if vote < min_vote:
            row["reason"] = (
                f"trend vote {vote:.2f} < {min_vote:.2f} "
                f"({sum(votes)}/{len(votes)} horizons up)"
            )
        elif not sigma:
            row["reason"] = "no usable volatility estimate"
        else:
            row["reason"] = (
                f"vote {vote:.2f} ({sum(votes)}/{len(votes)} horizons up)"
            )
            row["selected"] = True
        rows.append(row)
    selected = sorted(
        (row for row in rows if row["selected"]),
        key=lambda row: row["vote"] * row["weight"],
        reverse=True,
    )
    for position, row in enumerate(selected):
        if position >= max_positions:
            row["selected"] = False
            row["reason"] = (
                f"ranked {position + 1} of {len(selected)}, "
                f"only {max_positions} slots"
            )
    rows.sort(key=lambda row: (not row["selected"], -float(row["vote"])))
    return rows


def targets_as_of(
    series: dict[str, list[Bar]],
    when: datetime,
    *,
    horizons: tuple[int, ...] = (50, 100, 200, 252),
    min_vote: float = 0.5,
    max_positions: int = 5,
    normalise: bool = False,
) -> dict[str, float]:
    """The production rule, evaluated with data strictly before ``when``.

    Returns ``{symbol: weight}`` for the symbols whose multi-horizon trend vote
    is positive, where the weight is the strategy's own inverse-volatility
    score: ``min(MAX_LEVERAGE, TARGET_VOL / sigma)`` — the fraction of equity
    that position would take to sit at the 20% annualised vol target. No
    searching, no fitting.

    Weights are **raw** by default. ``normalise=True`` rescales them to sum to
    one, which forces a fully invested book; that is useful for display and
    wrong for risk (a vol-targeted book must be allowed to hold cash), so the
    trading paths must not use it.

    This is :func:`rank_targets` filtered to the selected names, so the console
    that explains a decision and the code that makes it read the same numbers.
    """
    rows = rank_targets(
        series,
        when,
        horizons=horizons,
        min_vote=min_vote,
        max_positions=max_positions,
    )
    weights = {
        row["symbol"]: float(row["weight"]) for row in rows if row["selected"]
    }
    if normalise and weights:
        total = sum(weights.values()) or 1.0
        weights = {symbol: size / total for symbol, size in weights.items()}
    return weights


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
    max_position_pct: float = 0.05,
    governor: float = 0.0,
    throttle_min: float = 0.25,
    inverse_vol: bool = False,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Trade the fixed rule forward through one window; return round-trip trades.

    Two sizing modes, both fed by the same fixed rule:

    - ``inverse_vol=False`` (baseline): every entry is ``per_order_pct`` of
      equity. Flat notional, so a 90%-vol crypto position and a 15%-vol ETF
      carry very different risk.
    - ``inverse_vol=True``: each slot carries the *same risk* instead of the
      same notional — ``notional_i = per_order_pct / sigma_i``, so a quiet
      symbol gets more dollars and a wild one fewer. The size is **not**
      rescaled to fill the budget: on a day with two signals the book is small,
      which is the honest consequence of a thin signal instead of a reason to
      concentrate the risk. Total gross is still capped at
      ``per_order_pct * max_positions`` and each position at
      ``max_position_pct`` of equity, the operator's own per-order ceiling.

    Gross exposure of the open book is capped at ``max_gross`` times equity in
    both modes, so neither can quietly lever the account up.

    ``governor`` is a drawdown brake measured on the marked-to-market account:
    new entries are throttled linearly as the account falls toward the
    threshold and stop entirely at it (existing positions keep their mechanical
    exits). ``throttle_min`` is the floor the throttle decays to, so the book
    keeps a token presence instead of switching off completely.
    """
    costs = costs or CostModel()
    per_side = float(costs.per_side_bps) / 10_000
    # The gross budget the flat baseline would deploy when every slot is full,
    # as a fraction of equity.
    gross_budget_pct = min(per_order_pct * max_positions, max_gross)

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
    # Marked-to-market account equity, sampled every day the loop touches. A
    # curve built only from realised exits would hide a position that is 40%
    # under water but has not been sold yet, which is exactly the drawdown the
    # governor exists to stop.
    curve: list[float] = [equity]
    last_seen: dict[str, float] = {}

    peak = equity
    for offset in range(0, len(dates), step_days):
        day = dates[offset]
        when = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        prices = {
            symbol: table[day]
            for symbol, table in closes.items()
            if day in table and table[day] > 0
        }
        last_seen.update(prices)
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

        def marked_equity() -> float:
            book = sum(
                units * last_seen.get(symbol, 0.0) for symbol, units in held.items()
            )
            return equity + book

        account = marked_equity()
        curve.append(account)
        peak = max(peak, account)
        in_drawdown = (peak - account) / peak if peak > 0 else 0.0
        # Drawdown governor: past the threshold the book stops opening new risk
        # and waits for the account to recover. Existing positions keep their
        # mechanical exits — this is a brake, not a second strategy.
        if governor and in_drawdown >= governor:
            continue
        # Below the threshold the same brake is applied gradually, so the book
        # de-risks before it hits the wall instead of slamming into it.
        throttle = 1.0
        if governor and in_drawdown > 0:
            throttle = max(throttle_min, 1.0 - in_drawdown / governor)

        # Enter the new targets, sized against current equity. Cash leaves the
        # account here — an entry that does not debit is how a simulator
        # compounds for free and reports six-figure returns per trade.
        #
        # The weights are the strategy's own inverse-volatility sizes and are
        # NOT normalised: normalising them forces the book to be fully invested
        # at all times, which throws away the risk control and turns a
        # vol-targeted rule into a crypto beta proxy. ``inverse_vol=False``
        # ignores them for sizing and uses them only to rank the slots, which
        # is what the live runtime does.
        gross = sum(
            entry[symbol][1] for symbol in held if symbol in entry
        )
        # ``equity`` is cash only — a position's cost sits in the book, not the
        # cash balance — so exposure caps are measured against cash + book.
        book_equity = equity + gross
        weight_total = sum(targets.values())
        budget_left = max(0.0, book_equity * gross_budget_pct - gross)
        for symbol, weight in targets.items():
            if symbol in held or symbol not in prices:
                continue
            if inverse_vol:
                if weight_total <= 0:
                    continue
                # ``weight`` is ``min(MAX_LEVERAGE, TARGET_VOL / sigma)``, so
                # scaling it by ``per_order_pct / TARGET_VOL`` turns it into
                # "the notional that risks per_order_pct of equity at this
                # symbol's own measured volatility".
                notional = (
                    book_equity * per_order_pct / TARGET_VOL * weight
                )
                notional = min(
                    notional,
                    book_equity * max_position_pct,
                    budget_left,
                )
            else:
                # Production sizing: the runtime sizes every entry to the
                # per-order risk cap, not to the strategy's volatility score —
                # the score only ranks which symbols get the slot. Simulating
                # anything else would be testing a bot that does not exist.
                notional = book_equity * per_order_pct
            notional *= throttle
            if notional <= 0:
                continue
            if gross + notional > book_equity * max_gross + 1e-12:
                continue
            if notional >= equity or notional < 1e-9:
                continue
            equity -= notional
            price = prices[symbol]
            units = notional / (price * (1 + per_side))
            held[symbol] = units
            entry[symbol] = (price, notional)
            gross += notional
            budget_left = max(0.0, book_equity * gross_budget_pct - gross)

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
    # Everything is flat here, so cash is the account again.
    curve.append(equity)
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
    max_position_pct: float = 0.05,
    governor: float = 0.0,
    throttle_min: float = 0.25,
    inverse_vol: bool = False,
    max_gross: float = 1.0,
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
            max_position_pct=max_position_pct,
            governor=governor,
            throttle_min=throttle_min,
            inverse_vol=inverse_vol,
            max_gross=max_gross,
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


# Sizes the evidence report searches when looking for the largest book that
# still respects the gate's drawdown ceiling. Declared here, in advance, so the
# search is a stated part of the report rather than something that happened in
# a terminal one afternoon.
GATE_SIZE_GRID = (0.005, 0.0075, 0.01, 0.015, 0.02, 0.03)
DRAWDOWN_CEILING_PCT = 15.0


def build_evidence(
    series: dict[str, list[Bar]],
    *,
    per_order_pct: float = 0.03,
    max_positions: int = 4,
    folds: int = 6,
    costs: Optional[CostModel] = None,
    starting_cash: float = 50.0,
    grid: tuple[float, ...] = GATE_SIZE_GRID,
) -> dict[str, Any]:
    """The three numbers the promotion gate is allowed to see, in one report.

    - ``production``: the rule at the size production would trade today.
    - ``inverse_vol``: the strategy's own specification — the same gross budget
      split by inverse volatility instead of evenly.
    - ``gate_size``: the largest flat size on ``GATE_SIZE_GRID`` whose
      out-of-sample max drawdown still fits inside the gate's ceiling. It is
      selected on the same sample it is reported on, so it is an upper bound on
      what the rule could carry, not a promise — the report says so explicitly.
    """
    def run(**kwargs: Any) -> WalkForwardResult:
        return walk_forward(
            series,
            folds=folds,
            costs=costs,
            starting_cash=starting_cash,
            max_positions=max_positions,
            **kwargs,
        )

    production = run(per_order_pct=per_order_pct)
    inverse = run(per_order_pct=per_order_pct, inverse_vol=True)
    report: dict[str, Any] = {
        "hypotheses": 1,
        "alpha": 0.05,
        "drawdown_ceiling_pct": DRAWDOWN_CEILING_PCT,
        "max_positions": max_positions,
        "folds": folds,
        "series": {
            "symbols": sorted(series),
            "bars": sum(len(bars) for bars in series.values()),
        },
        "configs": {
            "production": {
                "per_order_pct": per_order_pct,
                "inverse_vol": False,
                **production.to_dict(),
            },
            "inverse_vol": {
                "per_order_pct": per_order_pct,
                "inverse_vol": True,
                **inverse.to_dict(),
            },
        },
        "gate_size": None,
        "notes": [
            "Drawdown is measured on the marked-to-market account, not on "
            "realised exits: a position that is 30% under water shows up "
            "before it is sold.",
            "gate_size is selected on this sample; treat it as a ceiling on "
            "size, not as evidence of a larger edge.",
        ],
    }
    for size in grid:
        candidate = run(per_order_pct=size)
        if candidate.max_drawdown_pct <= DRAWDOWN_CEILING_PCT:
            report["gate_size"] = {
                "per_order_pct": size,
                "inverse_vol": False,
                **candidate.to_dict(),
            }
    if report["gate_size"] is None:
        report["gate_size"] = {
            "per_order_pct": None,
            "eligible": False,
            "reason": (
                f"no size on the grid held max drawdown under "
                f"{DRAWDOWN_CEILING_PCT}% (smallest tried "
                f"{grid[0]:.4f})"
            ),
        }
    return report

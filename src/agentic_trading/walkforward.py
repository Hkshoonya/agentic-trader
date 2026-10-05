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
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from agentic_trading.backtest import CostModel
from agentic_trading.history import Bar
from agentic_trading.orders import is_crypto_symbol

TARGET_VOL = 0.20
MAX_LEVERAGE = 1.5
EWMA_LAMBDA = 0.94

# The momentum rotation's specification, fixed before its trial began (see the
# 2026-09-23 research note). Per book: the regime symbol, the moving average it
# must close above for the book to hold anything, the momentum lookback in bars,
# and how many names the book holds.
ROTATION_BOOKS = {
    "equity": ("SPY", 200, 63, 3),
    "crypto": ("BTCUSD", 100, 60, 2),
}

# The dip reversal's specification, fixed on design grounds on 2026-09-29
# before any backtest of it was run: the regime symbol and its moving average,
# the moving average each name must itself be above, the pullback lookback in
# bars, and how many names the book holds. Equities only: weekly turnover at the
# crypto spread (~60 bps a side) would cost more than a bounce pays.
REVERSAL_BOOK = ("SPY", 200, 200, 5, 3)

# Which ranking each strategy trades, so the evidence gate and the console
# grade the rule the daemon actually runs. The desk has no single rule: its
# members are judged by their live books, so the walk-forward grades nothing
# (zero trades keeps the promotion gate shut).
STRATEGY_RULES = {
    "trend_crypto": "trend",
    "momentum_rotation": "rotation",
    "dip_reversal": "reversal",
    "desk": "none",
}


# Configurations examined before each rule was fixed. The trend rule came from
# its specification; the rotation was chosen from ~40 variants (6 candidates,
# two 15-cell parameter grids and robustness runs), so its significance bar is
# divided by that count rather than pretending it was the only idea tried.
RULE_HYPOTHESES = {"trend": 1, "rotation": 40, "reversal": 1}


def rule_for_strategy(strategy: str) -> str:
    return STRATEGY_RULES.get(str(strategy or ""), "trend")


@dataclass(frozen=True)
class SmallAccountFloor:
    """Runtime small-account sizing inputs reproduced by the simulator.

    The live loop does not keep a $5 order at a fixed percentage of equity. It
    recomputes the percentage needed for the same dollar target, and its daily
    cap can limit how many such entries are accepted. Keeping those inputs as a
    first-class object prevents the evidence report from accidentally testing a
    compounding percentage book while the daemon runs a fixed-dollar book.
    """

    target_notional: float
    policy_per_order_pct: float
    policy_daily_notional_pct: float
    max_order_pct: float
    max_daily_notional_pct: float
    margin: float = 0.02
    proportional: bool = False

    @property
    def sized_notional(self) -> float:
        return self.target_notional * (1.0 + self.margin)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_notional": self.target_notional,
            "sized_notional": self.sized_notional,
            "margin": self.margin,
            "policy_per_order_pct": self.policy_per_order_pct,
            "policy_daily_notional_pct": self.policy_daily_notional_pct,
            "max_order_pct": self.max_order_pct,
            "max_daily_notional_pct": self.max_daily_notional_pct,
            "proportional": self.proportional,
        }


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
    return_pct: float = 0.0
    bootstrap_p_value: float = 1.0
    significance_method: str = "moving_block_bootstrap_daily_account_returns"
    significance_observations: int = 0
    significance_block_days: int = 20
    alpha: float = 0.05

    @property
    def eligible(self) -> bool:
        return (
            self.trades >= 30
            and self.expectancy_bps > 1.0
            and self.return_pct > 0.0
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
            "return_pct": round(self.return_pct, 4),
            "bootstrap_p_value": round(self.bootstrap_p_value, 4),
            "significance_method": self.significance_method,
            "significance_observations": self.significance_observations,
            "significance_block_days": self.significance_block_days,
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
    # Keep this byte-for-byte equivalent in meaning to the production strategy:
    # evidence must rank the same symbols, not a research approximation.
    var = statistics.pvariance(rets) or 1e-6
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
    rule: str = "trend",
    books: Optional[Mapping[str, tuple[str, int, int, int]]] = None,
    reversal: Optional[tuple[str, int, int, int, int]] = None,
) -> list[dict[str, Any]]:
    """Every symbol's vote and size, chosen or not, for the console.

    :func:`targets_as_of` answers "what would the book be"; this answers "why is
    each name in or out", which is the question an operator actually asks when
    nothing has traded. Both read the same numbers, so the explanation cannot
    drift from the decision.
    """
    if rule == "none":
        return []
    if rule == "rotation":
        return rank_rotation(series, when, max_positions=max_positions, books=books)
    if rule == "reversal":
        return rank_reversal(series, when, max_positions=max_positions, book=reversal)
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
            row["reason"] = f"vote {vote:.2f} ({sum(votes)}/{len(votes)} horizons up)"
            row["selected"] = True
        rows.append(row)
    selected = sorted(
        (row for row in rows if row["selected"]),
        key=lambda row: (
            -(float(row["vote"]) * float(row["weight"])),
            str(row["symbol"]),
        ),
    )
    for position, row in enumerate(selected):
        if position >= max_positions:
            row["selected"] = False
            row["reason"] = (
                f"ranked {position + 1} of {len(selected)}, only {max_positions} slots"
            )
    rows.sort(
        key=lambda row: (
            not row["selected"],
            -(float(row["vote"]) * float(row["weight"])),
            str(row["symbol"]),
        )
    )
    return rows


def rotation_anchor(when: datetime) -> datetime:
    """Monday 00:00 UTC of ``when``'s week: the rotation decides once a week.

    Every quote in a week ranks on the same bars, so the book changes on the
    weekly boundary and nowhere else — the cadence the rule was tested at —
    without a second clock beside the daemon's daily decision.
    """
    stamp = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    day = stamp.astimezone(timezone.utc).date()
    monday = day - timedelta(days=day.weekday())
    return datetime.combine(monday, time.min, tzinfo=timezone.utc)


def rank_rotation(
    series: dict[str, list[Bar]],
    when: datetime,
    *,
    max_positions: int = 5,
    books: Optional[Mapping[str, tuple[str, int, int, int]]] = None,
) -> list[dict[str, Any]]:
    """Weekly momentum rotation: each book holds its strongest recent names.

    Per book (equity, crypto): rank members by their lookback return using bars
    strictly before this week's anchor, and hold the top few whose return is
    positive — but only while the book's regime symbol closes above its moving
    average. A missing regime symbol means the book holds nothing: no guessing.
    Every selected name has weight 1.0, one full per-order budget.
    """
    anchor = rotation_anchor(when)
    closes = {
        symbol: [float(bar.close) for bar in bars if bar.start < anchor]
        for symbol, bars in series.items()
    }

    def key(symbol: str) -> str:
        return symbol.replace("-", "").upper()

    rows: list[dict[str, Any]] = []
    for book, (regime_symbol, regime_bars, lookback, top) in (books or ROTATION_BOOKS).items():
        crypto = book == "crypto"
        regime = next(
            (
                values
                for symbol, values in closes.items()
                if key(symbol) == regime_symbol
            ),
            None,
        )
        risk_on = (
            regime is not None
            and len(regime) >= regime_bars
            and regime[-1] > sum(regime[-regime_bars:]) / regime_bars
        )
        ranked: list[dict[str, Any]] = []
        for symbol, values in closes.items():
            if is_crypto_symbol(symbol) != crypto:
                continue
            row: dict[str, Any] = {
                "symbol": symbol,
                "bars": len(values),
                "vote": 0.0,
                "vol_pct": None,
                "weight": 0.0,
                "selected": False,
                "book": book,
            }
            rows.append(row)
            if len(values) < lookback + 1 or values[-1 - lookback] <= 0:
                row["reason"] = f"only {len(values)} bars (needs {lookback + 1})"
                continue
            momentum = values[-1] / values[-1 - lookback] - 1
            row["vote"] = round(momentum, 4)
            if not risk_on:
                row["reason"] = (
                    f"{regime_symbol} is not above its {regime_bars}-day average, "
                    f"so the {book} book holds cash"
                )
            elif momentum <= 0:
                row["reason"] = f"{lookback}-day return {momentum:+.1%} is not positive"
            else:
                ranked.append(row)
        ranked.sort(key=lambda row: (-float(row["vote"]), str(row["symbol"])))
        for position, row in enumerate(ranked):
            if position < top:
                row["selected"] = True
                row["weight"] = 1.0
                row["reason"] = (
                    f"#{position + 1} {book} name by {lookback}-day return "
                    f"{float(row['vote']):+.1%}"
                )
            else:
                row["reason"] = (
                    f"{lookback}-day return {float(row['vote']):+.1%} ranks "
                    f"{position + 1}; the {book} book holds {top}"
                )
    selected = sorted(
        (row for row in rows if row["selected"]),
        key=lambda row: (-float(row["vote"]), str(row["symbol"])),
    )
    for position, row in enumerate(selected):
        if position >= max_positions:
            row["selected"] = False
            row["weight"] = 0.0
            row["reason"] = f"ranked {position + 1}, only {max_positions} slots"
    rows.sort(
        key=lambda row: (not row["selected"], -float(row["vote"]), str(row["symbol"]))
    )
    return rows


def rank_reversal(
    series: dict[str, list[Bar]],
    when: datetime,
    *,
    max_positions: int = 5,
    book: Optional[tuple[str, int, int, int, int]] = None,
) -> list[dict[str, Any]]:
    """Weekly dip reversal: hold last week's deepest pullbacks in rising names.

    On bars strictly before this week's anchor: while SPY closes above its
    200-day average, rank the equities that close above their own 200-day
    average by their 5-bar return, and hold the most negative few. A name
    that rose, a name in a downtrend and every coin are left out. Each
    selected name has weight 1.0, one full per-order budget, like the rotation.
    """
    regime_symbol, regime_bars, trend_bars, lookback, top = book or REVERSAL_BOOK
    anchor = rotation_anchor(when)
    closes = {
        symbol: [float(bar.close) for bar in bars if bar.start < anchor]
        for symbol, bars in series.items()
    }
    regime = next(
        (
            values
            for symbol, values in closes.items()
            if symbol.replace("-", "").upper() == regime_symbol
        ),
        None,
    )
    risk_on = (
        regime is not None
        and len(regime) >= regime_bars
        and regime[-1] > sum(regime[-regime_bars:]) / regime_bars
    )
    rows: list[dict[str, Any]] = []
    ranked: list[dict[str, Any]] = []
    for symbol, values in closes.items():
        if is_crypto_symbol(symbol):
            continue
        row: dict[str, Any] = {
            "symbol": symbol,
            "bars": len(values),
            "vote": 0.0,
            "vol_pct": None,
            "weight": 0.0,
            "selected": False,
            "book": "equity",
        }
        rows.append(row)
        needed = max(trend_bars, lookback + 1)
        if len(values) < needed or values[-1 - lookback] <= 0:
            row["reason"] = f"only {len(values)} bars (needs {needed})"
            continue
        pullback = values[-1] / values[-1 - lookback] - 1
        row["vote"] = round(pullback, 4)
        if not risk_on:
            row["reason"] = (
                f"{regime_symbol} is not above its {regime_bars}-day average, "
                "so the dip book holds cash"
            )
        elif values[-1] <= sum(values[-trend_bars:]) / trend_bars:
            row["reason"] = (
                f"below its own {trend_bars}-day average: a fall, not a dip"
            )
        elif pullback >= 0:
            row["reason"] = f"{lookback}-day return {pullback:+.1%} is no pullback"
        else:
            ranked.append(row)
    ranked.sort(key=lambda row: (float(row["vote"]), str(row["symbol"])))
    for position, row in enumerate(ranked):
        if position < min(top, max_positions):
            row["selected"] = True
            row["weight"] = 1.0
            row["reason"] = (
                f"#{position + 1} pullback: {lookback}-day return "
                f"{float(row['vote']):+.1%} in an uptrend"
            )
        else:
            row["reason"] = (
                f"{lookback}-day pullback {float(row['vote']):+.1%} ranks "
                f"{position + 1}; the dip book holds {top}"
            )
    rows.sort(
        key=lambda row: (not row["selected"], float(row["vote"]), str(row["symbol"]))
    )
    return rows


def targets_as_of(
    series: dict[str, list[Bar]],
    when: datetime,
    *,
    horizons: tuple[int, ...] = (50, 100, 200, 252),
    min_vote: float = 0.5,
    max_positions: int = 5,
    normalise: bool = False,
    rule: str = "trend",
    books: Optional[Mapping[str, tuple[str, int, int, int]]] = None,
    reversal: Optional[tuple[str, int, int, int, int]] = None,
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
        rule=rule,
        books=books,
        reversal=reversal,
    )
    weights = {
        row["symbol"]: round(float(row["weight"]), 6) for row in rows if row["selected"]
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
    proportional: bool = False,
    small_account_floor: Optional[SmallAccountFloor] = None,
    rule: str = "trend",
    horizons: tuple[int, ...] = (50, 100, 200, 252),
    min_vote: float = 0.5,
    books: Optional[Mapping[str, tuple[str, int, int, int]]] = None,
    reversal: Optional[tuple[str, int, int, int, int]] = None,
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

    ``small_account_floor`` reproduces the daemon's conditional fixed-dollar
    sizing. The effective order and daily percentages are re-derived from
    marked-to-market equity each day, weighted entries are raised only to the
    useful minimum the live sizer would use, and entries beyond that day's
    opening-notional cap are skipped.

    ``governor`` is a drawdown brake measured on the marked-to-market account:
    new entries are throttled linearly as the account falls toward the
    threshold and stop entirely at it (existing positions keep their mechanical
    exits). ``throttle_min`` is the floor the throttle decays to, so the book
    keeps a token presence instead of switching off completely.
    """
    costs = costs or CostModel()
    # Per-symbol (per_side, fee): crypto and equities can cost differently.
    side_costs: dict[str, tuple[float, float]] = {}

    def cost_of(symbol: str) -> tuple[float, float]:
        if symbol not in side_costs:
            model = costs.for_symbol(symbol)
            side_costs[symbol] = (
                float(model.per_side_bps) / 10_000,
                max(0.0, float(model.fee_per_order)),
            )
        return side_costs[symbol]
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
        targets = targets_as_of(series, when, horizons=horizons, min_vote=min_vote, max_positions=max_positions,
                                rule=rule, books=books, reversal=reversal)

        # Exit anything no longer targeted. Positions are held as units, so the
        # entry price already contains the entry cost and the exit price the
        # exit cost: neither side can be double counted or forgotten.
        for symbol in list(held):
            if symbol in targets or symbol not in prices:
                continue
            price = prices[symbol]
            units = held.pop(symbol)
            entry_price, notional = entry.pop(symbol)
            per_side, fee_per_order = cost_of(symbol)
            proceeds = units * price * (1 - per_side) - fee_per_order
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

        book = sum(units * last_seen.get(symbol, 0.0) for symbol, units in held.items())
        account = equity + book
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
        gross = sum(entry[symbol][1] for symbol in held if symbol in entry)
        # ``equity`` is cash only — a position's value sits in the book, not the
        # cash balance — so sizing uses current marked-to-market account value.
        # Using entry cost here would freeze the equity input after a position
        # moved and diverge from the broker equity the runtime sizes against.
        book_equity = account
        weight_total = sum(targets.values())
        budget_left = max(0.0, book_equity * gross_budget_pct - gross)
        runtime_order_pct = per_order_pct
        runtime_daily_pct: Optional[float] = None
        if small_account_floor is not None and book_equity > 0:
            needed = small_account_floor.sized_notional / book_equity
            runtime_order_pct = small_account_floor.policy_per_order_pct
            runtime_daily_pct = small_account_floor.policy_daily_notional_pct
            if needed > runtime_order_pct:
                if (
                    needed <= small_account_floor.max_order_pct
                    and needed <= small_account_floor.max_daily_notional_pct
                ):
                    runtime_order_pct = needed
                    runtime_daily_pct = min(
                        max(runtime_daily_pct, needed),
                        small_account_floor.max_daily_notional_pct,
                    )
        daily_opening_notional = 0.0
        for symbol, weight in targets.items():
            if symbol in held or symbol not in prices:
                continue
            if proportional:
                # The operator's per-order budget is the ceiling; the strategy's
                # inverse-vol weight decides how much of it this symbol takes.
                # A 60%-vol pair gets a third of what a 20%-vol name gets, which
                # is the point of weighting them in the first place.
                notional = book_equity * runtime_order_pct * min(1.0, weight)
            elif inverse_vol:
                if weight_total <= 0:
                    continue
                # ``weight`` is ``min(MAX_LEVERAGE, TARGET_VOL / sigma)``, so
                # scaling it by ``per_order_pct / TARGET_VOL`` turns it into
                # "the notional that risks per_order_pct of equity at this
                # symbol's own measured volatility".
                notional = book_equity * runtime_order_pct / TARGET_VOL * weight
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
                notional = book_equity * runtime_order_pct

            if small_account_floor is not None:
                # Mirror size_intent's weighted-floor rule. A volatile symbol's
                # share may fall below the useful minimum, but the unweighted
                # operator cap can authorise exactly the floor — never more.
                unweighted_cap = book_equity * runtime_order_pct
                if (
                    proportional
                    and notional < small_account_floor.sized_notional
                    and unweighted_cap + 1e-12 >= small_account_floor.target_notional
                ):
                    notional = min(unweighted_cap, small_account_floor.sized_notional)
            notional *= throttle
            if notional <= 0:
                continue
            if (
                small_account_floor is not None
                and notional + 1e-12 < small_account_floor.target_notional
            ):
                # This is the same fail-closed result as quantity rounding below
                # the runtime's effective minimum. In particular, it stops the
                # floor when a falling account no longer fits the operator's
                # percentage ceiling.
                continue
            if (
                runtime_daily_pct is not None
                and daily_opening_notional + notional
                > book_equity * runtime_daily_pct + 1e-12
            ):
                continue
            if gross + notional > book_equity * max_gross + 1e-12:
                continue
            per_side, fee_per_order = cost_of(symbol)
            if notional >= equity or notional <= fee_per_order:
                continue
            equity -= notional
            price = prices[symbol]
            # ``notional`` is the total cash budget, including the entry fee,
            # matching both the live small-account constraint and backtest.py.
            units = (notional - fee_per_order) / (price * (1 + per_side))
            held[symbol] = units
            entry[symbol] = (price, notional)
            gross += notional
            daily_opening_notional += notional
            budget_left = max(0.0, book_equity * gross_budget_pct - gross)

    # Close whatever is still open at the end of the window, at its last close.
    # The window's last day is often one only coins trade on (a weekend), so a
    # stock has no bar that day; skipping it would drop the position from the
    # account, which is how held equities once vanished at every window end.
    last = dates[-1]
    for symbol in list(held):
        table = closes.get(symbol) or {}
        price = table.get(last) or last_seen.get(symbol)
        if price is None:
            continue
        units = held.pop(symbol)
        entry_price, notional = entry.pop(symbol)
        per_side, fee_per_order = cost_of(symbol)
        proceeds = units * price * (1 - per_side) - fee_per_order
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


def bootstrap_p(
    returns_bps: list[float], *, samples: int = 2000, seed: int = 7
) -> float:
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


SIGNIFICANCE_BLOCK_DAYS = 20


def moving_block_bootstrap_p(
    returns_bps: list[float],
    *,
    block_days: int = SIGNIFICANCE_BLOCK_DAYS,
    samples: int = 2000,
    seed: int = 7,
) -> float:
    """Probability that mean account return is non-positive by block bootstrap.

    Trade outcomes from the same market regime are not independent observations.
    Resampling individual trades makes a correlated crypto book look as though it
    has far more evidence than it does. This resamples contiguous *daily account*
    returns in circular blocks, preserving short-run clustering while producing
    samples of the same length as the observed path.
    """
    size = len(returns_bps)
    if size < 5 or block_days < 1 or size < block_days:
        return 1.0
    width = min(size, int(block_days))
    rng = random.Random(seed)
    worse = 0
    for _ in range(samples):
        draw: list[float] = []
        while len(draw) < size:
            start = rng.randrange(size)
            draw.extend(returns_bps[(start + offset) % size] for offset in range(width))
        if sum(draw[:size]) / size <= 0:
            worse += 1
    return (worse + 1) / (samples + 1)


def _curve_returns_bps(curve: list[float]) -> list[float]:
    """Convert a marked-to-market account curve to consecutive returns."""
    out: list[float] = []
    for previous, current in zip(curve, curve[1:], strict=False):
        if previous <= 0:
            continue
        out.append((current / previous - 1.0) * 10_000)
    return out


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
    proportional: bool = False,
    max_gross: float = 1.0,
    small_account_floor: Optional[SmallAccountFloor] = None,
    rule: str = "trend",
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
            proportional=proportional,
            max_gross=max_gross,
            small_account_floor=small_account_floor,
            rule=rule,
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
        result.profit_factor = gross_win / gross_loss if gross_loss else float("inf")
        result.max_drawdown_pct = _curve_drawdown(equity_curve)
    result.final_equity = equity
    result.return_pct = (
        (equity / starting_cash - 1.0) * 100 if starting_cash > 0 else 0.0
    )
    account_returns = _curve_returns_bps(equity_curve)
    result.significance_observations = len(account_returns)
    result.significance_block_days = SIGNIFICANCE_BLOCK_DAYS
    result.bootstrap_p_value = moving_block_bootstrap_p(
        account_returns,
        block_days=SIGNIFICANCE_BLOCK_DAYS,
    )
    result.alpha = 0.05
    return result


# Sizes the evidence report searches when looking for the largest book that
# still respects the gate's drawdown ceiling. Declared here, in advance, so the
# search is a stated part of the report rather than something that happened in
# a terminal one afternoon.
GATE_SIZE_GRID = (0.005, 0.0075, 0.01, 0.015, 0.02, 0.03)
DRAWDOWN_CEILING_PCT = 15.0
COST_STRESS_MULTIPLIER = Decimal("2")


def build_evidence(
    series: dict[str, list[Bar]],
    *,
    per_order_pct: float = 0.03,
    max_positions: int = 4,
    folds: int = 6,
    costs: Optional[CostModel] = None,
    starting_cash: float = 50.0,
    grid: tuple[float, ...] = GATE_SIZE_GRID,
    proportional: bool = False,
    small_account_floor: Optional[SmallAccountFloor] = None,
    rule: str = "trend",
) -> dict[str, Any]:
    """The three numbers the promotion gate is allowed to see, in one report.

    - ``production``: the rule at the size production would trade today.
    - ``inverse_vol``: the strategy's own specification — the same gross budget
      split by inverse volatility instead of evenly.
    - ``gate_size``: the largest candidate size in the configured sizing mode
      whose out-of-sample max drawdown still fits inside the gate's ceiling.
      The exact production point uses the fixed-dollar floor model when one is
      configured. It is selected on the same sample it is reported on, so it is
      an upper bound on what the rule could carry, not a promise.
    """

    def run(*, use_floor: bool = False, **kwargs: Any) -> WalkForwardResult:
        # ``costs`` may be overridden per run (the gross pass switches costs off),
        # so it is popped rather than passed twice.
        return walk_forward(
            series,
            folds=folds,
            costs=kwargs.pop("costs", costs),
            starting_cash=starting_cash,
            max_positions=max_positions,
            proportional=proportional,
            small_account_floor=(small_account_floor if use_floor else None),
            rule=rule,
            **kwargs,
        )

    base_costs = costs or CostModel()
    production = run(per_order_pct=per_order_pct, use_floor=True)
    inverse = run(per_order_pct=per_order_pct, inverse_vol=True, use_floor=True)
    # What the edge survives in costs. The gate grades with an assumed cost
    # model, and an edge whose break-even is below realistic costs is not an
    # edge — so the gross expectancy is measured once with costs switched off
    # and the break-even implied by it is reported next to the net number.
    gross = run(
        per_order_pct=per_order_pct,
        costs=CostModel(spread_bps=Decimal("0"), slippage_bps=Decimal("0")),
        use_floor=True,
    )
    stressed_costs = base_costs.scaled(COST_STRESS_MULTIPLIER)
    stressed = run(
        per_order_pct=per_order_pct,
        costs=stressed_costs,
        use_floor=True,
    )
    report: dict[str, Any] = {
        "schema_version": 4,
        "rule": rule,
        "hypotheses": RULE_HYPOTHESES.get(rule, 1),
        "alpha": 0.05,
        "sizing": (
            "small_account_floor"
            if small_account_floor is not None
            else ("proportional" if proportional else "flat")
        ),
        "base_sizing": "proportional" if proportional else "flat",
        "drawdown_ceiling_pct": DRAWDOWN_CEILING_PCT,
        "max_positions": max_positions,
        "folds": folds,
        "starting_equity": round(starting_cash, 4),
        "series": {
            "symbols": sorted(series),
            "bars": sum(len(bars) for bars in series.values()),
        },
        "configs": {
            "production": {
                "per_order_pct": per_order_pct,
                "inverse_vol": False,
                "sizing": (
                    "small_account_floor"
                    if small_account_floor is not None
                    else ("proportional" if proportional else "flat")
                ),
                **production.to_dict(),
            },
            "inverse_vol": {
                "per_order_pct": per_order_pct,
                "inverse_vol": True,
                **inverse.to_dict(),
            },
        },
        "gate_size": None,
        "cost_stress": {
            "multiplier": float(COST_STRESS_MULTIPLIER),
            "per_side_bps": float(stressed_costs.per_side_bps),
            "fee_per_order_usd": float(stressed_costs.fee_per_order),
            **stressed.to_dict(),
        },
        "costs": {
            "gross_expectancy_bps": round(gross.expectancy_bps, 3),
            "net_expectancy_bps": round(production.expectancy_bps, 3),
            "break_even_per_side_bps": round(gross.expectancy_bps / 2, 3),
            "assumed_per_side_bps": float(((costs or CostModel()).per_side_bps)),
            "assumed_fee_per_order_usd": float((costs or CostModel()).fee_per_order),
            "assumed_round_trip_fixed_usd": float(
                (costs or CostModel()).fee_per_order * 2
            ),
            # The crypto book's own charge (equal to the fields above when no
            # separate crypto model is in force).
            "assumed_crypto_per_side_bps": float(
                (costs or CostModel()).for_symbol("BTC-USD").per_side_bps
            ),
            "assumed_crypto_fee_per_order_usd": float(
                (costs or CostModel()).for_symbol("BTC-USD").fee_per_order
            ),
            "trades": production.trades,
        },
        "notes": [
            "Drawdown is measured on the marked-to-market account, not on "
            "realised exits: a position that is 30% under water shows up "
            "before it is sold.",
            "That size is chosen on this sample; treat it as a ceiling on how "
            "much to trade, not as evidence of a larger edge.",
            "Promotion also requires the same rule and size to remain profitable "
            "when every modeled execution cost is doubled.",
            "Significance is block-bootstrapped from daily marked-to-market "
            "account returns, not from trades treated as independent bets.",
        ],
    }
    if small_account_floor is not None:
        report["small_account_floor"] = small_account_floor.to_dict()
        report["notes"].append(
            "Production reproduces the runtime's fixed-dollar small-account "
            "target and daily opening-notional cap; it does not compound the "
            "initial floor percentage as equity changes."
        )
    frontier: list[dict[str, Any]] = []
    # Always grade the exact production size. A small-account floor can be
    # 10.22% rather than one of the pre-registered frontier points; omitting it
    # would test the $5 order in the headline while selecting the drawdown gate
    # from unrelated 0.5%-3% positions.
    tested_sizes = sorted({*grid, float(per_order_pct)})
    for size in tested_sizes:
        is_production_size = math.isclose(
            size,
            float(per_order_pct),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        candidate_uses_floor = bool(small_account_floor and is_production_size)
        candidate = run(
            per_order_pct=size,
            use_floor=candidate_uses_floor,
        )
        frontier.append(
            {
                "per_order_pct": size,
                "sizing": (
                    "small_account_floor"
                    if candidate_uses_floor
                    else ("proportional" if proportional else "flat")
                ),
                "trades": candidate.trades,
                "expectancy_bps": round(candidate.expectancy_bps, 3),
                "max_drawdown_pct": round(candidate.max_drawdown_pct, 3),
                "final_equity": round(candidate.final_equity, 4),
                "inside_ceiling": candidate.max_drawdown_pct <= DRAWDOWN_CEILING_PCT,
            }
        )
        if candidate.max_drawdown_pct <= DRAWDOWN_CEILING_PCT:
            report["gate_size"] = {
                "per_order_pct": size,
                "inverse_vol": False,
                "sizing": (
                    "small_account_floor"
                    if candidate_uses_floor
                    else ("proportional" if proportional else "flat")
                ),
                **candidate.to_dict(),
            }
    # The measured cost of sizing up, so "temporarily bigger bets on a small
    # account" is a stated trade with a number attached, not a vibe.
    report["size_frontier"] = sorted(frontier, key=lambda row: row["per_order_pct"])
    if report["gate_size"] is None:
        report["gate_size"] = {
            "per_order_pct": None,
            "eligible": False,
            "reason": (
                f"no size on the grid held max drawdown under "
                f"{DRAWDOWN_CEILING_PCT}% (smallest tried "
                f"{tested_sizes[0]:.4f})"
            ),
        }
    return report

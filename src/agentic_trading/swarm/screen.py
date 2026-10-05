"""The birth screen: a filter, never evidence.

A recipe must have made money after costs over the three years before its
birth, with enough trades to mean something and a survivable drawdown. That
history is the same one every other recipe was screened on, so passing proves
nothing; only forward results count. The screen just stops obvious losers and
near-copies of living agents from taking a slot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from agentic_trading.history import Bar
from agentic_trading.swarm.life import Agent, as_datetime, daily_returns, drawdown_pct, through, window_dates
from agentic_trading.swarm.recipe import Recipe, sim_kwargs, universe_series
from agentic_trading.walkforward import simulate

SCREEN_YEARS = 3
MIN_TRADES = 20
MAX_DRAWDOWN_PCT = 30.0
DUPLICATE_CORRELATION = 0.9
SIGNATURE_DAYS = 120
MIN_OVERLAP = 60


@dataclass(frozen=True)
class Screen:
    passed: bool
    reason: str
    trades: int = 0
    return_pct: float = 0.0
    drawdown_pct: float = 0.0
    signature: dict[str, float] = field(default_factory=dict)


def screen(recipe: Recipe, series: dict[str, list[Bar]], birth: date, *, costs: Any, cash: Any) -> Screen:
    last = birth - timedelta(days=1)
    start = last - timedelta(days=365 * SCREEN_YEARS)
    scoped = universe_series(through(series, last), recipe.universe)
    days = window_dates(scoped, start, last)
    trades, curve = simulate(scoped, start=as_datetime(start), end=as_datetime(last), costs=costs,
                             starting_cash=float(cash), **sim_kwargs(recipe))
    if not curve:
        return Screen(False, "no history to screen on")
    returns = daily_returns(curve, len(days))
    tail = list(zip(days[: len(returns)], returns))[-SIGNATURE_DAYS:]
    signature = {d.isoformat(): r for d, r in tail}
    total = round((curve[-1] / curve[0] - 1) * 100, 3) if curve[0] > 0 else 0.0
    worst = _curve_drawdown(curve)
    result = dict(trades=len(trades), return_pct=total, drawdown_pct=worst, signature=signature)
    if len(trades) < MIN_TRADES:
        return Screen(False, f"only {len(trades)} trades in {SCREEN_YEARS} years (needs {MIN_TRADES})", **result)
    if curve[-1] <= curve[0]:
        return Screen(False, f"lost {total:+.1f}% after costs", **result)
    if worst >= MAX_DRAWDOWN_PCT:
        return Screen(False, f"drawdown {worst:.0f}% (limit {MAX_DRAWDOWN_PCT:.0f}%)", **result)
    return Screen(True, "passed", **result)


def _curve_drawdown(curve: list[float]) -> float:
    returns = [curve[i + 1] / curve[i] - 1 if curve[i] > 0 else 0.0 for i in range(len(curve) - 1)]
    return drawdown_pct(returns)


def correlation(a: dict[str, float], b: dict[str, float], min_overlap: int = MIN_OVERLAP) -> Optional[float]:
    shared = sorted(set(a) & set(b))
    if len(shared) < min_overlap:
        return None
    x = [a[k] for k in shared]
    y = [b[k] for k in shared]
    mx, my = math.fsum(x) / len(x), math.fsum(y) / len(y)
    sxy = math.fsum((p - mx) * (q - my) for p, q in zip(x, y))
    sxx = math.fsum((p - mx) ** 2 for p in x)
    syy = math.fsum((q - my) ** 2 for q in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def duplicate_of(signature: dict[str, float], living: Iterable[Agent]) -> Optional[str]:
    for agent in living:
        value = correlation(signature, agent.signature)
        if value is not None and value > DUPLICATE_CORRELATION:
            return agent.recipe.name
    return None

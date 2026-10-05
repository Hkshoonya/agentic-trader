"""An agent's forward life: the same ``simulate`` as its screen, on bars after its birth only.

A recipe is frozen when it is born. Its record is rebuilt each step from its
birth to the step's last finished day, so a restarted machine loses nothing and
the record can always be reproduced from ``data/bars``. Its excess is measured
against the desk's benchmark, 60% QQQ / 40% BTC, so swarm numbers line up with
the allocator's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from agentic_trading.history import Bar
from agentic_trading.swarm.recipe import Recipe, rule_kwargs, sim_kwargs, universe_series, validate
from agentic_trading.walkforward import TARGET_VOL, simulate, targets_as_of

BENCHMARK = (("QQQ", 0.6), ("BTCUSD", 0.4))


def as_datetime(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)


def through(series: dict[str, list[Bar]], day: date) -> dict[str, list[Bar]]:
    trimmed = {s: [b for b in bars if b.start.date() <= day] for s, bars in series.items()}
    return {s: bars for s, bars in trimmed.items() if bars}


def window_dates(series: dict[str, list[Bar]], start: date, end: date) -> list[date]:
    """The days ``simulate`` walks for this series and window (its own rule, repeated)."""
    days = {bar.start.date() for bars in series.values() for bar in bars}
    return sorted(d for d in days if start <= d <= end)


def daily_returns(curve: list[float], days: int) -> list[float]:
    """``simulate``'s curve is [cash, one mark per day..., cash after closing]; returns per day."""
    values = curve[: days + 1]
    return [values[i + 1] / values[i] - 1 if values[i] > 0 else 0.0 for i in range(len(values) - 1)]


def drawdown_pct(returns: Iterable[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for r in returns:
        equity *= 1 + r
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak if peak > 0 else 0.0)
    return round(worst * 100, 3)


@dataclass(frozen=True)
class Record:
    days: tuple[str, ...] = ()
    returns: tuple[float, ...] = ()
    excess: tuple[float, ...] = ()
    trades: int = 0
    drawdown_pct: float = 0.0
    return_pct: float = 0.0

    @property
    def excess_pct(self) -> float:
        return round(math.fsum(self.excess) * 100, 3)

    def upto(self, day: str) -> "Record":
        """The record as it stood before ``day`` (ISO date): what a past day could have known."""
        keep = sum(1 for d in self.days if d < day)
        returns = self.returns[:keep]
        return Record(self.days[:keep], returns, self.excess[:keep], self.trades,
                      drawdown_pct(returns), _total_pct(returns))


def _total_pct(returns: Iterable[float]) -> float:
    return round((math.prod(1 + r for r in returns) - 1) * 100, 3)


def benchmark_returns(series: dict[str, list[Bar]], days: list[date]) -> list[float]:
    out = [0.0] * len(days)
    for symbol, share in BENCHMARK:
        closes = sorted((b.start.date(), float(b.close)) for b in series.get(symbol, []) if b.close > 0)
        index, previous = 0, None
        table = dict(closes)
        for position, day in enumerate(days):
            while index < len(closes) and closes[index][0] < day:
                previous = closes[index][1]
                index += 1
            if day in table and previous:
                out[position] += share * (table[day] / previous - 1)
    return out


def forward_record(recipe: Recipe, series: dict[str, list[Bar]], birth: date, as_of: date, *,
                   costs: Any, cash: Any) -> Record:
    if as_of < birth:
        return Record()
    scoped = universe_series(through(series, as_of), recipe.universe)
    days = window_dates(scoped, birth, as_of)
    trades, curve = simulate(scoped, start=as_datetime(birth), end=as_datetime(as_of), costs=costs,
                             starting_cash=float(cash), **sim_kwargs(recipe))
    if not curve:
        return Record()
    returns = daily_returns(curve, len(days))
    bench = benchmark_returns(series, days[: len(returns)])
    return Record(tuple(d.isoformat() for d in days[: len(returns)]), tuple(returns),
                  tuple(r - b for r, b in zip(returns, bench)), len(trades),
                  drawdown_pct(returns), _total_pct(returns))


def agent_weights(recipe: Recipe, series: dict[str, list[Bar]], day: date) -> dict[str, float]:
    """What the agent would hold on ``day``, as fractions of its book (P4)."""
    targets = targets_as_of(universe_series(series, recipe.universe), as_datetime(day), **rule_kwargs(recipe))
    size = recipe.per_order_pct
    weights = {s: (min(size, size / TARGET_VOL * w) if recipe.inverse_vol else size) for s, w in targets.items()}
    weights = {s: w for s, w in weights.items() if w > 0}
    total = sum(weights.values())
    return {s: w / total for s, w in weights.items()} if total > 1 else weights


@dataclass
class Agent:
    recipe: Recipe
    born: str
    signature: dict[str, float] = field(default_factory=dict)
    errored: str = ""
    record: Record = field(default_factory=Record)

    def to_row(self) -> dict[str, Any]:
        return {"recipe": self.recipe.to_dict(), "born": self.born, "signature": self.signature,
                "errored": self.errored}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Agent":
        return cls(validate(row["recipe"]), str(row["born"]),
                   {str(k): float(v) for k, v in (row.get("signature") or {}).items()},
                   str(row.get("errored") or ""))

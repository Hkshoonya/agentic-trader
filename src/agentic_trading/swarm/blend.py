"""The swarm's book, and the cull.

An agent earns a share of the book only after 20 forward days, and only while
its last 60 forward days beat the benchmark. Shares are inverse to each
contributor's own forward volatility, so a wild agent doesn't drown the calm
ones. With no contributor, the book holds the benchmark, so the swarm's excess
comes from its agents alone.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Iterable

from agentic_trading.history import Bar
from agentic_trading.swarm.life import Agent, agent_weights

TRAILING_DAYS = 60
BENCHMARK_WEIGHTS = {"QQQ": 0.6, "BTCUSD": 0.4}
MONDAY = 0


def contributing(agent: Agent, *, nursery_days: int) -> bool:
    record = agent.record
    return (not agent.errored and len(record.days) >= nursery_days
            and math.fsum(record.excess[-TRAILING_DAYS:]) > 0)


def _vol(returns: tuple[float, ...]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = math.fsum(returns) / len(returns)
    return math.sqrt(math.fsum((r - mean) ** 2 for r in returns) / len(returns))


def shares(agents: Iterable[Agent], *, nursery_days: int) -> dict[str, float]:
    raw = {}
    for agent in agents:
        vol = _vol(agent.record.returns)
        if contributing(agent, nursery_days=nursery_days) and vol > 0:
            raw[agent.recipe.id] = 1 / vol
    total = math.fsum(raw.values())
    return {key: value / total for key, value in raw.items()} if total > 0 else {}


def blend_weights(agents: Iterable[Agent], agent_shares: dict[str, float], series: dict[str, list[Bar]],
                  day: date) -> dict[str, float]:
    if not agent_shares:
        return dict(BENCHMARK_WEIGHTS)
    total: dict[str, float] = {}
    for agent in agents:
        share = agent_shares.get(agent.recipe.id, 0.0)
        if share <= 0:
            continue
        for symbol, weight in agent_weights(agent.recipe, series, day).items():
            total[symbol] = total.get(symbol, 0.0) + share * weight
    return {symbol: round(weight, 9) for symbol, weight in total.items() if weight > 0}


def cull(agents: Iterable[Agent], *, today: date, max_drawdown_pct: float,
         cull_after_days: int) -> list[tuple[Agent, str]]:
    alive = list(agents)
    deaths = [(a, f"drawdown {a.record.drawdown_pct:.0f}% (limit {max_drawdown_pct:.0f}%)")
              for a in alive if a.record.drawdown_pct >= max_drawdown_pct]
    if today.weekday() != MONDAY:  # P1: the quarter cull is weekly
        return deaths
    dead = {id(a) for a, _ in deaths}
    mature = [a for a in alive if id(a) not in dead and not a.errored and len(a.record.days) >= cull_after_days]
    if len(mature) < 4:
        return deaths
    mature.sort(key=lambda a: (a.record.excess_pct, a.recipe.id))
    for agent in mature[: len(mature) // 4]:
        if agent.record.excess_pct <= 0:
            deaths.append((agent, f"bottom quarter after {len(agent.record.days)} days: "
                                  f"{agent.record.excess_pct:+.1f}% vs the benchmark"))
    return deaths

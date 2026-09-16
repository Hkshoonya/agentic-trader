"""Evolutionary strategy search with an honest held-out validation split.

The search runs on a training slice only. The champion is then validated on a
slice the search never saw. Selection pressure inflates in-sample results, so
only the out-of-sample numbers are allowed to justify promotion.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from agentic_trading.backtest import (
    CostModel,
    Genome,
    Metrics,
    fold_metrics,
    run_backtest,
)
from agentic_trading.history import Bar

RANGES: dict[str, tuple[int, int]] = {
    "lookback": (1, 20),
    "entry_bps": (2, 200),
    "tp_bps": (20, 800),
    "sl_bps": (20, 800),
    "max_hold_bars": (2, 40),
    "vol_window": (5, 30),
    "max_vol_bps": (50, 800),
    "trend_window": (10, 120),
    "position_pct": (50, 100),
}

_INT_FIELDS = tuple(RANGES)


def random_genome(rng: random.Random) -> Genome:
    return Genome(
        mode=rng.choice(["momentum", "mean_reversion"]),
        **{name: rng.randint(*bounds) for name, bounds in RANGES.items()},
        use_trend_filter=rng.random() < 0.7,
    )


def mutate(genome: Genome, rng: random.Random, *, rate: float = 0.35) -> Genome:
    values = genome.to_dict()
    if rng.random() < rate:
        values["mode"] = rng.choice(["momentum", "mean_reversion"])
    for name, (low, high) in RANGES.items():
        if rng.random() < rate:
            span = max(1, (high - low) // 4)
            candidate = int(values[name]) + rng.randint(-span, span)
            values[name] = max(low, min(high, candidate))
    if rng.random() < rate:
        values["use_trend_filter"] = not bool(values["use_trend_filter"])
    return Genome.from_dict(values)


def crossover(a: Genome, b: Genome, rng: random.Random) -> Genome:
    left, right = a.to_dict(), b.to_dict()
    child = {
        name: (left[name] if rng.random() < 0.5 else right[name])
        for name in _INT_FIELDS
    }
    child["use_trend_filter"] = (
        left["use_trend_filter"] if rng.random() < 0.5 else right["use_trend_filter"]
    )
    child["mode"] = left["mode"] if rng.random() < 0.5 else right["mode"]
    return Genome.from_dict(child)


def fitness(metrics: Metrics, *, min_trades: int) -> float:
    """Selection score for the *training* slice only.

    Requires a minimum sample, then rewards expectancy while steering hard away
    from deep drawdowns: a genome that earns 160bps but draws down 26% is not
    tradeable on a small account whose kill switch trips at -3% daily.
    """
    if metrics.trades < min_trades:
        return float("-inf")
    excess_drawdown = max(0.0, metrics.max_drawdown_pct - DRAWDOWN_TARGET_PCT)
    return metrics.expectancy_bps - DRAWDOWN_PENALTY_PER_PCT * excess_drawdown


DRAWDOWN_TARGET_PCT = 10.0
DRAWDOWN_PENALTY_PER_PCT = 8.0


@dataclass
class EvolutionResult:
    champion: Genome
    in_sample: Metrics
    out_of_sample: Metrics
    oos_folds: list[Metrics] = field(default_factory=list)
    ranked: list[dict[str, Any]] = field(default_factory=list)
    population_size: int = 0
    generations: int = 0
    evaluated: int = 0
    seed: int = 0
    train_bars: int = 0
    test_bars: int = 0
    min_oos_trades: int = 30

    @property
    def oos_folds_positive(self) -> int:
        return sum(1 for fold in self.oos_folds if fold.expectancy_bps > 0)

    @property
    def oos_fold_count(self) -> int:
        return len(self.oos_folds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "champion": self.champion.to_dict(),
            "in_sample": self.in_sample.to_dict(),
            "out_of_sample": self.out_of_sample.to_dict(),
            "oos_folds": [fold.to_dict() for fold in self.oos_folds],
            "oos_folds_positive": self.oos_folds_positive,
            "oos_fold_count": self.oos_fold_count,
            "ranked": self.ranked,
            "population_size": self.population_size,
            "generations": self.generations,
            "evaluated": self.evaluated,
            "seed": self.seed,
            "train_bars": self.train_bars,
            "test_bars": self.test_bars,
            "min_oos_trades": self.min_oos_trades,
        }


def evolve(
    bars: list[Bar],
    *,
    population: int = 24,
    generations: int = 6,
    seed: int = 42,
    train_fraction: float = 0.7,
    folds: int = 3,
    min_trades: int = 10,
    min_oos_trades: int = 30,
    top: int = 5,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
) -> EvolutionResult:
    """Evolve genomes on a training slice, then validate on held-out bars."""
    if not 0.3 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be between 0.3 and 0.9")
    if len(bars) < 60:
        raise ValueError(
            f"not enough bars to evolve honestly ({len(bars)}); "
            "fetch more history first"
        )

    split = int(len(bars) * train_fraction)
    train, test = bars[:split], bars[split:]
    rng = random.Random(seed)
    costs = costs or CostModel()

    population_genomes = [random_genome(rng) for _ in range(population)]
    evaluated = 0
    scored: list[tuple[Genome, Metrics]] = []

    for generation in range(generations):
        results: list[tuple[Genome, Metrics]] = []
        for genome in population_genomes:
            metrics = run_backtest(
                train,
                genome,
                costs=costs,
                starting_cash=starting_cash,
                bootstrap_samples=0,
            )
            evaluated += 1
            results.append((genome, metrics))
        results.sort(key=lambda pair: fitness(pair[1], min_trades=min_trades), reverse=True)
        scored = results

        if generation == generations - 1:
            break
        survivors = results[: max(2, population // 2)]
        children: list[Genome] = [genome for genome, _ in survivors]
        while len(children) < population:
            parent_a = rng.choice(survivors)[0]
            parent_b = rng.choice(survivors)[0]
            child = crossover(parent_a, parent_b, rng)
            children.append(mutate(child, rng))
        population_genomes = children[:population]

    champion, in_sample = scored[0]
    oos = run_backtest(
        test,
        champion,
        costs=costs,
        starting_cash=starting_cash,
        bootstrap_samples=1000,
        seed=seed,
    )
    folds_metrics = fold_metrics(oos.trades_detail, folds=folds)

    ranked = [
        {
            "genome": genome.to_dict(),
            "in_sample": metrics.to_dict(),
            "fitness": (
                None
                if fitness(metrics, min_trades=min_trades) == float("-inf")
                else round(fitness(metrics, min_trades=min_trades), 3)
            ),
        }
        for genome, metrics in scored[:top]
    ]

    return EvolutionResult(
        champion=champion,
        in_sample=in_sample,
        out_of_sample=oos,
        oos_folds=folds_metrics,
        ranked=ranked,
        population_size=population,
        generations=generations,
        evaluated=evaluated,
        seed=seed,
        train_bars=len(train),
        test_bars=len(test),
        min_oos_trades=min_oos_trades,
    )

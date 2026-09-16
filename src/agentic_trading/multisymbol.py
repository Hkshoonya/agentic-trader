"""Pooled multi-symbol evaluation.

Single-symbol SPY momentum fires too rarely to clear the gate's sample floor
(~16 in-sample trades over 1,700 bars), so a verdict is impossible no matter how
long it runs. Evaluating one rule across many liquid symbols multiplies the
independent trade count without changing the rule, which is what makes a
statistical decision possible at all.

Each symbol is simulated independently with its own cash balance (no shared
capital, no cross-symbol position interaction); the resulting trades are then
pooled for expectancy, fold consistency and bootstrap significance. Metrics are
computed by the same `_summarize` used for single-series runs so pooled and
single-symbol numbers stay comparable.
"""

from __future__ import annotations

import random
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from agentic_trading.backtest import (
    CostModel,
    Genome,
    Metrics,
    _summarize,
    fold_metrics,
    run_backtest,
)
from agentic_trading.evolution import (
    EvolutionResult,
    crossover,
    fitness,
    mutate,
    random_genome,
)
from agentic_trading.history import Bar, load_bars


def load_symbol_bars(
    bar_dir: Path | str, symbols: Iterable[str], *, interval: str = "5minute"
) -> dict[str, list[Bar]]:
    """Load ``{symbol: bars}`` from ``<bar_dir>/<SYMBOL>_<interval>.jsonl``."""
    directory = Path(bar_dir)
    loaded: dict[str, list[Bar]] = {}
    for symbol in symbols:
        path = directory / f"{symbol.upper()}_{interval}.jsonl"
        if not path.is_file():
            continue
        bars = load_bars(path)
        if bars:
            loaded[symbol.upper()] = bars
    return loaded


def pooled_metrics(
    symbol_bars: dict[str, list[Bar]],
    genome: Genome,
    *,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
    bootstrap_samples: int = 1000,
    seed: int = 7,
) -> Metrics:
    """Simulate ``genome`` on every symbol and pool the resulting trades."""
    trades = []
    for bars in symbol_bars.values():
        metrics = run_backtest(
            bars,
            genome,
            costs=costs,
            starting_cash=starting_cash,
            bootstrap_samples=0,
        )
        trades.extend(metrics.trades_detail)
    trades.sort(key=lambda trade: trade.exit_time)

    curve: list[tuple[object, Decimal]] = []
    running = Decimal("0")
    for trade in trades:
        running += trade.pnl
        curve.append((trade.exit_time, starting_cash + running))
    return _summarize(
        trades,
        curve,
        starting_cash=starting_cash,
        final_equity=starting_cash + running,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
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


def evolve_multi(
    symbol_bars: dict[str, list[Bar]],
    *,
    population: int = 24,
    generations: int = 6,
    seed: int = 42,
    train_fraction: float = 0.7,
    folds: int = 3,
    min_trades: int = 20,
    min_oos_trades: int = 30,
    top: int = 5,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
) -> EvolutionResult:
    """Evolve on pooled training trades, validate on pooled held-out trades."""
    if not 0.3 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be between 0.3 and 0.9")
    if not symbol_bars:
        raise ValueError("no bar files loaded; fetch history for the symbols first")
    shortest = min(len(bars) for bars in symbol_bars.values())
    if shortest < 60:
        raise ValueError(f"not enough bars to evolve honestly ({shortest} minimum)")

    train, test = _split(symbol_bars, train_fraction)
    rng = random.Random(seed)
    costs = costs or CostModel()

    population_genomes = [random_genome(rng) for _ in range(population)]
    evaluated = 0
    scored: list[tuple[Genome, Metrics]] = []

    for generation in range(generations):
        results: list[tuple[Genome, Metrics]] = []
        for genome in population_genomes:
            metrics = pooled_metrics(
                train,
                genome,
                costs=costs,
                starting_cash=starting_cash,
                bootstrap_samples=0,
            )
            evaluated += 1
            results.append((genome, metrics))
        results.sort(
            key=lambda pair: fitness(pair[1], min_trades=min_trades), reverse=True
        )
        scored = results
        if generation == generations - 1:
            break
        survivors = results[: max(2, population // 2)]
        children = [genome for genome, _ in survivors]
        while len(children) < population:
            parent_a = rng.choice(survivors)[0]
            parent_b = rng.choice(survivors)[0]
            children.append(mutate(crossover(parent_a, parent_b, rng), rng))
        population_genomes = children[:population]

    champion, in_sample = scored[0]
    oos = pooled_metrics(
        test,
        champion,
        costs=costs,
        starting_cash=starting_cash,
        bootstrap_samples=1000,
        seed=seed,
    )
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
        oos_folds=fold_metrics(oos.trades_detail, folds=folds),
        ranked=ranked,
        population_size=population,
        generations=generations,
        evaluated=evaluated,
        seed=seed,
        train_bars=sum(len(bars) for bars in train.values()),
        test_bars=sum(len(bars) for bars in test.values()),
        min_oos_trades=min_oos_trades,
    )

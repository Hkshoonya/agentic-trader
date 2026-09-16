"""Strategy families as entry overlays.

A *family* narrows when an existing genome is allowed to enter, using only
information available at the entry bar — never look-ahead:

- ``base`` — no overlay, every signal taken
- ``vol_regime`` — enter only when realized volatility sits inside a band
- ``session_effects`` — enter only inside a window of the trading session

Implementing families as overlays (rather than new genes) keeps the genome and
backtester untouched: the same tested entry/exit engine powers every family, and
the overlay parameters are searched separately, which also limits how much the
search can overfit any single dimension.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from agentic_trading.backtest import (
    CostModel,
    Genome,
    Metrics,
    Trade,
    _summarize,
    fold_metrics,
    realized_vol_bps,
    run_backtest,
)
from agentic_trading.evolution import (
    EvolutionResult,
    crossover,
    fitness,
    mutate,
    random_genome,
)
from agentic_trading.history import Bar

ET = ZoneInfo("America/New_York")
SESSION_OPEN_MINUTE = 9 * 60 + 30

BASE = "base"
VOL_REGIME = "vol_regime"
SESSION_EFFECTS = "session_effects"
FAMILIES = (BASE, VOL_REGIME, SESSION_EFFECTS)
INTRADAY_ONLY = (SESSION_EFFECTS,)

# Candidate overlays. Small, explicit grids: the point is to test a hypothesis
# class, not to mine the surface until something looks good.
VOL_BANDS: tuple[tuple[int, int], ...] = (
    (0, 150),
    (0, 300),
    (0, 600),
    (100, 250),
    (150, 300),
    (200, 500),
    (300, 600),
)
SESSION_WINDOWS: tuple[tuple[int, int], ...] = (
    (0, 390),   # whole session
    (0, 30),    # first 30 minutes
    (0, 60),    # opening hour
    (30, 180),
    (60, 240),
    (330, 390),  # closing hour
)


def families_for(interval: str) -> tuple[str, ...]:
    """Families that make sense for a bar interval (daily bars have no session)."""
    if interval.lower() in ("day", "week", "month"):
        return tuple(name for name in FAMILIES if name not in INTRADAY_ONLY)
    return FAMILIES


def entry_minute(bar_start: datetime) -> int:
    """Minutes since the 09:30 ET session open for a bar timestamp."""
    local = bar_start.astimezone(ET)
    return local.hour * 60 + local.minute - SESSION_OPEN_MINUTE


def overlay_trades(
    symbol_bars: dict[str, list[Bar]],
    genome: Genome,
    *,
    family: str,
    params: tuple[int, int],
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
) -> list[Trade]:
    """Run every symbol and keep only the trades the overlay permits."""
    kept: list[Trade] = []
    for bars in symbol_bars.values():
        metrics = run_backtest(
            bars,
            genome,
            costs=costs,
            starting_cash=starting_cash,
            bootstrap_samples=0,
        )
        if family == BASE:
            kept.extend(metrics.trades_detail)
            continue
        index_by_time = {bar.start: i for i, bar in enumerate(bars)}
        for trade in metrics.trades_detail:
            position = index_by_time.get(trade.entry_time)
            if position is None:
                continue
            if family == VOL_REGIME:
                vol = realized_vol_bps(bars, position, genome.vol_window)
                if vol is None or not (params[0] <= vol <= params[1]):
                    continue
            elif family == SESSION_EFFECTS:
                minute = entry_minute(trade.entry_time)
                if not (params[0] <= minute <= params[1]):
                    continue
            else:
                raise ValueError(f"unknown family: {family!r}")
            kept.append(trade)
    kept.sort(key=lambda trade: trade.exit_time)
    return kept


def metrics_from_trades(
    trades: list[Trade],
    *,
    starting_cash: Decimal = Decimal("50"),
    bootstrap_samples: int = 0,
    seed: int = 7,
) -> Metrics:
    running = Decimal("0")
    curve: list[tuple[Any, Decimal]] = []
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


def candidates_for(family: str) -> tuple[tuple[int, int], ...]:
    if family == VOL_REGIME:
        return VOL_BANDS
    if family == SESSION_EFFECTS:
        return SESSION_WINDOWS
    if family == BASE:
        return ((0, 0),)
    raise ValueError(f"unknown family: {family!r}")


def select_overlay(
    symbol_bars: dict[str, list[Bar]],
    genome: Genome,
    *,
    family: str,
    min_trades: int,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
) -> tuple[Optional[tuple[int, int]], Metrics, list[dict[str, Any]]]:
    """Pick the overlay parameter set with the best training expectancy.

    Returns ``(params, metrics, evaluated)`` where ``params`` is ``None`` when no
    candidate produced enough trades to be considered.
    """
    best_params: Optional[tuple[int, int]] = None
    best_metrics = Metrics()
    evaluated: list[dict[str, Any]] = []
    for params in candidates_for(family):
        trades = overlay_trades(
            symbol_bars,
            genome,
            family=family,
            params=params,
            costs=costs,
            starting_cash=starting_cash,
        )
        metrics = metrics_from_trades(trades, starting_cash=starting_cash)
        evaluated.append(
            {
                "params": list(params),
                "trades": metrics.trades,
                "expectancy_bps": round(metrics.expectancy_bps, 3),
            }
        )
        if metrics.trades < min_trades:
            continue
        if best_params is None or metrics.expectancy_bps > best_metrics.expectancy_bps:
            best_params, best_metrics = params, metrics
    return best_params, best_metrics, evaluated


def evolve_with_families(
    symbol_bars: dict[str, list[Bar]],
    *,
    families: tuple[str, ...] = FAMILIES,
    population: int = 24,
    generations: int = 6,
    seed: int = 42,
    train_fraction: float = 0.7,
    folds: int = 3,
    min_trades: int = 20,
    min_oos_trades: int = 30,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
) -> EvolutionResult:
    """Search a genome, then search each family overlay; validate on held-out bars."""
    import random as _random

    if not symbol_bars:
        raise ValueError("no bar files loaded; fetch history for the symbols first")
    shortest = min(len(bars) for bars in symbol_bars.values())
    if shortest < 60:
        raise ValueError(f"not enough bars to evolve honestly ({shortest} minimum)")
    unknown = [name for name in families if name not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown families: {unknown}")

    train: dict[str, list[Bar]] = {}
    test: dict[str, list[Bar]] = {}
    for symbol, bars in symbol_bars.items():
        cut = max(1, int(len(bars) * train_fraction))
        train[symbol] = bars[:cut]
        test[symbol] = bars[cut:]

    rng = _random.Random(seed)
    costs = costs or CostModel()
    population_genomes = [random_genome(rng) for _ in range(population)]
    evaluated = 0
    scored: list[tuple[Genome, Metrics]] = []

    for generation in range(generations):
        results: list[tuple[Genome, Metrics]] = []
        for genome in population_genomes:
            trades = overlay_trades(
                train,
                genome,
                family=BASE,
                params=(0, 0),
                costs=costs,
                starting_cash=starting_cash,
            )
            metrics = metrics_from_trades(trades, starting_cash=starting_cash)
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
            child = mutate(
                crossover(rng.choice(survivors)[0], rng.choice(survivors)[0], rng),
                rng,
            )
            children.append(child)
        population_genomes = children[:population]

    champion, in_sample = scored[0]
    ranked = [
        {
            "genome": genome.to_dict(),
            "in_sample": metrics.to_dict(),
            "fitness": round(fitness(metrics, min_trades=min_trades), 3)
            if fitness(metrics, min_trades=min_trades) != float("-inf")
            else None,
        }
        for genome, metrics in scored[:5]
    ]

    # Family selection uses training data only; the winning overlay is then
    # measured once on bars the search never saw.
    best_family = BASE
    best_params: tuple[int, int] = (0, 0)
    best_train = in_sample
    family_report: dict[str, Any] = {}
    for family in families:
        params, metrics, evaluated_candidates = select_overlay(
            train,
            champion,
            family=family,
            min_trades=min_trades,
            costs=costs,
            starting_cash=starting_cash,
        )
        family_report[family] = {
            "chosen_params": list(params) if params else None,
            "candidates": evaluated_candidates,
        }
        if params is None:
            continue
        if metrics.expectancy_bps > best_train.expectancy_bps or best_family == BASE:
            if family == BASE or metrics.expectancy_bps > best_train.expectancy_bps:
                best_family, best_params, best_train = family, params, metrics

    oos_trades = overlay_trades(
        test,
        champion,
        family=best_family,
        params=best_params,
        costs=costs,
        starting_cash=starting_cash,
    )
    oos = metrics_from_trades(
        oos_trades, starting_cash=starting_cash, bootstrap_samples=1000, seed=seed
    )
    ranked.insert(
        0,
        {
            "selected_family": best_family,
            "overlay_params": list(best_params),
            "families": family_report,
        },
    )
    return EvolutionResult(
        champion=champion,
        in_sample=best_train,
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

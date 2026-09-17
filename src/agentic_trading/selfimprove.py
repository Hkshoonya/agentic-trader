"""Self-evaluation cycle: evolve, assess, promote or demote — with evidence.

The agent may only change its own trading stage through this module, and only
when the operator has opted in with ``autonomy = "auto"`` **and**
``AGENTIC_ALLOW_AUTONOMY=1``. Every decision is journaled with the evidence that
justified it, including every refusal.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from agentic_trading.backtest import CostModel
from agentic_trading.config import Config
from agentic_trading.evolution import EvolutionResult, evolve
from agentic_trading.history import load_bars
from agentic_trading import jsonio
from agentic_trading.promotion import (
    Assessment,
    PromotionPolicy,
    PromotionState,
    apply_assessment,
    assess,
    check_demotion,
    load_state,
    policy_from_config,
    save_state,
)


def autonomy_enabled() -> bool:
    """Auto-promotion requires explicit operator consent in the environment."""
    return os.environ.get("AGENTIC_ALLOW_AUTONOMY") == "1"


def run_evolution(
    config: Config,
    *,
    population: Optional[int] = None,
    generations: Optional[int] = None,
    seed: int = 42,
    starting_cash: Decimal = Decimal("50"),
) -> EvolutionResult:
    """Evolve on the configured history.

    ``history_path`` may be a single bar file (one symbol) or a directory of
    bar files, in which case the pooled multi-symbol evaluator is used — the
    same one the CLI exposes. A directory is read as all ``*_day.jsonl`` files,
    falling back to ``*_5minute.jsonl``; the pooled daily horizon is where the
    evaluation actually has enough independent trades to decide anything.
    """
    if config.history_path is None:
        raise ValueError(
            "no history_path configured; run 'agentic-trading fetch-history' first"
        )

    if Path(config.history_path).is_dir():
        from agentic_trading.multisymbol import evolve_multi, load_symbol_bars

        directory = Path(config.history_path)
        day_files = sorted(directory.glob("*_day.jsonl"))
        interval = "day" if day_files else "5minute"
        symbols = sorted(
            path.name[: -len(f"_{interval}.jsonl")]
            for path in directory.glob(f"*_{interval}.jsonl")
        )
        symbol_bars = load_symbol_bars(directory, symbols, interval=interval)
        if not symbol_bars:
            raise ValueError(f"no usable bar files in {directory}")
        return evolve_multi(
            symbol_bars,
            population=population or config.evolution_population,
            generations=generations or config.evolution_generations,
            seed=seed,
            min_oos_trades=config.min_oos_trades,
            starting_cash=starting_cash,
        )

    bars = load_bars(config.history_path)
    if len(bars) < 60:
        raise ValueError(
            f"only {len(bars)} bars available at {config.history_path}; "
            "fetch more history before evolving"
        )
    return evolve(
        bars,
        population=population or config.evolution_population,
        generations=generations or config.evolution_generations,
        seed=seed,
        min_oos_trades=config.min_oos_trades,
        costs=CostModel(),
        starting_cash=starting_cash,
    )


def write_evolution(result: EvolutionResult, state_dir: Path | str) -> Path:
    path = Path(state_dir) / "evolution.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["run_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(jsonio.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def evaluate_and_record(
    config: Config,
    *,
    equity: Optional[Decimal] = None,
    population: Optional[int] = None,
    generations: Optional[int] = None,
    seed: int = 42,
) -> tuple[Assessment, PromotionState, list[dict[str, Any]]]:
    """Run evolution, assess it, update promotion state, return journal events."""
    policy = policy_from_config(config)
    result = run_evolution(
        config, population=population, generations=generations, seed=seed
    )
    write_evolution(result, config.state_dir)
    assessment = assess(result, policy)
    state = load_state(config.state_dir)
    events = apply_assessment(state, assessment, policy, equity=equity)
    save_state(config.state_dir, state)

    events.extend(update_limits(config, assessment=assessment))
    return assessment, state, events


def update_limits(
    config: Config,
    *,
    assessment: Any = None,
    eligible: Optional[bool] = None,
    reset: bool = False,
) -> list[dict[str, Any]]:
    """Scale the risk budget to the confidence the evidence has earned.

    ``assessment`` is the graded result from :func:`evaluate_and_record`; the
    legacy ``eligible`` flag still works for callers that only have the verdict.
    Neither direction can exceed what the operator authorised — see
    :mod:`agentic_trading.limits`.
    """
    from agentic_trading.limits import (
        load_limits,
        propose,
        propose_from_assessment,
        save_limits,
    )

    previous = load_limits(config.state_dir)
    if assessment is not None:
        updated = propose_from_assessment(
            config, assessment, current=previous, reset=reset
        )
    else:
        updated = propose(
            config, eligible=bool(eligible), current=previous
        )
    if previous is not None and previous.to_dict() == updated.to_dict():
        return []
    save_limits(config.state_dir, updated)
    return [
        {
            "event": "limits_updated",
            "reason": updated.reason,
            "max_order_pct": updated.max_order_pct,
            "daily_notional_pct": updated.daily_notional_pct,
            "confidence": updated.confidence,
            "session_policy": updated.session_policy or None,
            "ceiling_max_order_pct": str(config.max_order_pct),
            "ceiling_daily_notional_pct": str(config.daily_notional_pct),
            "target_max_order_pct": updated.details.get("target_max_order_pct"),
            "components": updated.details.get("components"),
        }
    ]


def apply_stage(
    config: Config, state: PromotionState
) -> list[dict[str, Any]]:
    """Persist the stage as a run mode and adjust RiskGuard caps for probation."""
    from agentic_trading.runtime import write_mode

    events: list[dict[str, Any]] = []
    target_mode = "shadow" if state.stage == "shadow" else "live"
    write_mode(config.state_dir, target_mode)
    events.append(
        {
            "event": "stage_applied",
            "stage": state.stage,
            "mode": target_mode,
            "autonomy": config.autonomy,
        }
    )
    return events


def demotion_event(
    config: Config,
    *,
    kill_switch: bool,
    consecutive_errors: int,
    current_equity: Optional[Decimal],
) -> Optional[dict[str, Any]]:
    state = load_state(config.state_dir)
    if state.stage == "shadow":
        return None
    event = check_demotion(
        state,
        policy=policy_from_config(config),
        kill_switch=kill_switch,
        consecutive_errors=consecutive_errors,
        current_equity=current_equity,
        max_consecutive_errors=config.max_consecutive_errors,
    )
    if event is None:
        return None
    save_state(config.state_dir, state)
    return event


def record_stage_start(config: Config, state: PromotionState) -> None:
    save_state(config.state_dir, state)


def current_stage(config: Config) -> str:
    return load_state(config.state_dir).stage

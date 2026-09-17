"""Promotion gate: when (and whether) the agent may trade real money.

The gate exists to answer one question with evidence: *has this strategy shown a
cost-adjusted edge on data the search never saw, with a large enough sample that
the result is not noise?* When the answer is no, the bot stays in shadow and
records exactly which requirement failed.

Stages:

- ``shadow`` — simulated only, default
- ``probation`` — live, but with a reduced per-order cap
- ``live`` — live at the configured caps

Promotion to the next stage requires ``required_cycles`` consecutive passing
assessments. Demotion is immediate on kill switch, broker error streak, or a
live drawdown beyond ``demote_drawdown_pct``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

STAGES = ("shadow", "probation", "live")

# Confidence is the continuous cousin of the pass/fail gate: it grades evidence
# that is not yet strong enough to promote, so risk can move in steps instead of
# jumping from "nothing" to "everything" the day the gate finally passes.
#
# The significance term deliberately uses the *uncorrected* 0.05 bar. Promotion
# still requires the Bonferroni-corrected threshold; confidence only asks "does
# this look real at all", because a score that is 0 for every run that has not
# yet passed would make the ladder a no-op.
_SIGNIFICANCE_REFERENCE = 0.05
_EXPECTANCY_TARGET_BPS = 25.0
_PROFIT_FACTOR_TARGET = 1.5
_CONFIDENCE_WEIGHTS = {
    "significance": 0.35,
    "sample": 0.20,
    "expectancy": 0.20,
    "folds": 0.10,
    "drawdown": 0.10,
    "stability": 0.05,
}


@dataclass(frozen=True)
class PromotionPolicy:
    min_oos_trades: int = 30
    min_oos_expectancy_bps: float = 1.0
    min_folds_positive_fraction: float = 0.6
    max_oos_drawdown_pct: float = 15.0
    max_bootstrap_p_value: float = 0.05
    required_cycles: int = 3
    demote_drawdown_pct: float = 5.0
    probation_max_order_pct: Decimal = Decimal("0.01")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["probation_max_order_pct"] = str(self.probation_max_order_pct)
        return data


@dataclass
class Assessment:
    eligible: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    confidence_parts: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "confidence": round(self.confidence, 4),
            "confidence_parts": dict(self.confidence_parts),
        }


def _unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def grade_confidence(
    *,
    oos_trades: int,
    expectancy_bps: float,
    positive_fraction: float,
    max_drawdown_pct: float,
    bootstrap_p_value: float,
    profit_factor: float,
    policy: PromotionPolicy,
) -> tuple[float, dict[str, float]]:
    """Grade how much the evidence looks like a real edge, on a 0..1 scale.

    Every component is bounded and reported separately so a change in risk can
    always be traced to the number that moved it.
    """
    parts = {
        "significance": _unit(
            (_SIGNIFICANCE_REFERENCE - bootstrap_p_value) / _SIGNIFICANCE_REFERENCE
        ),
        "sample": _unit(oos_trades / max(1, policy.min_oos_trades)),
        "expectancy": _unit(expectancy_bps / _EXPECTANCY_TARGET_BPS),
        "folds": _unit(positive_fraction),
        "drawdown": _unit(
            1.0 - (max_drawdown_pct / max(1e-9, policy.max_oos_drawdown_pct))
        ),
        "stability": _unit(profit_factor / _PROFIT_FACTOR_TARGET),
    }
    confidence = sum(
        _CONFIDENCE_WEIGHTS[name] * value for name, value in parts.items()
    )
    return round(confidence, 6), {
        name: round(value, 4) for name, value in parts.items()
    }


def assess(
    evolution: Any,
    policy: PromotionPolicy,
) -> Assessment:
    """Decide whether an evolution result justifies promotion.

    ``evolution`` is an :class:`agentic_trading.evolution.EvolutionResult` (or any
    object exposing ``in_sample``/``out_of_sample``/``oos_folds``/``ranked``).
    """
    oos = evolution.out_of_sample
    folds = list(getattr(evolution, "oos_folds", []))
    folds_positive = sum(1 for fold in folds if fold.expectancy_bps > 0)
    folds_total = len(folds)
    positive_fraction = (folds_positive / folds_total) if folds_total else 0.0

    reasons: list[str] = []
    if evolution.in_sample.trades < policy.min_oos_trades:
        reasons.append(
            f"in-sample sample too small ({evolution.in_sample.trades} trades "
            f"< {policy.min_oos_trades})"
        )
    if oos.trades < policy.min_oos_trades:
        reasons.append(
            f"out-of-sample sample too small ({oos.trades} trades "
            f"< {policy.min_oos_trades})"
        )
    if oos.expectancy_bps < policy.min_oos_expectancy_bps:
        reasons.append(
            f"out-of-sample expectancy {oos.expectancy_bps:.2f}bps "
            f"< required {policy.min_oos_expectancy_bps:.2f}bps after costs"
        )
    if folds_total and positive_fraction < policy.min_folds_positive_fraction:
        reasons.append(
            f"only {folds_positive}/{folds_total} out-of-sample folds profitable "
            f"(need {policy.min_folds_positive_fraction:.0%})"
        )
    if not folds_total:
        reasons.append("no out-of-sample folds were evaluated")
    if oos.max_drawdown_pct > policy.max_oos_drawdown_pct:
        reasons.append(
            f"out-of-sample drawdown {oos.max_drawdown_pct:.1f}% "
            f"> allowed {policy.max_oos_drawdown_pct:.1f}%"
        )
    # Searching N genomes means evaluating N hypotheses, so the significance bar
    # has to tighten with N (Bonferroni). Without this, adding strategy families
    # simply manufactures false positives: the session that produced +159bps at
    # p=0.011 from 240 genomes saw that edge invert to -94bps when the selection
    # criterion changed. The threshold scales instead of the optimism.
    tested = int(getattr(evolution, "evaluated", 0) or 0)
    alpha = policy.max_bootstrap_p_value
    if tested > 1:
        alpha = policy.max_bootstrap_p_value / tested
    if oos.bootstrap_p_value > alpha:
        if tested > 1:
            reasons.append(
                f"edge does not survive the search: p={oos.bootstrap_p_value:.4f} "
                f"> {alpha:.6f} (0.05 / {tested} hypotheses tested)"
            )
        else:
            reasons.append(
                f"edge indistinguishable from noise (bootstrap p="
                f"{oos.bootstrap_p_value:.3f} > {policy.max_bootstrap_p_value})"
            )
    if oos.trades > 0 and oos.expectancy_bps > 0 and oos.profit_factor < 1.0:
        reasons.append(
            f"profit factor {oos.profit_factor:.2f} < 1.0 despite positive expectancy"
        )

    score = (
        oos.expectancy_bps * min(1.0, oos.trades / max(1, policy.min_oos_trades))
        - 0.05 * oos.max_drawdown_pct
    )
    evidence = {
        "oos_trades": oos.trades,
        "oos_expectancy_bps": round(oos.expectancy_bps, 3),
        "oos_win_rate": round(oos.win_rate, 4),
        "oos_profit_factor": (
            None if oos.profit_factor == float("inf") else round(oos.profit_factor, 4)
        ),
        "oos_max_drawdown_pct": round(oos.max_drawdown_pct, 3),
        "oos_bootstrap_p_value": round(oos.bootstrap_p_value, 4),
        "tested_hypotheses": tested,
        "effective_alpha": alpha,
        "folds_positive": folds_positive,
        "folds_total": folds_total,
        "train_bars": getattr(evolution, "train_bars", 0),
        "test_bars": getattr(evolution, "test_bars", 0),
        "genomes_evaluated": getattr(evolution, "evaluated", 0),
        "seed": getattr(evolution, "seed", 0),
    }
    confidence, confidence_parts = grade_confidence(
        oos_trades=oos.trades,
        expectancy_bps=oos.expectancy_bps,
        positive_fraction=positive_fraction,
        max_drawdown_pct=oos.max_drawdown_pct,
        bootstrap_p_value=oos.bootstrap_p_value,
        profit_factor=oos.profit_factor,
        policy=policy,
    )
    return Assessment(
        eligible=not reasons,
        score=score,
        reasons=reasons,
        evidence=evidence,
        confidence=confidence,
        confidence_parts=confidence_parts,
    )


@dataclass
class PromotionState:
    stage: str = "shadow"
    streak: int = 0
    updated_at: str = ""
    stage_since: str = ""
    stage_equity: str = ""
    last_assessment: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PromotionState":
        return cls(
            stage=str(raw.get("stage", "shadow")),
            streak=int(raw.get("streak", 0)),
            updated_at=str(raw.get("updated_at", "")),
            stage_since=str(raw.get("stage_since", "")),
            stage_equity=str(raw.get("stage_equity", "")),
            last_assessment=dict(raw.get("last_assessment", {})),
            history=list(raw.get("history", [])),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_assessment(
    state: PromotionState,
    assessment: Assessment,
    policy: PromotionPolicy,
    *,
    equity: Optional[Decimal] = None,
) -> list[dict[str, Any]]:
    """Advance the streak and stage. Returns transitions to journal, if any."""
    events: list[dict[str, Any]] = []
    if not assessment.eligible:
        if state.streak:
            events.append(
                {
                    "event": "promotion_streak_reset",
                    "previous_streak": state.streak,
                    "reasons": assessment.reasons,
                }
            )
        state.streak = 0
    else:
        state.streak += 1
        if state.streak >= policy.required_cycles:
            next_stage = _next_stage(state.stage)
            if next_stage is not None:
                events.append(
                    {
                        "event": "promotion",
                        "from": state.stage,
                        "to": next_stage,
                        "streak": state.streak,
                        "evidence": assessment.evidence,
                    }
                )
                state.stage = next_stage
                state.stage_since = _now()
                state.stage_equity = str(equity) if equity is not None else ""
                state.streak = 0

    state.updated_at = _now()
    state.last_assessment = assessment.to_dict()
    state.history.append({"at": state.updated_at, **state.last_assessment})
    state.history = state.history[-50:]
    return events


def _next_stage(stage: str) -> Optional[str]:
    if stage == "shadow":
        return "probation"
    if stage == "probation":
        return "live"
    return None


def check_demotion(
    state: PromotionState,
    *,
    policy: PromotionPolicy,
    kill_switch: bool,
    consecutive_errors: int,
    current_equity: Optional[Decimal],
    max_consecutive_errors: int,
) -> Optional[dict[str, Any]]:
    """Return a demotion event when the live stage is no longer justified."""
    if state.stage == "shadow":
        return None

    if kill_switch:
        return _demote(state, "kill_switch_active")
    if consecutive_errors >= max_consecutive_errors:
        return _demote(state, "consecutive_broker_errors")
    if current_equity is not None and state.stage_equity:
        try:
            reference = Decimal(state.stage_equity)
        except Exception:  # noqa: BLE001 — corrupt state must not block demotion
            reference = Decimal("0")
        if reference > 0:
            drawdown = float((reference - current_equity) / reference * 100)
            if drawdown >= policy.demote_drawdown_pct:
                return _demote(
                    state, f"drawdown_{drawdown:.1f}pct_exceeds_limit"
                )
    return None


def _demote(state: PromotionState, reason: str) -> dict[str, Any]:
    event = {
        "event": "demotion",
        "from": state.stage,
        "to": "shadow",
        "reason": reason,
        "streak": 0,
    }
    state.stage = "shadow"
    state.streak = 0
    state.updated_at = _now()
    state.stage_since = state.updated_at
    state.stage_equity = ""
    return event


def state_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "promotion.json"


def policy_from_config(config: Any) -> PromotionPolicy:
    """Build the policy from bot config (keeps thresholds operator-controlled)."""
    return PromotionPolicy(
        min_oos_trades=int(getattr(config, "min_oos_trades", 30)),
        required_cycles=int(getattr(config, "promotion_cycles_required", 3)),
    )


def load_state(state_dir: Path | str) -> PromotionState:
    path = state_path(state_dir)
    if not path.is_file():
        return PromotionState()
    try:
        return PromotionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return PromotionState()


def save_state(state_dir: Path | str, state: PromotionState) -> None:
    path = state_path(state_dir)
    from agentic_trading import jsonio

    # Atomic: the daemon reads the stage while the evaluation worker writes it.
    jsonio.write_text(path, jsonio.dumps(state.to_dict(), indent=2) + "\n")

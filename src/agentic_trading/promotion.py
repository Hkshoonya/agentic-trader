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
from typing import Any, Mapping, Optional

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
    # "Profitable recently" cannot mean one lucky close. The most recent
    # consecutive folds must each contain a usable sample and make money at the
    # account level, including under the execution-cost stress scenario.
    min_recent_positive_folds: int = 2
    min_recent_fold_trades: int = 10
    min_cost_stress_multiplier: float = 2.0
    min_forward_trades: int = 0
    min_forward_days: float = 0.0
    max_oos_drawdown_pct: float = 15.0
    max_bootstrap_p_value: float = 0.05
    required_cycles: int = 3
    demote_drawdown_pct: float = 5.0
    probation_max_order_pct: Decimal = Decimal("0.01")
    # The operator's explicit permission to trade a size the drawdown evidence
    # does not support. Off unless the config says otherwise; when on, the size
    # mismatch is reported as a note on every assessment instead of silently
    # passing — the gate keeps telling the truth about what it is looking at.
    accept_evidence_override: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["probation_max_order_pct"] = str(self.probation_max_order_pct)
        return data


@dataclass
class Assessment:
    eligible: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    # Things the operator has deliberately accepted rather than things that are
    # wrong. They travel with the verdict so the console can show the trade-off
    # instead of hiding it.
    notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    confidence_parts: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "notes": list(self.notes),
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
    confidence = sum(_CONFIDENCE_WEIGHTS[name] * value for name, value in parts.items())
    return round(confidence, 6), {
        name: round(value, 4) for name, value in parts.items()
    }


EVIDENCE_MAX_AGE_DAYS = 30.0


def assess_walkforward(
    report: Mapping[str, Any],
    policy: PromotionPolicy,
    *,
    live_per_order_pct: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Assessment:
    """Decide whether the *pre-registered* rule has earned promotion.

    The search path has to divide its significance bar by every genome it tried,
    which is correct statistics and an experiment too broad for its sample. The
    walk-forward test in :mod:`agentic_trading.walkforward` is the opposite
    experiment: one fixed rule, declared before the numbers were seen, for which
    a plain ``p <= 0.05`` is the honest bar.

    This is the primary evidence the gate uses when a report exists. It is
    deliberately strict about size: passing on the *rule* while trading a book
    the drawdown ceiling does not support is how a good hypothesis becomes a
    margin call, so the traded size has to fit inside the measured ``gate_size``.
    """
    config = (report.get("configs") or {}).get("production") or {}
    gate = report.get("gate_size") or {}
    reasons: list[str] = []
    forward_reasons: list[str] = []
    notes: list[str] = []

    generated = str(report.get("generated_at") or "")
    age_days: Optional[float] = None
    if generated:
        try:
            stamp = datetime.fromisoformat(generated)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_days = (
                (now or datetime.now(timezone.utc)) - stamp
            ).total_seconds() / 86_400
        except ValueError:
            age_days = None
    if age_days is None:
        reasons.append("walk-forward report has no usable timestamp")
    elif age_days > EVIDENCE_MAX_AGE_DAYS:
        reasons.append(
            f"walk-forward report is {age_days:.0f} days old "
            f"(limit {EVIDENCE_MAX_AGE_DAYS:.0f})"
        )

    trades = int(config.get("trades") or 0)
    expectancy = float(config.get("expectancy_bps") or 0.0)
    account_return_raw = config.get("return_pct")
    account_return = None if account_return_raw is None else float(account_return_raw)
    drawdown = float(config.get("max_drawdown_pct") or 0.0)
    p_value = float(config.get("bootstrap_p_value") or 1.0)
    profit_factor = float(config.get("profit_factor") or 0.0)
    folds = list(config.get("folds") or [])

    def fold_profit(fold: Mapping[str, Any]) -> float:
        # Account return is the result that matters. Mean return per trade can
        # be positive while a fold loses money when a few large positions or
        # costs dominate. Legacy reports lack return_pct, so retain their old
        # interpretation until they are refreshed.
        field = "return_pct" if "return_pct" in fold else "expectancy_bps"
        return float(fold.get(field) or 0.0)

    folds_positive = sum(1 for fold in folds if fold_profit(fold) > 0)
    folds_total = len(folds)
    positive_fraction = (folds_positive / folds_total) if folds_total else 0.0
    latest_fold_return = fold_profit(folds[-1]) if folds else None
    latest_has_account_return = bool(folds and "return_pct" in folds[-1])
    recent_fold_count = max(0, int(policy.min_recent_positive_folds))
    recent_folds = folds[-recent_fold_count:] if recent_fold_count else []
    per_order_pct = float(config.get("per_order_pct") or 0.0)
    forward = (
        report.get("forward") if isinstance(report.get("forward"), Mapping) else {}
    )
    forward_trades = int(forward.get("completed_trades") or 0)
    forward_days = float(forward.get("observed_days") or 0.0)
    forward_pnl = float(forward.get("realized_pnl") or 0.0)
    forward_required = bool(policy.min_forward_trades or policy.min_forward_days)
    cost_check = (
        report.get("cost_check")
        if isinstance(report.get("cost_check"), Mapping)
        else None
    )
    capital_check = (
        report.get("capital_check")
        if isinstance(report.get("capital_check"), Mapping)
        else None
    )
    sizing_check = (
        report.get("sizing_check")
        if isinstance(report.get("sizing_check"), Mapping)
        else None
    )
    model_check = (
        report.get("model_check")
        if isinstance(report.get("model_check"), Mapping)
        else None
    )
    stress = (
        report.get("cost_stress")
        if isinstance(report.get("cost_stress"), Mapping)
        else None
    )

    if trades < policy.min_oos_trades:
        reasons.append(
            f"walk-forward sample too small ({trades} trades < {policy.min_oos_trades})"
        )
    if expectancy < policy.min_oos_expectancy_bps:
        reasons.append(
            f"walk-forward expectancy {expectancy:.2f}bps "
            f"< required {policy.min_oos_expectancy_bps:.2f}bps after costs"
        )
    if account_return is None:
        reasons.append("walk-forward report has no account-level return")
    elif account_return <= 0:
        reasons.append(
            f"walk-forward account return is not positive ({account_return:.3f}%)"
        )
    if folds_total and positive_fraction < policy.min_folds_positive_fraction:
        reasons.append(
            f"only {folds_positive}/{folds_total} walk-forward folds profitable "
            f"(need {policy.min_folds_positive_fraction:.0%})"
        )
    if not folds_total:
        reasons.append("no walk-forward folds were evaluated")
    elif latest_fold_return is not None and latest_fold_return <= 0:
        metric = (
            f"account return {latest_fold_return:.3f}%"
            if latest_has_account_return
            else f"expectancy {latest_fold_return:.3f}bps"
        )
        reasons.append(
            f"latest walk-forward fold has non-positive {metric} "
            "(promotion requires current, not only historical, profitability)"
        )
    if recent_fold_count:
        if folds_total < recent_fold_count:
            reasons.append(
                f"only {folds_total} walk-forward folds exist "
                f"(need {recent_fold_count} recent folds)"
            )
        else:
            missing_returns = sum(
                1 for fold in recent_folds if "return_pct" not in fold
            )
            thin = sum(
                1
                for fold in recent_folds
                if int(fold.get("trades") or 0) < policy.min_recent_fold_trades
            )
            losing = sum(
                1 for fold in recent_folds if float(fold.get("return_pct") or 0.0) <= 0
            )
            if missing_returns:
                reasons.append(
                    f"{missing_returns}/{recent_fold_count} recent walk-forward "
                    "folds lack account-return evidence"
                )
            if thin:
                reasons.append(
                    f"{thin}/{recent_fold_count} recent walk-forward folds have "
                    f"fewer than {policy.min_recent_fold_trades} trades"
                )
            if losing:
                reasons.append(
                    f"{losing}/{recent_fold_count} recent walk-forward folds "
                    "have non-positive account returns"
                )
    if drawdown > policy.max_oos_drawdown_pct:
        reasons.append(
            f"walk-forward drawdown {drawdown:.1f}% "
            f"> allowed {policy.max_oos_drawdown_pct:.1f}%"
        )
    # A rule declared in advance pays no Bonferroni penalty. A rule chosen from
    # several candidates pays for every one the report says was examined.
    tested_hypotheses = max(1, int(report.get("hypotheses") or 1))
    effective_alpha = policy.max_bootstrap_p_value / tested_hypotheses
    if p_value > effective_alpha:
        reasons.append(
            f"walk-forward edge indistinguishable from noise "
            f"(bootstrap p={p_value:.4f} > {effective_alpha:.6g}"
            + (
                f", 0.05 / {tested_hypotheses} hypotheses tested)"
                if tested_hypotheses > 1
                else ")"
            )
        )
    if trades > 0 and expectancy > 0 and profit_factor < 1.0:
        reasons.append(
            f"profit factor {profit_factor:.2f} < 1.0 despite positive expectancy"
        )
    stress_multiplier = 0.0
    stress_expectancy = 0.0
    stress_return: Optional[float] = None
    stress_drawdown = 0.0
    stress_latest_return: Optional[float] = None
    if stress is None:
        reasons.append("walk-forward report has no doubled-cost stress test")
    else:
        stress_multiplier = float(stress.get("multiplier") or 0.0)
        stress_expectancy = float(stress.get("expectancy_bps") or 0.0)
        stress_return_raw = stress.get("return_pct")
        stress_return = None if stress_return_raw is None else float(stress_return_raw)
        stress_drawdown = float(stress.get("max_drawdown_pct") or 0.0)
        stress_folds = list(stress.get("folds") or [])
        if stress_folds and "return_pct" in stress_folds[-1]:
            stress_latest_return = float(stress_folds[-1].get("return_pct") or 0.0)
        if stress_multiplier + 1e-9 < policy.min_cost_stress_multiplier:
            reasons.append(
                f"execution-cost stress is only {stress_multiplier:.2f}x "
                f"(need at least {policy.min_cost_stress_multiplier:.2f}x)"
            )
        if stress_expectancy < policy.min_oos_expectancy_bps:
            reasons.append(
                f"doubled-cost expectancy {stress_expectancy:.2f}bps "
                f"< required {policy.min_oos_expectancy_bps:.2f}bps"
            )
        if stress_return is None:
            reasons.append("doubled-cost stress has no account-level return")
        elif stress_return <= 0:
            reasons.append(
                f"doubled-cost account return is not positive ({stress_return:.3f}%)"
            )
        if stress_drawdown > policy.max_oos_drawdown_pct:
            reasons.append(
                f"doubled-cost drawdown {stress_drawdown:.1f}% "
                f"> allowed {policy.max_oos_drawdown_pct:.1f}%"
            )
        if stress_latest_return is None:
            reasons.append("doubled-cost stress has no latest-fold account return")
        elif stress_latest_return <= 0:
            reasons.append(
                "latest walk-forward fold loses money when execution costs "
                f"are doubled ({stress_latest_return:.3f}%)"
            )
    if cost_check is not None and not bool(cost_check.get("current")):
        reasons.append(
            "walk-forward cost model is stale: report used "
            f"{float(cost_check.get('reported_per_side_bps') or 0):.2f}bps/side + "
            f"${float(cost_check.get('reported_fee_per_order_usd') or 0):.2f}/order, "
            "current evidence requires "
            f"{float(cost_check.get('current_per_side_bps') or 0):.2f}bps/side + "
            f"${float(cost_check.get('current_fee_per_order_usd') or 0):.2f}/order"
        )
    if capital_check is not None and not bool(capital_check.get("current")):
        reasons.append(
            "walk-forward capital assumption is stale: report tested "
            f"${float(capital_check.get('reported_equity') or 0):.2f}, current "
            f"equity is ${float(capital_check.get('current_equity') or 0):.2f}"
        )
    if sizing_check is not None and not bool(sizing_check.get("current")):
        if not bool(sizing_check.get("floor_model_current", True)):
            reasons.append(
                "walk-forward sizing is stale: the report did not reproduce "
                "the current fixed-dollar floor and daily opening cap"
            )
        else:
            reasons.append(
                "walk-forward sizing is stale: report tested "
                f"{float(sizing_check.get('reported_per_order_pct') or 0) * 100:.2f}% "
                "per order, current small-account policy may use "
                f"{float(sizing_check.get('current_per_order_pct') or 0) * 100:.2f}%"
            )
    if model_check is not None and not bool(model_check.get("current")):
        reasons.append(
            "walk-forward model is stale: report strategy, sizing mode, "
            "position count, or symbol universe differs from the current book"
        )
    if forward_required:
        if forward_trades < policy.min_forward_trades:
            forward_reasons.append(
                f"forward shadow sample too small ({forward_trades} completed trades "
                f"< {policy.min_forward_trades})"
            )
        if forward_days < policy.min_forward_days:
            forward_reasons.append(
                f"forward shadow window too short ({forward_days:.1f} days "
                f"< {policy.min_forward_days:.1f})"
            )
        if forward_trades >= policy.min_forward_trades and forward_pnl <= 0:
            forward_reasons.append(
                f"forward shadow P&L is not positive after modeled costs "
                f"(${forward_pnl:.2f})"
            )

    gate_size = gate.get("per_order_pct")
    if gate_size is None:
        reasons.append(
            f"no size holds the {policy.max_oos_drawdown_pct:.0f}% drawdown "
            "ceiling on this history"
        )
    elif (
        live_per_order_pct is not None and live_per_order_pct > float(gate_size) * 1.001
    ):
        override = bool(getattr(policy, "accept_evidence_override", False))
        message = (
            f"trading {live_per_order_pct * 100:.2f}% per order but the evidence "
            f"only supports {float(gate_size) * 100:.2f}% inside the drawdown ceiling"
        )
        if override:
            # The operator may authorise a size the evidence does not: it is
            # their account, and pretending the number is compliant would be
            # worse than saying out loud that it is not. Recorded as a warning by
            # the caller, never as a silent pass.
            notes.append(message)
        else:
            reasons.append(message)

    # The $5 floor is used to collect genuinely forward observations, so it
    # cannot itself require those observations or the system deadlocks forever.
    # It may activate only after every retrospective, cost, recency and sizing
    # check has passed at the exact floor size. Live promotion still requires
    # both this result and all forward requirements below.
    retrospective_reasons = list(reasons)
    retrospective_eligible = not retrospective_reasons
    reasons.extend(forward_reasons)

    score = (
        expectancy * min(1.0, trades / max(1, policy.min_oos_trades)) - 0.05 * drawdown
    )
    confidence, confidence_parts = grade_confidence(
        oos_trades=trades,
        expectancy_bps=expectancy,
        positive_fraction=positive_fraction,
        max_drawdown_pct=drawdown,
        bootstrap_p_value=p_value,
        profit_factor=profit_factor,
        policy=policy,
    )
    retrospective_confidence = confidence
    if forward_required:
        trade_progress = (
            1.0
            if policy.min_forward_trades <= 0
            else min(1.0, forward_trades / policy.min_forward_trades)
        )
        day_progress = (
            1.0
            if policy.min_forward_days <= 0
            else min(1.0, forward_days / policy.min_forward_days)
        )
        forward_progress = min(trade_progress, day_progress)
        if forward_trades >= policy.min_forward_trades and forward_pnl <= 0:
            forward_progress = 0.0
        confidence *= forward_progress
        confidence_parts["forward"] = round(forward_progress, 4)
    evidence = {
        "source": "walkforward",
        "oos_trades": trades,
        "oos_expectancy_bps": round(expectancy, 3),
        "oos_return_pct": (
            None if account_return is None else round(account_return, 4)
        ),
        "oos_win_rate": config.get("win_rate"),
        "oos_profit_factor": (
            None if profit_factor == float("inf") else round(profit_factor, 4)
        ),
        "oos_max_drawdown_pct": round(drawdown, 3),
        "oos_bootstrap_p_value": round(p_value, 4),
        "tested_hypotheses": tested_hypotheses,
        "effective_alpha": effective_alpha,
        "folds_positive": folds_positive,
        "folds_total": folds_total,
        "latest_fold_return_pct": (
            latest_fold_return if latest_has_account_return else None
        ),
        "recent_folds_required": recent_fold_count,
        "recent_fold_min_trades": policy.min_recent_fold_trades,
        "cost_stress_multiplier": round(stress_multiplier, 4),
        "cost_stress_expectancy_bps": round(stress_expectancy, 3),
        "cost_stress_return_pct": (
            None if stress_return is None else round(stress_return, 4)
        ),
        "cost_stress_max_drawdown_pct": round(stress_drawdown, 3),
        "cost_stress_latest_fold_return_pct": stress_latest_return,
        "forward_trades": forward_trades,
        "forward_days": round(forward_days, 2),
        "forward_realized_pnl": round(forward_pnl, 4),
        "forward_required": forward_required,
        "retrospective_eligible": retrospective_eligible,
        "retrospective_reasons": retrospective_reasons,
        "retrospective_confidence": round(retrospective_confidence, 4),
        "per_order_pct": per_order_pct,
        "gate_size_pct": None if gate_size is None else float(gate_size),
        "report_age_days": None if age_days is None else round(age_days, 2),
        "report_generated_at": generated,
        "reported_starting_equity": report.get("starting_equity"),
        "capital_current": (
            None if capital_check is None else bool(capital_check.get("current"))
        ),
        "sizing_current": (
            None if sizing_check is None else bool(sizing_check.get("current"))
        ),
        "model_current": (
            None if model_check is None else bool(model_check.get("current"))
        ),
        # Identity of the evidence itself, so re-running the report on the same
        # bars cannot advance the promotion streak.
        "report_key": "|".join(
            [
                str((report.get("series") or {}).get("bars")),
                str(len((report.get("series") or {}).get("symbols") or [])),
                str(trades),
                f"{expectancy:.2f}",
                str(account_return),
                f"{drawdown:.3f}",
                f"{p_value:.5f}",
                str(per_order_pct),
                str(gate_size),
                str(latest_fold_return),
                f"{stress_multiplier:.2f}",
                f"{stress_expectancy:.2f}",
                str(stress_return),
                str(stress_latest_return),
                str(forward_trades),
                f"{forward_pnl:.4f}",
                str((cost_check or {}).get("current")),
                str((capital_check or {}).get("current")),
                str((sizing_check or {}).get("current")),
                str((model_check or {}).get("current")),
                json.dumps(report.get("small_account_floor"), sort_keys=True),
            ]
        ),
        "symbols": len((report.get("series") or {}).get("symbols") or []),
        "bars": (report.get("series") or {}).get("bars"),
    }
    return Assessment(
        eligible=not reasons,
        score=score,
        reasons=reasons,
        notes=notes,
        evidence=evidence,
        confidence=confidence,
        confidence_parts=confidence_parts,
    )


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
        if state.stage != "shadow":
            event = _demote(state, "evidence_gate_failed")
            event["reasons"] = list(assessment.reasons)
            events.append(event)
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
                return _demote(state, f"drawdown_{drawdown:.1f}pct_exceeds_limit")
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
    requires_forward = str(getattr(config, "strategy", "")) in (
        "trend_crypto",
        "momentum_rotation",
    )
    return PromotionPolicy(
        min_oos_trades=int(getattr(config, "min_oos_trades", 30)),
        min_recent_positive_folds=int(getattr(config, "min_recent_positive_folds", 2)),
        min_recent_fold_trades=int(getattr(config, "min_recent_fold_trades", 10)),
        required_cycles=int(getattr(config, "promotion_cycles_required", 3)),
        min_forward_trades=(
            int(getattr(config, "min_forward_trades", 30)) if requires_forward else 0
        ),
        min_forward_days=(
            float(getattr(config, "min_forward_days", 30.0))
            if requires_forward
            else 0.0
        ),
        accept_evidence_override=bool(
            getattr(config, "accept_evidence_override", False)
        ),
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

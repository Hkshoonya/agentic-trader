"""Risk limits the agent may adjust — bounded by operator ceilings.

The agent's risk budget tracks how much its evidence looks like a real edge
(``Assessment.confidence``, 0..1): the configured values are the *ceiling*, and
the operating budget moves between a floor and that ceiling as confidence
changes. Four rules keep that from becoming a machine that talks itself into
bigger positions:

1. **The ceiling is the operator's.** No confidence value, streak, or score can
   push a cap above ``config.max_order_pct`` / ``config.daily_notional_pct``.
2. **Growth is rate-limited.** One assessment may raise a cap by at most
   ``GROW_FACTOR`` (25%), so a lucky run cannot triple size in one step.
3. **Growth needs improvement.** Raising a cap requires confidence to have
   actually increased since the last assessment; otherwise it holds. Falling
   confidence always cuts, by up to ``DE_RISK_FACTOR`` (50%) per assessment.
4. **A demotion resets.** Kill switch or demotion drops the budget to the floor
   and confidence has to be rebuilt from scratch.

The asymmetry is deliberate: shrinking is immediate, growing is slow, and the
worst case is always a limit the human already approved.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

FILE_NAME = "effective_limits.json"

# The agent never shrinks a limit to zero: a floor keeps the strategy testable
# instead of silently disabling itself.
MIN_ORDER_PCT = Decimal("0.002")
MIN_DAILY_PCT = Decimal("0.01")
DE_RISK_FACTOR = Decimal("0.5")
GROW_FACTOR = Decimal("1.25")
# Lowest fraction of the operator ceiling the ladder can reach: confidence 0
# still trades, just at a quarter of the authorised size.
MIN_SCALE = Decimal("0.25")
# Confidences are stored rounded to 4 places; differences smaller than that are
# recomputation noise, not a change in the evidence.
CONFIDENCE_TOLERANCE = Decimal("0.0001")

# Ordered widest-last, so "one step wider" is unambiguous.
SESSION_POLICIES = ("regular", "extended", "all", "any")
# Widening trading hours is a risk change, so it needs real confidence.
WIDEN_POLICY_CONFIDENCE = 0.75


@dataclass(frozen=True)
class Limits:
    max_order_pct: str
    daily_notional_pct: str
    reason: str
    updated_at: str
    confidence: str = "0"
    session_policy: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(state_dir: Path | str) -> Path:
    return Path(state_dir) / FILE_NAME


def load_limits(state_dir: Path | str) -> Optional[Limits]:
    path = _path(state_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return Limits(
            max_order_pct=str(raw["max_order_pct"]),
            daily_notional_pct=str(raw["daily_notional_pct"]),
            reason=str(raw.get("reason", "")),
            updated_at=str(raw.get("updated_at", "")),
            confidence=str(raw.get("confidence", "0")),
            session_policy=str(raw.get("session_policy", "")),
            details=dict(raw.get("details") or {}),
        )
    except (KeyError, TypeError):
        return None


def save_limits(state_dir: Path | str, limits: Limits) -> Path:
    path = _path(state_dir)
    from agentic_trading import jsonio

    # Atomic: a torn read would parse as "no limits stored" and silently restore
    # the operator's ceiling instead of the agent's reduced budget.
    jsonio.write_text(path, jsonio.dumps(limits.to_dict(), indent=2) + "\n")
    return path


def _clamp(value: Decimal, ceiling: Decimal, floor: Decimal) -> Decimal:
    return max(floor, min(value, ceiling))


def _target_for_confidence(ceiling: Decimal, confidence: float) -> Decimal:
    """The budget a given confidence justifies under a given ceiling."""
    scale = MIN_SCALE + (Decimal(1) - MIN_SCALE) * Decimal(
        str(round(max(0.0, min(1.0, confidence)), 6))
    )
    return _clamp(ceiling * scale, ceiling, MIN_ORDER_PCT)


def _row_bounds(config: Any, row: Any) -> tuple[Decimal, Decimal]:
    """A schedule row's ``(share at full confidence, share at zero)``."""
    ceiling = Decimal(str(row[1]))
    floor = Decimal(str(row[2])) if len(row) > 2 else Decimal("0")
    if floor <= 0:
        default = Decimal(str(getattr(config, "daily_budget_confidence_floor", "0.70")))
        floor = ceiling * default
    return ceiling, min(floor, ceiling)


def scheduled_daily_bounds(config: Any, equity: float) -> tuple[Decimal, Decimal]:
    """The share band this account size sits in, ``(at full, at zero)`` confidence.

    The schedule is a list of ``(up_to_equity, share, floor?)`` rows, smallest
    first. A row applies until the next row's threshold, and the last row covers
    everything above it. Between two rows both ends of the band move linearly,
    so growing the account does not step the budget off a cliff on the day it
    crosses a line. An empty schedule falls back to the flat
    ``daily_notional_pct`` — with no band, because a flat ceiling is not a range.
    """
    rows = tuple(getattr(config, "daily_budget_schedule", ()) or ())
    flat = Decimal(str(getattr(config, "daily_notional_pct", "0") or "0"))
    if not rows:
        return flat, flat
    size = Decimal(str(max(0.0, float(equity))))
    previous_limit = Decimal("0")
    previous_ceiling, previous_floor = _row_bounds(config, rows[0])
    for row in rows:
        limit = Decimal(str(row[0]))
        ceiling, floor = _row_bounds(config, row)
        if size <= limit:
            if limit <= previous_limit:
                return ceiling, floor
            span = limit - previous_limit
            progress = (size - previous_limit) / span if span > 0 else Decimal("1")
            return (
                previous_ceiling + (ceiling - previous_ceiling) * progress,
                previous_floor + (floor - previous_floor) * progress,
            )
        previous_limit, previous_ceiling, previous_floor = limit, ceiling, floor
    return previous_ceiling, previous_floor


def scheduled_daily_share(config: Any, equity: float, confidence: float = 1.0) -> Decimal:
    """The day's share of the account at this size and confidence."""
    ceiling, floor = scheduled_daily_bounds(config, equity)
    trust = Decimal(str(round(max(0.0, min(1.0, float(confidence))), 6)))
    return floor + (ceiling - floor) * trust


def budget_ceilings(
    config: Any,
    *,
    equity: Any,
    confidence: float,
) -> tuple[Decimal, Decimal]:
    """The (per-order, per-day) ceilings this account size and confidence earn.

    The schedule sets the day's share; confidence scales it from
    ``daily_budget_confidence_floor`` of that share up to the whole of it, so a
    tier is something the evidence has to earn rather than a default the system
    holds. The per-order ceiling is the day's share divided across the book's
    open slots — one order can never eat the day — and is hard-bounded by
    ``max_order_hard_pct``.
    """
    if not getattr(config, "daily_budget_schedule", ()):
        # No schedule: the operator's flat pair is already the answer, and
        # inventing a per-order ceiling here would quietly change the risk of
        # every config written before this feature existed.
        return (
            Decimal(str(getattr(config, "max_order_pct", "0") or "0")),
            Decimal(str(getattr(config, "daily_notional_pct", "0") or "0")),
        )
    daily = scheduled_daily_share(config, float(equity), confidence)
    slots = max(1, int(getattr(config, "max_open_positions", 1) or 1))
    hard = Decimal(str(getattr(config, "max_order_hard_pct", "0.25")))
    order = min(hard, daily / Decimal(slots))
    return (max(order, MIN_ORDER_PCT), max(daily, MIN_DAILY_PCT))


def propose(
    config: Any,
    *,
    eligible: bool,
    current: Optional[Limits] = None,
) -> Limits:
    """Next set of effective limits, given whether the evidence passed.

    Kept for callers that only have the pass/fail verdict; it is the two-point
    version of :func:`propose_from_assessment` (pass ⇒ ceiling, fail ⇒ one
    de-risking step).
    """
    ceiling_order = Decimal(str(config.max_order_pct))
    ceiling_daily = Decimal(str(config.daily_notional_pct))

    if eligible:
        # Evidence passed: restore the operator's ceilings, never beyond them.
        return Limits(
            max_order_pct=str(ceiling_order),
            daily_notional_pct=str(ceiling_daily),
            reason="evidence_passed",
            updated_at=_now(),
        )

    # De-risk once, not on every evaluation. Halving repeatedly spirals the cap
    # below the minimum order size, which silently disables trading entirely
    # instead of trading smaller.
    if current is not None and current.reason == "evidence_not_passed_de_risk":
        return current

    base_order = (
        Decimal(current.max_order_pct) if current is not None else ceiling_order
    )
    base_daily = (
        Decimal(current.daily_notional_pct) if current is not None else ceiling_daily
    )
    return Limits(
        max_order_pct=str(
            _clamp(base_order * DE_RISK_FACTOR, ceiling_order, MIN_ORDER_PCT)
        ),
        daily_notional_pct=str(
            _clamp(base_daily * DE_RISK_FACTOR, ceiling_daily, MIN_DAILY_PCT)
        ),
        reason="evidence_not_passed_de_risk",
        updated_at=_now(),
    )


def _wider_session(base: str, ceiling: str, confidence: float, previous: str) -> str:
    """One step wider than what is already in force, within the operator bound."""
    if base not in SESSION_POLICIES or ceiling not in SESSION_POLICIES:
        return previous or base
    if SESSION_POLICIES.index(ceiling) <= SESSION_POLICIES.index(base):
        return base  # nothing wider was authorised
    if confidence < WIDEN_POLICY_CONFIDENCE:
        return base  # fall back to what the operator configured
    current = previous if previous in SESSION_POLICIES else base
    step = min(SESSION_POLICIES.index(current) + 1, SESSION_POLICIES.index(ceiling))
    return SESSION_POLICIES[max(step, SESSION_POLICIES.index(base))]


def propose_from_assessment(
    config: Any,
    assessment: Any,
    *,
    current: Optional[Limits] = None,
    reset: bool = False,
) -> Limits:
    """Scale the risk budget to the confidence the evidence has earned.

    ``reset`` (kill switch, demotion) drops straight to the floor: after a loss
    event the agent does not get to keep the budget it had talked itself into.
    """
    ceiling_order = Decimal(str(config.max_order_pct))
    ceiling_daily = Decimal(str(config.daily_notional_pct))
    confidence = 0.0 if reset else float(getattr(assessment, "confidence", 0.0) or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    # Grade at the precision we store, so recomputation noise cannot move the
    # budget in either direction.
    confidence = round(confidence, 4)

    scale = MIN_SCALE + (Decimal(1) - MIN_SCALE) * Decimal(str(round(confidence, 6)))
    target_order = _target_for_confidence(ceiling_order, confidence)
    target_daily = _clamp(ceiling_daily * scale, ceiling_daily, MIN_DAILY_PCT)

    previous_confidence = (
        float(current.confidence or 0.0) if current is not None else 0.0
    )
    base_order = (
        Decimal(current.max_order_pct) if current is not None else ceiling_order
    )
    base_daily = (
        Decimal(current.daily_notional_pct) if current is not None else ceiling_daily
    )

    if reset or current is None:
        # No stored budget yet: start where the evidence actually is, rather
        # than at the ceiling. The operator's ceiling is a maximum, not a
        # default the agent has to spend down.
        next_order, next_daily = target_order, target_daily
        reason = "reset_after_demotion" if reset else "confidence_target"
    elif target_order < base_order or target_daily < base_daily:
        # Cut fast: a single bad assessment may halve the budget, but never take
        # it below the target the current confidence justifies.
        next_order = max(target_order, base_order * DE_RISK_FACTOR)
        next_daily = max(target_daily, base_daily * DE_RISK_FACTOR)
        # The budget can be above its target without the evidence getting
        # worse — it sat at a ceiling that confidence does not fully justify
        # yet. Saying "confidence_down" there would be a lie on the console.
        if Decimal(str(round(confidence, 4))) >= Decimal(str(previous_confidence)):
            reason = "budget_above_target"
        else:
            reason = "confidence_down"
    elif target_order > base_order or target_daily > base_daily:
        # Grow slowly, and only while the evidence is not deteriorating: at most
        # one GROW_FACTOR step per assessment, and never past the target the
        # current confidence justifies.
        # A re-run that lands within the stored precision is the same evidence,
        # not evidence getting worse: without this the budget can freeze on
        # floating-point noise.
        if (
            Decimal(str(round(confidence, 4)))
            < Decimal(str(previous_confidence)) - CONFIDENCE_TOLERANCE
        ):
            next_order, next_daily = base_order, base_daily
            reason = "confidence_fell_hold"
        else:
            next_order = min(target_order, base_order * GROW_FACTOR)
            next_daily = min(target_daily, base_daily * GROW_FACTOR)
            reason = "confidence_up"
    else:
        next_order, next_daily = base_order, base_daily
        reason = "confidence_flat"

    base_policy = str(getattr(config, "session_policy", "") or "")
    ceiling_policy = str(
        getattr(config, "max_session_policy", "") or base_policy or ""
    )
    policy = _wider_session(
        base_policy,
        ceiling_policy,
        confidence,
        current.session_policy if current is not None else "",
    )

    return Limits(
        max_order_pct=str(_clamp(next_order, ceiling_order, MIN_ORDER_PCT)),
        daily_notional_pct=str(_clamp(next_daily, ceiling_daily, MIN_DAILY_PCT)),
        reason=reason,
        updated_at=_now(),
        confidence=str(round(confidence, 4)),
        session_policy=policy,
        details={
            "target_max_order_pct": str(round(target_order, 6)),
            "target_daily_notional_pct": str(round(target_daily, 6)),
            "previous_confidence": str(round(previous_confidence, 4)),
            "components": dict(getattr(assessment, "confidence_parts", {}) or {}),
            "ceiling_max_order_pct": str(ceiling_order),
            "ceiling_daily_notional_pct": str(ceiling_daily),
        },
    )


def apply_to_guard(guard: Any, config: Any) -> None:
    """Tighten a RiskGuard to the stored effective limits (never loosen it)."""
    stored = load_limits(config.state_dir)
    if stored is None:
        return
    ceiling_order = Decimal(str(config.max_order_pct))
    ceiling_daily = Decimal(str(config.daily_notional_pct))
    guard.max_order_pct = min(
        Decimal(str(guard.max_order_pct)),
        _clamp(Decimal(stored.max_order_pct), ceiling_order, MIN_ORDER_PCT),
    )
    guard.daily_notional_pct = min(
        Decimal(str(guard.daily_notional_pct)),
        _clamp(Decimal(stored.daily_notional_pct), ceiling_daily, MIN_DAILY_PCT),
    )


def reconcile(config: Any) -> Optional[Limits]:
    """Pull the stored budget back under the operator's *current* ceilings.

    The ladder only re-prices itself when a full evaluation runs, and the
    evaluation is deliberately skipped when the bars have not changed. Without
    this, lowering a ceiling in the config would leave the state file — and
    every screen that reads it — advertising a budget the operator no longer
    authorises, even though the guard itself clamps the live order path.

    Only ever tightens: a raised ceiling is not permission the agent may grant
    itself, it just stops the next assessment from being capped.
    """
    from dataclasses import replace

    stored = load_limits(config.state_dir)
    if stored is None:
        return None
    ceiling_order = Decimal(str(config.max_order_pct))
    ceiling_daily = Decimal(str(config.daily_notional_pct))
    # A schedule is the operator re-pricing the budget for this account size, so
    # the stored budget follows it rather than clamping below it — otherwise a
    # $50 account would keep the 4% day it was given when the config was written
    # for a flat ceiling. A budget that was cut for a *reason* (the evidence
    # failed, or the kill switch fired) is never re-priced up: that cut is the
    # ladder doing its job, and the schedule does not undo it.
    scheduled = bool(getattr(config, "daily_budget_schedule", ()))
    de_risked = str(stored.reason) in (
        "evidence_not_passed_de_risk",
        "reset",
        "kill_switch",
    )
    if scheduled and not de_risked:
        order, daily = ceiling_order, ceiling_daily
    else:
        order = _clamp(Decimal(stored.max_order_pct), ceiling_order, MIN_ORDER_PCT)
        daily = _clamp(Decimal(stored.daily_notional_pct), ceiling_daily, MIN_DAILY_PCT)
    unchanged = order == Decimal(stored.max_order_pct) and daily == Decimal(
        stored.daily_notional_pct
    )
    reason = (
        "ceiling_reconciled"
        if unchanged
        else ("schedule_applied" if scheduled and not de_risked else "ceiling_lowered")
    )
    confidence = float(stored.confidence or 0.0)
    details = dict(stored.details or {})
    if not unchanged:
        # Only record the cut when there was one: rewriting this on every
        # restart would churn the file (and its timestamp) forever.
        details["reconciled_from_max_order_pct"] = stored.max_order_pct
    details["ceiling_max_order_pct"] = str(ceiling_order)
    details["ceiling_daily_notional_pct"] = str(ceiling_daily)
    details["target_max_order_pct"] = str(
        round(_target_for_confidence(ceiling_order, confidence), 6)
    )
    daily_scale = MIN_SCALE + (Decimal(1) - MIN_SCALE) * Decimal(
        str(round(max(0.0, min(1.0, confidence)), 6))
    )
    details["target_daily_notional_pct"] = str(
        round(_clamp(ceiling_daily * daily_scale, ceiling_daily, MIN_DAILY_PCT), 6)
    )
    # The stored budget may already fit the new ceiling while the ceiling it was
    # priced under is still recorded in ``details`` — that record has to move
    # too, or the console shows a budget that was justified by a limit the
    # operator has since withdrawn.
    if unchanged and details == dict(stored.details or {}):
        return stored
    updated = replace(
        stored,
        max_order_pct=str(order),
        daily_notional_pct=str(daily),
        reason=reason,
        updated_at=_now(),
        details=details,
    )
    save_limits(config.state_dir, updated)
    return updated

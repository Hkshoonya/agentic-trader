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
# Confidence must improve by at least this much before a cap may grow.
GROWTH_EPSILON = 0.01

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

    scale = MIN_SCALE + (Decimal(1) - MIN_SCALE) * Decimal(str(round(confidence, 6)))
    target_order = _clamp(ceiling_order * scale, ceiling_order, MIN_ORDER_PCT)
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
        reason = "confidence_down"
    elif target_order > base_order or target_daily > base_daily:
        # Grow slowly, and only while the evidence is not deteriorating: at most
        # one GROW_FACTOR step per assessment, and never past the target the
        # current confidence justifies.
        if confidence + 1e-9 < previous_confidence:
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

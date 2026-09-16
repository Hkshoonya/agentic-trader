"""Risk limits the agent may adjust — bounded by operator ceilings.

The asymmetry is the point:

- **Reducing** a limit needs no permission. De-risking is always safe, so the
  agent may do it on its own the moment evidence stops supporting it.
- **Raising** a limit is capped by the operator's configured values, which are
  the ceiling it can never exceed no matter how confident it becomes, and the
  ceiling is only restored after an assessment that actually passes.

That gives adaptive risk without building a machine that can talk itself into
bigger positions: the worst case is always a limit the human already approved.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class Limits:
    max_order_pct: str
    daily_notional_pct: str
    reason: str
    updated_at: str

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
        )
    except (KeyError, TypeError):
        return None


def save_limits(state_dir: Path | str, limits: Limits) -> Path:
    path = _path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(limits.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def _clamp(value: Decimal, ceiling: Decimal, floor: Decimal) -> Decimal:
    return max(floor, min(value, ceiling))


def propose(
    config: Any,
    *,
    eligible: bool,
    current: Optional[Limits] = None,
) -> Limits:
    """Next set of effective limits, given whether the evidence passed."""
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

"""Per-order confidence: why *this* order looked good, or did not.

The system already reports an **evidence** grade — one number describing how
much the strategy's edge has been proven, recomputed when the evaluation runs
and identical for every order by construction. That is the right number to
size the risk budget from, and the wrong number to put next to an order: it
cannot tell "the edge is thin" apart from "this entry is chasing a spike".

This module produces the second number. It is a **reporting** grade only: it
never sizes a trade, never overrides a veto, and never blocks anything the rest
of the pipeline allowed. Its job is to make each decision explainable, so a
rejection the operator disagrees with can be traced to the feature that caused
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Weights sum to 1. Trend dominates because the rule is a trend rule; the rest
# are the ways a trend entry usually fails.
_WEIGHTS = {
    "trend": 0.30,
    "extension": 0.16,
    "volume": 0.12,
    "liquidity": 0.12,
    "regime": 0.18,
    "volatility": 0.12,
}


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _linear(value: float, low: float, high: float) -> float:
    """0 at ``low``, 1 at ``high``; flat outside."""
    if high == low:
        return 1.0 if value >= high else 0.0
    return _clip((value - low) / (high - low))


@dataclass
class OrderConfidence:
    score: float
    verdict: str
    parts: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "verdict": self.verdict,
            "parts": {name: round(value, 4) for name, value in self.parts.items()},
            "notes": dict(self.notes),
        }


def grade_order(
    features: Any,
    *,
    regime: Optional[dict[str, Any]] = None,
    advisor: Optional[dict[str, Any]] = None,
    blocked_by: Optional[str] = None,
) -> OrderConfidence:
    """Score one order's own evidence on 0..1.

    ``features`` is a :class:`agentic_trading.llm.market.MarketFeatures` (or any
    object with the same attributes); ``regime`` is the gate's view for the
    symbol, if one has been computed.
    """
    parts: dict[str, float] = {}
    notes: dict[str, str] = {}

    # Trend: the rule wants the multi-horizon vote positive, and it wants the
    # move to be a trend rather than a single bar.
    ret_5 = float(getattr(features, "ret_5_pct", 0.0) or 0.0)
    ret_20 = float(getattr(features, "ret_20_pct", 0.0) or 0.0)
    trend_pct = float(getattr(features, "trend_pct", 0.0) or 0.0)
    parts["trend"] = _clip(0.5 * _linear(ret_5, 0.0, 3.0) + 0.5 * _linear(ret_20, 0.0, 6.0))
    if trend_pct < 0:
        parts["trend"] = min(parts["trend"], 0.25)
        notes["trend"] = f"trend slope is negative ({trend_pct:.1f}%)"

    # Extension: a good trend entry is not a parabolic one. Price pinned at the
    # top of its recent range after a >20% run is a chase, not a signal.
    range_position = float(getattr(features, "range_position", 0.5) or 0.0)
    parts["extension"] = 1.0 - _linear(ret_20, 12.0, 30.0) * _linear(range_position, 0.75, 1.0)
    if parts["extension"] < 0.5:
        notes["extension"] = (
            f"extended: {ret_20:+.1f}% over 20 bars at range position {range_position:.2f}"
        )

    # Volume: a move on no volume is a move that can vanish.
    volume_z = getattr(features, "volume_z", None)
    coverage = getattr(features, "volume_coverage", None)
    if volume_z is None:
        # Not measured is not the same as measured bad — neutral, and say so.
        parts["volume"] = 0.5
        notes["volume"] = "volume not measured for this symbol"
    elif coverage is not None and float(coverage) < 0.5:
        parts["volume"] = 0.35
        notes["volume"] = f"volume history only {float(coverage):.0%} complete"
    else:
        parts["volume"] = _clip(0.5 + 0.25 * float(volume_z))

    # Liquidity: a wide spread eats the edge before the trade exists.
    spread = getattr(features, "spread_bps", None)
    if spread is None:
        parts["liquidity"] = 0.5
        notes["liquidity"] = "spread not measured"
    else:
        parts["liquidity"] = 1.0 - _linear(float(spread), 5.0, 40.0)
        if float(spread) > 40:
            notes["liquidity"] = f"spread {float(spread):.0f}bps is wider than the edge"

    # Regime: the gate's own confidence that conditions suit the rule.
    if regime is None:
        parts["regime"] = 0.5
        notes["regime"] = "no regime read yet"
    else:
        label = str(regime.get("regime", "")).lower()
        confidence = float(regime.get("confidence", 0.0) or 0.0)
        if label in ("trend_up", "trend", "bull"):
            parts["regime"] = _clip(confidence)
        elif label == "chop":
            parts["regime"] = _clip(1.0 - confidence)
        elif label in ("panic", "trend_down", "bear"):
            parts["regime"] = _clip((1.0 - confidence) * 0.5)
        else:
            parts["regime"] = 0.5
        notes["regime"] = f"{label} c={confidence:.2f}"

    # Volatility: the same trend is worth less when the instrument moves 8% a
    # day, because the stop distance — and therefore the risk — is larger.
    vol_pct = float(getattr(features, "vol_pct", 0.0) or 0.0)
    annualised = vol_pct * (252**0.5)
    parts["volatility"] = 1.0 - _linear(annualised, 45.0, 120.0)
    if annualised >= 100.0:
        notes["volatility"] = f"{annualised:.0f}% annualised vol"

    score = sum(_WEIGHTS[name] * value for name, value in parts.items())

    if blocked_by:
        verdict = "rejected"
        notes["verdict"] = blocked_by
    elif advisor and str(advisor.get("action", "")).lower() in ("veto", "skip"):
        verdict = "rejected"
        notes["verdict"] = f"advisor veto: {str(advisor.get('reason', ''))[:120]}"
    elif score >= 0.65:
        verdict = "buy"
    elif score >= 0.45:
        verdict = "marginal"
    else:
        verdict = "weak"
    return OrderConfidence(score=score, verdict=verdict, parts=parts, notes=notes)

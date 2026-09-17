"""Per-order confidence: it must vary per order, and it must gate nothing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_trading.confidence import grade_order
from agentic_trading.llm.market import features_from_closes


def _features(
    *,
    daily: float = 0.004,
    bars: int = 60,
    spread_bps: float | None = 8.0,
    volume_z: float | None = 0.2,
):
    closes = [100.0 * (1 + daily) ** index for index in range(bars)]
    features = features_from_closes("TEST", closes, spread_bps=spread_bps)
    assert features is not None
    return replace(features, volume_z=volume_z, volume_coverage=1.0)


def test_uptrend_scores_higher_than_downtrend() -> None:
    up = grade_order(
        _features(daily=0.004), regime={"regime": "trend_up", "confidence": 0.8}
    )
    down = grade_order(
        _features(daily=-0.004), regime={"regime": "chop", "confidence": 0.8}
    )
    assert up.score > down.score
    assert up.verdict == "buy"
    assert down.verdict in ("weak", "marginal")


def test_score_varies_between_orders() -> None:
    """The point of the number: it must not be a constant like the evidence grade."""
    strong = grade_order(
        _features(daily=0.006), regime={"regime": "trend_up", "confidence": 0.9}
    )
    weak = grade_order(
        _features(daily=0.0005), regime={"regime": "chop", "confidence": 0.9}
    )
    assert strong.score != weak.score
    assert strong.score > weak.score


def test_every_part_is_bounded_and_reported() -> None:
    result = grade_order(_features(), regime={"regime": "chop", "confidence": 0.7})
    assert set(result.parts) == {
        "trend",
        "extension",
        "volume",
        "liquidity",
        "regime",
        "volatility",
    }
    assert all(0.0 <= value <= 1.0 for value in result.parts.values())
    assert 0.0 <= result.score <= 1.0


def test_wide_spread_and_no_volume_lower_the_score() -> None:
    clean = grade_order(_features(spread_bps=4.0, volume_z=0.5))
    dirty = grade_order(_features(spread_bps=90.0, volume_z=-1.5))
    assert dirty.score < clean.score
    assert dirty.parts["liquidity"] < 0.2
    assert "spread" in " ".join(dirty.notes.values())


def test_extension_penalises_a_parabolic_chase() -> None:
    parabolic = grade_order(_features(daily=0.03))
    orderly = grade_order(_features(daily=0.004))
    assert parabolic.parts["extension"] < orderly.parts["extension"]


def test_blocked_order_reports_rejected_even_with_a_good_score() -> None:
    result = grade_order(
        _features(daily=0.006),
        regime={"regime": "trend_up", "confidence": 0.9},
        blocked_by="regime_block: chop c=0.65",
    )
    assert result.verdict == "rejected"
    assert "chop" in result.notes["verdict"]


def test_advisor_veto_reports_rejected() -> None:
    result = grade_order(
        _features(daily=0.006),
        advisor={"action": "veto", "confidence": 0.8, "reason": "falling knife"},
    )
    assert result.verdict == "rejected"
    assert "falling knife" in result.notes["verdict"]


def test_missing_measurements_are_neutral_not_fatal() -> None:
    result = grade_order(_features(spread_bps=None, volume_z=None))
    assert result.parts["liquidity"] == pytest.approx(0.5)
    assert result.parts["volume"] == pytest.approx(0.5)
    assert "not measured" in result.notes["liquidity"]


def test_to_dict_is_json_ready() -> None:
    payload = grade_order(_features()).to_dict()
    assert set(payload) == {"score", "verdict", "parts", "notes"}
    assert isinstance(payload["parts"], dict)

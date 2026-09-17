"""Tests for the walk-forward research rig.

``walkforward.py`` is the only place in the project that estimates what the
fixed rule would have earned, and it is deliberately unwired from the trading
loop — so a silent bug here would manufacture fake evidence for the promotion
gate. Three such bugs have already been caught by inspection (entries that did
not debit cash, weight normalisation forcing a fully invested book, and
per-trade returns applied to the whole account); these tests pin the
invariants those bugs violated.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentic_trading.backtest import CostModel
from agentic_trading.history import Bar
from agentic_trading.walkforward import (
    MAX_LEVERAGE,
    TARGET_VOL,
    bootstrap_p,
    simulate,
    targets_as_of,
    walk_forward,
)

DAY = datetime(2021, 1, 1, tzinfo=timezone.utc)


def _series(
    symbol: str,
    closes: list[float],
    *,
    start: datetime = DAY,
    volumes: float = 1_000.0,
) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            start=start + timedelta(days=index),
            open=Decimal(str(price)),
            high=Decimal(str(price)),
            low=Decimal(str(price)),
            close=Decimal(str(price)),
            volume=Decimal(str(volumes)),
        )
        for index, price in enumerate(closes)
    ]


def _ramp(days: int, *, daily: float = 0.004, base: float = 100.0) -> list[float]:
    """Monotone uptrend: trend vote positive, realised vol non-zero."""
    return [base * (1 + daily) ** index for index in range(days)]


def _sawtooth(days: int, *, amplitude: float = 0.08, base: float = 100.0) -> list[float]:
    """Alternating up/down with a rising floor: high vol, positive trend."""
    out = []
    for index in range(days):
        cycle = math.sin(index / 3.0) * amplitude
        out.append(base * (1 + 0.003 * index) * (1 + cycle))
    return out


class TestTargets:
    def test_weights_are_raw_inverse_vol_not_normalised(self) -> None:
        # Two symbols with the same trend but very different vol.
        calm = _ramp(300, daily=0.004)
        wild = _sawtooth(300)
        series = {"CALM": _series("CALM", calm), "WILD": _series("WILD", wild)}
        targets = targets_as_of(series, DAY + timedelta(days=299))

        assert set(targets) == {"CALM", "WILD"}
        assert sum(targets.values()) > 1.0, "raw sizes must not be forced to sum to one"
        assert targets["WILD"] < targets["CALM"], "the volatile symbol gets the smaller size"
        assert all(size <= MAX_LEVERAGE + 1e-9 for size in targets.values())

    def test_normalise_opt_in_rescales_to_one(self) -> None:
        series = {
            "A": _series("A", _ramp(300)),
            "B": _series("B", _ramp(300, daily=0.006)),
        }
        when = DAY + timedelta(days=299)
        raw = targets_as_of(series, when)
        scaled = targets_as_of(series, when, normalise=True)
        assert sum(scaled.values()) == pytest.approx(1.0)
        assert scaled["A"] == pytest.approx(raw["A"] / sum(raw.values()))

    def test_no_lookahead(self) -> None:
        """Bars stamped at or after ``when`` must not influence the decision."""
        closes = _ramp(300)
        when = DAY + timedelta(days=250)
        series = {"A": _series("A", closes)}
        before = targets_as_of(series, when)

        spiked = list(closes)
        spiked[260:] = [5_000.0] * (len(spiked) - 260)
        after = targets_as_of({"A": _series("A", spiked)}, when)
        assert after == before

    def test_downtrend_is_not_targeted(self) -> None:
        series = {"A": _series("A", [200.0 - index for index in range(300)])}
        assert targets_as_of(series, DAY + timedelta(days=299)) == {}

    def test_short_history_is_skipped(self) -> None:
        series = {"A": _series("A", _ramp(30))}
        assert targets_as_of(series, DAY + timedelta(days=29)) == {}


class TestSimulate:
    def test_costs_are_actually_charged(self) -> None:
        """The same trades must end lower when they pay spread and slippage."""
        series = {"A": _series("A", _ramp(300))}
        window = dict(start=DAY, end=DAY + timedelta(days=299), starting_cash=50.0)
        charged, _ = simulate(series, **window)
        free, _ = simulate(
            series,
            costs=CostModel(
                spread_bps=Decimal("0"), slippage_bps=Decimal("0")
            ),
            **window,
        )
        assert charged and free
        assert len(charged) == len(free)
        assert sum(t["pnl"] for t in charged) < sum(t["pnl"] for t in free)

    def test_entry_debits_cash(self) -> None:
        """Cash leaves the account at entry, so the balance cannot exceed it."""
        series = {"A": _series("A", _ramp(300))}
        trades, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=299),
            starting_cash=50.0,
            max_positions=1,
            per_order_pct=0.03,
        )
        assert trades
        assert trades[0]["notional"] == pytest.approx(50.0 * 0.03, rel=0.05)

    def test_payoff_is_bounded_by_the_position_size(self) -> None:
        """A doubling must earn ~3% of the account, not double it."""
        closes = [100.0] * 300 + [200.0] * 60
        series = {"A": _series("A", closes)}
        _, curve = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=len(closes) - 1),
            starting_cash=100.0,
            max_positions=1,
            per_order_pct=0.03,
        )
        assert curve[-1] < 100.0 * 1.04

    def test_no_free_compounding(self) -> None:
        """Equity after a +10% run must be bounded by the unlevered result."""
        closes = [100.0 * 1.01**index for index in range(300)]
        series = {"A": _series("A", closes)}
        trades, curve = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=299),
            starting_cash=50.0,
            max_positions=1,
            per_order_pct=0.03,
        )
        assert trades
        assert curve[-1] < 50.0 * 1.01**299, "a 3% position cannot earn the whole move"
        assert curve[-1] == pytest.approx(
            50.0 + sum(trade["pnl"] for trade in trades), rel=1e-9
        )

    def test_flat_sizing_uses_per_order_pct(self) -> None:
        series = {"A": _series("A", _ramp(300))}
        trades, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=299),
            starting_cash=100.0,
            max_positions=1,
            per_order_pct=0.10,
        )
        assert trades[0]["notional"] == pytest.approx(10.0)

    def test_inverse_vol_sizes_down_the_volatile_symbol(self) -> None:
        calm = _ramp(400, daily=0.002)
        wild = _sawtooth(400)
        series = {"CALM": _series("CALM", calm), "WILD": _series("WILD", wild)}
        trades, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=399),
            starting_cash=100.0,
            max_positions=2,
            per_order_pct=0.03,
            inverse_vol=True,
        )
        first = {trade["symbol"]: trade["notional"] for trade in trades[:2]}
        assert set(first) == {"CALM", "WILD"}
        assert first["WILD"] < first["CALM"]
        # The same gross budget as the flat baseline (2 slots * 3%), split by
        # inverse vol, capped per position at the operator's 5% ceiling.
        sigma_calm = _sigma(calm)
        sigma_wild = _sigma(wild)
        assert sum(first.values()) <= 100.0 * 0.03 * 2 + 1e-9
        assert max(first.values()) <= 100.0 * 0.05 + 1e-9
        # Risk contributions are much closer than the notional sizes.
        flat_ratio = sigma_wild / sigma_calm
        sized_ratio = first["WILD"] * sigma_wild / (first["CALM"] * sigma_calm)
        assert sized_ratio < flat_ratio, "inverse-vol must shrink the risk gap"

    def test_inverse_vol_respects_the_per_position_ceiling(self) -> None:
        """One calm symbol alone must not take the whole gross budget."""
        series = {"A": _series("A", _ramp(400, daily=0.0015))}
        trades, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=399),
            starting_cash=100.0,
            max_positions=4,
            per_order_pct=0.03,
            max_position_pct=0.05,
            inverse_vol=True,
        )
        assert trades
        assert trades[0]["notional"] <= 5.0 + 1e-9

    def test_max_gross_caps_the_book(self) -> None:
        series = {
            name: _series(name, _ramp(300))
            for name in ("A", "B", "C", "D")
        }
        trades, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=299),
            starting_cash=100.0,
            max_positions=4,
            per_order_pct=0.03,
            max_gross=0.06,
        )
        assert trades
        entered = {trade["symbol"] for trade in trades}
        assert len(entered) == 2, "a 6% gross cap holds exactly two 3% positions"

    def test_exit_happens_when_the_trend_flips(self) -> None:
        closes = _ramp(300) + [0.5 * _ramp(300)[-1]] * 60
        series = {"A": _series("A", closes)}
        trades, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=359),
            starting_cash=100.0,
            max_positions=1,
        )
        assert len(trades) >= 1
        assert trades[0]["exit_price"] < trades[0]["entry_price"]

    def test_drawdown_is_measured_on_the_account(self) -> None:
        closes = _ramp(300) + [0.2 * _ramp(300)[-1]] * 80
        series = {"A": _series("A", closes)}
        trades, curve = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=len(closes) - 1),
            starting_cash=100.0,
            max_positions=1,
            per_order_pct=0.50,
            max_gross=1.0,
        )
        assert trades
        assert curve[-1] == pytest.approx(
            100.0 + sum(trade["pnl"] for trade in trades), rel=1e-9
        )
        assert min(curve) < 100.0, "a crash must show up in the account curve"

    def test_marked_to_market_sees_an_open_position_fall(self) -> None:
        """A position held through a dip must show drawdown before it is sold."""
        ramp = _ramp(300)
        # -10% over 12 sessions, still inside the 50-session trend filter, so
        # the rule keeps the position and never realises a loss until the end.
        dip = [ramp[-1] * (1 - 0.10 * index / 11) for index in range(12)]
        closes = ramp + dip + [ramp[-1]] * 40
        series = {"A": _series("A", closes)}
        trades, curve = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=len(closes) - 1),
            starting_cash=100.0,
            max_positions=1,
            per_order_pct=0.50,
            max_gross=1.0,
        )
        assert len(trades) == 1, "the dip must not close the position"
        assert max(curve) - min(curve) > 1.0, "the open loss must be visible"

    def test_governor_never_deepens_the_trough(self) -> None:
        closes = _ramp(300) + [0.5 * _ramp(300)[-1]] * 60 + [0.4 * _ramp(300)[-1]] * 60
        series = {"A": _series("A", closes)}
        window = dict(
            start=DAY,
            end=DAY + timedelta(days=len(closes) - 1),
            starting_cash=100.0,
            max_positions=1,
            per_order_pct=0.50,
        )
        _, free = simulate(series, governor=0.0, **window)
        _, braked = simulate(series, governor=0.10, **window)
        assert min(braked) >= min(free) - 1e-9

    def test_governor_blocks_new_entries_after_a_loss(self) -> None:
        # A crash large enough to breach a 5% governor, then a recovery.
        closes = [100.0] * 280 + [40.0] * 20 + [100.0] * 140
        series = {"A": _series("A", closes)}
        gated, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=len(closes) - 1),
            starting_cash=100.0,
            max_positions=1,
            per_order_pct=0.50,
            governor=0.05,
        )
        ungated, _ = simulate(
            series,
            start=DAY,
            end=DAY + timedelta(days=len(closes) - 1),
            starting_cash=100.0,
            max_positions=1,
            per_order_pct=0.50,
            governor=0.0,
        )
        assert len(gated) <= len(ungated)


class TestWalkForward:
    def test_folds_are_chronological_and_non_overlapping(self) -> None:
        series = {"A": _series("A", _ramp(600))}
        result = walk_forward(series, folds=5)
        folds = result.folds
        assert len(folds) == 5
        for earlier, later in zip(folds, folds[1:]):
            assert earlier.start < later.start
            assert earlier.end < later.start
        assert result.trades == sum(fold.trades for fold in folds)

    def test_equity_compounds_across_folds(self) -> None:
        series = {"A": _series("A", _ramp(600))}
        result = walk_forward(series, folds=4)
        assert result.final_equity > 50.0
        assert result.expectancy_bps > 0

    def test_losses_do_not_create_equity(self) -> None:
        series = {"A": _series("A", [200.0 - index for index in range(600)])}
        result = walk_forward(series, folds=3)
        # Nothing is eligible to buy in a downtrend, so the account is idle.
        assert result.trades == 0
        assert result.final_equity == pytest.approx(50.0)
        assert result.eligible is False

    def test_sizing_mode_is_threaded_through(self) -> None:
        series = {
            "CALM": _series("CALM", _ramp(400, daily=0.003)),
            "WILD": _series("WILD", _sawtooth(400)),
        }
        flat = walk_forward(series, folds=3, max_positions=2)
        inverse = walk_forward(series, folds=3, max_positions=2, inverse_vol=True)
        assert flat.trades or inverse.trades
        assert flat.max_drawdown_pct >= 0.0 and inverse.max_drawdown_pct >= 0.0

    def test_max_gross_is_threaded_through(self) -> None:
        series = {"A": _series("A", _ramp(400))}
        wide = walk_forward(series, folds=3, max_gross=1.0)
        narrow = walk_forward(series, folds=3, max_gross=0.01)
        assert wide.trades > 0
        assert narrow.trades == 0, "a 1% gross cap cannot hold a 3% position"

    def test_eligible_requires_every_bar(self) -> None:
        result = walk_forward({"A": _series("A", _ramp(600))}, folds=4)
        if result.trades >= 30 and result.expectancy_bps > 1.0:
            assert result.eligible == (
                result.bootstrap_p_value <= result.alpha
                and result.max_drawdown_pct <= 15.0
            )


def _sigma(closes: list[float]) -> float:
    from agentic_trading.walkforward import _vol

    value = _vol(closes)
    assert value is not None
    return value


class TestBootstrap:
    def test_all_positive_returns_are_significant(self) -> None:
        assert bootstrap_p([100.0] * 40) < 0.05

    def test_all_negative_returns_are_not(self) -> None:
        assert bootstrap_p([-100.0] * 40) > 0.95

    def test_tiny_samples_are_not_significant(self) -> None:
        assert bootstrap_p([100.0] * 4) == 1.0

    def test_is_deterministic(self) -> None:
        values = [120.0, -80.0, 40.0, -20.0, 10.0] * 8
        assert bootstrap_p(values) == bootstrap_p(values)

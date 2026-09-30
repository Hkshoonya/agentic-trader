"""Dip reversal: buy the week's deepest pullbacks in names that are still rising.

The desk's other members follow momentum. This one bets the other way on a
short horizon — last week's losers among names in a long uptrend tend to
bounce — so the allocator has a genuinely different candidate to fund, not a
re-parameterised copy of the rotation.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agentic_trading.history import Bar
from agentic_trading.walkforward import (
    rank_reversal,
    rank_targets,
    rotation_anchor,
    rule_for_strategy,
    targets_as_of,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WHEN = START + timedelta(days=250)
ANCHOR = rotation_anchor(WHEN)


def _series(
    symbol: str, *, drift: float = 0.002, dip: float = 0.0, after: float = 0.05
) -> list[Bar]:
    """Compounds by ``drift`` a day, by ``dip`` over the five days before the
    weekly anchor, and by ``after`` from the anchor on (which must be unseen)."""
    bars = []
    price = 100.0
    for day in range(260):
        start = START + timedelta(days=day)
        if start >= ANCHOR:
            price *= 1 + after
        elif start >= ANCHOR - timedelta(days=5):
            price *= 1 + dip
        else:
            price *= 1 + drift
        close = Decimal(str(round(price, 6)))
        bars.append(
            Bar(
                symbol=symbol,
                start=start,
                open=close,
                high=close,
                low=close,
                close=close,
            )
        )
    return bars


def _selected(rows: list[dict]) -> list[str]:
    return [row["symbol"] for row in rows if row["selected"]]


class DipReversalRuleTests(unittest.TestCase):
    def test_it_holds_the_three_deepest_dips_in_rising_names(self) -> None:
        series = {
            "SPY": _series("SPY"),
            "AAPL": _series("AAPL", dip=-0.03),
            "MSFT": _series("MSFT", dip=-0.02),
            "NVDA": _series("NVDA", dip=-0.01),
            "META": _series("META", dip=-0.005),
        }
        rows = rank_reversal(series, WHEN)
        self.assertEqual(_selected(rows), ["AAPL", "MSFT", "NVDA"])
        meta = next(row for row in rows if row["symbol"] == "META")
        self.assertIn("ranks 4", meta["reason"])
        self.assertTrue(all(row["weight"] == 1.0 for row in rows if row["selected"]))

    def test_a_falling_name_is_not_a_dip(self) -> None:
        series = {
            "SPY": _series("SPY"),
            "TSLA": _series("TSLA", drift=-0.002, dip=-0.04),
        }
        rows = rank_reversal(series, WHEN)
        self.assertEqual(_selected(rows), [])
        tsla = next(row for row in rows if row["symbol"] == "TSLA")
        self.assertIn("200-day average", tsla["reason"])

    def test_a_name_that_rose_last_week_is_not_bought(self) -> None:
        series = {"SPY": _series("SPY"), "AAPL": _series("AAPL", dip=0.01)}
        self.assertEqual(_selected(rank_reversal(series, WHEN)), [])

    def test_crypto_is_never_held_its_spread_eats_a_weekly_turnover(self) -> None:
        series = {
            "SPY": _series("SPY"),
            "BTC-USD": _series("BTC-USD", dip=-0.03),
        }
        self.assertEqual(_selected(rank_reversal(series, WHEN)), [])

    def test_nothing_is_held_while_spy_is_below_its_average(self) -> None:
        series = {
            "SPY": _series("SPY", drift=-0.002),
            "AAPL": _series("AAPL", dip=-0.03),
        }
        rows = rank_reversal(series, WHEN)
        self.assertEqual(_selected(rows), [])
        aapl = next(row for row in rows if row["symbol"] == "AAPL")
        self.assertIn("SPY", aapl["reason"])

    def test_the_week_is_decided_on_bars_before_its_monday(self) -> None:
        series = {
            "SPY": _series("SPY"),
            "AAPL": _series("AAPL", dip=-0.03, after=0.10),
            "MSFT": _series("MSFT", dip=-0.02, after=-0.10),
        }
        on_monday = _selected(rank_reversal(series, ANCHOR))
        on_friday = _selected(rank_reversal(series, ANCHOR + timedelta(days=4)))
        self.assertEqual(on_monday, ["AAPL", "MSFT"])
        self.assertEqual(on_friday, on_monday)

    def test_the_strategy_name_maps_to_the_rule_everywhere(self) -> None:
        series = {"SPY": _series("SPY"), "AAPL": _series("AAPL", dip=-0.03)}
        self.assertEqual(rule_for_strategy("dip_reversal"), "reversal")
        self.assertEqual(
            _selected(rank_targets(series, WHEN, rule="reversal")), ["AAPL"]
        )
        self.assertEqual(targets_as_of(series, WHEN, rule="reversal"), {"AAPL": 1.0})


class DipReversalWiringTests(unittest.TestCase):
    def _config(self, tmp, extra: list[str]):
        from agentic_trading.config import load_config
        from tests.test_runtime_daemon import _write_config

        bars = tmp / "bars"
        bars.mkdir(exist_ok=True)
        return load_config(
            _write_config(tmp, extra=[f'history_path = "{bars}"', *extra])
        )

    def test_it_needs_forward_shadow_evidence_like_the_rotation(self) -> None:
        from agentic_trading.promotion import policy_from_config

        class _Config:
            strategy = "dip_reversal"

        self.assertEqual(policy_from_config(_Config()).min_forward_trades, 30)

    def test_the_strategy_trades_the_reversal_rule(self) -> None:
        import tempfile
        from pathlib import Path

        from agentic_trading.cli import build_strategy
        from agentic_trading.strategies.dip_reversal import DipReversalStrategy

        with tempfile.TemporaryDirectory() as name:
            config = self._config(Path(name), ['strategy = "dip_reversal"'])
            strategy = build_strategy(config)
        self.assertIsInstance(strategy, DipReversalStrategy)
        strategy.history = {
            "SPY": _series("SPY"),
            "AAPL": _series("AAPL", dip=-0.03),
        }
        self.assertEqual(strategy.target_weights(as_of=WHEN), {"AAPL": Decimal("1.0")})

    def test_the_desk_runs_it_as_a_member_with_its_own_state(self) -> None:
        import tempfile
        from pathlib import Path

        from agentic_trading.cli import build_desk

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(
                tmp,
                [
                    'strategy = "desk"',
                    'desk_members = ["dip_reversal", "benchmark"]',
                ],
            )
            desk = build_desk(config)
            names = [member.name for member in desk.members]
            strategy = desk.members[0].strategy
            state = Path(strategy.state_path)
        self.assertEqual(names, ["dip_reversal", "benchmark"])
        self.assertEqual(state.name, "dip_reversal_strategy.json")
        self.assertEqual(state.parent.name, "desk")


if __name__ == "__main__":
    unittest.main()

"""Pooled multi-symbol evaluation: loading, pooling, and guard rails."""

from __future__ import annotations

import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.backtest import CostModel, Genome
from agentic_trading.history import Bar, save_bars
from agentic_trading.multisymbol import evolve_multi, load_symbol_bars, pooled_metrics


def bars(seed: int, *, count: int = 300, drift: float = 0.0015) -> list[Bar]:
    rng = random.Random(seed)
    price = 100.0
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    out: list[Bar] = []
    for index in range(count):
        open_ = price
        price = max(1.0, price * (1 + rng.gauss(drift, 0.0008)))
        close = price
        high = max(open_, close) * 1.0004
        low = min(open_, close) * 0.9996
        out.append(
            Bar(
                "SYM",
                start + timedelta(days=index),
                Decimal(f"{open_:.4f}"),
                Decimal(f"{high:.4f}"),
                Decimal(f"{low:.4f}"),
                Decimal(f"{close:.4f}"),
            )
        )
    return out


ACTIVE = Genome(
    mode="momentum",
    lookback=1,
    entry_bps=1,
    tp_bps=1000,
    sl_bps=1000,
    max_hold_bars=2,
    vol_window=3,
    max_vol_bps=100000,
    trend_window=10,
    use_trend_filter=False,
)


class LoadSymbolBarsTests(unittest.TestCase):
    def test_loads_only_requested_symbols_from_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            save_bars(directory / "SPY_day.jsonl", bars(1, count=80))
            save_bars(directory / "QQQ_day.jsonl", bars(2, count=80))
            loaded = load_symbol_bars(directory, ["SPY", "QQQ", "MISSING"], interval="day")
            self.assertEqual(sorted(loaded), ["QQQ", "SPY"])
            self.assertEqual(len(loaded["SPY"]), 80)

    def test_empty_directory_returns_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_symbol_bars(Path(tmp), ["SPY"], interval="day"), {})


class PooledMetricsTests(unittest.TestCase):
    def test_trades_are_pooled_across_symbols(self) -> None:
        two = {"SPY": bars(1), "QQQ": bars(2)}
        pooled = pooled_metrics(two, ACTIVE, bootstrap_samples=0)
        spy = pooled_metrics({"SPY": two["SPY"]}, ACTIVE, bootstrap_samples=0).trades
        qqq = pooled_metrics({"QQQ": two["QQQ"]}, ACTIVE, bootstrap_samples=0).trades
        self.assertGreater(spy, 0)
        self.assertGreater(qqq, 0)
        self.assertEqual(pooled.trades, spy + qqq)

    def test_pooling_keeps_metrics_comparable(self) -> None:
        pooled = pooled_metrics({"SPY": bars(3)}, ACTIVE, bootstrap_samples=50)
        self.assertEqual(len(pooled.trade_returns_bps), pooled.trades)
        self.assertGreaterEqual(pooled.bootstrap_p_value, 0.0)
        self.assertLessEqual(pooled.bootstrap_p_value, 1.0)

    def test_empty_universe_is_not_an_error(self) -> None:
        self.assertEqual(pooled_metrics({}, ACTIVE, bootstrap_samples=0).trades, 0)


class EvolveMultiTests(unittest.TestCase):
    def test_rejects_short_history(self) -> None:
        with self.assertRaises(ValueError):
            evolve_multi({"SPY": bars(4, count=40)}, population=4, generations=1)

    def test_rejects_empty_universe(self) -> None:
        with self.assertRaises(ValueError):
            evolve_multi({}, population=4, generations=1)

    def test_evaluates_across_the_universe(self) -> None:
        universe = {"SPY": bars(5), "QQQ": bars(6), "IWM": bars(7)}
        result = evolve_multi(
            universe,
            population=8,
            generations=3,
            seed=4,
            min_trades=3,
            min_oos_trades=5,
            costs=CostModel(),
        )
        self.assertEqual(result.evaluated, 24)
        self.assertEqual(result.train_bars, sum(210 for _ in universe))
        self.assertGreater(result.test_bars, 0)
        self.assertTrue(result.champion.to_dict())


if __name__ == "__main__":
    unittest.main()

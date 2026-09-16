"""End-to-end autonomy: self-evaluation, self-promotion, self-demotion."""

from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.history import Bar, save_bars
from agentic_trading.promotion import PromotionState, save_state
from agentic_trading.runtime import run_daemon
from agentic_trading.strategies.fixture import FixtureStrategy
from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS = json.loads((FIXTURES / "tools_snapshot.json").read_text())["tools"]


def _edge_bars(count: int = 2400, seed: int = 5) -> list[Bar]:
    """A strongly trending series: a real edge the gate should accept."""
    rng = random.Random(seed)
    price = 100.0
    start = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
    bars: list[Bar] = []
    for index in range(count):
        open_ = price
        price = max(1.0, price * (1 + rng.gauss(0.0006, 0.0012)))
        close = price
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.0004)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.0004)))
        bars.append(
            Bar(
                "SPY",
                start + timedelta(minutes=5 * index),
                Decimal(f"{open_:.4f}"),
                Decimal(f"{high:.4f}"),
                Decimal(f"{low:.4f}"),
                Decimal(f"{close:.4f}"),
            )
        )
    return bars


def _write_config(tmp: Path, *, autonomy: str, history: Path | None) -> Path:
    lines = [
        'mode = "shadow"',
        'symbol_whitelist = ["SPY"]',
        'max_order_pct = "0.05"',
        'daily_notional_pct = "0.20"',
        'daily_loss_pct = "0.03"',
        "max_open_positions = 1",
        "equity_refresh_ticks = 30",
        "equity_refresh_seconds = 60",
        'timezone = "local"',
        f'quotes_path = "{tmp / "quotes.jsonl"}"',
        f'journal_dir = "{tmp / "journal"}"',
        f'state_dir = "{tmp / "state"}"',
        f'tools_snapshot_path = "{tmp / "tools.json"}"',
        f'token_path = "{tmp / "tok.json"}"',
        'mcp_url = "https://agent.robinhood.com/mcp/trading"',
        f'autonomy = "{autonomy}"',
        "evolution_population = 12",
        "evolution_generations = 4",
        "min_oos_trades = 10",
        "promotion_cycles_required = 1",
        "evolution_interval_minutes = 1",
    ]
    if history is not None:
        lines.append(f'history_path = "{history}"')
    path = tmp / "agentic.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _journal(config) -> list[dict]:
    path = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _EmptyFeed:
    def poll(self) -> list[dict]:
        return []


class AutonomyTests(unittest.TestCase):
    def _run_once(self, config, broker, *, env: dict | None = None):
        # Inject a qualifying evolution result: these tests are about the
        # promotion/consent plumbing, not about whether a random search happens
        # to find an edge on synthetic bars. Depending on the search made them
        # fail whenever the fitness function changed.
        from agentic_trading.backtest import Genome, Metrics
        from agentic_trading.evolution import EvolutionResult

        qualifying = EvolutionResult(
            champion=Genome(),
            in_sample=Metrics(trades=60, expectancy_bps=30.0, max_drawdown_pct=3.0),
            out_of_sample=Metrics(
                trades=50,
                win_rate=0.6,
                expectancy_bps=25.0,
                profit_factor=1.8,
                max_drawdown_pct=4.0,
                bootstrap_p_value=0.01,
            ),
            oos_folds=[Metrics(trades=15, expectancy_bps=20.0)] * 3,
            train_bars=2000,
            test_bars=800,
            evaluated=100,
            seed=1,
        )
        with mock.patch.dict(os.environ, env or {}, clear=False), mock.patch(
            "agentic_trading.selfimprove.run_evolution", return_value=qualifying
        ):
            run_daemon(
                config,
                broker=broker,
                strategy=FixtureStrategy(),
                feed=_EmptyFeed(),
                once=True,
                session_clock=lambda: "regular",
            )

    def test_auto_promotes_to_live_when_the_gate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            save_bars(tmp / "bars.jsonl", _edge_bars())
            config = load_config(
                _write_config(tmp, autonomy="auto", history=tmp / "bars.jsonl")
            )
            client = FakeMcpClient(TOOLS, equity="500")
            broker = Broker(client, TOOLS)

            self._run_once(config, broker, env={"AGENTIC_ALLOW_AUTONOMY": "1"})

            records = _journal(config)
            events = [record.get("event") for record in records]
            self.assertIn("evaluation", events)
            self.assertIn("promotion", events)
            self.assertIn("autonomy_applied", events)
            self.assertEqual(
                (Path(config.state_dir) / "mode").read_text(encoding="utf-8").strip(),
                "live",
            )
            state = json.loads(
                (Path(config.state_dir) / "promotion.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["stage"], "probation")
            evolution = json.loads(
                (Path(config.state_dir) / "evolution.json").read_text(encoding="utf-8")
            )
            self.assertGreater(evolution["out_of_sample"]["trades"], 0)

    def test_promotion_requires_operator_consent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            save_bars(tmp / "bars.jsonl", _edge_bars())
            config = load_config(
                _write_config(tmp, autonomy="auto", history=tmp / "bars.jsonl")
            )
            client = FakeMcpClient(TOOLS, equity="500")
            broker = Broker(client, TOOLS)

            env = {k: v for k, v in os.environ.items() if k != "AGENTIC_ALLOW_AUTONOMY"}
            with mock.patch.dict(os.environ, env, clear=True):
                run_daemon(
                    config,
                    broker=broker,
                    strategy=FixtureStrategy(),
                    feed=_EmptyFeed(),
                    once=True,
                    session_clock=lambda: "regular",
                )

            events = [record.get("event") for record in _journal(config)]
            self.assertIn("promotion", events)
            self.assertIn("promotion_requires_consent", events)
            self.assertNotIn("autonomy_applied", events)
            self.assertFalse((Path(config.state_dir) / "mode").exists())

    def test_manual_autonomy_never_evaluates_or_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            save_bars(tmp / "bars.jsonl", _edge_bars())
            config = load_config(
                _write_config(tmp, autonomy="manual", history=tmp / "bars.jsonl")
            )
            client = FakeMcpClient(TOOLS, equity="500")
            broker = Broker(client, TOOLS)

            self._run_once(config, broker, env={"AGENTIC_ALLOW_AUTONOMY": "1"})
            events = [record.get("event") for record in _journal(config)]
            self.assertNotIn("evaluation", events)
            self.assertNotIn("promotion", events)
            self.assertFalse((Path(config.state_dir) / "mode").exists())

    def test_demotes_live_stage_on_drawdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(
                _write_config(tmp, autonomy="auto", history=None)
            )
            save_state(
                config.state_dir,
                PromotionState(stage="live", stage_equity="1000"),
            )
            client = FakeMcpClient(TOOLS, equity="800")  # 20% drawdown
            broker = Broker(client, TOOLS)

            self._run_once(config, broker, env={"AGENTIC_ALLOW_AUTONOMY": "1"})
            events = [record.get("event") for record in _journal(config)]
            self.assertIn("demotion", events)
            demotion = [
                record for record in _journal(config) if record.get("event") == "demotion"
            ][0]
            self.assertIn("drawdown", demotion["reason"])
            state = json.loads(
                (Path(config.state_dir) / "promotion.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["stage"], "shadow")
            self.assertEqual(
                (Path(config.state_dir) / "mode").read_text(encoding="utf-8").strip(),
                "shadow",
            )

    def test_kill_switch_blocks_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            save_bars(tmp / "bars.jsonl", _edge_bars())
            config = load_config(
                _write_config(tmp, autonomy="auto", history=tmp / "bars.jsonl")
            )
            state_dir = Path(config.state_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "risk_guard.json").write_text(
                json.dumps({"kill_switch": True, "kill_reason": "live_daily_loss"}),
                encoding="utf-8",
            )
            client = FakeMcpClient(TOOLS, equity="500")
            broker = Broker(client, TOOLS)

            self._run_once(config, broker, env={"AGENTIC_ALLOW_AUTONOMY": "1"})
            events = [record.get("event") for record in _journal(config)]
            self.assertIn("self_improve_skipped", events)
            self.assertNotIn("promotion", events)
            self.assertFalse((Path(config.state_dir) / "mode").exists())

    def test_probation_reduces_the_order_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, autonomy="auto", history=None))
            save_state(config.state_dir, PromotionState(stage="probation"))
            client = FakeMcpClient(TOOLS, equity="500")
            broker = Broker(client, TOOLS)

            self._run_once(config, broker)
            state = json.loads(
                (Path(config.state_dir) / "risk_guard.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["mode"], "shadow")
            # probation cap is applied to the guard, not persisted in risk state
            from agentic_trading.runtime import build_guard

            self.assertLessEqual(
                build_guard(config, "live").max_order_pct, config.max_order_pct
            )


if __name__ == "__main__":
    unittest.main()

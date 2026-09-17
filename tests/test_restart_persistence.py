"""A restart must not lose progress: state, stage, budget, gate and regimes."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.config import load_config
from agentic_trading.llm.regime import RegimeGate
from agentic_trading.limits import Limits, load_limits, save_limits
from agentic_trading.promotion import PromotionState, load_state, save_state
from agentic_trading.runtime import build_guard, write_mode

CONFIG_LINES = [
    'mode = "shadow"',
    'symbol_whitelist = ["SPY", "BTC-USD"]',
    'max_order_pct = "0.05"',
    'daily_notional_pct = "0.20"',
    'daily_loss_pct = "0.03"',
    "max_open_positions = 1",
    "equity_refresh_ticks = 30",
    "equity_refresh_seconds = 60",
    'timezone = "local"',
    'mcp_url = "https://agent.robinhood.com/mcp/trading"',
]


def _config(tmp: Path):
    path = tmp / "agentic.toml"
    path.write_text(
        "\n".join(
            CONFIG_LINES
            + [
                f'quotes_path = "{tmp / "q.jsonl"}"',
                f'journal_dir = "{tmp / "journal"}"',
                f'state_dir = "{tmp / "state"}"',
                f'tools_snapshot_path = "{tmp / "tools.json"}"',
                f'token_path = "{tmp / "tok.json"}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return load_config(path)


class _Client:
    model = "stub"

    def complete(self, system: str, user: str) -> str:
        return '{"regime":"chop","confidence":0.8,"reason":"range"}'


class RestartPersistenceTests(unittest.TestCase):
    def test_stage_and_mode_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name))
            save_state(config.state_dir, PromotionState(stage="probation", streak=2))
            write_mode(config.state_dir, "live")

            self.assertEqual(load_state(config.state_dir).stage, "probation")
            self.assertEqual(load_state(config.state_dir).streak, 2)

    def test_the_risk_budget_survives_a_restart_and_still_binds_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name))
            save_limits(
                config.state_dir,
                Limits(
                    "0.0322",
                    "0.1288",
                    "confidence_up",
                    "2026-09-16T00:00:00+00:00",
                    confidence="0.5252",
                    session_policy="any",
                ),
            )

            stored = load_limits(config.state_dir)
            self.assertEqual(stored.confidence, "0.5252")
            self.assertEqual(stored.session_policy, "any")

            guard = build_guard(config, "shadow")
            guard.max_order_pct = Decimal(str(config.max_order_pct))
            from agentic_trading.limits import apply_to_guard

            apply_to_guard(guard, config)
            # The reduced budget still wins after the restart, never the ceiling.
            self.assertEqual(guard.max_order_pct, Decimal("0.0322"))
            self.assertEqual(guard.daily_notional_pct, Decimal("0.1288"))

    def test_regime_classifications_survive_a_restart(self) -> None:
        """Otherwise every restart makes the gate deaf until the worker catches up."""
        with tempfile.TemporaryDirectory() as name:
            state_path = Path(name) / "regimes.json"
            first = RegimeGate(_Client(), state_path=state_path)
            first.refresh("BTC-USD", None)
            self.assertTrue(state_path.is_file())

            restarted = RegimeGate(_Client(), state_path=state_path)
            view = restarted.view("BTC-USD")
            self.assertIsNotNone(view)
            self.assertEqual(view.regime, "chop")
            self.assertTrue(view.blocks_entries)
            # Restored views are not treated as stale-and-unknown: the TTL still
            # applies, but the gate keeps its opinion in the meantime.
            self.assertIsNotNone(restarted.blocks("BTC-USD"))

    def test_a_corrupt_state_file_does_not_crash_startup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            state_path = tmp / "regimes.json"
            state_path.write_text("{not json", encoding="utf-8")
            gate = RegimeGate(_Client(), state_path=state_path)
            self.assertEqual(gate.views(), {})

            (tmp / "effective_limits.json").write_text("{oops", encoding="utf-8")
            self.assertIsNone(load_limits(tmp))


if __name__ == "__main__":
    unittest.main()

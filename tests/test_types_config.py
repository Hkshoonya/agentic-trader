from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from agentic_trading.cli import build_strategy, load_scalper_config
from agentic_trading.types import OrderIntent, Side
from agentic_trading.config import load_config
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.strategies.spy_scalper import SpyScalperStrategy


class OrderIntentTests(unittest.TestCase):
    def test_notional_from_quantity_and_ref_price(self):
        intent = OrderIntent(
            decision_id="d1",
            symbol="spy",
            side=Side.BUY,
            quantity=Decimal("2"),
            ref_price=Decimal("100"),
            reason="test",
            created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(intent.symbol, "SPY")
        self.assertEqual(intent.resolved_notional(), Decimal("200"))

    def test_rejects_missing_notional_inputs(self):
        with self.assertRaises(ValueError):
            OrderIntent(
                decision_id="d1",
                symbol="SPY",
                side=Side.BUY,
                reason="x",
                created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
            )


class ConfigTests(unittest.TestCase):
    def test_load_example_defaults_shadow(self):
        cfg = load_config("config/agentic.example.toml")
        self.assertEqual(cfg.mode, "shadow")
        self.assertEqual(cfg.max_order_pct, Decimal("0.05"))
        self.assertEqual(cfg.symbol_whitelist, frozenset({"SPY"}))
        self.assertEqual(cfg.strategy, "fixture")
        self.assertIsNone(cfg.scalper_config)

    def test_load_strategy_and_scalper_config(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            cfg_path = tmp / "agentic.toml"
            cfg_path.write_text(
                "\n".join(
                    [
                        'mode = "shadow"',
                        'strategy = "spy_scalper"',
                        'scalper_config = "config.json"',
                        'symbol_whitelist = ["SPY"]',
                        'max_order_pct = "0.05"',
                        'daily_notional_pct = "0.20"',
                        'daily_loss_pct = "0.03"',
                        "max_open_positions = 1",
                        "equity_refresh_ticks = 30",
                        "equity_refresh_seconds = 60",
                        'timezone = "local"',
                        'quotes_path = "data/spy_quotes.jsonl"',
                        'journal_dir = "data/journal"',
                        'state_dir = "data/state"',
                        'tools_snapshot_path = "data/tools_snapshot.json"',
                        'token_path = "~/tokens.json"',
                        'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.strategy, "spy_scalper")
            self.assertEqual(cfg.scalper_config, Path("config.json"))


class StrategyFactoryTests(unittest.TestCase):
    def _minimal_config(self, tmp: Path, *, strategy: str = "fixture") -> Path:
        cfg_path = tmp / "agentic.toml"
        cfg_path.write_text(
            "\n".join(
                [
                    'mode = "shadow"',
                    f'strategy = "{strategy}"',
                    'symbol_whitelist = ["SPY"]',
                    'max_order_pct = "0.05"',
                    'daily_notional_pct = "0.20"',
                    'daily_loss_pct = "0.03"',
                    "max_open_positions = 1",
                    "equity_refresh_ticks = 30",
                    "equity_refresh_seconds = 60",
                    'timezone = "local"',
                    'quotes_path = "data/spy_quotes.jsonl"',
                    'journal_dir = "data/journal"',
                    'state_dir = "data/state"',
                    'tools_snapshot_path = "data/tools_snapshot.json"',
                    'token_path = "~/tokens.json"',
                    'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return cfg_path

    def test_build_strategy_defaults_to_fixture(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            cfg = load_config(self._minimal_config(Path(tmp_name)))
            strategy = build_strategy(cfg)
            self.assertIsInstance(strategy, FixtureStrategy)

    def test_build_strategy_selects_spy_scalper(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            cfg = load_config(
                self._minimal_config(Path(tmp_name), strategy="spy_scalper")
            )
            strategy = build_strategy(cfg)
            self.assertIsInstance(strategy, SpyScalperStrategy)

    def test_cli_override_selects_spy_scalper(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            cfg = load_config(self._minimal_config(Path(tmp_name), strategy="fixture"))
            strategy = build_strategy(cfg, strategy_name="spy_scalper")
            self.assertIsInstance(strategy, SpyScalperStrategy)

    def test_load_scalper_config_defaults(self):
        cfg = load_scalper_config(None)
        self.assertEqual(cfg.symbol, "SPY")
        self.assertEqual(cfg.initial_cash, Decimal("50"))


if __name__ == "__main__":
    unittest.main()

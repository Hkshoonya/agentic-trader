from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from agentic_trading.cli import build_strategy, load_scalper_config
from agentic_trading.types import OrderIntent, Side
from agentic_trading.config import load_config
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.strategies.llm_multi_asset import LlmMultiAssetStrategy
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
        self.assertEqual(cfg.min_forward_trades, 30)
        self.assertEqual(cfg.min_forward_days, 30.0)
        self.assertEqual(cfg.small_account_target_notional, Decimal("5.00"))
        self.assertEqual(cfg.small_account_max_order_pct, Decimal("0"))
        self.assertEqual(cfg.small_account_max_daily_pct, Decimal("0"))

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

    def test_quoted_boolean_cannot_enable_an_autonomy_switch(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "agentic.toml"
            original = Path("config/agentic.example.toml").read_text(encoding="utf-8")
            path.write_text(original + '\nauto_arm = "false"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "auto_arm must be a TOML boolean"):
                load_config(path)

    def test_risk_percentages_are_bounded_and_finite(self):
        original = Path("config/agentic.example.toml").read_text(encoding="utf-8")
        for field, value in (
            ("max_order_pct", '"1.01"'),
            ("daily_loss_pct", '"NaN"'),
            ("max_cost_share_of_order", '"-0.01"'),
            ("small_account_max_daily_pct", '"1.01"'),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp_name:
                path = Path(tmp_name) / "agentic.toml"
                lines = [
                    f"{field} = {value}" if line.startswith(f"{field} = ") else line
                    for line in original.splitlines()
                ]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, field):
                    load_config(path)

    def test_small_account_target_cannot_undercut_the_broker_minimum(self):
        original = Path("config/agentic.example.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "agentic.toml"
            path.write_text(
                original.replace(
                    'small_account_target_notional = "5.00"',
                    'small_account_target_notional = "0.50"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "small_account_target_notional"):
                load_config(path)

    def test_remote_plain_http_broker_endpoint_is_rejected(self):
        original = Path("config/agentic.example.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "agentic.toml"
            path.write_text(
                original.replace(
                    'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                    'mcp_url = "http://broker.example/mcp"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_config(path)


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

    def test_build_strategy_selects_llm(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            cfg = load_config(self._minimal_config(Path(tmp_name), strategy="llm"))
            strategy = build_strategy(cfg)
            self.assertIsInstance(strategy, LlmMultiAssetStrategy)

    def test_cli_override_selects_llm(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            cfg = load_config(self._minimal_config(Path(tmp_name), strategy="fixture"))
            strategy = build_strategy(cfg, strategy_name="llm")
            self.assertIsInstance(strategy, LlmMultiAssetStrategy)

    def test_load_scalper_config_defaults(self):
        cfg = load_scalper_config(None)
        self.assertEqual(cfg.symbol, "SPY")
        self.assertEqual(cfg.initial_cash, Decimal("50"))


if __name__ == "__main__":
    unittest.main()

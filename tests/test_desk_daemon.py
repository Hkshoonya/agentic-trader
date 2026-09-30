"""The desk inside the real daemon loop: sizer, cost floor, guard and journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading import execution
from agentic_trading.broker import Broker
from agentic_trading.cli import build_strategy
from agentic_trading.config import load_config
from agentic_trading.runtime import run_daemon
from tests.fakes import FakeMcpClient
from tests.test_crypto_session_window import (
    FIXED_NOW,
    _quote,
    _records,
    _StubFeed,
    _tools,
    _write_config,
)


def _desk_config(tmp: Path, *, equity: str):
    path = _write_config(tmp, whitelist='"BTC-USD", "QQQ"')
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            'strategy = "desk"\n'
            'desk_members = ["benchmark"]\n'
            f'history_path = "{tmp / "bars"}"\n'
        )
    config = load_config(path)
    state = Path(config.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    (state / "account.json").write_text(
        json.dumps({"last_equity": equity}), encoding="utf-8"
    )
    return config


def _run(config, client: FakeMcpClient):
    desk = build_strategy(config)
    tools = _tools()
    run_daemon(
        config,
        broker=Broker(client, tools),
        strategy=desk,
        feed=_StubFeed([_quote("BTC-USD")]),
        once=True,
        clock=lambda: FIXED_NOW,
        session_clock=lambda: "weekend",
    )
    return desk


class DeskDaemonTests(unittest.TestCase):
    def test_a_benchmark_leg_becomes_one_shadow_order_the_ledger_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _desk_config(Path(name), equity="50")
            client = FakeMcpClient(_tools())
            desk = _run(config, client)
            records = _records(config)
        accepted = [
            r
            for r in records
            if r.get("event") == "accepted"
            and (r.get("intent") or {}).get("reason") == "desk_rebalance"
        ]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["mode"], "shadow")
        self.assertEqual(accepted[0]["symbol"], "BTC-USD")
        self.assertEqual(client.calls_named("place_crypto_order"), [])
        # The desk's account ledger holds exactly what the runtime filled.
        self.assertEqual(
            desk.account.positions["BTC-USD"], Decimal(accepted[0]["quantity"])
        )

    def test_a_leg_under_the_crypto_cost_floor_is_never_sent(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _desk_config(Path(name), equity="7")
            # One measured crypto round trip lost $0.10: at the default 2% cost
            # share, the runtime refuses any crypto buy under $5.
            execution.record_round_trip(
                config.state_dir,
                symbol="XLM-USD",
                buy_notional=5.00,
                sell_notional=4.90,
            )
            _run(config, FakeMcpClient(_tools()))
            records = _records(config)
        refused = [
            r
            for r in records
            if r.get("event") == "rejected"
            and str(r.get("reason", "")).startswith("cost_too_high_for_size")
        ]
        # 40% of $7 is a $2.80 BTC leg: under the floor, so it must not be sent.
        self.assertEqual(refused, [])
        self.assertFalse(
            any(
                (r.get("intent") or {}).get("reason") == "desk_rebalance"
                for r in records
            )
        )


class DeskDaemonAdvisoryTests(unittest.TestCase):
    def test_a_vetoing_advisor_does_not_stop_the_desk_in_the_real_loop(self) -> None:
        """End to end: the desk's own leg reaches the ledger through run_daemon
        even when every advisory layer would refuse it, and each opinion is kept."""
        from unittest import mock

        from tests.test_desk_advisory import _ChopGate, _VetoAdvisor

        with tempfile.TemporaryDirectory() as name:
            config = _desk_config(Path(name), equity="50")
            with mock.patch(
                "agentic_trading.llm.advisor.build_advisor", return_value=_VetoAdvisor()
            ), mock.patch(
                "agentic_trading.llm.advisor.build_regime_gate", return_value=_ChopGate()
            ):
                _run(config, FakeMcpClient(_tools()))
            records = _records(config)
        desk = [
            r
            for r in records
            if (r.get("intent") or {}).get("reason") == "desk_rebalance"
        ]
        self.assertEqual([r["event"] for r in desk], ["accepted"])
        overruled = {r["layer"] for r in records if r.get("event") == "advisory_overruled"}
        self.assertEqual(overruled, {"regime", "llm"})

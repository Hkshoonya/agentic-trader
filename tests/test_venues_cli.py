"""The venues commands on fakes: check, smoke (paper only), arm/disarm."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from agentic_trading.venues.cli import cmd_arm, cmd_check, cmd_disarm, cmd_smoke, sample_streams

T0 = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
PAPER_SECRET = "papersecret-DO-NOT-LEAK-1234567890"


def _config(tmp: Path, *, live=False) -> str:
    from tests.test_runtime_daemon import _write_config

    secrets = tmp / "secrets.toml"
    body = f'[alpaca_paper]\nkey = "PKTESTKEY0000000WXYZ"\nsecret = "{PAPER_SECRET}"\n'
    if live:
        body += '[alpaca_live]\nkey = "AKLIVEKEY0000000LIVE"\nsecret = "livesecret-0000000000"\n'
    secrets.write_text(body)
    os.chmod(secrets, 0o600)
    path = _write_config(tmp, extra=[
        "[venues]", "enabled = true", f'secrets_path = "{secrets}"', f'stream_dir = "{tmp / "stream"}"',
        f"use_alpaca_live = {'true' if live else 'false'}",
    ])
    return str(path)


class FakeClient:
    def __init__(self, paper, fail=None):
        self.paper, self.fail = paper, fail
        self.submitted, self.cancelled = [], []

    def get_account(self):
        if self.fail:
            raise RuntimeError(self.fail)
        return NS(equity="100000", cash="100000", buying_power="200000", daytrade_count=0, pattern_day_trader=False)

    def get_all_positions(self):
        return []

    def get_orders(self, filter=None):
        return []

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return NS(id="v-1", client_order_id=order_data.client_order_id, status=NS(value="accepted"),
                  filled_qty="0", filled_avg_price=None)

    def get_order_by_client_id(self, client_id):
        return NS(id="v-1", client_order_id=client_id, status=NS(value="new"), filled_qty="0", filled_avg_price=None)

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)


class OneQuoteStream:
    def __init__(self):
        self.handler = None

    def subscribe_quotes(self, handler, *symbols):
        self.handler = handler

    def subscribe_trades(self, handler, *symbols):
        pass

    def run(self):
        asyncio.run(self.handler(NS(symbol="SPY", bid_price=500, ask_price=500.02, timestamp=T0)))

    def stop(self):
        pass


class ArmTests(unittest.TestCase):
    def test_arming_needs_yes_and_disarm_undoes_it(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name))
            out: list[str] = []
            self.assertEqual(cmd_arm(path, "alpaca_live", 2, False, out=out.append, now=lambda: T0), 2)
            self.assertIn("--yes", out[-1])
            state = Path(name) / "state" / "venues_arm.json"
            self.assertFalse(state.exists())
            self.assertEqual(cmd_arm(path, "alpaca_live", 2, True, out=out.append, now=lambda: T0), 0)
            self.assertIn("alpaca_live", json.loads(state.read_text()))
            self.assertEqual(cmd_disarm(path, "alpaca_live", out=out.append), 0)
            self.assertNotIn("alpaca_live", json.loads(state.read_text()))

    def test_the_main_cli_routes_venues_and_rejects_paper_arming(self) -> None:
        from agentic_trading.cli import main

        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name))
            with self.assertRaises(SystemExit):
                main(["venues", "arm", "alpaca_paper", "--hours", "1", "--yes", "--config", path])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["venues", "arm", "coinbase", "--hours", "1", "--config", path]), 2)


class SmokeTests(unittest.TestCase):
    def _factories(self):
        self.clients = []

        def trading(creds, paper):
            client = FakeClient(paper)
            self.clients.append(client)
            return client

        return {"alpaca_trading": trading}

    def test_in_hours_a_one_dollar_market_order_is_sent_and_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name), live=True)
            out: list[str] = []
            code = cmd_smoke(path, out=out.append, factories=self._factories(), sleep=lambda s: None,
                             now=lambda: T0, session=lambda now: "regular")
            journal = (Path(name) / "journal" / "venues-2026-09-30.jsonl").read_text()
        self.assertEqual(code, 0)
        self.assertEqual([c.paper for c in self.clients], [True])  # never a live client
        request = self.clients[0].submitted[0]
        self.assertEqual((request.symbol, request.notional), ("SPY", 1.0))
        self.assertEqual(self.clients[0].cancelled, ["v-1"])
        self.assertIn("venue_submitted", journal)
        self.assertIn("venue_cancelled", journal)
        self.assertNotIn(PAPER_SECRET, journal)

    def test_out_of_hours_it_is_a_limit_that_cannot_fill(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name))
            cmd_smoke(path, out=lambda s: None, factories=self._factories(), sleep=lambda s: None,
                      now=lambda: T0, session=lambda now: "afterhours")
        request = self.clients[0].submitted[0]
        self.assertEqual((request.qty, request.limit_price), (1.0, 1.0))

    def test_without_paper_keys_smoke_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name))
            (Path(name) / "secrets.toml").write_text("")
            out: list[str] = []
            self.assertEqual(cmd_smoke(path, out=out.append, factories=self._factories(),
                                       sleep=lambda s: None, now=lambda: T0), 2)
        self.assertIn("paper keys", out[-1])


class CheckTests(unittest.TestCase):
    def test_check_shows_accounts_and_samples_streams_without_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name))
            out: list[str] = []
            factories = {"alpaca_trading": lambda c, p: FakeClient(p),
                         "alpaca_stock_stream": lambda c: OneQuoteStream(),
                         "alpaca_crypto_stream": lambda c: OneQuoteStream()}
            code = cmd_check(path, out=out.append, factories=factories, sleep=lambda s: None,
                             now=lambda: T0, sample_seconds=0.01)
        text = "\n".join(out)
        self.assertEqual(code, 0)
        self.assertIn("alpaca_paper", text)
        self.assertIn("100,000.00", text)
        self.assertIn("…WXYZ", text)
        self.assertIn("alpaca_stocks: 1 ticks", text)
        self.assertNotIn(PAPER_SECRET, text)

    def test_a_login_failure_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name))
            out: list[str] = []
            code = cmd_check(path, out=out.append,
                             factories={"alpaca_trading": lambda c, p: FakeClient(p, fail="401 Unauthorized"),
                                        "alpaca_stock_stream": lambda c: OneQuoteStream(),
                                        "alpaca_crypto_stream": lambda c: OneQuoteStream()},
                             sleep=lambda s: None, now=lambda: T0, sample_seconds=0.01)
        self.assertEqual(code, 1)
        self.assertIn("login failed", "\n".join(out))

    def test_a_running_service_is_read_not_doubled(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _config(Path(name))
            state = Path(name) / "state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "venues.json").write_text(json.dumps({"as_of": T0.isoformat(), "streams": [
                {"key": "alpaca_stocks", "status": "live", "last_tick_age_s": 0.4, "delay_ms_median": 31,
                 "reconnects": 0}], "venues": [], "latest": {}}))
            out: list[str] = []

            def no_stream(c):
                raise AssertionError("check opened a second stream")

            cmd_check(path, out=out.append, factories={"alpaca_trading": lambda c, p: FakeClient(p),
                                                       "alpaca_stock_stream": no_stream,
                                                       "alpaca_crypto_stream": no_stream},
                      sleep=lambda s: None, now=lambda: T0)
        self.assertIn("from the running venues service", "\n".join(out))


class SampleTests(unittest.TestCase):
    def test_sampling_counts_ticks_and_reports_errors(self) -> None:
        good = NS(key="a", run=lambda emit: emit(NS(received_at=T0, exchange_at=T0)), stop=lambda: None)

        def broken(emit):
            raise RuntimeError("boom")

        bad = NS(key="b", run=broken, stop=lambda: None)
        result = sample_streams([good, bad], 0.01, sleep=lambda s: None)
        self.assertEqual(result["a"]["ticks"], 1)
        self.assertEqual(result["a"]["median_ms"], 0.0)
        self.assertIn("boom", result["b"]["error"])

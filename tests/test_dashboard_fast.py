"""/api/fast: whitelisted switchboard state; fast events reach the ticker in time order."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.dashboard_desk import DeskEventCache, label, ticker_item
from agentic_trading.dashboard_fast import fast_view

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _state(folder: Path, as_of: datetime) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fast.json").write_text(json.dumps({
        "enabled": True, "as_of": as_of.isoformat(), "failed": "", "halted": False, "skipped": 3,
        "secret": "LEAK-top",
        "coins": [{"symbol": "BTC/USD", "regime": "trending", "standing_aside": False, "api_key": "LEAK-coin",
                   "trade": {"playbook": "breakout", "entry": 105.2, "stop": 99.2, "target": None,
                             "pnl_pct": 0.5, "opened_at": as_of.isoformat(), "note": "LEAK-trade"}}],
        "book": {"equity": "50.25", "return_pct": 0.5, "entries": 1, "exits": 0, "cash": "LEAK-book"},
        "mirror": {"equity": "49.80", "return_pct": -0.4, "unpriced": 1},
        "recent": [{"at": as_of.isoformat(), "event": "fast_entry", "symbol": "BTC/USD",
                    "text": "Bought $16.67 of BTC/USD", "quantity": "LEAK-recent"}],
    }))


class FastViewTests(unittest.TestCase):
    def test_no_switchboard_means_disabled_with_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            view = fast_view(name, now=T0)
        self.assertFalse(view["enabled"])
        self.assertIn("[fast] enabled = true", view["note"])

    def test_only_whitelisted_fields_pass_and_would_earn_comes_from_the_desk(self) -> None:
        events = [{"event": "desk_allocation", "would_earn": {"switchboard": 0.42}}]
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            view = fast_view(name, events, now=T0 + timedelta(seconds=2))
        self.assertNotIn("LEAK", json.dumps(view))
        self.assertFalse(view["stale"])
        self.assertEqual(view["coins"][0]["trade"]["playbook"], "breakout")
        self.assertEqual((view["book"]["return_pct"], view["mirror"]["unpriced"]), (0.5, 1))
        self.assertEqual(view["would_earn"], 0.42)

    def test_an_old_file_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            self.assertTrue(fast_view(name, now=T0 + timedelta(minutes=2))["stale"])

    def test_the_route_serves_it(self) -> None:
        from agentic_trading.config import load_config
        from agentic_trading.dashboard import serve
        from tests.test_runtime_daemon import _write_config

        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name)))
            _state(Path(config.state_dir), datetime.now(timezone.utc))
            server = serve(config, host="127.0.0.1", port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/fast"
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["coins"][0]["symbol"], "BTC/USD")


class TickerTests(unittest.TestCase):
    def test_fast_trades_are_ticker_news_and_merge_in_time_order(self) -> None:
        item = ticker_item({"event": "fast_entry", "at": T0.isoformat(), "text": "Bought $16.67 of BTC/USD"})
        self.assertEqual((item["kind"], item["text"]), ("fast", "Bought $16.67 of BTC/USD"))
        self.assertEqual(label("switchboard"), "Switchboard")
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "2026-10-05.jsonl").write_text(json.dumps(
                {"event": "desk_allocation", "at": "2026-10-05T11:00:00+00:00", "changed": False}) + "\n")
            (folder / "fast-2026-10-05.jsonl").write_text(
                json.dumps({"event": "fast_entry", "at": "2026-10-05T10:00:00.500000+00:00", "text": "in"}) + "\n"
                + json.dumps({"event": "fast_exit", "at": "2026-10-05T12:00:00.250000+00:00", "text": "out"}) + "\n")
            events = DeskEventCache(folder).read()
        self.assertEqual([e["event"] for e in events], ["fast_entry", "desk_allocation", "fast_exit"])

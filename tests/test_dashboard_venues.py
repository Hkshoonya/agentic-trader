"""/api/venues: a whitelisted, secret-free view of the venues service."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.dashboard_venues import venues_view

T0 = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)


def _state(tmp: Path, as_of: datetime) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "venues.json").write_text(json.dumps({
        "as_of": as_of.isoformat(), "secret": "LEAK-top",
        "streams": [{"key": "alpaca_stocks", "venue": "alpaca", "status": "live", "connected": True,
                     "last_tick_age_s": 0.4, "delay_ms_median": 31, "delay_ms_p95": 80, "reconnects": 0,
                     "skew": 0, "last_error": "", "api_key": "LEAK-stream"}],
        "venues": [{"name": "alpaca_paper", "mode": "paper", "status": "ok", "equity": "100000",
                    "cash": "100000", "day_trades": 0, "armed": False, "last_error": "", "key": "LEAK-venue"}],
        "latest": {"alpaca:SPY": {"bid": "500"}},
    }))


class VenuesViewTests(unittest.TestCase):
    def test_no_service_means_disabled_with_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            view = venues_view(name, now=T0)
        self.assertFalse(view["enabled"])
        self.assertIn("not running", view["note"])

    def test_only_whitelisted_fields_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            view = venues_view(name, now=T0 + timedelta(seconds=2))
        text = json.dumps(view)
        self.assertNotIn("LEAK", text)
        self.assertFalse(view["stale"])
        self.assertEqual(view["streams"][0]["delay_ms_median"], 31)
        self.assertEqual(view["venues"][0]["equity"], "100000")

    def test_an_old_file_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            self.assertTrue(venues_view(name, now=T0 + timedelta(minutes=2))["stale"])

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
                url = f"http://127.0.0.1:{server.server_address[1]}/api/venues"
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["streams"][0]["key"], "alpaca_stocks")

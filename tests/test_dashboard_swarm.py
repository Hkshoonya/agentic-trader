"""/api/swarm: whitelisted swarm state; births and deaths reach the ticker."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.dashboard_desk import DeskEventCache, label, ticker_item
from agentic_trading.dashboard_swarm import swarm_view

T0 = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)


def _state(folder: Path, stepped: datetime) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "swarm.json").write_text(json.dumps({
        "as_of": "2026-10-04", "stepped_at": stepped.isoformat(), "stale": False, "note": "", "trials": 31,
        "alive": 1, "secret": "LEAK-top",
        "book": {"equity": "50.40", "return_pct": 0.8, "cash": "LEAK-book"},
        "agents": [{"id": "ab12cd34", "name": "trend-ab12", "family": "trend", "universe": "crypto",
                    "origin": "mutation", "born": "2026-09-01", "forward_days": 33, "excess_pct": 1.2,
                    "return_pct": 2.0, "drawdown_pct": 3.1, "state": "contributing", "share": 1.0,
                    "errored": "LEAK-error", "signature": "LEAK-signature"}],
        "recent": [{"at": stepped.isoformat(), "event": "swarm_birth", "text": "trend-ab12 was born",
                    "agent": "LEAK-recent"}],
        "scout": {"enabled": True, "spent": 1, "budget": 3, "last_rationale": "coins trend", "key": "LEAK-scout"},
    }))


class SwarmViewTests(unittest.TestCase):
    def test_no_swarm_means_disabled_with_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            view = swarm_view(Path(name))
        self.assertFalse(view["enabled"])
        self.assertIn("[swarm] enabled = true", view["note"])

    def test_only_whitelisted_fields_pass_and_would_earn_comes_from_the_desk(self) -> None:
        events = [{"event": "desk_allocation", "would_earn": {"swarm": 0.25, "switchboard": 0.0}}]
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            view = swarm_view(Path(name), events, now=T0 + timedelta(hours=1))
        self.assertNotIn("LEAK", json.dumps(view))
        self.assertEqual((view["trials"], view["alive"], view["would_earn"], view["stale"]), (31, 1, 0.25, False))
        self.assertEqual(view["agents"][0]["state"], "contributing")

    def test_a_day_and_a_half_without_a_step_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            self.assertTrue(swarm_view(Path(name), now=T0 + timedelta(hours=37))["stale"])

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
                url = f"http://127.0.0.1:{server.server_address[1]}/api/swarm"
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(payload["agents"][0]["name"], "trend-ab12")


class SwarmTickerTests(unittest.TestCase):
    def test_births_and_deaths_are_ticker_news_in_time_order(self) -> None:
        item = ticker_item({"event": "swarm_death", "at": T0.isoformat(), "text": "trend-ab12 died: drawdown 26%"})
        self.assertEqual((item["kind"], item["text"]), ("swarm", "trend-ab12 died: drawdown 26%"))
        self.assertEqual(label("swarm"), "Swarm")
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "2026-10-05.jsonl").write_text(json.dumps(
                {"event": "desk_allocation", "at": "2026-10-05T11:00:00+00:00", "changed": False}) + "\n")
            (folder / "swarm-2026-10-05.jsonl").write_text(json.dumps(
                {"event": "swarm_birth", "at": "2026-10-05T00:30:00+00:00", "text": "born"}) + "\n")
            (folder / "fast-2026-10-05.jsonl").write_text(json.dumps(
                {"event": "fast_exit", "at": "2026-10-05T12:00:00+00:00", "text": "out"}) + "\n")
            events = DeskEventCache(folder).read()
        self.assertEqual([e["event"] for e in events], ["swarm_birth", "desk_allocation", "fast_exit"])

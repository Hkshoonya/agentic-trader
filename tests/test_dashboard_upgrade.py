"""The Evolution card: the state, and three fenced buttons that only flip switches."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agentic_trading.dashboard_upgrade import upgrade_view
from agentic_trading.upgrade.control import load_control, pause


def _post(port: int, body: dict, *, content_type: str = "application/json", origin: str | None = None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/upgrade", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": content_type,
                                                             **({"Origin": origin} if origin else {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, {}


class UpgradeViewTests(unittest.TestCase):
    def test_the_view_whitelists_and_reads_the_switches(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            pause(state, "operator")
            (state / "upgrade.json").write_text(json.dumps({"enabled": True, "secret": "LEAK", "last": {
                "at": "t", "outcome": "shipped", "message": "m", "task": "x", "token": "LEAK"}}))
            view = upgrade_view(state)
        self.assertNotIn("LEAK", json.dumps(view))
        self.assertEqual((view["paused"], view["enabled"], view["last"]["outcome"]), (True, True, "shipped"))

    def test_the_buttons_flip_switches_behind_the_fences(self) -> None:
        from agentic_trading.config import load_config
        from agentic_trading.dashboard import serve
        from tests.test_runtime_daemon import _write_config

        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name)))
            server = serve(config, host="127.0.0.1", port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = server.server_address[1]
            try:
                self.assertEqual(_post(port, {"action": "pause"})[0], 200)
                self.assertTrue(load_control(config.state_dir).paused)
                self.assertEqual(_post(port, {"action": "rollback"})[0], 200)
                self.assertTrue(load_control(config.state_dir).rollback_requested)
                self.assertEqual(_post(port, {"action": "resume"})[0], 200)
                self.assertFalse(load_control(config.state_dir).paused)
                self.assertEqual(_post(port, {"action": "deploy"})[0], 400)
                self.assertEqual(_post(port, {"action": "pause"}, content_type="text/plain")[0], 415)
                self.assertEqual(_post(port, {"action": "pause"}, origin="http://evil.example")[0], 403)
            finally:
                server.shutdown()
                server.server_close()

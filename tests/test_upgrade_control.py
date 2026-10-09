"""The upgrader's switches: off by default, pause always wins, a broken file reads as paused."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentic_trading.upgrade.control import exclusive, load_control, pause, request_rollback, resume, save_control
from agentic_trading.upgrade.run import RunResult, real_runner
from agentic_trading.upgrade.settings import load_upgrade_config


class SettingsTests(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "agentic.toml"
            path.write_text(text)
            return load_upgrade_config(path)

    def test_off_by_default_and_strict(self) -> None:
        config = self._load("")
        self.assertEqual((config.enabled, config.canary_hours, config.max_files, config.max_lines),
                         (False, 24, 12, 400))
        for text, needle in {'[upgrade]\nenabeld = true\n': "unknown keys",
                             '[upgrade]\nenabled = "true"\n': "upgrade.enabled",
                             '[upgrade]\nmax_lines = 5000\n': "upgrade.max_lines"}.items():
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, needle):
                self._load(text)


class ControlTests(unittest.TestCase):
    def test_pause_resume_and_rollback_requests(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            self.assertFalse(load_control(state).paused)
            pause(state, "operator pressed pause")
            self.assertEqual((load_control(state).paused, load_control(state).reason), (True, "operator pressed pause"))
            request_rollback(state)
            self.assertTrue(load_control(state).rollback_requested)
            resume(state)
            control = load_control(state)
            self.assertEqual((control.paused, control.reason, control.rollback_requested), (False, "", True))

    def test_an_unreadable_control_file_reads_as_paused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / "upgrade").mkdir()
            (state / "upgrade" / "control.json").write_text("{nope")
            control = load_control(state)
        self.assertTrue(control.paused)
        self.assertIn("unreadable", control.reason)

    def test_canary_and_last_shipped_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            control = load_control(state)
            control.canary = {"commit": "abc", "until": "2026-10-07T02:00:00+00:00"}
            control.last_shipped = {"commit": "abc", "title": "t"}
            save_control(state, control)
            again = load_control(state)
        self.assertEqual((again.canary["commit"], again.last_shipped["title"]), ("abc", "t"))


class LockTests(unittest.TestCase):
    def test_one_upgrader_job_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with exclusive(name) as first:
                with exclusive(name) as second:
                    self.assertEqual((first, second), (True, False))
            with exclusive(name) as again:
                self.assertTrue(again)

    def test_run_and_watch_step_aside_while_the_other_works(self) -> None:
        import contextlib
        import io
        from argparse import Namespace
        from unittest.mock import patch

        from agentic_trading.config import load_config
        from agentic_trading.upgrade.cli import dispatch_upgrade
        from tests.test_runtime_daemon import _write_config

        with tempfile.TemporaryDirectory() as name:
            path = _write_config(Path(name))
            state = load_config(path).state_dir
            for action in ("run", "watch"):
                with self.subTest(action=action), exclusive(state), \
                        patch("agentic_trading.upgrade.watchdog.watch", side_effect=AssertionError("ran")), \
                        patch("agentic_trading.upgrade.cycle.run_cycle", side_effect=AssertionError("ran")):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = dispatch_upgrade(Namespace(config=str(path), upgrade_action=action))
                    self.assertEqual(code, 0)
                    self.assertIn("busy", out.getvalue())


class RunnerTests(unittest.TestCase):
    def test_the_real_runner_reports_exit_codes_and_timeouts(self) -> None:
        ok = real_runner(["python3", "-c", "print('hi')"])
        self.assertEqual((ok.code, ok.out.strip()), (0, "hi"))
        slow = real_runner(["python3", "-c", "import time; time.sleep(5)"], timeout=0.5)
        self.assertEqual(slow.code, 124)
        missing = real_runner(["no-such-binary-xyz"])
        self.assertEqual(missing.code, 127)
        self.assertIsInstance(ok, RunResult)

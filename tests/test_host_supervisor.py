"""The Windows host: the same units as deploy/, run with systemd's restart, limit and timer rules."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from agentic_trading import filelock
from agentic_trading.host import client
from agentic_trading.host.supervisor import Supervisor, spawn_process
from agentic_trading.host.units import SERVICES, TIMERS, Service, Timer
from agentic_trading.upgrade import services as manager
from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.ship import Shipyard
from agentic_trading.upgrade.watchdog import checks

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _directives(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, value = line.split("=", 1)
            found[key.strip()] = value.strip()
    return found


def _launched(name: str) -> tuple[str, ...]:
    """The agentic-trading arguments a deploy/ launcher execs (or runs, for the upgrade one)."""
    text = (DEPLOY / name).read_text(encoding="utf-8")
    match = re.search(r'"\.venv/bin/agentic-trading"\s+([^)\n]+?)(?:\s+2>&1\))?\s*(?:\|\||$)', text, re.M)
    assert match, name
    return tuple(match.group(1).split())


def _seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+)(min|s)?", value)
    assert match, value
    return float(match.group(1)) * (60 if match.group(2) == "min" else 1)


class UnitParityTests(unittest.TestCase):
    """deploy/ is the source of truth; the Windows host may not drift from it."""

    def test_every_service_matches_its_unit(self) -> None:
        self.assertEqual({s.name for s in SERVICES},
                         {p.stem for p in DEPLOY.glob("*.service")} - {"agentic-trading-alert"}
                         - {t.name for t in TIMERS})
        for service in SERVICES:
            with self.subTest(service=service.name):
                unit = _directives(DEPLOY / f"{service.name}.service")
                launcher = Path(unit["ExecStart"]).name
                self.assertEqual(service.args, _launched(launcher))
                self.assertEqual(service.restart, unit["Restart"])
                self.assertEqual(service.restart_seconds, _seconds(unit["RestartSec"]))
                self.assertEqual(service.log, Path(unit["StandardOutput"].removeprefix("append:")).name)
                self.assertEqual(service.prevent_exit, tuple(int(c) for c in unit.get("RestartPreventExitStatus", "").split()))
                self.assertEqual(service.stop_seconds, _seconds(unit.get("TimeoutStopSec", "90")))
                limit = (int(unit["StartLimitBurst"]), _seconds(unit["StartLimitIntervalSec"])) \
                    if "StartLimitBurst" in unit else None
                self.assertEqual(service.start_limit, limit)
                self.assertEqual(service.alert_on_failure, unit.get("OnFailure") == "agentic-trading-alert.service")
                env = dict(e.split("=", 1) for e in unit["Environment"].split()) if "Environment" in unit else {}
                self.assertEqual(service.env, env)

    def test_every_timer_matches_its_unit(self) -> None:
        self.assertEqual({t.name for t in TIMERS}, {p.stem for p in DEPLOY.glob("*.timer")})
        for timer in TIMERS:
            with self.subTest(timer=timer.name):
                unit = _directives(DEPLOY / f"{timer.name}.service")
                when = _directives(DEPLOY / f"{timer.name}.timer")
                self.assertEqual(timer.args, _launched(Path(unit["ExecStart"]).name))
                self.assertEqual(timer.timeout_seconds, _seconds(unit["TimeoutStartSec"]))
                self.assertEqual(timer.log, Path(unit["StandardOutput"].removeprefix("append:")).name)
                self.assertEqual(timer.low_priority, unit.get("Nice") == "10")
                self.assertEqual(timer.persistent, when.get("Persistent") == "true")
                calendar = re.fullmatch(r"\*-\*-\* (\d\d):(\d\d):00 UTC", when.get("OnCalendar", ""))
                self.assertEqual(timer.daily_utc,
                                 clock_time(int(calendar[1]), int(calendar[2])) if calendar else None)
                self.assertEqual(timer.every_seconds,
                                 _seconds(when["OnUnitActiveSec"]) if "OnUnitActiveSec" in when else None)
                self.assertEqual(timer.boot_seconds, _seconds(when["OnBootSec"]) if "OnBootSec" in when else None)
                sourced = "upgrade.env" in (DEPLOY / Path(unit["ExecStart"]).name).read_text(encoding="utf-8")
                self.assertEqual(timer.env_file is not None, sourced)


class ScheduleTests(unittest.TestCase):
    daily = Timer("d", (), "d.log", 60.0, daily_utc=clock_time(0, 30))
    persistent = Timer("p", (), "p.log", 60.0, daily_utc=clock_time(0, 30), persistent=True)
    every = Timer("e", (), "e.log", 60.0, every_seconds=300.0, boot_seconds=300.0)

    def test_a_daily_timer_fires_at_its_next_utc_time(self) -> None:
        self.assertEqual(self.daily.next_run(None, T0, T0), datetime(2026, 10, 19, 0, 30, tzinfo=timezone.utc))
        early = datetime(2026, 10, 18, 0, 10, tzinfo=timezone.utc)
        self.assertEqual(self.daily.next_run(None, early, early), datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc))

    def test_a_persistent_timer_catches_up_on_the_run_it_missed(self) -> None:
        missed = datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(self.persistent.next_run(missed, T0, T0), datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc))
        ran = datetime(2026, 10, 18, 0, 30, 5, tzinfo=timezone.utc)
        self.assertEqual(self.persistent.next_run(ran, T0, T0), datetime(2026, 10, 19, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(self.persistent.next_run(None, T0, T0), datetime(2026, 10, 19, 0, 30, tzinfo=timezone.utc))

    def test_an_interval_timer_waits_for_boot_then_repeats_from_its_last_start(self) -> None:
        self.assertEqual(self.every.next_run(None, T0, T0), T0 + timedelta(minutes=5))
        self.assertEqual(self.every.next_run(T0 + timedelta(minutes=5), T0, T0), T0 + timedelta(minutes=10))
        stale = T0 - timedelta(days=1)  # a start from before this boot: OnBootSec again
        self.assertEqual(self.every.next_run(stale, T0, T0), T0 + timedelta(minutes=5))


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid, self.code, self.stopped = pid, None, False

    def poll(self) -> Optional[int]:
        return self.code

    def stop(self, timeout: float) -> int:
        self.stopped = True
        self.code = 0 if self.code is None else self.code
        return self.code


class _Host:
    """A supervisor with fake processes, a hand-turned clock and recorded alerts."""

    def __init__(self, root: Path, services=SERVICES, timers=TIMERS, home: Optional[Path] = None,
                 sandbox: bool = True) -> None:
        self.now, self.procs, self.alerts, self.spawned = T0, [], [], []
        self.state = root / "state"
        self.sup = Supervisor(root, self.state, spawn=self.spawn, clock=lambda: self.now,
                              alert=lambda *a: self.alerts.append(a), home=home or root,
                              services=services, timers=timers, sandbox=sandbox)

    def spawn(self, argv, *, cwd, env, log, low_priority):
        proc = _FakeProc(1000 + len(self.procs))
        self.procs.append(proc)
        self.spawned.append({"argv": argv, "env": env, "log": log, "low": low_priority})
        return proc

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.sup.tick()

    def status(self) -> dict:
        return json.loads((self.state / "host" / "status.json").read_text(encoding="utf-8"))


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def test_services_start_in_unit_order_with_their_environment(self) -> None:
        host = _Host(self.root)
        host.sup.start()
        argv = [s["argv"][3:] for s in host.spawned]
        self.assertEqual(argv, [list(s.args) for s in SERVICES])
        self.assertEqual(host.spawned[0]["env"]["AGENTIC_ALLOW_AUTONOMY"], "1")
        self.assertNotIn("AGENTIC_ALLOW_LIVE", {k for s in host.spawned for k in s["env"] if k not in os.environ})
        self.assertTrue(all(s["env"]["AGENTIC_STOP_FILE"] for s in host.spawned))
        self.assertEqual(host.status()["services"]["agentic-trading"]["state"], "active")

    def test_restart_always_waits_its_delay(self) -> None:
        host = _Host(self.root, services=SERVICES[:1], timers=())
        host.sup.start()
        host.procs[0].code = 1
        host.advance(1)
        self.assertEqual(host.status()["services"]["agentic-trading"]["state"], "activating")
        host.advance(10)
        self.assertEqual(len(host.procs), 1)
        host.advance(5)
        self.assertEqual(len(host.procs), 2)

    def test_on_failure_respects_the_prevented_exit_status(self) -> None:
        venues = SERVICES[2]
        host = _Host(self.root, services=(venues,), timers=())
        host.sup.start()
        host.procs[0].code = 2  # "not configured": systemd would not restart it either
        host.advance(1)
        host.advance(30)
        self.assertEqual(len(host.procs), 1)
        self.assertEqual(host.status()["services"][venues.name]["state"], "inactive")
        host.sup.handle("start", [venues.name])
        host.procs[-1].code = 1
        host.advance(1)
        host.advance(11)
        self.assertEqual(len(host.procs), 3)

    def test_a_crash_loop_stops_at_the_start_limit_and_tells_the_operator(self) -> None:
        host = _Host(self.root, services=SERVICES[:1], timers=())
        host.sup.start()
        for _ in range(20):
            host.procs[-1].code = 1
            host.advance(1)
            host.advance(15)
        self.assertEqual(len(host.procs), 15)
        self.assertEqual(host.status()["services"]["agentic-trading"]["state"], "failed")
        self.assertEqual([a[0] for a in host.alerts], ["Agentic Trader STOPPED"])
        self.assertTrue((host.state / "STOPPED_AT.txt").is_file())
        ok, _ = host.sup.handle("restart", ["agentic-trading.service"])
        self.assertTrue(ok)
        host.advance(0)
        self.assertEqual(host.status()["services"]["agentic-trading"]["state"], "active")

    def test_restart_stops_through_the_stop_file_then_starts(self) -> None:
        host = _Host(self.root, timers=())
        host.sup.start()
        first = list(host.procs)
        ok, note = host.sup.handle("restart", [s.name for s in SERVICES])
        self.assertTrue(ok, note)
        self.assertTrue(all(p.stopped for p in first))
        self.assertEqual(len(host.procs), 6)
        self.assertFalse(any(Path(s["env"]["AGENTIC_STOP_FILE"]).exists() for s in host.spawned[3:]))
        self.assertFalse(host.sup.handle("restart", ["nope"])[0])

    def test_a_timer_runs_when_due_and_records_its_result(self) -> None:
        watch = TIMERS[2]
        host = _Host(self.root, services=(), timers=(watch,))
        host.sup.start()
        host.advance(299)
        self.assertEqual(host.spawned, [])
        host.advance(1)
        self.assertEqual(host.spawned[0]["argv"][3:], list(watch.args))
        host.procs[0].code = 0
        host.advance(1)
        self.assertEqual(host.status()["timers"][watch.name]["result"], "success")
        host.advance(298)
        self.assertEqual(len(host.procs), 1)
        host.advance(1)
        host.procs[1].code = 3
        host.advance(1)
        self.assertEqual(client.result(host.state, f"{watch.name}.service"), "exit-code")

    def test_a_run_past_its_timeout_is_stopped(self) -> None:
        swarm = TIMERS[0]
        host = _Host(self.root, services=(), timers=(swarm,))
        host.sup.start()
        host.sup.handle("start", [swarm.name])
        self.assertTrue(host.spawned[0]["low"])
        host.advance(45 * 60 + 1)
        self.assertTrue(host.procs[0].stopped)
        self.assertEqual(host.status()["timers"][swarm.name]["result"], "timeout")

    def test_a_missed_persistent_run_happens_at_start(self) -> None:
        swarm = TIMERS[0]
        records = self.root / "state" / "host" / "timers.json"
        records.parent.mkdir(parents=True)
        records.write_text(json.dumps({swarm.name: {"last_start": "2026-10-16T00:30:00+00:00"}}))
        host = _Host(self.root, services=(), timers=(swarm,))
        host.sup.start()
        host.advance(1)
        self.assertEqual(len(host.procs), 1)

    def test_the_upgrade_gets_its_private_env_file_and_reports_how_it_went(self) -> None:
        upgrade = TIMERS[1]
        home = self.root / "home"
        env_file = home / ".config" / "agentic-trading" / "upgrade.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("# key\nexport DEEPSEEK_API_KEY='k-123'\n")
        host = _Host(self.root, services=(), timers=(upgrade,), home=home)
        host.sup.start()
        host.sup.handle("start", [upgrade.name])
        self.assertEqual(host.spawned[0]["env"]["DEEPSEEK_API_KEY"], "k-123")
        host.procs[0].code = 1
        host.advance(1)
        self.assertEqual(host.alerts[-1][0], "Agentic self-upgrade")
        self.assertEqual(host.alerts[-1][2], "critical")

    def test_without_a_sandbox_the_upgrade_is_off_and_says_why(self) -> None:
        host = _Host(self.root, services=(), timers=TIMERS, sandbox=False)
        host.sup.start()
        ok, note = host.sup.handle("start", ["agentic-trading-upgrade"])
        self.assertFalse(ok)
        self.assertIn("needs Linux", note)
        host.advance(24 * 3600)
        started = [s["argv"][3:5] for s in host.spawned]
        self.assertNotIn(["upgrade", "run"], started)
        self.assertIn(["upgrade", "watch"], started)  # the canary still serves the console's Rollback
        self.assertEqual(host.status()["timers"]["agentic-trading-upgrade"]["state"], "disabled")

    def test_requests_are_served_from_files(self) -> None:
        host = _Host(self.root, timers=())
        host.sup.start()
        folder = host.state / "host" / "requests"
        (folder / "1.json").write_text(json.dumps({"action": "stop", "units": ["agentic-trading-venues"]}))
        host.advance(1)
        answer = json.loads((folder / "1.done").read_text())
        self.assertTrue(answer["ok"])
        self.assertEqual(host.status()["services"]["agentic-trading-venues"]["state"], "inactive")

    def test_one_supervisor_per_checkout(self) -> None:
        state = self.root / "state"
        with filelock.exclusive(state / "host" / "host.lock"):
            second = Supervisor(self.root, state, spawn=lambda *a, **k: None, services=(), timers=())
            self.assertEqual(second.run(), 1)


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def test_a_stale_heartbeat_reads_as_inactive(self) -> None:
        host = _Host(self.root, timers=())
        host.sup.start()
        self.assertEqual(client.unit_state(host.state, "agentic-trading", now=T0 + timedelta(seconds=5)), "active")
        self.assertEqual(client.unit_state(host.state, "agentic-trading", now=T0 + timedelta(minutes=5)), "inactive")
        self.assertEqual(client.unit_state(host.state, "nope", now=T0), "unknown")

    def test_logs_since_keeps_only_newer_lines(self) -> None:
        state = self.root / "state"
        state.mkdir()
        (state / "daemon.log").write_text(
            "[2026-10-18 11:59:59] old\n[2026-10-18 12:00:00] Traceback (most recent call last):\n"
            "[2026-10-18 12:00:00]   boom\n", encoding="utf-8")
        self.assertEqual(client.logs_since(state, "agentic-trading.service", T0),
                         "Traceback (most recent call last):\n  boom")

    def test_a_request_to_a_stopped_host_fails_at_once(self) -> None:
        ok, note = client.request(self.root, "restart", ["agentic-trading"], timeout=1)
        self.assertFalse(ok)
        self.assertIn("not running", note)


class WindowsCommandTests(unittest.TestCase):
    """On Windows the upgrader asks the host what it would ask systemd."""

    def setUp(self) -> None:
        patcher = patch("agentic_trading.upgrade.services.windows", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ship_restarts_through_the_host(self) -> None:
        calls = []
        yard = Shipyard(Path("/repo"), Path("/state"), lambda argv, **k: calls.append(argv) or RunResult(0, ""))
        self.assertTrue(yard.restart())
        self.assertEqual(calls[0][1:5], ["-m", "agentic_trading", "host", "restart"])
        self.assertEqual(calls[0][-3:], ["agentic-trading", "agentic-trading-dashboard", "agentic-trading-venues"])

    def test_the_canary_reads_the_host(self) -> None:
        seen = []

        def runner(argv, cwd=None, timeout=600.0):
            seen.append(argv[3:5])
            verb = argv[4]
            return RunResult(0, {"is-active": "active", "show": "success", "logs": ""}[verb])

        with tempfile.TemporaryDirectory() as journal:
            problems = checks(runner, journal_dir=Path(journal), since=T0, now=T0, http_get=lambda url: 200)
        self.assertEqual(problems, [])
        self.assertEqual({tuple(v) for v in seen},
                         {("host", "is-active"), ("host", "logs"), ("host", "show")})

    def test_the_verbs_answer_as_systemctl_does(self) -> None:
        self.assertEqual(manager.is_active("x")[3:], ["host", "is-active", "--config", "config/agentic.toml", "x"])
        self.assertEqual(manager.result("x.service")[-4:], ["x.service", "-p", "Result", "--value"])


class StopFileTests(unittest.TestCase):
    def test_nothing_watches_without_the_variable(self) -> None:
        from agentic_trading import stopfile

        self.assertIsNone(stopfile.watch({}))

    def test_a_real_child_shuts_down_through_its_own_sigint_handler(self) -> None:
        """The whole Windows stop path, with a real process: file, SIGINT, the child's own handler."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            child = root / "child.py"
            child.write_text(textwrap.dedent("""
                import signal, sys, time
                from agentic_trading import stopfile
                stopping = []
                signal.signal(signal.SIGINT, lambda *a: stopping.append(1))
                stopfile.watch(interval=0.05)
                print("ready", flush=True)
                deadline = time.time() + 30
                while not stopping and time.time() < deadline:
                    time.sleep(0.05)
                print("stopped cleanly" if stopping else "never asked", flush=True)
                sys.exit(0 if stopping else 1)
            """), encoding="utf-8")
            stop = root / "stop" / "child.stop"
            env = dict(os.environ, AGENTIC_STOP_FILE=str(stop), PYTHONPATH=str(ROOT / "src"))
            log = root / "child.log"
            proc = spawn_process([sys.executable, str(child)], cwd=root, env=env, log=log, low_priority=True)
            deadline = time.time() + 20
            while "ready" not in (log.read_text(encoding="utf-8") if log.is_file() else "") and time.time() < deadline:
                time.sleep(0.05)
            stop.parent.mkdir(parents=True)
            stop.write_text("stop")
            self.assertEqual(proc.stop(20), 0)
            text = log.read_text(encoding="utf-8")
        self.assertIn("stopped cleanly", text)
        self.assertRegex(text, r"(?m)^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ready$")



class ConsolePortTests(unittest.TestCase):
    def test_a_second_console_cannot_share_a_live_ones_port(self) -> None:
        """Windows' SO_REUSEADDR would let both bind and split the canary's requests between them."""
        from http.server import BaseHTTPRequestHandler

        from agentic_trading.dashboard import _ConsoleServer

        first = _ConsoleServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            _ConsoleServer(("127.0.0.1", first.server_address[1]), BaseHTTPRequestHandler).server_close()


if __name__ == "__main__":
    unittest.main()

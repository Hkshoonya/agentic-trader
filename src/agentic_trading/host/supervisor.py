"""Run the services and timers as systemd would: restart rules, start limits, schedules, logs.

One process, one loop, a tick a second. Everything it knows is published to
``state/host/status.json``; everything it is asked to do arrives as a file under
``state/host/requests/`` (see ``client``). The children are the same ``agentic-trading`` commands the
Linux launchers ``exec``, so nothing about trading differs between the two hosts.

Windows cannot send a child SIGTERM, and a console control event never reaches a process on another
console. So each child is given ``AGENTIC_STOP_FILE``; when the supervisor creates that file, the
child raises SIGINT on itself (``stopfile``) and shuts down through its own handlers. One that does
not finish within its stop timeout is killed, as systemd would kill it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from agentic_trading import filelock, jsonio
from agentic_trading.host.units import (SANDBOX_MISSING, SERVICES, TIMERS, Service, Timer, find,
                                        sandbox_available)

TICK_SECONDS = 1.0
STAMP = "%Y-%m-%d %H:%M:%S"  # log line prefix, UTC: what ``client.logs_since`` filters on
LOG_LIMIT = 20 * 1024 * 1024  # bytes before a log rolls over to ``<name>.1``


def host_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "host"


class Proc(Protocol):
    pid: int

    def poll(self) -> Optional[int]: ...

    def stop(self, timeout: float) -> int: ...


Spawn = Callable[..., Proc]


@dataclass
class _ServiceState:
    unit: Service
    proc: Optional[Proc] = None
    state: str = "inactive"  # active, activating, inactive, failed
    since: Optional[datetime] = None
    next_start: Optional[datetime] = None
    last_exit: Optional[int] = None
    starts: deque = field(default_factory=deque)
    stop_file: Optional[Path] = None


@dataclass
class _TimerState:
    unit: Timer
    proc: Optional[Proc] = None
    started: Optional[datetime] = None
    last_start: Optional[datetime] = None
    result: str = ""  # success, exit-code, timeout, signal: systemd's Result= words
    next_run: Optional[datetime] = None
    stop_file: Optional[Path] = None


class Supervisor:
    def __init__(self, repo: Path, state_dir: Path, *, spawn: Optional[Spawn] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 alert: Optional[Callable[[str, str, str], Any]] = None,
                 home: Optional[Path] = None,
                 services: Sequence[Service] = SERVICES, timers: Sequence[Timer] = TIMERS,
                 sandbox: Optional[bool] = None) -> None:
        self.repo, self.state_dir = Path(repo), Path(state_dir)
        self.dir = host_dir(state_dir)
        self.spawn = spawn or spawn_process
        self.clock = clock
        self.alert = alert or _desktop_alert
        self.home = Path.home() if home is None else Path(home)
        self.booted = clock()
        self.services = {unit.name: _ServiceState(unit) for unit in services}
        self.timers = {unit.name: _TimerState(unit) for unit in timers}
        self.stopping = False
        self.sandbox = sandbox_available() if sandbox is None else sandbox

    # ----- lifecycle -------------------------------------------------------------------------------

    def run(self, *, stop: Optional[threading.Event] = None) -> int:
        """Hold the host lock and supervise until asked to shut down."""
        with filelock.exclusive(self.dir / "host.lock") as held:
            if not held:
                print("host: another supervisor already runs this checkout", file=sys.stderr)
                return 1
            self.start()
            try:
                while not self.stopping and not (stop is not None and stop.is_set()):
                    self.tick()
                    time.sleep(TICK_SECONDS)
            finally:
                self.shutdown()
        return 0

    def start(self) -> None:
        (self.dir / "requests").mkdir(parents=True, exist_ok=True)
        self._log_host(f"host up for {self.repo}")
        records = self._load_timer_records()
        now = self.clock()
        for timer in self.timers.values():
            record = records.get(timer.unit.name, {})
            timer.last_start = _parse(record.get("last_start"))
            timer.result = str(record.get("result") or "")
            timer.next_run = None if self._disabled(timer.unit) else                 timer.unit.next_run(timer.last_start, self.booted, now)
        for service in self.services.values():  # in unit order: the dashboard comes up after the daemon
            self._start_service(service, now)
        self._publish(now)

    def shutdown(self) -> None:
        now = self.clock()
        for service in reversed(list(self.services.values())):
            self._stop_service(service, "stopped")
        for timer in self.timers.values():
            if timer.proc is not None:
                self._stop_proc(timer.proc, timer.stop_file, 60.0)
                self._finish_timer(timer, "signal", now)
        self._log_host("host down")
        self._publish(now, up=False)

    def tick(self) -> None:
        now = self.clock()
        self._serve_requests(now)
        for service in self.services.values():
            self._watch_service(service, now)
        for timer in self.timers.values():
            self._watch_timer(timer, now)
        self._publish(now)

    # ----- services --------------------------------------------------------------------------------

    def _start_service(self, service: _ServiceState, now: datetime) -> None:
        unit = service.unit
        if unit.start_limit is not None:
            burst, interval = unit.start_limit
            while service.starts and (now - service.starts[0]).total_seconds() > interval:
                service.starts.popleft()
            if len(service.starts) >= burst:
                service.state, service.next_start, service.since = "failed", None, now
                self._log_host(f"{unit.name}: start limit hit ({burst} starts in {interval:.0f}s); not restarting")
                if unit.alert_on_failure:
                    self._stopped_alert(unit, now)
                return
        service.starts.append(now)
        service.stop_file = self._fresh_stop_file(unit.name)
        try:
            service.proc = self.spawn(self._argv(unit.args), cwd=self.repo, env=self._env(unit.env, service.stop_file),
                                      log=self.state_dir / unit.log, low_priority=False)
        except OSError as exc:
            service.proc, service.last_exit = None, None
            service.state, service.since = "activating", now
            service.next_start = now + timedelta(seconds=unit.restart_seconds)
            self._log_host(f"{unit.name}: could not start: {exc}")
            return
        service.state, service.since, service.next_start = "active", now, None
        self._log_host(f"{unit.name}: started (pid {service.proc.pid})")

    def _watch_service(self, service: _ServiceState, now: datetime) -> None:
        unit = service.unit
        if service.proc is not None:
            code = service.proc.poll()
            if code is None:
                return
            service.proc, service.last_exit, service.since = None, code, now
            self._log_host(f"{unit.name}: exited with {code}")
            restart = unit.restart == "always" or (code != 0 and code not in unit.prevent_exit)
            if restart and not self.stopping:
                service.state, service.next_start = "activating", now + timedelta(seconds=unit.restart_seconds)
            else:
                service.state = "inactive" if code == 0 or code in unit.prevent_exit else "failed"
            return
        if service.state == "activating" and service.next_start is not None and now >= service.next_start:
            self._start_service(service, now)

    def _stop_service(self, service: _ServiceState, state: str) -> None:
        if service.proc is not None:
            service.last_exit = self._stop_proc(service.proc, service.stop_file, service.unit.stop_seconds)
            self._log_host(f"{service.unit.name}: stopped ({service.last_exit})")
        service.proc, service.state, service.next_start, service.since = None, state, None, self.clock()

    def restart(self, names: Sequence[str]) -> tuple[bool, str]:
        """``systemctl restart``: stop all named, then start them in unit order. A restart clears a
        failed unit's start-limit count, as the operator restarting it by hand means to."""
        chosen = [self.services[n] for n in self.services if n in names]
        unknown = [n for n in names if n not in self.services]
        if unknown:
            return False, f"not a service: {', '.join(unknown)}"
        for service in reversed(chosen):
            self._stop_service(service, "inactive")
        now = self.clock()
        for service in chosen:
            service.starts.clear()
            self._start_service(service, now)
        failed = [s.unit.name for s in chosen if s.state != "active"]
        if failed:
            return False, f"did not start: {', '.join(failed)}"
        return True, "restarted " + ", ".join(names)

    # ----- timers ----------------------------------------------------------------------------------

    def _watch_timer(self, timer: _TimerState, now: datetime) -> None:
        unit = timer.unit
        if timer.proc is not None:
            code = timer.proc.poll()
            if code is None and timer.started is not None and \
                    (now - timer.started).total_seconds() > unit.timeout_seconds:
                self._stop_proc(timer.proc, timer.stop_file, 30.0)
                self._finish_timer(timer, "timeout", now)
            elif code is not None:
                self._finish_timer(timer, "success" if code == 0 else "exit-code", now, code)
            return
        if timer.next_run is not None and now >= timer.next_run:
            self.run_timer(timer.unit.name)

    def run_timer(self, name: str) -> tuple[bool, str]:
        """``systemctl start x.service`` for a oneshot: start it now unless it is already running."""
        timer = self.timers.get(name)
        if timer is None:
            return False, f"not a timer: {name}"
        if timer.proc is not None:
            return True, f"{name} is already running"
        if self._disabled(timer.unit):
            return False, f"{name} is {SANDBOX_MISSING}"
        now = self.clock()
        unit = timer.unit
        timer.stop_file = self._fresh_stop_file(unit.name)
        env = self._env({**self._env_file(unit.env_file)}, timer.stop_file)
        try:
            timer.proc = self.spawn(self._argv(unit.args), cwd=self.repo, env=env, log=self.state_dir / unit.log,
                                    low_priority=unit.low_priority)
        except OSError as exc:
            timer.result, timer.last_start = "exit-code", now
            timer.next_run = unit.next_run(now, self.booted, now)
            self._save_timer_records()
            self._log_host(f"{name}: could not start: {exc}")
            return False, str(exc)
        timer.started = timer.last_start = now
        timer.next_run = unit.next_run(now, self.booted, now)
        self._save_timer_records()
        self._log_host(f"{name}: started (pid {timer.proc.pid})")
        return True, f"started {name}"

    def _finish_timer(self, timer: _TimerState, result: str, now: datetime, code: Optional[int] = None) -> None:
        timer.proc, timer.started, timer.result = None, None, result
        self._save_timer_records()
        self._log_host(f"{timer.unit.name}: finished, {result}" + (f" ({code})" if code is not None else ""))
        if timer.unit.notify_result:
            tail = _tail(self.state_dir / timer.unit.log, 3)
            self.alert("Agentic self-upgrade", tail[-300:] or result, "normal" if result == "success" else "critical")

    # ----- requests --------------------------------------------------------------------------------

    def _serve_requests(self, now: datetime) -> None:
        folder = self.dir / "requests"
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            try:
                request = json.loads(jsonio.read_text(path))
            except (OSError, ValueError):
                continue
            ok, note = self.handle(str(request.get("action") or ""), [str(n) for n in request.get("units") or []])
            jsonio.write_text(path.with_suffix(".done"), jsonio.dumps({"ok": ok, "note": note, "at": now.isoformat()}))
            path.unlink(missing_ok=True)

    def handle(self, action: str, names: Sequence[str]) -> tuple[bool, str]:
        bare = [n.removesuffix(".service").removesuffix(".timer") for n in names]
        if action == "restart":
            return self.restart(bare)
        if action == "start":
            notes = []
            for name in bare:
                unit = find(name)
                if name in self.timers:
                    notes.append(self.run_timer(name))
                elif name in self.services:
                    service = self.services[name]
                    if service.proc is None:
                        service.starts.clear()
                        self._start_service(service, self.clock())
                    notes.append((service.state == "active", f"{name} is {service.state}"))
                else:
                    notes.append((False, f"no such unit: {name}" if unit is None else f"cannot start {name}"))
            return all(ok for ok, _ in notes), "; ".join(note for _, note in notes)
        if action == "stop":
            for name in bare:
                if name in self.services:
                    self._stop_service(self.services[name], "inactive")
            return True, "stopped " + ", ".join(bare)
        if action == "shutdown":
            self.stopping = True
            return True, "the host is shutting down"
        return False, f"unknown action: {action}"

    # ----- status ----------------------------------------------------------------------------------

    def _publish(self, now: datetime, up: bool = True) -> None:
        payload = {
            "pid": os.getpid(), "up": up, "heartbeat": now.isoformat(), "booted": self.booted.isoformat(),
            "repo": str(self.repo),
            "services": {s.unit.name: {"state": s.state, "pid": s.proc.pid if s.proc else None,
                                       "since": s.since.isoformat() if s.since else None,
                                       "last_exit": s.last_exit, "log": s.unit.log}
                         for s in self.services.values()},
            "timers": {t.unit.name: {"state": "active" if t.proc else "disabled" if self._disabled(t.unit)
                                     else "inactive",
                                     "note": SANDBOX_MISSING if self._disabled(t.unit) else "",
                                     "last_start": t.last_start.isoformat() if t.last_start else None,
                                     "result": t.result, "log": t.unit.log,
                                     "next_run": t.next_run.isoformat() if t.next_run else None}
                       for t in self.timers.values()},
        }
        jsonio.write_text(self.dir / "status.json", jsonio.dumps(payload, indent=2) + "\n")

    def _load_timer_records(self) -> dict[str, Any]:
        path = self.dir / "timers.json"
        try:
            return dict(json.loads(jsonio.read_text(path))) if path.is_file() else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_timer_records(self) -> None:
        records = {t.unit.name: {"last_start": t.last_start.isoformat() if t.last_start else None,
                                 "result": t.result} for t in self.timers.values()}
        jsonio.write_text(self.dir / "timers.json", jsonio.dumps(records, indent=2) + "\n")

    # ----- helpers ---------------------------------------------------------------------------------

    def _disabled(self, unit: Timer) -> bool:
        return unit.needs_sandbox and not self.sandbox

    def _argv(self, args: Sequence[str]) -> list[str]:
        return [child_python(), "-m", "agentic_trading", *args]

    def _env(self, extra: dict[str, str], stop_file: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(extra)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                    "AGENTIC_STOP_FILE": str(stop_file)})
        return env

    def _env_file(self, relative: Optional[str]) -> dict[str, str]:
        """``set -a; . FILE``: KEY=VALUE lines from the operator's private env file, if there is one."""
        if not relative:
            return {}
        path = self.home / relative
        values: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        except OSError:
            return {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
        return values

    def _fresh_stop_file(self, name: str) -> Path:
        path = self.dir / "stop" / f"{name}.stop"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        return path

    def _stop_proc(self, proc: Proc, stop_file: Optional[Path], timeout: float) -> int:
        if stop_file is not None:
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text("stop\n", encoding="utf-8")
        return proc.stop(timeout)

    def _stopped_alert(self, unit: Service, now: datetime) -> None:
        """What agentic-trading-alert.service does: tell the operator, and leave the time on disk."""
        (self.state_dir / "STOPPED_AT.txt").write_text(now.astimezone().isoformat(timespec="seconds") + "\n",
                                                       encoding="utf-8")
        self.alert("Agentic Trader STOPPED",
                   f"The daemon failed repeatedly and is not running. See data/state/{unit.log}", "critical")

    def _log_host(self, text: str) -> None:
        _append(self.state_dir / "host.log", text, self.clock())


# ----- the real processes ------------------------------------------------------------------------------


def child_python() -> str:
    """``python.exe`` beside the running interpreter: under ``pythonw.exe`` (no console, as the logon
    task runs the host) children still get working stdout and stderr to log."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe" if os.name == "nt" else exe.name)
    return str(console if console.is_file() else exe)


class _RealProc:
    def __init__(self, popen: subprocess.Popen, pump: threading.Thread) -> None:
        self.popen, self.pump, self.pid = popen, pump, popen.pid

    def poll(self) -> Optional[int]:
        return self.popen.poll()

    def stop(self, timeout: float) -> int:
        try:
            code = self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            code = self.popen.wait()
        self.pump.join(timeout=5)
        return code


def spawn_process(argv: Sequence[str], *, cwd: Path, env: dict[str, str], log: Path, low_priority: bool) -> Proc:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        if low_priority:
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
    popen = subprocess.Popen(list(argv), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags,
                             encoding="utf-8", errors="replace", bufsize=1)
    pump = threading.Thread(target=_pump, args=(popen, log), daemon=True, name=f"log:{log.name}")
    pump.start()
    return _RealProc(popen, pump)


_LOG_LOCKS: dict[Path, threading.Lock] = {}


def _pump(popen: subprocess.Popen, log: Path) -> None:
    assert popen.stdout is not None
    for line in popen.stdout:
        _append(log, line.rstrip("\n"), datetime.now(timezone.utc))


def _append(log: Path, text: str, now: datetime) -> None:
    """One timestamped line; the swarm and the canary share upgrade.log, so writes are serialised."""
    lock = _LOG_LOCKS.setdefault(log, threading.Lock())
    with lock:
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log.is_file() and log.stat().st_size > LOG_LIMIT:
                os.replace(log, log.with_name(log.name + ".1"))
        except OSError:
            pass
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"[{now.astimezone(timezone.utc):{STAMP}}] {text}\n")


def _tail(log: Path, lines: int) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(line.split("] ", 1)[-1] for line in text.splitlines()[-lines:])


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _desktop_alert(title: str, body: str, urgency: str) -> None:
    from agentic_trading.notify import Alert, desktop_channel

    if os.environ.get("AGENTIC_NOTIFY", "1") == "0":
        return
    channel = desktop_channel()
    if channel is not None:
        channel.send(Alert(key="host", title=title, body=body, urgency=urgency))

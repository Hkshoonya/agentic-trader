"""The canary: every 5 minutes, check that an upgrade broke nothing; if it did, or the operator
asks, take it back and pause (spec U6, U8). It checks crashes, not decisions."""

from __future__ import annotations

import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agentic_trading.upgrade.control import load_control, save_control
from agentic_trading.upgrade.run import Runner
from agentic_trading.upgrade.ship import SERVICES
from agentic_trading.upgrade.tasks import Task, note_health, record

GRACE = timedelta(minutes=15)
APIS = ("/api/desk", "/api/swarm", "/api/fast")


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # loopback only
            return int(response.status)
    except Exception:  # noqa: BLE001 — any failure is "not 200"
        return 0


def checks(runner: Runner, *, journal_dir: Path, since: datetime, now: datetime,
           http_get: Callable[[str], int]) -> list[str]:
    problems = []
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    for service in SERVICES:
        if runner(["systemctl", "--user", "is-active", service], timeout=30.0).out.strip() != "active":
            problems.append(f"{service} is not active")
        logs = runner(["journalctl", "--user", "-u", service, "--since", stamp, "--no-pager", "-o", "cat"],
                      timeout=60.0).out
        if "Traceback (most recent call last)" in logs:
            problems.append(f"{service} logged a traceback")
    if now - since >= GRACE:
        today = Path(journal_dir) / f"{now.astimezone(timezone.utc):%Y-%m-%d}.jsonl"
        fresh = today.is_file() and now.timestamp() - today.stat().st_mtime < GRACE.total_seconds()
        if not fresh:
            problems.append("the trader has not written its journal for 15 minutes")
    for path in APIS:
        if http_get(f"http://127.0.0.1:8787{path}") != 200:
            problems.append(f"the dashboard's {path} does not answer")
    result = runner(["systemctl", "--user", "show", "agentic-trading-swarm.service", "-p", "Result", "--value"],
                    timeout=30.0).out.strip()
    if result not in ("success", ""):
        problems.append(f"the last swarm step ended with {result}")
    return problems


def watch(*, state_dir: Path, journal_dir: Path, now: datetime, runner: Runner, shipyard: Any,
          journal: Callable[[dict], None], notify: Callable[[dict], Any],
          http_get: Callable[[str], int] = http_status) -> str:
    control = load_control(state_dir)
    if control.rollback_requested:
        target = control.canary or control.last_shipped
        if not target:
            control.rollback_requested = False
            save_control(state_dir, control)
            return "nothing to roll back"
        return _rollback(state_dir, control, target, "the operator asked for a rollback", now, shipyard, journal,
                         notify)
    if not control.canary:
        return "idle"
    since = datetime.fromisoformat(control.canary["started_at"])
    problems = checks(runner, journal_dir=journal_dir, since=since, now=now, http_get=http_get)
    if problems:
        for problem in problems:
            note_health(state_dir, problem)
        return _rollback(state_dir, control, control.canary, "; ".join(problems), now, shipyard, journal, notify)
    if now >= datetime.fromisoformat(control.canary["until"]):
        title = control.canary.get("title", "")
        control.canary = {}
        save_control(state_dir, control)
        journal({"event": "upgrade_canary_passed", "at": now.isoformat(), "text": f"the canary passed: {title}"})
        return "passed"
    return "watching"


def _rollback(state_dir: Path, control: Any, target: dict, why: str, now: datetime, shipyard: Any,
              journal: Callable[[dict], None], notify: Callable[[dict], Any]) -> str:
    ok, note = shipyard.rollback(str(target["commit"]), restore=bool(control.canary))
    if not ok:
        control.paused, control.reason = True, f"rollback failed: {note}"
        save_control(state_dir, control)
        event = {"event": "upgrade_rollback_failed", "at": now.isoformat(), "reason": f"{why} — {note}",
                 "text": f"rolling back {target.get('title', '')} FAILED: {note}"}
        journal(event)
        notify(event)
        return "rollback_failed"
    control.canary, control.last_shipped, control.rollback_requested = {}, {}, False
    control.paused, control.reason = True, f"rolled back: {why}"[:300]
    save_control(state_dir, control)
    record(state_dir, Task(str(target.get("task", "")), str(target.get("title", "")), "", "upgrade"),
           "rolled_back", now, why)
    event = {"event": "upgrade_rolled_back", "at": now.isoformat(), "reason": why,
             "text": f"rolled back {target.get('title', '')}: {why}"[:300]}
    journal(event)
    notify(event)
    return "rolled_back"

"""``agentic-trading upgrade``: run today's upgrade, watch the canary, or flip the switches."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIONS = ("run", "watch", "pause", "resume", "rollback", "status")


def add_upgrade_parser(sub: Any) -> None:
    parser = sub.add_parser("upgrade", help="The self-upgrader: run, watch, pause, resume, rollback, status")
    actions = parser.add_subparsers(dest="upgrade_action", required=True)
    for name in ACTIONS:
        actions.add_parser(name).add_argument("--config", required=True)


def dispatch_upgrade(args: Any) -> int:
    from agentic_trading.config import load_config
    from agentic_trading.journal import DecisionJournal
    from agentic_trading.upgrade import control as switches
    from agentic_trading.upgrade.run import real_runner
    from agentic_trading.upgrade.settings import load_upgrade_config
    from agentic_trading.upgrade.ship import Shipyard

    config = load_config(args.config)
    state, journal_dir = Path(config.state_dir), Path(config.journal_dir)
    repo = Path(args.config).resolve().parent.parent
    action = args.upgrade_action
    if action in ("pause", "resume", "rollback"):
        {"pause": lambda: switches.pause(state, "paused from the command line"),
         "resume": lambda: switches.resume(state), "rollback": lambda: switches.request_rollback(state)}[action]()
        DecisionJournal(journal_dir).append({"event": "upgrade_control", "action": action, "source": "cli"})
        print(f"upgrade {action}: done" + (" (the watchdog rolls back within 5 minutes)" if action == "rollback" else ""))
        return 0
    if action == "status":
        control = switches.load_control(state)
        try:
            last = json.loads((state / "upgrade.json").read_text(encoding="utf-8")).get("last") or {}
        except (OSError, ValueError):
            last = {}
        print(f"paused: {control.paused} {control.reason}".rstrip())
        print(f"canary: {control.canary.get('title', '-')} until {control.canary.get('until', '-')}")
        print(f"last: {last.get('outcome', '-')} — {last.get('message', '-')}")
        return 0
    now = datetime.now(timezone.utc)
    yard = Shipyard(repo, state, real_runner)
    if action == "watch":
        from agentic_trading.notify import build_notifier
        from agentic_trading.upgrade.watchdog import watch
        from agentic_trading.fast.service import FastJournal

        notifier = build_notifier()
        result = watch(state_dir=state, journal_dir=journal_dir, now=now, runner=real_runner, shipyard=yard,
                       journal=FastJournal(journal_dir, prefix="upgrade").append,
                       notify=(notifier.dispatch if notifier else (lambda record: None)))
        print(f"upgrade watch: {result}")
        return 0
    from agentic_trading.upgrade.cycle import run_cycle

    mode_file = state / "mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else str(config.mode)
    outcome = run_cycle(state_dir=state, journal_dir=journal_dir, repo=repo, venv=repo / ".venv",
                        settings=load_upgrade_config(args.config), mode=mode, now=now, runner=real_runner,
                        shipyard=yard, probe_paths=(repo / "config" / "secrets.toml", Path.home() / ".bashrc"))
    print(f"upgrade run: {outcome.message}")
    return outcome.code

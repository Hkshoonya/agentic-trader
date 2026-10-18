"""``agentic-trading host``: the Windows host's supervisor and the systemctl verbs that talk to it.

    host run        --config C                 supervise every service and timer (the logon task runs this)
    host status     --config C                 what is running, what failed, when each timer fires next
    host restart    --config C UNIT...         systemctl restart
    host start      --config C UNIT...         start a stopped service, or run a timer's job now
    host stop       --config C UNIT...         stop a service until the next restart or start
    host shutdown   --config C                 stop everything and exit the supervisor
    host is-active  --config C UNIT            prints active/inactive/failed; exit 0 only when active
    host show       --config C UNIT -p Result  a timer's last result (success, exit-code, timeout)
    host logs       --config C UNIT --since S  log lines since "YYYY-MM-DD HH:MM:SS UTC"
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERBS = ("run", "status", "restart", "start", "stop", "shutdown", "is-active", "show", "logs")


def add_host_parser(sub: Any) -> None:
    parser = sub.add_parser("host", help="The Windows host: supervise every service and timer, or ask it to act")
    verbs = parser.add_subparsers(dest="host_verb", required=True)
    for verb in VERBS:
        action = verbs.add_parser(verb)
        action.add_argument("--config", required=True)
        if verb in ("restart", "start", "stop"):
            action.add_argument("units", nargs="+")
        if verb in ("is-active", "show", "logs"):
            action.add_argument("unit")
        if verb == "show":
            action.add_argument("-p", "--property", default="Result", choices=("Result",))
            action.add_argument("--value", action="store_true")
        if verb == "logs":
            action.add_argument("--since", required=True)


def dispatch_host(args: Any) -> int:
    from agentic_trading.config import load_config
    from agentic_trading.host import client

    config = load_config(args.config)
    repo = Path(args.config).resolve().parent.parent
    # The children run in the checkout, where a relative state_dir lands; so does every verb here.
    state = Path(config.state_dir) if Path(config.state_dir).is_absolute() else repo / config.state_dir
    verb = args.host_verb
    if verb == "run":
        from agentic_trading.host.supervisor import Supervisor

        return Supervisor(repo, state).run()
    if verb == "is-active":
        found = client.unit_state(state, args.unit)
        print(found)
        return 0 if found == "active" else 3
    if verb == "show":
        print(client.result(state, args.unit))
        return 0
    if verb == "logs":
        since = datetime.strptime(args.since.removesuffix(" UTC"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        print(client.logs_since(state, args.unit, since))
        return 0
    if verb == "status":
        return _print_status(client.status(state), client.running(client.status(state)))
    ok, note = client.request(state, verb, getattr(args, "units", ()))
    print(f"host {verb}: {note}")
    return 0 if ok else 1


def _print_status(state: dict[str, Any], up: bool) -> int:
    if not state:
        print("host: never started here (agentic-trading host run --config ...)")
        return 3
    print(f"host: {'running' if up else 'NOT running'} (pid {state.get('pid')}, heartbeat {state.get('heartbeat')})")
    for name, row in (state.get("services") or {}).items():
        exit_note = f", last exit {row['last_exit']}" if row.get("last_exit") is not None else ""
        shown = row.get("state") if up else "inactive"
        print(f"  {name:32} {shown:10} pid {row.get('pid') or '-'}{exit_note}  log data/state/{row.get('log')}")
    for name, row in (state.get("timers") or {}).items():
        if row.get("state") == "disabled":
            print(f"  {name:32} {row.get('note')}")
            continue
        print(f"  {name:32} next {row.get('next_run') or '-'}  last {row.get('last_start') or 'never'}"
              f" {row.get('result') or ''}")
    return 0 if up else 3

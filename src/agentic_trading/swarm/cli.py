"""``agentic-trading swarm``: run today's step, or show the population."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def add_swarm_parser(sub: Any) -> None:
    parser = sub.add_parser("swarm", help="The agent swarm: run today's step, or show the population")
    actions = parser.add_subparsers(dest="swarm_action", required=True)
    for name, text in (("step", "Run the daily step (the systemd timer runs this)"),
                       ("status", "What the swarm holds and who is alive, in plain words")):
        actions.add_parser(name, help=text).add_argument("--config", required=True)


def dispatch_swarm(args: Any) -> int:
    from agentic_trading.config import load_config

    config = load_config(args.config)
    if args.swarm_action == "status":
        try:
            data = json.loads((Path(config.state_dir) / "swarm.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("swarm status: the swarm has not run yet (no swarm.json)")
            return 1
        print("\n".join(status_lines(data)))
        return 0
    from agentic_trading.swarm.settings import load_swarm_config
    from agentic_trading.swarm.step import run_step

    swarm = load_swarm_config(args.config)
    if not swarm.enabled:
        print("swarm step: [swarm] enabled is not true; nothing to do")
        return 0
    result = run_step(config, swarm)
    print(result.message)
    return result.code


def status_lines(data: dict[str, Any]) -> list[str]:
    book = data.get("book") or {}
    lines = [f"swarm as of {data.get('as_of')}: {data.get('alive', 0)} alive, "
             f"{data.get('trials', 0)} recipes tried, book ${book.get('equity')} ({book.get('return_pct')}%)"]
    if data.get("stale"):
        lines.append(f"  {data.get('note')}")
    for agent in data.get("agents") or []:
        lines.append(f"  {agent.get('name')}: {agent.get('state')}, {agent.get('forward_days')} days, "
                     f"{agent.get('excess_pct'):+.2f}% vs benchmark, share {agent.get('share')}")
    return lines

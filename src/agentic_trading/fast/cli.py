"""``agentic-trading fast``: replay the switchboard over past prices, or show its status."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_trading.fast.bars import BAR_MINUTES


def add_fast_parser(sub: Any) -> None:
    parser = sub.add_parser("fast", help="The switchboard: replay it over past prices, or show its status")
    actions = parser.add_subparsers(dest="fast_action", required=True)
    replay_p = actions.add_parser("replay", help="Run the switchboard over recorded prices or past 1-minute bars")
    replay_p.add_argument("--config", required=True)
    replay_p.add_argument("--source", choices=("recorded", "bars"), default="recorded")
    replay_p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    replay_p.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    replay_p.add_argument("--bar-minutes", type=int, choices=BAR_MINUTES, default=None,
                          help="Try another bar length without editing the config (default: the config's)")
    status_p = actions.add_parser("status", help="What the running switchboard is doing, in plain words")
    status_p.add_argument("--config", required=True)


def dispatch_fast(args: Any, *, fetch: Optional[Callable[..., Any]] = None, today: Optional[date] = None) -> int:
    if args.fast_action == "replay":
        return cmd_replay(args.config, args.source, args.start, args.end, fetch=fetch, today=today,
                          bar_minutes=args.bar_minutes)
    return cmd_status(args.config)


def cmd_replay(config_path: str, source: str, start_text: str, end_text: str, *,
               fetch: Optional[Callable[..., Any]] = None, today: Optional[date] = None,
               bar_minutes: Optional[int] = None) -> int:
    from agentic_trading.config import load_config
    from agentic_trading.fast.replay import bar_ticks, fetch_bars, median_spread, recorded_ticks, replay
    from agentic_trading.fast.settings import load_fast_config
    from agentic_trading.fast.store import FastStore
    from agentic_trading.venues.settings import load_venues_config

    try:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    except ValueError:
        print("fast replay: dates must look like 2026-10-01")
        return 2
    if end < start:
        print("fast replay: --to is before --from")
        return 2
    config = load_config(config_path)
    fast = load_fast_config(config_path)
    if bar_minutes is not None:
        fast = dataclasses.replace(fast, bar_minutes=bar_minutes)
    venues = load_venues_config(config_path)
    if source == "recorded":
        ticks: Any = recorded_ticks(venues.stream_dir, fast.symbols, start, end)
        label = "exact, recorded prices"
    else:
        end = min(end, (today or datetime.now(timezone.utc).date()) - timedelta(days=1))
        if end < start:
            print("fast replay: bars exist only for finished days; pick an earlier --from")
            return 2
        cache = Path(config.state_dir).parent / "fastbars"
        rows = fetch_bars(fast.symbols, start, end, cache, **({"fetch": fetch} if fetch else {}))
        ticks = bar_ticks(rows, median_spread(venues.stream_dir, fast.symbols))
        label = "approximate, from 1-minute bars"
    label += f"; trading {fast.bar_minutes}-minute bars"
    report = replay(ticks, fast, label=label, start=start.isoformat(), end=end.isoformat(),
                    starting_equity=FastStore(config.state_dir).starting_equity())
    if report.ticks == 0:
        print(f"fast replay: no prices between {start} and {end}")
        return 1
    print("\n".join(report.lines()))
    return 0


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):+.2f}%"


def status_lines(data: dict[str, Any]) -> list[str]:
    head = f"switchboard as of {data.get('as_of')}"
    if data.get("failed"):
        head += f" — STOPPED: {data['failed']}"
    book, mirror = data.get("book") or {}, data.get("mirror") or {}
    lines = [head, f"  paper book ${book.get('equity')} ({_pct(book.get('return_pct'))} at Alpaca costs; "
                   f"{_pct(mirror.get('return_pct'))} at Coinbase costs)"]
    for coin in data.get("coins") or []:
        trade = coin.get("trade")
        if trade:
            what = (f"{trade.get('playbook')} open at {trade.get('entry')}, stop {trade.get('stop')}, "
                    f"{_pct(trade.get('pnl_pct'))}")
        elif coin.get("standing_aside"):
            what = "standing aside"
        else:
            what = "watching for a setup"
        lines.append(f"  {coin.get('symbol')}: {coin.get('regime')} · {what}")
    for record in (data.get("recent") or [])[:5]:
        lines.append(f"  {str(record.get('at', ''))[11:19]} {record.get('text', '')}")
    return lines


def cmd_status(config_path: str) -> int:
    from agentic_trading.config import load_config

    config = load_config(config_path)
    try:
        data = json.loads((Path(config.state_dir) / "fast.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("the switchboard has not run yet: set [fast] enabled = true and restart agentic-trading-venues")
        return 1
    print("\n".join(status_lines(data)))
    return 0

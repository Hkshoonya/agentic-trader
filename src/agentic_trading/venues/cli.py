"""``agentic-trading venues …``: run the gateway, check it, smoke-test paper, arm live.

``check`` is read-only. ``smoke`` only ever builds the Alpaca *paper* venue.
``arm`` needs ``--yes``, because arming lets a live venue place real orders.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_trading.venues import arming
from agentic_trading.venues.model import VenueOrder, new_client_order_id

FRESH = timedelta(seconds=10)
_TERMINAL = ("filled", "canceled", "cancelled", "rejected", "expired")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def add_venues_parser(sub: Any) -> None:
    parser = sub.add_parser("venues", help="Alpaca and Coinbase connections: run, check, smoke, arm, disarm")
    actions = parser.add_subparsers(dest="venues_action", required=True)
    for name, text in (
        ("run", "Run the venues service (streams, recorder, health)"),
        ("check", "Read-only: accounts, positions, open orders and stream delay"),
        ("smoke", "Alpaca PAPER only: place and cancel one tiny order"),
    ):
        action = actions.add_parser(name, help=text)
        action.add_argument("--config", required=True)
    arm_p = actions.add_parser("arm", help="Let a live venue place real orders for N hours")
    arm_p.add_argument("venue", choices=arming.LIVE_VENUES)
    arm_p.add_argument("--hours", type=float, required=True)
    arm_p.add_argument("--yes", action="store_true")
    arm_p.add_argument("--config", required=True)
    disarm_p = actions.add_parser("disarm", help="Stop a live venue placing real orders")
    disarm_p.add_argument("venue", choices=arming.LIVE_VENUES)
    disarm_p.add_argument("--config", required=True)


def _load(config_path: str) -> tuple[Any, Any, dict[str, Any]]:
    from agentic_trading.config import load_config
    from agentic_trading.venues.secrets import load_credentials
    from agentic_trading.venues.settings import load_venues_config

    config = load_config(config_path)
    venues_config = load_venues_config(config_path)
    return config, venues_config, load_credentials(venues_config.secrets_path)


def sample_streams(sources: list[Any], seconds: float, *,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, dict[str, Any]]:
    """Run each source for ``seconds`` in its own thread; count ticks. Writes nothing."""
    tally = {s.key: {"ticks": 0, "delays": [], "error": ""} for s in sources}
    lock = threading.Lock()
    threads = []
    for source in sources:
        def emit(tick: Any, key: str = source.key) -> None:
            with lock:
                tally[key]["ticks"] += 1
                delay = (tick.received_at - tick.exchange_at).total_seconds() * 1000
                tally[key]["delays"].append(max(0.0, delay))

        def target(src: Any = source, sink: Callable[[Any], None] = emit) -> None:
            try:
                src.run(sink)
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                tally[src.key]["error"] = f"{type(exc).__name__}: {exc}"[:200]

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        threads.append(thread)
    sleep(seconds)
    for source in sources:
        try:
            source.stop()
        except Exception:  # noqa: BLE001
            pass
    for thread in threads:
        thread.join(timeout=5)
    return {
        key: {
            "ticks": row["ticks"],
            "median_ms": statistics.median(row["delays"]) if row["delays"] else None,
            "error": row["error"],
        }
        for key, row in tally.items()
    }


def _fresh_state(path: Path, now: datetime) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        as_of = datetime.fromisoformat(str(data["as_of"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data if now - as_of <= FRESH else None


def cmd_check(config_path: str, *, out: Callable[[str], None] = print, factories: Optional[dict] = None,
              sleep: Callable[[float], None] = time.sleep, now: Optional[Callable[[], datetime]] = None,
              sample_seconds: float = 10.0) -> int:
    from agentic_trading.venues.daemon import build_sources, build_venues, classify_error
    from agentic_trading.venues.secrets import CredentialsError, redact

    try:
        config, venues_config, creds = _load(config_path)
    except CredentialsError as exc:
        out(f"venues: {exc}")
        return 2
    current = (now or _utcnow)()
    failed = False
    venues = build_venues(venues_config, creds, factories=factories)
    wanted = [name for name, flag in (("alpaca_paper", venues_config.use_alpaca_paper),
                                      ("alpaca_live", venues_config.use_alpaca_live),
                                      ("coinbase", venues_config.use_coinbase)) if flag]
    for name in wanted:
        venue = venues.get(name)
        if venue is None:
            out(f"{name}: no keys (add them to {venues_config.secrets_path})")
            continue
        try:
            account = venue.account()
            positions, orders = venue.positions(), venue.open_orders()
            extra = f", {account.day_trades} day trades" if name.startswith("alpaca") else ""
            out(f"{name} ({venue.mode}, key {creds[name].hint}): equity ${account.equity:,.2f}, "
                f"cash ${account.cash:,.2f}, {len(positions)} positions, {len(orders)} open orders{extra}")
        except Exception as exc:  # noqa: BLE001 - reported per venue
            auth = classify_error(exc) == "auth"
            failed = failed or auth
            out(f"{name}: {'login failed' if auth else 'error'}: {redact(str(exc), creds.values())[:200]}")
    running = _fresh_state(Path(config.state_dir) / "venues.json", current)
    if running is not None:
        out("streams (from the running venues service):")
        for s in running.get("streams", []):
            out(f"  {s.get('key')}: {s.get('status')}, last tick {s.get('last_tick_age_s')}s ago, "
                f"median delay {s.get('delay_ms_median')} ms, reconnects {s.get('reconnects')}")
        return 1 if failed else 0
    sources = build_sources(venues_config, creds, factories=factories)
    if sources:
        out(f"sampling streams for {sample_seconds:g}s…")
        for key, row in sample_streams(sources, sample_seconds, sleep=sleep).items():
            median = "-" if row["median_ms"] is None else f"{row['median_ms']:.0f}"
            line = f"  {key}: {row['ticks']} ticks, median delay {median} ms"
            if row["error"]:
                line += f", error: {redact(row['error'], creds.values())}"
                failed = failed or classify_error(RuntimeError(row["error"])) == "auth"
            out(line)
    return 1 if failed else 0


def cmd_smoke(config_path: str, *, out: Callable[[str], None] = print, factories: Optional[dict] = None,
              sleep: Callable[[float], None] = time.sleep, now: Optional[Callable[[], datetime]] = None,
              session: Optional[Callable[[datetime], str]] = None) -> int:
    from agentic_trading.session import session_for
    from agentic_trading.venues.daemon import build_venues
    from agentic_trading.venues.guard import Limits, VenueGuard
    from agentic_trading.venues.journal import VenueJournal
    from agentic_trading.venues.secrets import CredentialsError

    try:
        config, venues_config, creds = _load(config_path)
    except CredentialsError as exc:
        out(f"venues: {exc}")
        return 2
    paper_only = replace(venues_config, use_alpaca_paper=True, use_alpaca_live=False, use_coinbase=False)
    venue = build_venues(paper_only, creds, factories=factories).get("alpaca_paper")
    if venue is None:
        out("smoke needs Alpaca paper keys: add [alpaca_paper] to config/secrets.toml")
        return 2
    current = (now or _utcnow)()
    if (session or session_for)(current) == "regular":
        order = VenueOrder(new_client_order_id("smoke"), "SPY", "buy", notional=Decimal("1"))
    else:  # a $1 limit on SPY cannot fill, so nothing is bought outside the session
        order = VenueOrder(new_client_order_id("smoke"), "SPY", "buy", type="limit",
                           qty=Decimal("1"), limit_price=Decimal("1.00"))
    guard = VenueGuard("alpaca_paper", Limits.for_venue(venues_config, "alpaca_paper"), config.state_dir)
    journal = VenueJournal(config.journal_dir, creds.values())
    base = {"venue": "alpaca_paper", "client_order_id": order.client_order_id, "symbol": order.symbol,
            "type": order.type, "smoke": True}
    verdict = guard.check(order, price=order.limit_price or Decimal("1"), account=venue.account(),
                          open_orders=len(venue.open_orders()), now=current)
    if not verdict.allowed:
        journal.append({**base, "event": "venue_refused", "reason": verdict.reason}, now=current)
        out(f"smoke refused by the guard: {verdict.reason}")
        return 1
    started = time.monotonic()
    ack = venue.submit(order)
    journal.append({**base, "event": "venue_submitted", "status": ack.status}, now=current)
    for _ in range(20):
        if ack.status != "pending_new":
            break
        sleep(0.5)
        ack = venue.get(order.client_order_id)
    elapsed_ms = (time.monotonic() - started) * 1000
    cancelled = False
    if ack.status not in _TERMINAL:
        venue.cancel(order.client_order_id)
        cancelled = True
        journal.append({**base, "event": "venue_cancelled"}, now=current)
    if ack.filled_qty > 0 and ack.filled_avg_price:
        guard.record_fill(order, notional=ack.filled_qty * ack.filled_avg_price, now=current)
    out(f"smoke: {order.type} {order.symbol} on alpaca_paper -> {ack.status} in {elapsed_ms:.0f} ms"
        f"{'; cancelled' if cancelled else ''}")
    return 0 if ack.status != "rejected" else 1


def cmd_arm(config_path: str, venue: str, hours: float, yes: bool, *, out: Callable[[str], None] = print,
            now: Optional[Callable[[], datetime]] = None) -> int:
    from agentic_trading.config import load_config
    from agentic_trading.venues.journal import VenueJournal

    if not yes:
        out(f"arming lets {venue} place REAL orders for {hours:g} hours; re-run with --yes to confirm")
        return 2
    config = load_config(config_path)
    current = (now or _utcnow)()
    record = arming.arm(config.state_dir, venue, hours=hours, now=current)
    VenueJournal(config.journal_dir).append({"event": "venue_armed", "venue": venue, "until": record["until"]},
                                            now=current)
    out(f"{venue} armed until {record['until']}")
    return 0


def cmd_disarm(config_path: str, venue: str, *, out: Callable[[str], None] = print) -> int:
    from agentic_trading.config import load_config
    from agentic_trading.venues.journal import VenueJournal

    config = load_config(config_path)
    arming.disarm(config.state_dir, venue)
    VenueJournal(config.journal_dir).append({"event": "venue_disarmed", "venue": venue})
    out(f"{venue} disarmed")
    return 0


def dispatch_venues(args: Any) -> int:
    action = args.venues_action
    if action == "run":
        from agentic_trading.venues.daemon import main_run

        return main_run(args.config)
    if action == "check":
        return cmd_check(args.config)
    if action == "smoke":
        return cmd_smoke(args.config)
    if action == "arm":
        return cmd_arm(args.config, args.venue, args.hours, args.yes)
    return cmd_disarm(args.config, args.venue)

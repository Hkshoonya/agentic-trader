"""The 30-day shadow trial: does the rotation beat just holding the market?

A rule that cannot beat buy-and-hold is not worth its complexity, so the trial
has one number and one verdict agreed before it started:

- **book** — the shadow account replayed from accepted shadow decisions since
  the trial began, charged per asset class (equities a few bps, crypto the
  Robinhood spread) and marked to the latest daily close;
- **benchmark** — the same starting cash split the way the rotation splits its
  slots (3 equity, 2 crypto): 60% QQQ, 40% BTC, bought and held;
- **verdict** — after ``TRIAL_DAYS``, keep if the book is ahead, kill if not.

Pre-trial positions are ignored: selling something bought before the trial is
not the trial's P&L.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from agentic_trading.orders import is_crypto_symbol
from agentic_trading.types import DESK_ORDER_REASON

TRIAL_DAYS = 30
FILE_NAME = "trial.json"
BENCHMARK = {"QQQ": 0.6, "BTCUSD": 0.4}
EQUITY_COST_BPS = Decimal("3")
CRYPTO_COST_BPS = Decimal("60")


def _path(config: Any) -> Path:
    return Path(config.state_dir) / FILE_NAME


def load_trial(config: Any) -> Optional[dict[str, Any]]:
    import json

    try:
        payload = json.loads(_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("started_at") else None


def start_trial(
    config: Any, *, starting_equity: Decimal, now: Optional[datetime] = None
) -> dict[str, Any]:
    from agentic_trading import jsonio

    started = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {
        "started_at": started.isoformat(),
        "ends_at": (started + timedelta(days=TRIAL_DAYS)).isoformat(),
        "strategy": config.strategy,
        "starting_equity": str(starting_equity),
        "benchmark": BENCHMARK,
        "rule": "keep if the book beats the benchmark after the trial; kill if not",
    }
    jsonio.write_text(_path(config), jsonio.dumps(payload, indent=2) + "\n")
    return payload


def _stamp(raw: Any) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _closes(config: Any, symbol: str) -> list[tuple[datetime, Decimal]]:
    from agentic_trading.history import load_bars

    path = Path(config.history_path or "data/bars") / (
        f"{symbol.replace('-', '').upper()}_day.jsonl"
    )
    try:
        return [(bar.start, Decimal(str(bar.close))) for bar in load_bars(path)]
    except (OSError, ValueError):
        return []


def _close_before(
    closes: list[tuple[datetime, Decimal]], when: datetime
) -> Optional[Decimal]:
    """The last daily close strictly before ``when``: what was known at that time."""
    known = [close for start, close in closes if start < when]
    return known[-1] if known else None


def _replay(config: Any, started: datetime) -> dict[str, Any]:
    """Cash, holdings and counts from accepted shadow decisions since ``started``."""
    from agentic_trading.journal import DecisionJournal

    trial = load_trial(config) or {}
    cash = Decimal(str(trial.get("starting_equity") or "0"))
    held: dict[str, Decimal] = {}
    last_price: dict[str, Decimal] = {}
    entries = exits = 0
    # Paper orders are sized off the real account value, not the paper book, so
    # after paper losses they can spend cash the book no longer has. The lowest
    # cash balance shows whether the return is quietly levered.
    lowest_cash = cash
    for record in DecisionJournal(Path(config.journal_dir)).iter_all():
        if (
            record.get("event") != "accepted"
            or str(record.get("mode") or "") != "shadow"
        ):
            continue
        intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
        if intent.get("reason") == DESK_ORDER_REASON:
            continue  # the desk's account orders are not the trial strategy's
        stamp = _stamp(record.get("at") or intent.get("created_at"))
        if stamp is None or stamp < started:
            continue
        symbol = str(intent.get("symbol") or "").upper()
        side = str(intent.get("side") or "").lower()
        try:
            quantity = Decimal(str(intent.get("quantity")))
            price = Decimal(str(intent.get("ref_price")))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if not symbol or quantity <= 0 or price <= 0:
            continue
        cost = (
            CRYPTO_COST_BPS if is_crypto_symbol(symbol) else EQUITY_COST_BPS
        ) / 10_000
        last_price[symbol] = price
        if side == "buy":
            cash -= quantity * price * (1 + cost)
            held[symbol] = held.get(symbol, Decimal("0")) + quantity
            entries += 1
            lowest_cash = min(lowest_cash, cash)
        elif side == "sell":
            quantity = min(quantity, held.get(symbol, Decimal("0")))
            if quantity <= 0:
                continue  # a pre-trial position: not the trial's P&L
            cash += quantity * price * (1 - cost)
            held[symbol] -= quantity
            if held[symbol] <= 0:
                held.pop(symbol)
            exits += 1
    return {
        "cash": cash,
        "held": held,
        "last_price": last_price,
        "entries": entries,
        "exits": exits,
        "lowest_cash": lowest_cash,
    }


def _member_book(config: Any, name: str) -> Any:
    """The desk member book that carries the trial, when the desk runs it."""
    if str(getattr(config, "strategy", "")) != "desk":
        return None
    from agentic_trading.desk.book import MemberBook

    path = Path(config.state_dir) / "desk" / f"{name}.json"
    if not path.is_file():
        return None
    book, reset = MemberBook.load(path, name=name, starting_equity=Decimal("0"))
    return None if reset else book


def seed_member_book(config: Any, book: Any) -> bool:
    """Hand the running trial's paper record to its desk member, once."""
    trial = load_trial(config)
    started = _stamp(trial.get("started_at")) if trial else None
    if trial is None or started is None or trial.get("strategy") != book.name:
        return False
    replay = _replay(config, started)
    prices: dict[str, Decimal] = {}
    for symbol in replay["held"]:
        closes = _closes(config, symbol)
        prices[symbol] = (
            closes[-1][1] if closes else replay["last_price"].get(symbol, Decimal("0"))
        )
    book.adopt(
        cash=replay["cash"],
        positions=replay["held"],
        prices=prices,
        starting_equity=Decimal(str(trial["starting_equity"])),
        entries=replay["entries"],
        exits=replay["exits"],
        lowest_cash=replay["lowest_cash"],
    )
    book.save()
    return True


def score_trial(config: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    trial = load_trial(config)
    if trial is None:
        return {"started": False, "note": "no trial running; start one with --start"}
    started = _stamp(trial["started_at"])
    if started is None:
        return {"started": False, "note": "trial state has no usable start time"}
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    starting = Decimal(str(trial.get("starting_equity") or "0"))
    positions: list[dict[str, Any]] = []
    member = _member_book(config, str(trial.get("strategy") or ""))
    if member is not None:
        # The desk runs the trial's strategy as a member: its book is the record,
        # marked at the live prices the desk last saw.
        cash = member.cash
        lowest_cash = member.lowest_cash
        entries, exits = member.entries, member.exits
        book = cash
        for symbol, quantity in sorted(member.positions.items()):
            value = quantity * member.prices.get(symbol, Decimal("0"))
            book += value
            positions.append(
                {"symbol": symbol, "quantity": str(quantity), "value": round(float(value), 2)}
            )
    else:
        replay = _replay(config, started)
        cash = replay["cash"]
        lowest_cash = replay["lowest_cash"]
        entries, exits = replay["entries"], replay["exits"]
        book = cash
        for symbol, quantity in sorted(replay["held"].items()):
            closes = _closes(config, symbol)
            mark = (
                closes[-1][1]
                if closes
                else replay["last_price"].get(symbol, Decimal("0"))
            )
            value = quantity * mark
            book += value
            positions.append(
                {"symbol": symbol, "quantity": str(quantity), "value": round(float(value), 2)}
            )

    benchmark_return = 0.0
    benchmark_parts = {}
    for symbol, share in BENCHMARK.items():
        closes = _closes(config, symbol)
        opening = _close_before(closes, started) if started else None
        latest = closes[-1][1] if closes else None
        if opening is None or latest is None or opening <= 0:
            benchmark_parts[symbol] = None
            continue
        change = float(latest / opening - 1)
        benchmark_parts[symbol] = round(change * 100, 2)
        benchmark_return += share * change

    book_return = float(book / starting - 1) if starting > 0 else 0.0
    days = (current - started).total_seconds() / 86_400 if started else 0.0
    ahead = book_return > benchmark_return
    if days < TRIAL_DAYS:
        verdict = "running"
    else:
        verdict = "keep" if ahead else "kill"
    return {
        "started": True,
        "started_at": trial["started_at"],
        "ends_at": trial.get("ends_at"),
        "days": round(days, 1),
        "strategy": trial.get("strategy"),
        "starting_equity": str(starting),
        "book_value": round(float(book), 2),
        "book_return_pct": round(book_return * 100, 2),
        "benchmark_return_pct": round(benchmark_return * 100, 2),
        "benchmark_parts_pct": benchmark_parts,
        "excess_pct": round((book_return - benchmark_return) * 100, 2),
        "entries": entries,
        "exits": exits,
        "positions": positions,
        "cash": round(float(cash), 2),
        "lowest_cash": round(float(lowest_cash), 2),
        "verdict": verdict,
    }


def format_trial(result: dict[str, Any]) -> str:
    if not result.get("started"):
        return str(result.get("note"))
    lines = [
        f"trial of {result['strategy']}: day {result['days']} of {TRIAL_DAYS} "
        f"(started {result['started_at'][:16]}, ends {str(result['ends_at'])[:10]})",
        f"  book       ${result['book_value']:.2f}  {result['book_return_pct']:+.2f}%  "
        f"({result['entries']} entries, {result['exits']} exits, cash ${result['cash']:.2f})",
        f"  benchmark  {result['benchmark_return_pct']:+.2f}%  "
        + ", ".join(
            f"{symbol} {'n/a' if value is None else f'{value:+.2f}%'}"
            for symbol, value in result["benchmark_parts_pct"].items()
        )
        + "  (60% QQQ / 40% BTC, held)",
        f"  excess     {result['excess_pct']:+.2f} points",
    ]
    if result.get("lowest_cash", 0) < 0:
        lines.append(
            f"  WARNING    paper cash fell to ${result['lowest_cash']:.2f}: the book "
            "bought more than it held, so its return is levered"
        )
    for position in result["positions"]:
        lines.append(f"    holding {position['symbol']:<9} ${position['value']:.2f}")
    verdict = {
        "running": "RUNNING — verdict at day 30",
        "keep": "KEEP — it beat buy-and-hold",
        "kill": "KILL — it did not beat buy-and-hold",
    }[result["verdict"]]
    lines.append(f"  verdict    {verdict}")
    return "\n".join(lines)

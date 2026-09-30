"""The strategy desk as the cockpit sees it: one read-only view for /api/desk.

Everything is read from disk and nothing is written. Qualification comes from
the allocator's own ``allocate``, so the console cannot disagree with the rule
that moves the money. A missing or unreadable piece degrades to an empty value
and a sentence; the endpoint never fails because the state is odd.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from agentic_trading.desk.allocator import MIN_SAMPLES, MemberRecord, allocate, min_t
from agentic_trading.desk.benchmark import BENCHMARK_SHARES
from agentic_trading.desk.book import MemberBook
from agentic_trading.types import DESK_ORDER_REASON

BENCHMARK = "benchmark"
TICKER_SIZE = 30
HISTORY_WEEKS = 12
LABELS = {
    "momentum_rotation": "Momentum rotation",
    "trend_crypto": "Crypto trend",
    "dip_reversal": "Dip buyer",
    "benchmark": "Buy-and-hold",
}


def label(name: str) -> str:
    return LABELS.get(name) or name.replace("_", " ").capitalize()


def next_allocation(now: datetime) -> datetime:
    """The next Monday 00:00 UTC strictly after ``now``: when the allocator runs."""
    day = now.astimezone(timezone.utc).date()
    return datetime.combine(
        day + timedelta(days=7 - day.weekday()), time.min, tzinfo=timezone.utc
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _pct(value: Any, start: Decimal) -> Optional[float]:
    number = _decimal(value)
    if number is None or start <= 0:
        return None
    return round(float(number / start - 1) * 100, 2)


def _load_book(desk_dir: Path, name: str) -> Optional[MemberBook]:
    book, reset = MemberBook.load(
        desk_dir / f"{name}.json", name=name, starting_equity=Decimal("0")
    )
    return None if reset or book.is_new else book


def _samples(book: Optional[MemberBook]) -> list[tuple[str, float]]:
    if book is None:
        return []
    out = []
    for day, equity in book.samples:
        number = _decimal(equity)
        if number is not None:
            out.append((day, float(number)))
    return out


def _member_view(
    name: str,
    book: Optional[MemberBook],
    *,
    stats: dict[str, dict[str, Any]],
    reasons: dict[str, str],
    weight: float,
    t_needed: float,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "name": name,
        "label": label(name),
        "is_benchmark": name == BENCHMARK,
        "series": [],
        "now_pct": None,
        "value": None,
        "holdings": [],
        "entries": 0,
        "exits": 0,
        "samples": 0,
        "samples_needed": MIN_SAMPLES,
        "t": None,
        "t_needed": round(t_needed, 2),
        "weight": weight,
        "reason": reasons.get(name, ""),
    }
    if book is None:
        view["reason"] = "no paper book yet"
        return view
    start = book.starting_equity
    view["series"] = [
        [day, pct]
        for day, equity in book.samples
        if (pct := _pct(equity, start)) is not None
    ]
    if not book.cash_pending:
        view["now_pct"] = _pct(book.equity, start)
        view["value"] = round(float(book.equity), 2)
    view["holdings"] = sorted(
        (
            {"symbol": symbol, "value": round(float(qty * book.prices[symbol]), 2)}
            for symbol, qty in book.positions.items()
            if qty > 0 and symbol in book.prices
        ),
        key=lambda row: (-row["value"], row["symbol"]),
    )
    view["entries"], view["exits"] = book.entries, book.exits
    mine = stats.get(name, {})
    view["samples"] = int(mine.get("samples", len(book.samples)))
    view["t"] = mine.get("t")
    if name == BENCHMARK:
        view["reason"] = "holds whatever no strategy has earned"
    return view


def _allocation_view(
    state: dict[str, Any], events: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    weights = {
        str(name): _float(weight)
        for name, weight in (state.get("allocations") or {}).items()
    }
    history: dict[str, dict[str, float]] = {}
    for record in events:
        if record.get("event") == "desk_allocation" and record.get("week"):
            history[str(record["week"])] = {
                str(name): _float(weight)
                for name, weight in (record.get("allocations") or {}).items()
            }
    weeks = sorted(history)[-HISTORY_WEEKS:]
    return {
        "week": state.get("allocation_week"),
        "weights": weights,
        "legs": {symbol: float(share) for symbol, share in BENCHMARK_SHARES.items()},
        "history": [{"week": week, "weights": history[week]} for week in weeks],
        "next_at": next_allocation(now).isoformat(),
    }


def _trial_view(config: Any, now: datetime) -> Optional[dict[str, Any]]:
    from agentic_trading import trial

    try:
        result = trial.score_trial(config, now=now)
    except Exception:  # noqa: BLE001 — a broken trial file must not break the console
        return None
    if not result.get("started"):
        return None
    strategy = str(result.get("strategy") or "")
    return {
        "strategy": strategy,
        "label": label(strategy),
        "day": result.get("days"),
        "days": trial.TRIAL_DAYS,
        "ends_at": str(result.get("ends_at") or "")[:10],
        "excess_pct": result.get("excess_pct"),
        "book_return_pct": result.get("book_return_pct"),
        "benchmark_return_pct": result.get("benchmark_return_pct"),
        "verdict": result.get("verdict"),
    }


LAYERS = {"llm": "The AI veto", "regime": "The regime check", "jev": "The chase check"}
TICKER_EVENTS = (
    "member_fill",
    "accepted",
    "desk_allocation",
    "advisory_overruled",
    "desk_member_failed",
    "selfcheck",
    "kill_switch",
)
FRESH = timedelta(hours=6)


def _money(value: Any) -> str:
    number = _decimal(value)
    return "$—" if number is None else f"${number:,.2f}"


def _stamp(raw: Any) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _is_news(record: dict[str, Any]) -> bool:
    event = record.get("event")
    if event not in TICKER_EVENTS:
        return False
    if event == "accepted":
        intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
        return intent.get("reason") == DESK_ORDER_REASON
    if event == "selfcheck":
        return record.get("healthy") is False
    return True


def ticker_item(record: dict[str, Any]) -> Optional[dict[str, str]]:
    """One journal event as one plain-English line, or None if it is not news."""
    if not _is_news(record):
        return None
    event = record["event"]
    if event == "member_fill":
        quantity, price = _decimal(record.get("quantity")), _decimal(record.get("price"))
        notional = quantity * price if quantity is not None and price is not None else None
        side = "bought" if str(record.get("side")).lower() == "buy" else "sold"
        kind = "fill"
        text = (
            f"{label(str(record.get('member')))} {side} {record.get('symbol')} "
            f"{_money(notional)} on paper"
        )
    elif event == "accepted":
        side = "bought" if str(record.get("side")).lower() == "buy" else "sold"
        where = "on paper" if record.get("mode") == "shadow" else "for real"
        kind = "order"
        text = (
            f"Desk {side} {record.get('symbol')} {_money(record.get('notional'))} "
            f"for the account, {where}"
        )
    elif event == "desk_allocation":
        kind = "allocation"
        if record.get("changed"):
            weights = sorted(
                (
                    (str(name), _float(weight))
                    for name, weight in (record.get("allocations") or {}).items()
                ),
                key=lambda pair: (-pair[1], pair[0]),
            )
            text = "New weekly allocation: " + ", ".join(
                f"{label(name)} {weight:.0%}" for name, weight in weights if weight > 0
            )
        else:
            text = "Weekly allocation checked: no change"
    elif event == "advisory_overruled":
        kind = "overruled"
        text = (
            f"{LAYERS.get(str(record.get('layer')), 'An advisor')} objected to "
            f"{record.get('symbol')}; the desk followed its evidence"
        )
    elif event == "desk_member_failed":
        kind = "error"
        text = f"{label(str(record.get('member')))} hit an error and sits out today"
    elif event == "selfcheck":
        failures = record.get("failures") or []
        first = failures[0].get("name") if failures and isinstance(failures[0], dict) else None
        kind = "error"
        text = f"Health check found a problem in {first or 'a check'}"
    else:  # kill_switch
        kind = "error"
        text = "Kill switch engaged: trading stopped"
    return {"at": str(record.get("at") or ""), "kind": kind, "text": text}


def story(
    members: list[dict[str, Any]],
    allocation: Optional[dict[str, Any]],
    account_value: Optional[float],
    ticker: list[dict[str, str]],
    *,
    symbols: int,
    now: datetime,
) -> dict[str, str]:
    """The three sentences on the left of the cockpit."""
    bench = next((m for m in members if m.get("is_benchmark")), None)
    racers = [m for m in members if not m.get("is_benchmark") and m.get("now_pct") is not None]
    if bench is None or bench.get("now_pct") is None or not racers:
        right_now = "The race starts when every book has its first prices."
    else:
        best = max(racers, key=lambda m: (m["now_pct"], m["name"]))
        gap = round(best["now_pct"] - bench["now_pct"], 2)
        right_now = (
            f"{best['label']} is beating buy-and-hold by {gap:+.2f} points."
            if gap > 0
            else "No strategy is ahead of buy-and-hold yet; the closest is "
            f"{best['label']} ({gap:+.2f} points)."
        )

    weights = (allocation or {}).get("weights") or {}
    funded = sorted(
        ((name, w) for name, w in weights.items() if name != BENCHMARK and w > 0),
        key=lambda pair: (-pair[1], pair[0]),
    )
    if not weights:
        money = "The desk has not made its first allocation yet."
    elif funded:
        parts = [f"{label(name)} {w:.0%}" for name, w in funded]
        parts.append(f"buy-and-hold {weights.get(BENCHMARK, 0.0):.0%}")
        money = "Capital follows the evidence: " + ", ".join(parts) + "."
    else:
        legs = ", ".join(
            f"{float(share):.0%} {symbol.replace('-USD', '')}"
            for symbol, share in BENCHMARK_SHARES.items()
        )
        amount = f"All {_money(account_value)}" if account_value is not None else "All the money"
        furthest = max(
            (int(m.get("samples") or 0) for m in members if not m.get("is_benchmark")),
            default=0,
        )
        why = (
            f"the furthest along has {furthest} of {MIN_SAMPLES} daily samples."
            if furthest < MIN_SAMPLES
            else "none has beaten buy-and-hold convincingly."
        )
        money = f"{amount} sits in buy-and-hold ({legs}). No strategy has earned capital yet: {why}"

    fresh = [
        item for item in ticker
        if (stamp := _stamp(item.get("at"))) is not None and now - stamp <= FRESH
    ]
    just_now = (
        fresh[0]["text"] + "."
        if fresh
        else f"Watching {symbols} symbols; nothing needs doing right now."
    )
    return {"right_now": right_now, "money": money, "just_now": just_now}


def _relevant_events(path: Path) -> list[dict[str, Any]]:
    """The ticker-worthy events of one journal file; broken lines are skipped."""
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not any(name in line for name in TICKER_EVENTS):
            continue  # most lines are quotes and cycles; skip them unparsed
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and _is_news(record):
            out.append(record)
    return out


class DeskEventCache:
    """Desk news from the dated journals, re-read only from files that changed.

    Old journals never change, so after the first read only today's file is
    parsed again, and only when its size or modification time moved.
    """

    def __init__(self, journal_dir: Path | str) -> None:
        self.journal_dir = Path(journal_dir)
        self._files: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def read(self, *, days: int = 90) -> list[dict[str, Any]]:
        try:
            paths = sorted(
                (p for p in self.journal_dir.glob("*.jsonl") if p.name[:1].isdigit()),
                key=lambda p: p.name,
            )[-days:]
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        with self._lock:
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self._files.get(path.name)
                if cached is None or cached[0] != stamp:
                    cached = (stamp, _relevant_events(path))
                    self._files[path.name] = cached
                events.extend(cached[1])
        return events


def build_desk_view(
    config: Any, events: list[dict[str, Any]], *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Everything the cockpit draws, from the desk's files and journal events."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ticker = [
        item for item in (ticker_item(record) for record in reversed(events)) if item
    ][:TICKER_SIZE]
    symbols = len(getattr(config, "effective_whitelist", ()) or ())
    if str(getattr(config, "strategy", "")) != "desk":
        return {
            "enabled": False,
            "strategy": str(getattr(config, "strategy", "")),
            "as_of": current.isoformat(),
            "members": [],
            "account": {"value": None, "series": []},
            "allocation": None,
            "trial": _trial_view(config, current),
            "story": {
                "right_now": "The cockpit follows the strategy desk; this bot runs "
                f"{label(str(getattr(config, 'strategy', '')))} on its own.",
                "money": "Its orders and positions are on the Orders tab.",
                "just_now": story([], None, None, ticker, symbols=symbols, now=current)["just_now"],
            },
            "ticker": ticker,
        }
    desk_dir = Path(config.state_dir) / "desk"
    names = [str(name) for name in config.desk_members]
    books = {name: _load_book(desk_dir, name) for name in names}
    try:
        result = allocate(
            [
                MemberRecord(name, _samples(book), book.entries if book else 0)
                for name, book in books.items()
            ],
            benchmark=BENCHMARK,
            previous={},
        )
        stats, reasons = result.stats, result.reasons
    except Exception:  # noqa: BLE001 — odd samples must not break the console
        stats, reasons = {}, {}
    state = _read_json(desk_dir / "desk.json")
    allocation = _allocation_view(state, events, current)
    bar = min_t(sum(1 for name in names if name != BENCHMARK))
    members = [
        _member_view(
            name,
            books[name],
            stats=stats,
            reasons=reasons,
            weight=allocation["weights"].get(name, 0.0),
            t_needed=bar,
        )
        for name in names
    ]
    account_book = _load_book(desk_dir, "account")
    account = {
        "value": (
            round(float(account_book.equity), 2)
            if account_book is not None and not account_book.cash_pending
            else None
        ),
        "series": [[day, round(value, 2)] for day, value in _samples(account_book)],
    }
    return {
        "enabled": True,
        "as_of": current.isoformat(),
        "members": members,
        "account": account,
        "allocation": allocation,
        "trial": _trial_view(config, current),
        "story": story(members, allocation, account["value"], ticker, symbols=symbols, now=current),
        "ticker": ticker,
    }

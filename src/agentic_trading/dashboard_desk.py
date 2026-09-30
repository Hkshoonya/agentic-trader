"""The strategy desk as the cockpit sees it: one read-only view for /api/desk.

Everything is read from disk and nothing is written. Qualification comes from
the allocator's own ``allocate``, so the console cannot disagree with the rule
that moves the money. A missing or unreadable piece degrades to an empty value
and a sentence; the endpoint never fails because the state is odd.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from agentic_trading.desk.allocator import MIN_SAMPLES, MemberRecord, allocate, min_t
from agentic_trading.desk.benchmark import BENCHMARK_SHARES
from agentic_trading.desk.book import MemberBook

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


def build_desk_view(
    config: Any, events: list[dict[str, Any]], *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Everything the cockpit draws, from the desk's files and journal events."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if str(getattr(config, "strategy", "")) != "desk":
        return {
            "enabled": False,
            "strategy": str(getattr(config, "strategy", "")),
            "as_of": current.isoformat(),
            "members": [],
            "account": {"value": None, "series": []},
            "allocation": None,
            "trial": _trial_view(config, current),
            "story": {},
            "ticker": [],
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
        "story": {},
        "ticker": [],
    }

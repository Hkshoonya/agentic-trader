"""The strategy desk: several strategies compete, capital follows live results.

To the runtime the desk is one strategy, so the risk guard, broker review,
kill switch and journal are unchanged. Inside it, every member trades its own
paper book in full. A weekly allocation decides how much of the account each
member's book is copied into, and netting turns that into one quantity target
per symbol. The desk emits only the gap for the symbol it was just quoted, so
every order carries a fresh price.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_trading.desk.allocator import UNFUNDED, MemberRecord, allocate, hold_unfunded
from agentic_trading.desk.book import ZERO, MemberBook
from agentic_trading.desk.member import Member, ReadOnlyMember, broker_symbol
from agentic_trading.desk.netting import MIN_USD, gap_intent, target_quantities
from agentic_trading.types import OrderIntent

COSTS_REFRESH_SECONDS = 60.0
# How often the desk checks whether a book another process writes has changed.
BOOK_POLL_SECONDS = 60.0
# A book another process writes that hasn't been marked for this many days is stale:
# its weight goes to the benchmark, so a dead job's frozen holdings are never funded.
STALE_BOOK_DAYS = 6  # the swarm marks 1-2 days behind; leave room for a long market weekend


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _mid(quote: dict[str, Any]) -> Optional[Decimal]:
    bid, ask = _decimal(quote.get("bid")), _decimal(quote.get("ask"))
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _stamp(raw: Any) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


class StrategyDesk:
    last_decided_day = ""

    def __init__(
        self,
        *,
        members: list[Member],
        account: MemberBook,
        costs: Callable[[], Any],
        journal: Callable[[dict[str, Any]], None],
        state_path: Path,
        benchmark: str = "benchmark",
        retry_seconds: float = 900.0,
        monotonic: Callable[[], float] = time.monotonic,
        min_notional: Optional[Callable[[str], Decimal]] = None,
    ) -> None:
        self.members = list(members)
        self.account = account
        self._costs_source = costs
        self.journal = journal
        self.state_path = Path(state_path)
        self.benchmark = benchmark
        self.retry_seconds = float(retry_seconds)
        self._monotonic = monotonic
        self._min_notional = min_notional or (lambda symbol: MIN_USD)
        self.allocations: dict[str, float] = {
            member.name: (1.0 if member.name == benchmark else 0.0) for member in self.members
        }
        self.allocation_week = ""
        self.targets: dict[str, Decimal] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self._pending = True
        # Symbols a member holds that had no quote at the last retarget: only
        # their arrival (or another event) may retarget again, so price moves
        # elsewhere cannot re-size targets while one symbol waits.
        self._awaiting: set[str] = set()
        self._save_error_at = -math.inf
        self._last_emit: dict[str, tuple[float, Decimal]] = {}
        self._costs: Any = None
        self._costs_at = -math.inf
        self._polled_at = -math.inf
        self._noted: set[tuple[str, str]] = set()  # (member, problem) already journaled
        self._scope: set[str] = set()  # symbols a funded read-only book changed: retarget just these
        self._stale_now: set[str] = set()  # funded read-only members whose weight is lent to the benchmark
        self._load()

    # -- runtime strategy interface ---------------------------------------

    def release_decision(self, day: str) -> bool:
        return False  # gaps are re-emitted by the desk itself; nothing to release

    def reload_history(self) -> int:
        total = 0
        for member in self.members:
            reload = getattr(member.strategy, "reload_history", None)
            if callable(reload):
                total += int(reload() or 0)
        return total

    def seed_positions(self, positions: dict[str, Any]) -> int:
        cleaned = {
            broker_symbol(symbol): Decimal(str(quantity))
            for symbol, quantity in (positions or {}).items()
            if Decimal(str(quantity)) > 0
        }
        self.account.set_holdings(cleaned)
        self._pending = True
        events: list[dict[str, Any]] = []
        self._persist(self._save, events)
        for event in events:
            self.journal(event)
        return len(cleaned)

    def note_fill(self, symbol: str, quantity: Any) -> None:
        key = broker_symbol(symbol)
        signed = Decimal(str(quantity))
        seen = self.quotes.get(key, {})
        price = _decimal(seen.get("ask" if signed > 0 else "bid")) or self.account.prices.get(key)
        if price is None:
            return
        self.account.apply_fill(key, signed, price, self.costs())
        events: list[dict[str, Any]] = []
        self._persist(self.account.save, events)
        for event in events:
            self.journal(event)

    def on_quote(self, quote: dict[str, Any]) -> list[OrderIntent]:
        symbol = broker_symbol(str(quote.get("symbol") or ""))
        if not symbol:
            return []
        stamp = _stamp(quote.get("observed_at"))
        quote = {**quote, "symbol": symbol}
        self.quotes[symbol] = quote
        if symbol in self._awaiting:
            self._pending = True
        costs = self.costs()

        # Mark first: a new UTC day's closing sample must be yesterday's equity,
        # not yesterday's equity minus the costs of today's first trades.
        mid = _mid(quote)
        sampled = False
        for book in [member.book for member in self.members] + [self.account]:
            if book.mark({symbol: mid} if mid is not None else {}, stamp):
                sampled = True

        events: list[dict[str, Any]] = []
        self._poll_read_only()
        for member in self.members:
            events.extend(member.on_quote(quote, self.quotes, costs))
        if any(event["event"] == "member_fill" for event in events):
            self._pending = True

        events.extend(self._reallocate(stamp))
        stale = self._stale_members(stamp)
        if stale != self._stale_now:
            self._stale_now = stale
            self._pending = True
        if self._pending:
            events.extend(self._retarget())
        elif self._scope:
            events.extend(self._retarget(only=self._scope))
        if events or sampled:
            self._persist(self._save, events)
        for event in events:
            self.journal(event)
        return self._emit(symbol, quote, stamp)

    # -- internals --------------------------------------------------------

    def costs(self) -> Any:
        now = self._monotonic()
        if self._costs is None or now - self._costs_at >= COSTS_REFRESH_SECONDS:
            self._costs = self._costs_source()
            self._costs_at = now
        return self._costs

    def _note(self, member: str, problem: str, event: dict[str, Any]) -> None:
        """Journal a member's problem once, until it clears."""
        if (member, problem) not in self._noted:
            self._noted.add((member, problem))
            self.journal(event)

    def _poll_read_only(self, force: bool = False) -> None:
        """Follow books other processes write (the swarm's changes daily), at most once a minute.

        Only a funded member's change retargets, and only its own symbols: an unfunded book
        changing must never re-size the account.
        """
        now = self._monotonic()
        if not force and now - self._polled_at < BOOK_POLL_SECONDS:
            return
        self._polled_at = now
        for member in self.members:
            check = getattr(member.book, "refresh_if_changed", None)
            if not callable(check):
                continue
            before = set(member.book.positions)
            if check() and self.allocations.get(member.name, 0.0) > 0:
                self._scope |= before | set(member.book.positions)
            error = getattr(member.book, "read_error", "")
            if error:
                self._note(member.name, "unreadable", {
                    "event": "desk_member_unreadable", "member": member.name, "problem": error,
                    "note": "keeping its last good book" if getattr(member.book, "good", True)
                    else "never read: holding the account's targets until it is"})
            else:
                self._noted.discard((member.name, "unreadable"))

    def _funded_read_only(self) -> list[Member]:
        return [m for m in self.members if isinstance(m, ReadOnlyMember) and m.name not in UNFUNDED
                and self.allocations.get(m.name, 0.0) > 0]

    def _stale_members(self, stamp: datetime) -> set[str]:
        """Funded read-only members not marked for over STALE_BOOK_DAYS: their weight goes to the
        benchmark while they stay stale (never saved, so it returns the moment a fresh book is read)."""
        stale = set()
        for member in self._funded_read_only():
            if not getattr(member.book, "good", True):
                continue  # never read: frozen, not stale (see _retarget)
            day = str(getattr(member.book, "_mark_day", "") or "")
            try:
                old = (stamp.date() - date.fromisoformat(day)).days > STALE_BOOK_DAYS
            except ValueError:
                old = True
            if old:
                stale.add(member.name)
                self._note(member.name, "stale", {
                    "event": "desk_member_stale", "member": member.name, "mark_day": day or None,
                    "note": f"not updated for over {STALE_BOOK_DAYS} days: its weight is lent to the benchmark"})
            else:
                self._noted.discard((member.name, "stale"))
        return stale

    def _effective(self) -> dict[str, float]:
        out = dict(self.allocations)
        for name in self._stale_now:
            out[self.benchmark] = round(out.get(self.benchmark, 0.0) + out.get(name, 0.0), 6)
            out[name] = 0.0
        return out

    def _drop_failed(self, weights: dict[str, float]) -> dict[str, float]:
        out = dict(weights)
        for member in self.members:
            if member.failed and out.get(member.name, 0.0) > 0:
                out[self.benchmark] = round(out.get(self.benchmark, 0.0) + out[member.name], 6)
                out[member.name] = 0.0
        return out

    def _reallocate(self, stamp: datetime) -> list[dict[str, Any]]:
        from agentic_trading.walkforward import rotation_anchor

        cleaned = self._drop_failed(self.allocations)
        if cleaned != self.allocations:
            self.allocations = cleaned
            self._pending = True
        week = rotation_anchor(stamp).date().isoformat()
        if week == self.allocation_week:
            return []
        self._poll_read_only(force=True)  # books other processes write, read fresh for the allocation
        records = [
            MemberRecord(
                member.name,
                [(day, float(equity)) for day, equity in member.book.samples],
                member.book.entries,
            )
            for member in self.members
        ]
        result = allocate(records, benchmark=self.benchmark, previous=self.allocations)
        weights, would_earn = hold_unfunded(self._drop_failed(result.weights), benchmark=self.benchmark)
        changed = weights != self.allocations
        self.allocation_week = week
        if changed:
            self.allocations = weights
            self._pending = True
        return [
            {
                "event": "desk_allocation",
                "week": week,
                "allocations": dict(self.allocations),
                "changed": changed,
                "reasons": result.reasons,
                "stats": result.stats,
                **({"would_earn": would_earn} if would_earn else {}),
            }
        ]

    def _retarget(self, only: Optional[set[str]] = None) -> list[dict[str, Any]]:
        if self.account.cash_pending:
            return []  # the account's value is unknown until every holding is priced
        if any(not getattr(m.book, "good", True) for m in self._funded_read_only()):
            return []  # a funded book never read: hold every target rather than sell its share
        prices = {s: m for s, q in self.quotes.items() if (m := _mid(q)) is not None}
        weights = {member.name: member.book.weights() for member in self.members}
        targets, unpriced = target_quantities(
            self._effective(), weights, self.account.equity, prices
        )
        for symbol in unpriced:
            if symbol in self.targets:
                targets[symbol] = self.targets[symbol]
        if only is not None:  # a funded book changed: re-size its symbols, hold every other quantity
            scoped = dict(self.targets)
            for symbol in only:
                if symbol in targets:
                    scoped[symbol] = targets[symbol]
                else:
                    scoped.pop(symbol, None)
            targets = scoped
            unpriced = [s for s in unpriced if s in only]
        self._pending = False
        self._scope = set()
        self._awaiting = (self._awaiting - only) | set(unpriced) if only is not None else set(unpriced)
        if targets == self.targets:
            return []
        self.targets = targets
        return [
            {
                "event": "desk_targets",
                "targets": {s: str(q) for s, q in sorted(targets.items())},
                "unpriced": sorted(unpriced),
                "account_equity": str(self.account.equity),
            }
        ]

    def _emit(self, symbol: str, quote: dict[str, Any], stamp: datetime) -> list[OrderIntent]:
        if self.account.cash_pending:
            return []
        held = self.account.positions.get(symbol, ZERO)
        if symbol not in self.targets and held <= 0:
            return []
        intent = gap_intent(
            symbol,
            target=self.targets.get(symbol, ZERO),
            held=held,
            quote=quote,
            created_at=stamp,
            min_buy_usd=self._min_notional(symbol),
        )
        if intent is None:
            return []
        now = self._monotonic()
        last = self._last_emit.get(symbol)
        if last is not None and last[1] == held and now - last[0] < self.retry_seconds:
            return []  # same holdings, same gap: a veto or rejection is not retried every quote
        self._last_emit[symbol] = (now, held)
        return [intent]

    def _persist(self, save: Callable[[], None], events: list[dict[str, Any]]) -> None:
        """Run ``save``; a disk error is journaled (hourly), never raised.

        An exception escaping ``on_quote`` counts toward the runtime's
        consecutive-error kill switch; a full disk must not halt paper trading.
        """
        try:
            save()
        except OSError as exc:
            now = self._monotonic()
            if now - self._save_error_at >= 3600:
                self._save_error_at = now
                events.append(
                    {"event": "desk_save_failed", "error": f"{type(exc).__name__}: {exc}"[:200]}
                )

    def _save(self) -> None:
        from agentic_trading import jsonio

        for member in self.members:
            member.book.save()
        self.account.save()
        payload = {
            "allocations": self.allocations,
            "allocation_week": self.allocation_week,
            "targets": {s: str(q) for s, q in self.targets.items()},
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        jsonio.write_text(self.state_path, jsonio.dumps(payload, indent=2) + "\n")

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        stored = raw.get("allocations")
        if isinstance(stored, dict):
            allocations = {str(k): float(v) for k, v in stored.items()}
            for member in self.members:
                allocations.setdefault(member.name, 0.0)
            self.allocations = allocations
        self.allocation_week = str(raw.get("allocation_week") or "")
        targets = raw.get("targets")
        if isinstance(targets, dict):
            self.targets = {str(s): Decimal(str(q)) for s, q in targets.items()}

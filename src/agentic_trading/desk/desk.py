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
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_trading.desk.allocator import MemberRecord, allocate
from agentic_trading.desk.book import ZERO, MemberBook
from agentic_trading.desk.member import Member, broker_symbol
from agentic_trading.desk.netting import gap_intent, target_quantities
from agentic_trading.types import OrderIntent

COSTS_REFRESH_SECONDS = 60.0


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
    ) -> None:
        self.members = list(members)
        self.account = account
        self._costs_source = costs
        self.journal = journal
        self.state_path = Path(state_path)
        self.benchmark = benchmark
        self.retry_seconds = float(retry_seconds)
        self._monotonic = monotonic
        self.allocations: dict[str, float] = {
            member.name: (1.0 if member.name == benchmark else 0.0) for member in self.members
        }
        self.allocation_week = ""
        self.targets: dict[str, Decimal] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self._pending = True
        self._last_emit: dict[str, tuple[float, Decimal]] = {}
        self._costs: Any = None
        self._costs_at = -math.inf
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
        self._save()
        return len(cleaned)

    def note_fill(self, symbol: str, quantity: Any) -> None:
        key = broker_symbol(symbol)
        signed = Decimal(str(quantity))
        seen = self.quotes.get(key, {})
        price = _decimal(seen.get("ask" if signed > 0 else "bid")) or self.account.prices.get(key)
        if price is None:
            return
        self.account.apply_fill(key, signed, price, self.costs())
        self.account.save()

    def on_quote(self, quote: dict[str, Any]) -> list[OrderIntent]:
        symbol = broker_symbol(str(quote.get("symbol") or ""))
        if not symbol:
            return []
        stamp = _stamp(quote.get("observed_at"))
        quote = {**quote, "symbol": symbol}
        self.quotes[symbol] = quote
        costs = self.costs()

        events: list[dict[str, Any]] = []
        for member in self.members:
            events.extend(member.on_quote(quote, self.quotes, costs))
        if any(event["event"] == "member_fill" for event in events):
            self._pending = True

        mid = _mid(quote)
        sampled = False
        for book in [member.book for member in self.members] + [self.account]:
            if book.mark({symbol: mid} if mid is not None else {}, stamp):
                sampled = True

        events.extend(self._reallocate(stamp))
        if self._pending:
            events.extend(self._retarget())
        for event in events:
            self.journal(event)
        if events or sampled:
            self._save()
        return self._emit(symbol, quote, stamp)

    # -- internals --------------------------------------------------------

    def costs(self) -> Any:
        now = self._monotonic()
        if self._costs is None or now - self._costs_at >= COSTS_REFRESH_SECONDS:
            self._costs = self._costs_source()
            self._costs_at = now
        return self._costs

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
        records = [
            MemberRecord(
                member.name,
                [(day, float(equity)) for day, equity in member.book.samples],
                member.book.entries,
            )
            for member in self.members
        ]
        result = allocate(records, benchmark=self.benchmark, previous=self.allocations)
        weights = self._drop_failed(result.weights)
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
            }
        ]

    def _retarget(self) -> list[dict[str, Any]]:
        if self.account.cash_pending:
            return []  # the account's value is unknown until every holding is priced
        prices = {s: m for s, q in self.quotes.items() if (m := _mid(q)) is not None}
        weights = {member.name: member.book.weights() for member in self.members}
        targets, unpriced = target_quantities(
            self.allocations, weights, self.account.equity, prices
        )
        for symbol in unpriced:
            if symbol in self.targets:
                targets[symbol] = self.targets[symbol]
        self._pending = bool(unpriced)
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
        )
        if intent is None:
            return []
        now = self._monotonic()
        last = self._last_emit.get(symbol)
        if last is not None and last[1] == held and now - last[0] < self.retry_seconds:
            return []  # same holdings, same gap: a veto or rejection is not retried every quote
        self._last_emit[symbol] = (now, held)
        return [intent]

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

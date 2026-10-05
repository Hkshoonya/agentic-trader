"""One member's paper account: cash, positions, marks, and a daily equity sample.

Every desk member trades its own book in full, whether or not it currently
holds any of the real allocation, so each builds a live record it can later
win capital with. The same class also keeps the desk's account ledger (fills
the runtime reports, by quantity).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

ZERO = Decimal("0")
STEP = Decimal("0.000001")
MIN_NOTIONAL = Decimal("1.00")


def _charges(costs: Any, symbol: str) -> tuple[Decimal, Decimal]:
    model = costs.for_symbol(symbol)
    per_side = Decimal(str(model.per_side_bps)) / Decimal("10000")
    fee = max(ZERO, Decimal(str(model.fee_per_order)))
    return per_side, fee


class MemberBook:
    def __init__(
        self, name: str, *, starting_equity: Decimal, path: Optional[Path] = None
    ) -> None:
        self.name = name
        self.path = Path(path) if path else None
        self.starting_equity = Decimal(str(starting_equity))
        self.cash = self.starting_equity
        # The lowest cash the book has held: the trial reports it so a paper
        # return that briefly spent more than it had cannot pass unnoticed.
        self.lowest_cash = self.starting_equity
        self.positions: dict[str, Decimal] = {}
        self.prices: dict[str, Decimal] = {}
        self.samples: list[tuple[str, str]] = []
        self.entries = 0
        self.exits = 0
        self.is_new = True
        self.cash_pending = False
        self._mark_day = ""

    # -- valuation -------------------------------------------------------

    @property
    def equity(self) -> Decimal:
        if self.cash_pending:
            return self.starting_equity
        return self.cash + sum(
            (qty * self.prices.get(symbol, ZERO) for symbol, qty in self.positions.items()),
            ZERO,
        )

    def weights(self) -> dict[str, float]:
        """Each priced position's share of equity."""
        equity = self.equity
        if equity <= 0:
            return {}
        return {
            symbol: float(qty * self.prices[symbol] / equity)
            for symbol, qty in self.positions.items()
            if qty > 0 and symbol in self.prices
        }

    def mark(self, prices: dict[str, Decimal], stamp: datetime) -> bool:
        """Update prices; return True when a new UTC day closed a daily sample."""
        when = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        day = when.astimezone(timezone.utc).date().isoformat()
        sampled = False
        if self._mark_day and day != self._mark_day:
            self.samples.append((self._mark_day, str(self.equity)))
            sampled = True
        self._mark_day = day
        for symbol, price in prices.items():
            if price is not None and price > 0:
                self.prices[symbol.upper()] = Decimal(str(price))
        if self.cash_pending and all(s in self.prices for s in self.positions):
            value = sum((q * self.prices[s] for s, q in self.positions.items()), ZERO)
            self.cash = self.starting_equity - value
            self.cash_pending = False
        return sampled

    # -- fills -----------------------------------------------------------

    def buy(
        self,
        symbol: str,
        notional: Decimal,
        price: Decimal,
        costs: Any,
        minimum: Decimal = MIN_NOTIONAL,
    ) -> Decimal:
        """Spend up to ``notional`` (never more than cash); return the quantity.

        ``minimum`` is the smallest order the account could actually place for
        this symbol: a member must not score trades the account cannot copy.
        """
        symbol = symbol.upper()
        per_side, fee = _charges(costs, symbol)
        budget = min(Decimal(str(notional)), self.cash)
        if budget < minimum or price <= 0 or budget <= fee:
            return ZERO
        qty = ((budget - fee) / (price * (1 + per_side))).quantize(STEP, rounding=ROUND_DOWN)
        if qty <= 0:
            return ZERO
        self.cash -= qty * price * (1 + per_side) + fee
        self.lowest_cash = min(self.lowest_cash, self.cash)
        self.positions[symbol] = self.positions.get(symbol, ZERO) + qty
        self.prices.setdefault(symbol, price)
        self.entries += 1
        self.is_new = False
        return qty

    def sell(self, symbol: str, quantity: Decimal, price: Decimal, costs: Any) -> Decimal:
        """Sell up to ``quantity`` of what is held; return the quantity sold."""
        symbol = symbol.upper()
        held = self.positions.get(symbol, ZERO)
        qty = min(Decimal(str(quantity)), held)
        if qty <= 0 or price <= 0:
            return ZERO
        per_side, fee = _charges(costs, symbol)
        self.cash += qty * price * (1 - per_side) - fee
        remaining = held - qty
        if remaining > 0:
            self.positions[symbol] = remaining
        else:
            self.positions.pop(symbol, None)
        self.exits += 1
        self.is_new = False
        return qty

    def apply_fill(
        self, symbol: str, signed_quantity: Decimal, price: Decimal, costs: Any
    ) -> None:
        """Record a fill the runtime reported (the account ledger). Buys may overdraw."""
        symbol = symbol.upper()
        qty = Decimal(str(signed_quantity))
        if qty == 0 or price <= 0:
            return
        per_side, fee = _charges(costs, symbol)
        if qty > 0:
            self.cash -= qty * price * (1 + per_side) + fee
            self.positions[symbol] = self.positions.get(symbol, ZERO) + qty
        else:
            sold = min(-qty, self.positions.get(symbol, ZERO))
            if sold <= 0:
                return
            self.cash += sold * price * (1 - per_side) - fee
            remaining = self.positions.get(symbol, ZERO) - sold
            if remaining > 0:
                self.positions[symbol] = remaining
            else:
                self.positions.pop(symbol, None)
        self.prices[symbol] = price
        self.is_new = False

    def set_holdings(self, positions: dict[str, Decimal]) -> None:
        """Adopt the runtime's holdings. A never-traded book derives cash once priced."""
        cleaned = {
            s.upper(): Decimal(str(q)) for s, q in positions.items() if Decimal(str(q)) > 0
        }
        if self.is_new and self.entries == 0 and self.exits == 0:
            self.positions = cleaned
            self.cash_pending = bool(cleaned)
            self.is_new = not cleaned
            return
        self.positions = cleaned

    def adopt(
        self,
        *,
        cash: Decimal,
        positions: dict[str, Decimal],
        prices: dict[str, Decimal],
        starting_equity: Decimal,
        entries: int,
        exits: int,
        lowest_cash: Optional[Decimal] = None,
    ) -> None:
        """Take over an existing paper record (the rotation trial's) wholesale."""
        self.cash = Decimal(str(cash))
        self.lowest_cash = min(
            self.cash, Decimal(str(lowest_cash)) if lowest_cash is not None else self.cash
        )
        self.positions = {s.upper(): Decimal(str(q)) for s, q in positions.items()}
        self.prices = {s.upper(): Decimal(str(p)) for s, p in prices.items()}
        self.starting_equity = Decimal(str(starting_equity))
        self.entries, self.exits = int(entries), int(exits)
        self.cash_pending = False
        self.is_new = False

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "starting_equity": str(self.starting_equity),
            "cash": str(self.cash),
            "lowest_cash": str(self.lowest_cash),
            "cash_pending": self.cash_pending,
            "positions": {s: str(q) for s, q in self.positions.items()},
            "prices": {s: str(p) for s, p in self.prices.items()},
            "samples": [list(row) for row in self.samples],
            "entries": self.entries,
            "exits": self.exits,
            "mark_day": self._mark_day,
        }

    def save(self) -> None:
        if self.path is None:
            return
        from agentic_trading import jsonio

        self.path.parent.mkdir(parents=True, exist_ok=True)
        jsonio.write_text(self.path, jsonio.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(
        cls, path: Path | str, *, name: str, starting_equity: Decimal
    ) -> tuple["MemberBook", bool]:
        path = Path(path)
        book = cls(name, starting_equity=starting_equity, path=path)
        if not path.is_file():
            return book, False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            book.starting_equity = Decimal(str(raw["starting_equity"]))
            book.cash = Decimal(str(raw["cash"]))
            book.lowest_cash = Decimal(str(raw.get("lowest_cash", raw["cash"])))
            book.cash_pending = bool(raw.get("cash_pending", False))
            book.positions = {s: Decimal(str(q)) for s, q in raw["positions"].items()}
            book.prices = {s: Decimal(str(p)) for s, p in raw["prices"].items()}
            book.samples = [(str(d), str(e)) for d, e in raw["samples"]]
            book.entries = int(raw["entries"])
            book.exits = int(raw["exits"])
            book._mark_day = str(raw.get("mark_day", ""))
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
            return cls(name, starting_equity=starting_equity, path=path), True
        book.is_new = False
        return book, False


def _stat(path: Optional[Path]) -> Optional[tuple[int, int]]:
    try:
        info = path.stat() if path is not None else None
    except OSError:
        return None
    return None if info is None else (info.st_mtime_ns, info.st_size)


class ReadOnlyBook(MemberBook):
    """A member book another process owns: the switchboard's (the venues service)
    or the swarm's (its daily job). The desk reads it, and never marks or writes it.

    A file that can't be read keeps the last good copy and sets ``read_error``:
    for a funded member, reading "nothing held" would sell everything it holds.
    """

    seen: Optional[tuple[int, int]] = None
    read_error = ""
    good = False  # read successfully at least once: until then the desk must not act on it

    @classmethod
    def load(cls, path: Path | str, *, name: str, starting_equity: Decimal) -> tuple["MemberBook", bool]:
        book, broken = super().load(path, name=name, starting_equity=starting_equity)
        present = Path(path).is_file()
        book.good = present and not broken
        book.seen = _stat(Path(path)) if book.good else None
        book.read_error = "" if book.good else ("unreadable" if broken else "missing")
        return book, broken

    def mark(self, prices: dict[str, Decimal], stamp: datetime) -> bool:
        return False

    def save(self) -> None:
        return None

    def refresh(self) -> bool:
        """Re-read the owner's latest file; on a missing or unreadable file keep the last good copy."""
        if self.path is None:
            return False
        if not self.path.is_file():
            self.read_error = "missing"
            return False
        fresh, broken = MemberBook.load(self.path, name=self.name, starting_equity=self.starting_equity)
        if broken:
            self.read_error = "unreadable"
            return False
        for key, value in vars(fresh).items():
            if key != "path":
                setattr(self, key, value)
        self.read_error = ""
        self.good = True
        self.seen = _stat(self.path)
        return True

    def refresh_if_changed(self) -> bool:
        """Re-read only when the file changed; True when the holdings changed (a retarget is due)."""
        stat = _stat(self.path)
        if stat is None:
            self.read_error = "missing" if self.path is not None else ""
            return False
        if stat == self.seen:
            return False
        before = dict(self.positions)
        return self.refresh() and self.positions != before  # a failed read is retried on the next poll

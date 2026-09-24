# Strategy Desk + Quote Tape Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run several strategies side by side in paper trading, give capital only to those beating 60% QQQ / 40% BTC buy-and-hold on live results, and record a clean ~5-second quote tape for the future fast lane.

**Architecture:** A `StrategyDesk` implements the existing runtime strategy interface, so the guard, broker review, kill switch and journal are unchanged. Inside it, each member strategy trades its own paper `MemberBook`. A pure weekly `allocate()` turns live records into weights, and pure netting turns weights into quantity targets. The desk emits only the gap for the quote's own symbol. `QuoteTape` is hooked right after `feed.poll()`.

**Tech Stack:** Python 3.11+, stdlib only (`gzip`, `json`, `decimal`, `statistics`), unittest-style tests run by pytest via `.venv/bin/python -m pytest`, ruff.

**Spec:** `docs/superpowers/specs/2026-09-23-strategy-desk-design.md`

## Global Constraints

- Benchmark: 60% QQQ / 40% BTC-USD, bought and held.
- Allocator eligibility: ≥ 20 daily equity samples, ≥ 1 entry, cumulative return > benchmark's, t-statistic of daily excess returns > 1.0.
- Weight ∝ t, capped at 0.60 per member; the remainder goes to the benchmark; hysteresis: keep the previous allocation unless some weight moves ≥ 0.10.
- Allocator runs when the week changes: Monday 00:00 UTC (`walkforward.rotation_anchor`).
- Netting threshold: act when |gap × price| > max($1.00, 5% of target value). A target of 0 sells everything held.
- Targets are recomputed only on events (allocation change, member fill, seeding); price drift alone never trades.
- Equity symbols act only when `quote["market_session"] == "regular"`.
- Tape path: `<tape_dir>/<SYMBOL>/<YYYY-MM-DD>.jsonl.gz` (UTC date of `observed_at`); `tape_enabled` defaults to `true`; a write error never raises.
- Shadow only. Nothing in this plan touches live placement or `AGENTIC_ALLOW_LIVE`.
- Money is `Decimal`; quantities are quantized to `Decimal("0.000001")` with `ROUND_DOWN`.
- Run tests with `.venv/bin/python -m pytest -q`; lint with `ruff check <files>`.
- Do NOT stage `data/bars/*` (unrelated local data) or `config/agentic.toml*` (gitignored/local).

## Review Focus

1. **A vetoed or rejected desk intent is re-emitted on every quote** (every ~5 s, calling the LLM advisor each time). Expected: at most one re-emission per symbol per `retry_seconds` unless holdings changed. Test in Task 7.
2. **The account book is seeded with holdings before any price is known** (startup). Expected: no division by zero or negative cash invented; cash is derived once every held symbol is priced. Test in Task 2.
3. **An allocation event arrives when a member holds a symbol with no quote yet.** Expected: that symbol keeps its previous target (never silently becomes a "sell all"). Test in Task 6.
4. **Symbol spelling mismatch** (`BTCUSD` from bar-keyed strategies vs `BTC-USD` from quotes). Expected: the desk keys everything in broker form, so a member's BTC is the account's BTC. Test in Task 4.
5. **Restart mid-week.** Expected: allocations and the allocation week are restored, so a restart never triggers an extra reallocation or trades. Test in Task 7.

---

### Task 1: Quote tape

**Files:**
- Create: `src/agentic_trading/tape.py`
- Modify: `src/agentic_trading/config.py` (fields + parsing)
- Modify: `src/agentic_trading/runtime.py` (`_Loop.__init__`, and right after `quotes = feed.poll()` in `run_daemon`)
- Test: `tests/test_tape.py`

**Interfaces:**
- Produces: `QuoteTape(directory: Path)`, `QuoteTape.record(quotes: list[dict], *, now: datetime | None = None) -> str | None` (returns an error message at most once per hour, else `None`; never raises). Config: `tape_enabled: bool = True`, `tape_dir: str = "data/tape"`.

- [ ] **Step 1: Write the failing tests**

```python
"""The quote tape: clean intraday data banked from every poll."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.tape import QuoteTape

T0 = datetime(2026, 9, 24, 1, 0, 1, tzinfo=timezone.utc)


def _quote(bid: str, observed: str = "2026-09-24T01:00:01Z") -> dict:
    return {
        "symbol": "BTC-USD",
        "bid": Decimal(bid),
        "ask": Decimal(bid) + Decimal("0.2"),
        "quote_at": observed,
        "observed_at": observed,
    }


def _rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


class QuoteTapeTests(unittest.TestCase):
    def test_quotes_append_to_a_daily_gzip_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name))
            self.assertIsNone(tape.record([_quote("100.5")], now=T0))
            self.assertIsNone(tape.record([_quote("101")], now=T0))
            rows = _rows(Path(name) / "BTC-USD" / "2026-09-24.jsonl.gz")
        self.assertEqual([row["bid"] for row in rows], ["100.5", "101"])
        self.assertEqual(
            set(rows[0]), {"symbol", "bid", "ask", "quote_at", "observed_at"}
        )

    def test_the_file_is_named_by_the_quote_day_not_the_clock(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name))
            tape.record([_quote("1", observed="2026-09-25T00:00:02Z")], now=T0)
            self.assertTrue(
                (Path(name) / "BTC-USD" / "2026-09-25.jsonl.gz").is_file()
            )

    def test_a_write_error_is_reported_once_per_hour_and_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            blocker = Path(name) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            tape = QuoteTape(blocker)
            first = tape.record([_quote("1")], now=T0)
            second = tape.record([_quote("1")], now=T0 + timedelta(minutes=10))
            third = tape.record([_quote("1")], now=T0 + timedelta(minutes=61))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(third)

    def test_quotes_without_a_usable_symbol_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name))
            tape.record([{**_quote("1"), "symbol": "../evil"}, {"bid": 1}], now=T0)
            self.assertEqual(list(Path(name).iterdir()), [])


class TapeDaemonTests(unittest.TestCase):
    def test_the_daemon_records_every_polled_quote(self) -> None:
        from agentic_trading.broker import Broker
        from agentic_trading.config import load_config
        from agentic_trading.strategies.fixture import FixtureStrategy
        from tests.fakes import FakeMcpClient
        from tests.test_runtime_daemon import (
            _quote as daemon_quote,
            _run,
            _StubFeed,
            _write_config,
            load_tools,
        )

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = load_config(
                _write_config(tmp, extra=[f'tape_dir = "{tmp / "tape"}"'])
            )
            tools = load_tools()
            _run(
                config,
                Broker(FakeMcpClient(tools), tools),
                FixtureStrategy(),
                _StubFeed([daemon_quote()]),
            )
            rows = _rows(tmp / "tape" / "SPY" / "2026-09-16.jsonl.gz")
        self.assertEqual(rows[0]["symbol"], "SPY")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_tape.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentic_trading.tape'`

- [ ] **Step 3: Implement `tape.py`**

```python
"""The quote tape: every polled quote, appended to a daily gzip file per symbol.

Backtests on the downloaded one-year 5-minute set were worthless: 43% of its
bars were interpolated flat fills. The daemon already sees a fresh quote about
every five seconds, so it records its own tape: the clean intraday data a fast
lane has to be designed on. Recording must never cost the trading loop
anything, so every failure is swallowed and reported at most once an hour.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

FIELDS = ("symbol", "bid", "ask", "quote_at", "observed_at")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,15}$")
_REPORT_EVERY = timedelta(hours=1)


class QuoteTape:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._last_error_at: Optional[datetime] = None

    def record(
        self, quotes: list[dict[str, Any]], *, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Append ``quotes``; return an error message at most once an hour."""
        current = now or datetime.now(timezone.utc)
        batches: dict[Path, list[str]] = {}
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").upper()
            if not _SYMBOL.match(symbol):
                continue
            day = _day(quote.get("observed_at"), current)
            row = {name: _text(quote.get(name)) for name in FIELDS}
            row["symbol"] = symbol
            path = self.directory / symbol / f"{day}.jsonl.gz"
            batches.setdefault(path, []).append(
                json.dumps(row, separators=(",", ":")) + "\n"
            )
        try:
            for path, lines in batches.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                # One gzip member per batch: a crash can truncate at most the
                # batch being written, and gzip readers concatenate members.
                with gzip.open(path, "at", encoding="utf-8") as handle:
                    handle.writelines(lines)
        except OSError as exc:
            if (
                self._last_error_at is None
                or current - self._last_error_at >= _REPORT_EVERY
            ):
                self._last_error_at = current
                return f"{type(exc).__name__}: {exc}"[:200]
        return None


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _day(raw: Any, fallback: datetime) -> str:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        stamp = fallback
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).date().isoformat()
```

- [ ] **Step 4: Add the config fields**

In `src/agentic_trading/config.py`, next to `shadow_full_size: bool = False`, add:

```python
    # Record every polled quote to <tape_dir>/<SYMBOL>/<date>.jsonl.gz: the
    # clean intraday data the fast lane will be designed on.
    tape_enabled: bool = True
    tape_dir: str = "data/tape"
```

In `load_config`, next to `shadow_full_size=_boolean(raw, "shadow_full_size", False),`, add:

```python
        tape_enabled=_boolean(raw, "tape_enabled", True),
        tape_dir=str(raw.get("tape_dir", "data/tape")),
```

- [ ] **Step 5: Hook the tape into the runtime**

In `_Loop.__init__` (`src/agentic_trading/runtime.py`), next to `self.strategy = strategy or FixtureStrategy()`, add:

```python
        from agentic_trading.tape import QuoteTape

        self.tape = QuoteTape(config.tape_dir) if config.tape_enabled else None
```

In `run_daemon`, directly after the `try: quotes = feed.poll() ... except ...: quotes = []` block and before the `if crypto_session:` filter, add:

```python
            if loop.tape is not None and quotes:
                tape_error = loop.tape.record(quotes)
                if tape_error:
                    journal.append({"event": "tape_write_failed", "error": tape_error})
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_tape.py`
Expected: 5 passed

- [ ] **Step 7: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && ruff check src/agentic_trading/tape.py src/agentic_trading/config.py src/agentic_trading/runtime.py tests/test_tape.py`
Expected: all pass; "All checks passed!"

- [ ] **Step 8: Commit**

```bash
git add src/agentic_trading/tape.py src/agentic_trading/config.py src/agentic_trading/runtime.py tests/test_tape.py
git commit -m "feat: record every polled quote to a daily gzip tape"
```

---
### Task 2: MemberBook — one paper account

**Files:**
- Create: `src/agentic_trading/desk/__init__.py` (one-line docstring: `"""The strategy desk: members, books, allocation and netting."""`)
- Create: `src/agentic_trading/desk/book.py`
- Test: `tests/test_desk_book.py`

**Interfaces:**
- Consumes: any `costs` object with `.for_symbol(symbol)` returning a model with `.per_side_bps` (Decimal) and `.fee_per_order` (Decimal), i.e. `agentic_trading.backtest.CostModel`.
- Produces: `MemberBook(name: str, *, starting_equity: Decimal, path: Path | None = None)` with attributes `cash: Decimal`, `positions: dict[str, Decimal]`, `prices: dict[str, Decimal]`, `samples: list[tuple[str, str]]` (UTC date, equity), `entries: int`, `exits: int`, `starting_equity: Decimal`, `is_new: bool`, `cash_pending: bool`; property `equity -> Decimal`; methods `weights() -> dict[str, float]`, `buy(symbol, notional, price, costs) -> Decimal`, `sell(symbol, quantity, price, costs) -> Decimal`, `apply_fill(symbol, signed_quantity, price, costs) -> None`, `set_holdings(positions: dict[str, Decimal]) -> None`, `mark(prices: dict[str, Decimal], stamp: datetime) -> bool`, `adopt(*, cash, positions, prices, starting_equity, entries, exits) -> None`, `save() -> None`, `MemberBook.load(path, *, name, starting_equity) -> tuple[MemberBook, bool]` (bool = the file existed but was unreadable). Module constant `MIN_NOTIONAL = Decimal("1.00")`.

- [ ] **Step 1: Write the failing tests**

```python
"""A member's paper book: fills, costs, marks, samples and persistence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.backtest import CostModel
from agentic_trading.desk.book import MemberBook

D = Decimal
FREE = CostModel(D("0"), D("0"), D("0"))
# Equities 2 bps/side; crypto a fixed $0.05 per order.
COSTS = CostModel(D("2"), D("1"), D("0"), crypto=CostModel(D("0"), D("0"), D("0.05")))
DAY1 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
DAY2 = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)


def _book(**kwargs) -> MemberBook:
    return MemberBook("m", starting_equity=D("50"), **kwargs)


class FillTests(unittest.TestCase):
    def test_a_buy_spends_notional_including_costs(self) -> None:
        book = _book()
        qty = book.buy("MSFT", D("10"), D("100"), COSTS)
        self.assertEqual(qty, D("0.099980"))  # 10 / (100 * 1.0002), rounded down
        self.assertEqual(book.positions["MSFT"], qty)
        self.assertAlmostEqual(float(book.cash), 40.0, places=3)
        self.assertEqual(book.entries, 1)

    def test_crypto_pays_the_fixed_fee(self) -> None:
        book = _book()
        qty = book.buy("BTC-USD", D("10"), D("100"), COSTS)
        self.assertEqual(qty, D("0.099500"))  # (10 - 0.05) / 100
        proceeds_before = book.cash
        book.sell("BTC-USD", qty, D("100"), COSTS)
        self.assertEqual(book.cash - proceeds_before, qty * 100 - D("0.05"))

    def test_a_member_never_borrows(self) -> None:
        book = _book()
        book.buy("MSFT", D("45"), D("10"), FREE)
        self.assertEqual(book.buy("AAPL", D("10"), D("10"), FREE), D("0.500000"))
        self.assertEqual(book.cash, D("0"))
        self.assertEqual(book.buy("NVDA", D("10"), D("10"), FREE), D("0"))

    def test_an_order_under_the_minimum_is_skipped(self) -> None:
        self.assertEqual(_book().buy("MSFT", D("0.99"), D("10"), FREE), D("0"))

    def test_a_sell_never_exceeds_holdings(self) -> None:
        book = _book()
        book.buy("MSFT", D("10"), D("10"), FREE)
        self.assertEqual(book.sell("MSFT", D("5"), D("10"), FREE), D("1.000000"))
        self.assertNotIn("MSFT", book.positions)
        self.assertEqual(book.exits, 1)
        self.assertEqual(book.sell("MSFT", D("1"), D("10"), FREE), D("0"))


class MarkTests(unittest.TestCase):
    def test_marks_value_positions_and_sample_once_per_day(self) -> None:
        book = _book()
        book.buy("MSFT", D("10"), D("10"), FREE)
        self.assertFalse(book.mark({"MSFT": D("12")}, DAY1))
        self.assertEqual(book.equity, D("52"))
        self.assertTrue(book.mark({"MSFT": D("11")}, DAY2))
        # The closing mark of day 1 is the last price seen that day.
        self.assertEqual(
            [(day, Decimal(value)) for day, value in book.samples],
            [("2026-09-24", D("52"))],
        )

    def test_weights_are_position_value_over_equity(self) -> None:
        book = _book()
        book.buy("MSFT", D("25"), D("10"), FREE)
        book.mark({"MSFT": D("10")}, DAY1)
        self.assertEqual(book.weights(), {"MSFT": 0.5})


class SeedingTests(unittest.TestCase):
    def test_holdings_seeded_before_any_price_derive_cash_once_priced(self) -> None:
        book = _book()
        book.set_holdings({"SOL-USD": D("0.05")})
        self.assertTrue(book.cash_pending)
        self.assertEqual(book.equity, D("50"))  # no invented value while unpriced
        book.mark({"SOL-USD": D("200")}, DAY1)
        self.assertFalse(book.cash_pending)
        self.assertEqual(book.cash, D("40"))
        self.assertEqual(book.equity, D("50"))

    def test_seeding_a_traded_book_reconciles_quantities_only(self) -> None:
        book = _book()
        book.buy("MSFT", D("10"), D("10"), FREE)
        cash = book.cash
        book.set_holdings({"MSFT": D("0.5")})
        self.assertEqual(book.positions, {"MSFT": D("0.5")})
        self.assertEqual(book.cash, cash)
        self.assertFalse(book.cash_pending)

    def test_the_account_ledger_follows_fills_by_quantity(self) -> None:
        book = _book()
        book.apply_fill("QQQ", D("0.1"), D("100"), FREE)
        book.apply_fill("QQQ", D("-0.04"), D("110"), FREE)
        self.assertEqual(book.positions["QQQ"], D("0.06"))
        self.assertEqual(book.cash, D("50") - D("10") + D("4.4"))


class PersistenceTests(unittest.TestCase):
    def test_a_saved_book_loads_back_identically(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "desk" / "m.json"
            book = MemberBook("m", starting_equity=D("50"), path=path)
            book.buy("MSFT", D("10"), D("10"), FREE)
            book.mark({"MSFT": D("11")}, DAY1)
            book.mark({"MSFT": D("11")}, DAY2)
            book.save()
            loaded, reset = MemberBook.load(path, name="m", starting_equity=D("99"))
        self.assertFalse(reset)
        self.assertFalse(loaded.is_new)
        self.assertEqual(loaded.cash, book.cash)
        self.assertEqual(loaded.positions, book.positions)
        self.assertEqual(loaded.samples, book.samples)
        self.assertEqual(loaded.starting_equity, D("50"))
        self.assertEqual((loaded.entries, loaded.exits), (1, 0))

    def test_a_missing_file_is_a_new_book(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            book, reset = MemberBook.load(
                Path(name) / "x.json", name="x", starting_equity=D("50")
            )
        self.assertTrue(book.is_new)
        self.assertFalse(reset)
        self.assertEqual(book.cash, D("50"))

    def test_a_corrupt_file_resets_the_book_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "x.json"
            path.write_text("{not json", encoding="utf-8")
            book, reset = MemberBook.load(path, name="x", starting_equity=D("50"))
        self.assertTrue(reset)
        self.assertEqual(book.cash, D("50"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_desk_book.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentic_trading.desk'`

- [ ] **Step 3: Implement `desk/book.py`**

```python
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

    def buy(self, symbol: str, notional: Decimal, price: Decimal, costs: Any) -> Decimal:
        """Spend up to ``notional`` (never more than cash); return the quantity."""
        symbol = symbol.upper()
        per_side, fee = _charges(costs, symbol)
        budget = min(Decimal(str(notional)), self.cash)
        if budget < MIN_NOTIONAL or price <= 0 or budget <= fee:
            return ZERO
        qty = ((budget - fee) / (price * (1 + per_side))).quantize(STEP, rounding=ROUND_DOWN)
        if qty <= 0:
            return ZERO
        self.cash -= qty * price * (1 + per_side) + fee
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
    ) -> None:
        """Take over an existing paper record (the rotation trial's) wholesale."""
        self.cash = Decimal(str(cash))
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_desk_book.py`
Expected: 13 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/agentic_trading/desk tests/test_desk_book.py
git add src/agentic_trading/desk/__init__.py src/agentic_trading/desk/book.py tests/test_desk_book.py
git commit -m "feat(desk): paper member book with per-class costs and daily samples"
```

---
### Task 3: Benchmark member strategy

**Files:**
- Create: `src/agentic_trading/desk/benchmark.py`
- Test: `tests/test_desk_members.py` (created here, extended in Task 4)

**Interfaces:**
- Produces: `BENCHMARK_SHARES: dict[str, Decimal] = {"QQQ": Decimal("0.6"), "BTC-USD": Decimal("0.4")}`; `BenchmarkStrategy(shares: dict[str, Decimal] | None = None)` implementing `on_quote(quote) -> list[OrderIntent]`, `seed_positions(positions) -> int`, `note_fill(symbol, quantity) -> None`, `release_decision(day) -> bool` (always False), `reload_history() -> int` (0), attribute `last_decided_day = ""`. Each buy intent carries `metadata={"target_share": "<share>"}`, which the member sizes as that share of its book equity (Task 4).

- [ ] **Step 1: Write the failing tests**

```python
"""Desk members: the benchmark strategy and the member wrapper."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agentic_trading.desk.benchmark import BenchmarkStrategy
from agentic_trading.types import Side

D = Decimal


def quote(symbol: str, bid: str, ask: str, session: str = "regular") -> dict:
    return {
        "symbol": symbol,
        "bid": D(bid),
        "ask": D(ask),
        "observed_at": "2026-09-24T14:00:00Z",
        "market_session": session,
    }


class BenchmarkStrategyTests(unittest.TestCase):
    def test_buys_each_share_once_with_its_target_share(self) -> None:
        strategy = BenchmarkStrategy()
        [intent] = strategy.on_quote(quote("BTC-USD", "100", "101"))
        self.assertEqual((intent.symbol, intent.side), ("BTC-USD", Side.BUY))
        self.assertEqual(intent.metadata, {"target_share": "0.4"})
        self.assertEqual(intent.ref_price, D("101"))
        strategy.note_fill("BTC-USD", D("0.1"))
        self.assertEqual(strategy.on_quote(quote("BTC-USD", "100", "101")), [])

    def test_equities_wait_for_the_regular_session(self) -> None:
        strategy = BenchmarkStrategy()
        self.assertEqual(strategy.on_quote(quote("QQQ", "500", "500.1", "extended")), [])
        [intent] = strategy.on_quote(quote("QQQ", "500", "500.1"))
        self.assertEqual(intent.metadata, {"target_share": "0.6"})

    def test_other_symbols_and_seeded_holdings_are_ignored(self) -> None:
        strategy = BenchmarkStrategy()
        self.assertEqual(strategy.on_quote(quote("MSFT", "1", "1")), [])
        self.assertEqual(strategy.seed_positions({"QQQ": "0.05"}), 1)
        self.assertEqual(strategy.on_quote(quote("QQQ", "500", "500.1")), [])
        self.assertFalse(strategy.release_decision("crypto:2026-09-24"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_desk_members.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentic_trading.desk.benchmark'`

- [ ] **Step 3: Implement `desk/benchmark.py`**

```python
"""The benchmark member: 60% QQQ / 40% BTC, bought once and held.

It is the desk's "do nothing smart" option. Every other member has to beat it
on live results to be given capital, and it holds whatever capital nobody
earned.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from agentic_trading.orders import is_crypto_symbol
from agentic_trading.types import OrderIntent, Side, new_decision_id

BENCHMARK_SHARES: dict[str, Decimal] = {"QQQ": Decimal("0.6"), "BTC-USD": Decimal("0.4")}


class BenchmarkStrategy:
    last_decided_day = ""

    def __init__(self, shares: Optional[dict[str, Decimal]] = None) -> None:
        self.shares = {s.upper(): Decimal(str(v)) for s, v in (shares or BENCHMARK_SHARES).items()}
        self._held: set[str] = set()

    def on_quote(self, quote: dict[str, Any]) -> list[OrderIntent]:
        symbol = str(quote.get("symbol") or "").upper()
        if symbol not in self.shares or symbol in self._held:
            return []
        if not is_crypto_symbol(symbol) and quote.get("market_session") != "regular":
            return []
        try:
            ask = Decimal(str(quote.get("ask")))
        except (ArithmeticError, TypeError, ValueError):
            return []
        if not ask.is_finite() or ask <= 0:
            return []
        return [
            OrderIntent(
                decision_id=new_decision_id(),
                symbol=symbol,
                side=Side.BUY,
                reason="benchmark_hold",
                created_at=_stamp(quote.get("observed_at")),
                quantity=Decimal("1"),  # placeholder; the member sizes by target_share
                ref_price=ask,
                metadata={"target_share": str(self.shares[symbol])},
            )
        ]

    def seed_positions(self, positions: dict[str, Any]) -> int:
        self._held = {
            s.upper() for s, q in (positions or {}).items() if Decimal(str(q)) > 0
        }
        return len(self._held)

    def note_fill(self, symbol: str, quantity: Any) -> None:
        if Decimal(str(quantity)) > 0:
            self._held.add(symbol.upper())

    def release_decision(self, day: str) -> bool:
        return False

    def reload_history(self) -> int:
        return 0


def _stamp(raw: Any) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_desk_members.py`
Expected: 3 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/agentic_trading/desk/benchmark.py tests/test_desk_members.py
git add src/agentic_trading/desk/benchmark.py tests/test_desk_members.py
git commit -m "feat(desk): benchmark member holding 60% QQQ / 40% BTC"
```

---

### Task 4: Member — a strategy trading its own paper book

**Files:**
- Create: `src/agentic_trading/desk/member.py`
- Test: `tests/test_desk_members.py` (append)

**Interfaces:**
- Consumes: `MemberBook` (Task 2), any strategy with `on_quote`, optional `note_fill`, optional `seed_positions`.
- Produces: `Member(name: str, strategy: Any, book: MemberBook, *, order_pct: Decimal)` with `name`, `strategy`, `book`, `failed: str` (empty when healthy), and `on_quote(quote: dict, quotes: dict[str, dict], costs: Any) -> list[dict]`, returning journal events (`member_fill` or `desk_member_failed`). Sizing: `target_share` metadata → share × book equity; otherwise `order_pct × book equity × min(1, weight or 1)`. Buys fill at the latest ask for that symbol in `quotes` (falling back to `intent.ref_price`); sells at the latest bid (same fallback). The book is authoritative: at construction the member seeds its strategy with the book's positions.

- [ ] **Step 1: Append the failing tests**

```python
from agentic_trading.backtest import CostModel  # noqa: E402
from agentic_trading.desk.book import MemberBook  # noqa: E402
from agentic_trading.desk.member import Member  # noqa: E402
from agentic_trading.types import OrderIntent  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

FREE = CostModel(D("0"), D("0"), D("0"))
NOW = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)


class _Scripted:
    """Emits a fixed list of intents once, and records note_fill calls."""

    def __init__(self, intents: list[OrderIntent]) -> None:
        self.intents = intents
        self.fills: list[tuple[str, Decimal]] = []
        self.seeded: dict = {}

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        out, self.intents = self.intents, []
        return out

    def note_fill(self, symbol: str, quantity: Decimal) -> None:
        self.fills.append((symbol, Decimal(str(quantity))))

    def seed_positions(self, positions: dict) -> int:
        self.seeded = dict(positions)
        return len(positions)


def _intent(symbol: str, side: Side, **kwargs) -> OrderIntent:
    return OrderIntent(
        decision_id=f"{symbol}-{side.value}",
        symbol=symbol,
        side=side,
        reason="test",
        created_at=NOW,
        quantity=kwargs.pop("quantity", D("1")),
        ref_price=kwargs.pop("ref_price", D("100")),
        **kwargs,
    )


def _member(strategy, *, order_pct: str = "0.19") -> Member:
    return Member(
        "m", strategy, MemberBook("m", starting_equity=D("50")), order_pct=D(order_pct)
    )


class MemberTests(unittest.TestCase):
    def test_an_entry_is_sized_by_order_pct_and_weight_at_the_live_ask(self) -> None:
        strategy = _Scripted([_intent("MSFT", Side.BUY, weight=D("0.5"))])
        member = _member(strategy)
        quotes = {"MSFT": quote("MSFT", "99", "100")}
        [event] = member.on_quote(quotes["MSFT"], quotes, FREE)
        self.assertEqual(event["event"], "member_fill")
        # 50 * 0.19 * 0.5 = 4.75 at the ask of 100
        self.assertEqual(member.book.positions["MSFT"], D("0.047500"))
        self.assertEqual(strategy.fills, [("MSFT", D("0.047500"))])

    def test_target_share_sizes_against_book_equity(self) -> None:
        strategy = _Scripted(
            [_intent("QQQ", Side.BUY, metadata={"target_share": "0.6"})]
        )
        member = _member(strategy)
        quotes = {"QQQ": quote("QQQ", "499", "500")}
        member.on_quote(quotes["QQQ"], quotes, FREE)
        self.assertEqual(member.book.positions["QQQ"], D("0.060000"))

    def test_an_exit_sells_at_the_live_bid_and_tells_the_strategy(self) -> None:
        strategy = _Scripted([_intent("MSFT", Side.BUY)])
        member = _member(strategy, order_pct="0.2")
        quotes = {"MSFT": quote("MSFT", "99", "100")}
        member.on_quote(quotes["MSFT"], quotes, FREE)
        held = member.book.positions["MSFT"]
        strategy.intents = [_intent("MSFT", Side.SELL, quantity=held)]
        member.on_quote(quotes["MSFT"], quotes, FREE)
        self.assertNotIn("MSFT", member.book.positions)
        self.assertEqual(strategy.fills[-1], ("MSFT", -held))
        self.assertAlmostEqual(float(member.book.cash), 50 - 10 + float(held) * 99, 6)

    def test_bar_keyed_symbols_are_filled_in_broker_form(self) -> None:
        # The trend strategy keys history as BTCUSD but emits BTC-USD intents;
        # a bare BTCUSD intent must still land on the BTC-USD position.
        strategy = _Scripted([_intent("BTCUSD", Side.BUY)])
        member = _member(strategy)
        quotes = {"BTC-USD": quote("BTC-USD", "99", "100")}
        member.on_quote(quotes["BTC-USD"], quotes, FREE)
        self.assertIn("BTC-USD", member.book.positions)
        self.assertNotIn("BTCUSD", member.book.positions)

    def test_a_raising_strategy_disables_only_this_member(self) -> None:
        class _Broken:
            def on_quote(self, quote: dict) -> list:
                raise RuntimeError("boom")

        member = _member(_Broken())
        q = quote("MSFT", "1", "1")
        [event] = member.on_quote(q, {"MSFT": q}, FREE)
        self.assertEqual(event["event"], "desk_member_failed")
        self.assertIn("boom", member.failed)
        self.assertEqual(member.on_quote(q, {"MSFT": q}, FREE), [])

    def test_the_book_is_authoritative_for_the_strategy_at_start(self) -> None:
        book = MemberBook("m", starting_equity=D("50"))
        book.buy("MSFT", D("10"), D("10"), FREE)
        strategy = _Scripted([])
        Member("m", strategy, book, order_pct=D("0.19"))
        self.assertEqual(strategy.seeded, {"MSFT": "1.000000"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_desk_members.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentic_trading.desk.member'`

- [ ] **Step 3: Implement `desk/member.py`**

```python
"""A desk member: an existing strategy, trading its own paper book in full.

The member converts the strategy's intents into paper fills at the live quote
(buys at the ask, sells at the bid) and reports each fill back to the strategy,
exactly as the runtime reports shadow fills, so the strategy's own ledger stays
true. A strategy that raises is disabled; the rest of the desk carries on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from agentic_trading.desk.book import ZERO, MemberBook
from agentic_trading.orders import is_crypto_symbol
from agentic_trading.types import Side


def broker_symbol(symbol: str) -> str:
    """BTCUSD -> BTC-USD; equities unchanged. The desk keys everything this way."""
    text = str(symbol).upper()
    if is_crypto_symbol(text) and "-" not in text and text.endswith("USD"):
        return f"{text[:-3]}-USD"
    return text


def _price(raw: Any, fallback: Any) -> Optional[Decimal]:
    for value in (raw, fallback):
        try:
            price = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if price.is_finite() and price > 0:
            return price
    return None


class Member:
    def __init__(
        self, name: str, strategy: Any, book: MemberBook, *, order_pct: Decimal
    ) -> None:
        self.name = name
        self.strategy = strategy
        self.book = book
        self.order_pct = Decimal(str(order_pct))
        self.failed = ""
        seed = getattr(strategy, "seed_positions", None)
        if callable(seed):
            seed({symbol: str(qty) for symbol, qty in book.positions.items()})

    def on_quote(
        self, quote: dict[str, Any], quotes: dict[str, dict[str, Any]], costs: Any
    ) -> list[dict[str, Any]]:
        if self.failed:
            return []
        try:
            intents = list(self.strategy.on_quote(quote) or [])
        except Exception as exc:  # noqa: BLE001 — one member must not stop the desk
            self.failed = f"{type(exc).__name__}: {exc}"[:200]
            return [
                {"event": "desk_member_failed", "member": self.name, "error": self.failed}
            ]
        events: list[dict[str, Any]] = []
        for intent in intents:
            symbol = broker_symbol(intent.symbol)
            side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))
            seen = quotes.get(symbol) or {}
            if side is Side.BUY:
                price = _price(seen.get("ask"), intent.ref_price)
                if price is None:
                    continue
                share = (intent.metadata or {}).get("target_share")
                if share is not None:
                    notional = self.book.equity * Decimal(str(share))
                else:
                    weight = Decimal(str(intent.weight)) if intent.weight is not None else Decimal("1")
                    notional = self.book.equity * self.order_pct * min(Decimal("1"), max(ZERO, weight))
                filled = self.book.buy(symbol, notional, price, costs)
                signed = filled
            else:
                price = _price(seen.get("bid"), intent.ref_price)
                if price is None or intent.quantity is None:
                    continue
                filled = self.book.sell(symbol, Decimal(str(intent.quantity)), price, costs)
                signed = -filled
            if filled <= 0:
                continue
            note = getattr(self.strategy, "note_fill", None)
            if callable(note):
                note(symbol, signed)
            events.append(
                {
                    "event": "member_fill",
                    "member": self.name,
                    "symbol": symbol,
                    "side": side.value,
                    "quantity": str(filled),
                    "price": str(price),
                    "reason": intent.reason,
                }
            )
        return events
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_desk_members.py`
Expected: 9 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/agentic_trading/desk/member.py tests/test_desk_members.py
git add src/agentic_trading/desk/member.py tests/test_desk_members.py
git commit -m "feat(desk): member wrapper filling strategy intents on its own paper book"
```

---
### Task 5: Allocator — weekly weights from live records

**Files:**
- Create: `src/agentic_trading/desk/allocator.py`
- Test: `tests/test_desk_allocator.py`

**Interfaces:**
- Produces: constants `MIN_SAMPLES = 20`, `MIN_T = 1.0`, `MAX_WEIGHT = 0.60`, `HYSTERESIS = 0.10`; `MemberRecord(name: str, samples: list[tuple[str, float]], entries: int)` (frozen dataclass); `Allocation(weights: dict[str, float], changed: bool, reasons: dict[str, str], stats: dict[str, dict])`; `allocate(records: list[MemberRecord], *, benchmark: str, previous: dict[str, float]) -> Allocation`. Pure, with no I/O. Every record's name appears in `weights`, and the weights sum to 1.0.

- [ ] **Step 1: Write the failing tests**

```python
"""The allocator: capital only for members beating buy-and-hold on live results."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from agentic_trading.desk.allocator import MemberRecord, allocate

START = date(2026, 9, 24)


def series(returns: list[float], start: float = 50.0) -> list[tuple[str, float]]:
    out, equity = [(START.isoformat(), start)], start
    for day, r in enumerate(returns, start=1):
        equity *= 1 + r
        out.append(((START + timedelta(days=day)).isoformat(), equity))
    return out


FLAT = [0.0] * 25
BENCH = MemberRecord("benchmark", series([0.001] * 25), entries=2)
# Beats the benchmark by a steady but noisy margin: t well above 1.
WINNER = [0.004 if i % 2 else 0.0015 for i in range(25)]


class AllocatorTests(unittest.TestCase):
    def test_no_evidence_means_everything_holds_the_benchmark(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER[:18]), entries=3)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights, {"benchmark": 1.0, "rot": 0.0})
        self.assertIn("19 daily samples", result.reasons["rot"])

    def test_twenty_samples_and_an_edge_qualify_capped_at_sixty_percent(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER[:19]), entries=3)],
            benchmark="benchmark",
            previous={"benchmark": 1.0},
        )
        self.assertEqual(result.weights, {"benchmark": 0.4, "rot": 0.6})
        self.assertTrue(result.changed)
        self.assertIn("qualifies", result.reasons["rot"])

    def test_a_member_with_no_entries_cannot_qualify(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER), entries=0)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights["rot"], 0.0)
        self.assertIn("no entries", result.reasons["rot"])

    def test_trailing_the_benchmark_disqualifies(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("flat", series(FLAT), entries=5)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights["flat"], 0.0)
        self.assertIn("trails the benchmark", result.reasons["flat"])

    def test_a_lucky_single_day_does_not_pass_the_consistency_bar(self) -> None:
        lucky = [0.0] * 24 + [0.10]  # beats in total, t well below 1
        result = allocate(
            [BENCH, MemberRecord("lucky", series(lucky), entries=1)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights["lucky"], 0.0)
        self.assertIn("t=", result.reasons["lucky"])

    def test_weights_split_by_t_between_qualifiers(self) -> None:
        strong = [0.006 if i % 2 else 0.003 for i in range(25)]
        result = allocate(
            [
                BENCH,
                MemberRecord("a", series(WINNER), entries=3),
                MemberRecord("b", series(strong), entries=3),
            ],
            benchmark="benchmark",
            previous={},
        )
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=6)
        self.assertGreater(result.weights["a"], 0.0)
        self.assertGreater(result.weights["b"], 0.0)
        self.assertLessEqual(max(result.weights["a"], result.weights["b"]), 0.6)

    def test_small_changes_keep_the_previous_allocation(self) -> None:
        previous = {"benchmark": 0.45, "rot": 0.55}
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER), entries=3)],
            benchmark="benchmark",
            previous=previous,
        )
        # The new weights would be 0.4 / 0.6, a 5-point move: under hysteresis.
        self.assertEqual(result.weights, previous)
        self.assertFalse(result.changed)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_desk_allocator.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentic_trading.desk.allocator'`

- [ ] **Step 3: Implement `desk/allocator.py`**

```python
"""Weekly allocation: capital goes only to members beating buy-and-hold live.

Weekly re-allocation on thin live data is a noise-chasing machine unless the
bar is explicit. A member needs 20 daily samples, at least one entry, a total
return above the benchmark's over the same days, and a t-statistic of daily
excess returns above 1.0. Weight follows that t (the edge's reliability, not
its size), capped at 60%; whatever nobody earned holds the benchmark.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

MIN_SAMPLES = 20
MIN_T = 1.0
MAX_WEIGHT = 0.60
HYSTERESIS = 0.10


@dataclass(frozen=True)
class MemberRecord:
    name: str
    samples: list[tuple[str, float]]
    entries: int


@dataclass
class Allocation:
    weights: dict[str, float]
    changed: bool
    reasons: dict[str, str] = field(default_factory=dict)
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)


def allocate(
    records: list[MemberRecord], *, benchmark: str, previous: dict[str, float]
) -> Allocation:
    bench = next((record for record in records if record.name == benchmark), None)
    bench_equity = dict(bench.samples) if bench else {}
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    stats: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.name == benchmark:
            continue
        mine = dict(record.samples)
        days = sorted(day for day in set(mine) & set(bench_equity) if mine[day] > 0 and bench_equity[day] > 0)
        stats[record.name] = {"samples": len(days), "entries": record.entries}
        if len(days) < MIN_SAMPLES:
            reasons[record.name] = f"{len(days)} daily samples (needs {MIN_SAMPLES})"
            continue
        if record.entries < 1:
            reasons[record.name] = "no entries yet"
            continue
        member_total = mine[days[-1]] / mine[days[0]] - 1
        bench_total = bench_equity[days[-1]] / bench_equity[days[0]] - 1
        stats[record.name]["excess_pct"] = round((member_total - bench_total) * 100, 3)
        if member_total <= bench_total:
            reasons[record.name] = (
                f"trails the benchmark by {(bench_total - member_total) * 100:.2f} points"
            )
            continue
        excess = [
            (mine[b] / mine[a] - 1) - (bench_equity[b] / bench_equity[a] - 1)
            for a, b in zip(days, days[1:])
        ]
        spread = statistics.stdev(excess) if len(excess) > 1 else 0.0
        if spread <= 0:
            reasons[record.name] = "excess returns never vary (t undefined)"
            continue
        t = statistics.mean(excess) / (spread / math.sqrt(len(excess)))
        stats[record.name]["t"] = round(t, 3)
        if t <= MIN_T:
            reasons[record.name] = f"t={t:.2f} is not above {MIN_T:.1f}"
            continue
        scores[record.name] = t
        reasons[record.name] = f"qualifies: t={t:.2f}"

    total = sum(scores.values())
    weights = {name: min(MAX_WEIGHT, t / total) for name, t in scores.items()} if total > 0 else {}
    weights[benchmark] = max(0.0, 1.0 - sum(weights.values()))
    for record in records:
        weights.setdefault(record.name, 0.0)
    weights = {name: round(value, 6) for name, value in weights.items()}

    if previous:
        keys = set(weights) | set(previous)
        moved = max(abs(weights.get(k, 0.0) - previous.get(k, 0.0)) for k in keys)
        if moved < HYSTERESIS:
            return Allocation(dict(previous), False, reasons, stats)
    return Allocation(weights, weights != previous, reasons, stats)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_desk_allocator.py`
Expected: 7 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/agentic_trading/desk/allocator.py tests/test_desk_allocator.py
git add src/agentic_trading/desk/allocator.py tests/test_desk_allocator.py
git commit -m "feat(desk): weekly allocator gated on live excess return over the benchmark"
```

---
### Task 6: Netting — allocations into quantity targets and gap orders

**Files:**
- Create: `src/agentic_trading/desk/netting.py`
- Test: `tests/test_desk_netting.py`

**Interfaces:**
- Consumes: `broker_symbol` from `desk/member.py` (Task 4).
- Produces: `MIN_USD = Decimal("1.00")`, `GAP_PCT = Decimal("0.05")`; `target_quantities(allocations: dict[str, float], member_weights: dict[str, dict[str, float]], account_equity: Decimal, prices: dict[str, Decimal]) -> tuple[dict[str, Decimal], set[str]]` (targets and the symbols that could not be priced); `gap_intent(symbol: str, *, target: Decimal, held: Decimal, quote: dict, created_at: datetime) -> OrderIntent | None`. Targets are **quantities**, so price drift never creates a gap.

- [ ] **Step 1: Write the failing tests**

```python
"""Netting: one order per symbol for the whole desk, only when the gap matters."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agentic_trading.desk.netting import gap_intent, target_quantities
from agentic_trading.types import Side

D = Decimal
NOW = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)


def quote(symbol: str, bid: str, ask: str, session: str = "regular") -> dict:
    return {"symbol": symbol, "bid": D(bid), "ask": D(ask), "market_session": session}


class TargetTests(unittest.TestCase):
    def test_two_members_wanting_the_same_symbol_make_one_target(self) -> None:
        targets, unpriced = target_quantities(
            {"a": 0.5, "b": 0.5, "benchmark": 0.0},
            {"a": {"MSFT": 0.4}, "b": {"MSFT": 0.2, "BTC-USD": 0.5}},
            D("100"),
            {"MSFT": D("10"), "BTC-USD": D("50")},
        )
        # MSFT: (0.5*0.4 + 0.5*0.2) * 100 = $30 -> 3 shares; BTC: 0.5*0.5*100 = $25 -> 0.5
        self.assertEqual(targets, {"MSFT": D("3.000000"), "BTC-USD": D("0.500000")})
        self.assertEqual(unpriced, set())

    def test_a_symbol_without_a_price_is_reported_not_zeroed(self) -> None:
        targets, unpriced = target_quantities(
            {"a": 1.0}, {"a": {"MSFT": 0.5}}, D("100"), {}
        )
        self.assertEqual(targets, {})
        self.assertEqual(unpriced, {"MSFT"})

    def test_bar_keyed_member_symbols_are_netted_in_broker_form(self) -> None:
        targets, _ = target_quantities(
            {"a": 1.0}, {"a": {"BTCUSD": 0.5}}, D("100"), {"BTC-USD": D("50")}
        )
        self.assertEqual(targets, {"BTC-USD": D("1.000000")})


class GapTests(unittest.TestCase):
    def test_a_material_shortfall_buys_the_gap_at_the_ask(self) -> None:
        intent = gap_intent(
            "MSFT", target=D("3"), held=D("1"), quote=quote("MSFT", "9.9", "10"), created_at=NOW
        )
        assert intent is not None
        self.assertEqual((intent.side, intent.quantity, intent.ref_price), (Side.BUY, D("2.000000"), D("10")))
        self.assertEqual(intent.reason, "desk_rebalance")

    def test_a_gap_under_one_dollar_or_five_percent_is_left_alone(self) -> None:
        # $0.90 gap on a $30 target: under both $1 and 5% ($1.50).
        self.assertIsNone(
            gap_intent("MSFT", target=D("3"), held=D("2.91"), quote=quote("MSFT", "10", "10"), created_at=NOW)
        )
        # $1.20 gap on a $100 target: over $1 but under 5% ($5).
        self.assertIsNone(
            gap_intent("MSFT", target=D("10"), held=D("9.88"), quote=quote("MSFT", "10", "10"), created_at=NOW)
        )

    def test_a_zero_target_sells_everything_even_dust(self) -> None:
        intent = gap_intent(
            "SOL-USD", target=D("0"), held=D("0.001"), quote=quote("SOL-USD", "200", "201"), created_at=NOW
        )
        assert intent is not None
        self.assertEqual((intent.side, intent.quantity, intent.ref_price), (Side.SELL, D("0.001"), D("200")))

    def test_an_excess_sells_only_the_gap(self) -> None:
        intent = gap_intent(
            "MSFT", target=D("1"), held=D("3"), quote=quote("MSFT", "10", "10.1"), created_at=NOW
        )
        assert intent is not None
        self.assertEqual((intent.side, intent.quantity), (Side.SELL, D("2.000000")))

    def test_equities_act_only_in_the_regular_session(self) -> None:
        self.assertIsNone(
            gap_intent("MSFT", target=D("3"), held=D("0"), quote=quote("MSFT", "10", "10", "extended"), created_at=NOW)
        )
        self.assertIsNotNone(
            gap_intent("BTC-USD", target=D("1"), held=D("0"), quote=quote("BTC-USD", "10", "10", "extended"), created_at=NOW)
        )

    def test_no_gap_no_intent(self) -> None:
        self.assertIsNone(
            gap_intent("MSFT", target=D("2"), held=D("2"), quote=quote("MSFT", "10", "10"), created_at=NOW)
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_desk_netting.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentic_trading.desk.netting'`

- [ ] **Step 3: Implement `desk/netting.py`**

```python
"""Netting: the desk's allocations become one quantity target per symbol.

Targets are quantities, fixed when an event (allocation change, member fill,
seeding) recomputes them. Holding a quantity through price drift is the whole
point: a dollar target recomputed on every quote would pay a rebalance charge
each time a crypto price moved 5%.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from agentic_trading.desk.member import broker_symbol
from agentic_trading.orders import is_crypto_symbol
from agentic_trading.types import OrderIntent, Side, new_decision_id

MIN_USD = Decimal("1.00")
GAP_PCT = Decimal("0.05")
STEP = Decimal("0.000001")
ZERO = Decimal("0")


def target_quantities(
    allocations: dict[str, float],
    member_weights: dict[str, dict[str, float]],
    account_equity: Decimal,
    prices: dict[str, Decimal],
) -> tuple[dict[str, Decimal], set[str]]:
    dollars: dict[str, Decimal] = {}
    for member, allocation in allocations.items():
        if allocation <= 0:
            continue
        for symbol, weight in member_weights.get(member, {}).items():
            key = broker_symbol(symbol)
            dollars[key] = dollars.get(key, ZERO) + (
                Decimal(str(allocation)) * Decimal(str(weight)) * account_equity
            )
    targets: dict[str, Decimal] = {}
    unpriced: set[str] = set()
    for symbol, usd in dollars.items():
        price = prices.get(symbol)
        if price is None or price <= 0:
            unpriced.add(symbol)
            continue
        targets[symbol] = (usd / price).quantize(STEP, rounding=ROUND_DOWN)
    return targets, unpriced


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def gap_intent(
    symbol: str,
    *,
    target: Decimal,
    held: Decimal,
    quote: dict[str, Any],
    created_at: datetime,
) -> Optional[OrderIntent]:
    symbol = broker_symbol(symbol)
    if not is_crypto_symbol(symbol) and quote.get("market_session") != "regular":
        return None
    gap = target - held
    if gap == 0:
        return None
    price = _decimal(quote.get("ask") if gap > 0 else quote.get("bid"))
    if price is None:
        return None
    selling_out = target <= 0 and held > 0
    if not selling_out:
        threshold = max(MIN_USD, GAP_PCT * target * price)
        if abs(gap) * price <= threshold:
            return None
    if gap > 0:
        side, quantity = Side.BUY, gap.quantize(STEP, rounding=ROUND_DOWN)
    else:
        side = Side.SELL
        quantity = held if selling_out else min(-gap, held).quantize(STEP, rounding=ROUND_DOWN)
    if quantity <= 0:
        return None
    return OrderIntent(
        decision_id=new_decision_id(),
        symbol=symbol,
        side=side,
        reason="desk_rebalance",
        created_at=created_at,
        quantity=quantity,
        ref_price=price,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_desk_netting.py`
Expected: 9 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/agentic_trading/desk/netting.py tests/test_desk_netting.py
git add src/agentic_trading/desk/netting.py tests/test_desk_netting.py
git commit -m "feat(desk): net member targets into one quantity target per symbol"
```

---
### Task 7: StrategyDesk — the runtime-facing strategy

**Files:**
- Create: `src/agentic_trading/desk/desk.py`
- Test: `tests/test_desk.py`

**Interfaces:**
- Consumes: `MemberBook` (Task 2), `BenchmarkStrategy` (Task 3, tests only), `Member` and `broker_symbol` (Task 4), `MemberRecord` and `allocate` (Task 5), `target_quantities` and `gap_intent` (Task 6), `walkforward.rotation_anchor`.
- Produces: `StrategyDesk(*, members: list[Member], account: MemberBook, costs: Callable[[], Any], journal: Callable[[dict], None], state_path: Path, benchmark: str = "benchmark", retry_seconds: float = 900.0, monotonic: Callable[[], float] = time.monotonic)`, with the runtime strategy interface: `on_quote(quote) -> list[OrderIntent]`, `note_fill(symbol, quantity) -> None`, `seed_positions(positions) -> int`, `release_decision(day) -> bool` (False), `reload_history() -> int`, `last_decided_day = ""`. Also exposes `allocations: dict[str, float]`, `allocation_week: str` and `targets: dict[str, Decimal]`. Journal events: `member_fill`, `desk_member_failed`, `desk_allocation`, `desk_targets`.

- [ ] **Step 1: Write the failing tests**

```python
"""The desk end to end: members, allocation, netting, throttling, restarts."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.backtest import CostModel
from agentic_trading.desk.benchmark import BenchmarkStrategy
from agentic_trading.desk.book import MemberBook
from agentic_trading.desk.desk import StrategyDesk
from agentic_trading.desk.member import Member
from agentic_trading.types import Side

D = Decimal
FREE = CostModel(D("0"), D("0"), D("0"))
THU = "2026-09-24T14:00:00Z"
FRI = "2026-09-25T14:00:00Z"
MON = "2026-09-28T14:00:00Z"


def quote(symbol: str, bid: str, ask: str, at: str = THU, session: str = "regular") -> dict:
    return {
        "symbol": symbol,
        "bid": D(bid),
        "ask": D(ask),
        "observed_at": at,
        "market_session": session,
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Broken:
    def on_quote(self, quote: dict) -> list:
        raise RuntimeError("boom")


def build(tmp: Path, *, extra: list[tuple[str, object]] = (), clock=None, events=None):
    desk_dir = tmp / "desk"
    members = []
    for name, strategy in [("benchmark", BenchmarkStrategy()), *extra]:
        book, _ = MemberBook.load(desk_dir / f"{name}.json", name=name, starting_equity=D("50"))
        members.append(Member(name, strategy, book, order_pct=D("0.19")))
    account, _ = MemberBook.load(desk_dir / "account.json", name="account", starting_equity=D("50"))
    log = events if events is not None else []
    desk = StrategyDesk(
        members=members,
        account=account,
        costs=lambda: FREE,
        journal=log.append,
        state_path=desk_dir / "desk.json",
        retry_seconds=900.0,
        monotonic=clock or _Clock(),
    )
    return desk, log


class DeskTests(unittest.TestCase):
    def test_the_benchmark_allocation_becomes_one_account_order(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, log = build(Path(name))
            [intent] = desk.on_quote(quote("BTC-USD", "99", "100"))
        self.assertEqual((intent.symbol, intent.side), ("BTC-USD", Side.BUY))
        # The benchmark member put 40% of $50 into BTC; the account follows 100%.
        # 40% of the member book, copied at 100%: ~0.2 BTC (0.2004 after marks).
        self.assertAlmostEqual(float(intent.quantity), 0.2, delta=0.002)
        kinds = [event["event"] for event in log]
        self.assertIn("member_fill", kinds)
        self.assertIn("desk_allocation", kinds)
        self.assertEqual(desk.allocations["benchmark"], 1.0)

    def test_a_filled_gap_and_later_price_drift_emit_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            [intent] = desk.on_quote(quote("BTC-USD", "99", "100"))
            desk.note_fill(intent.symbol, intent.quantity)
            self.assertEqual(desk.on_quote(quote("BTC-USD", "99", "100")), [])
            self.assertEqual(desk.on_quote(quote("BTC-USD", "149", "150")), [])

    def test_an_unfilled_gap_is_retried_only_after_the_retry_window(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            clock = _Clock()
            desk, _ = build(Path(name), clock=clock)
            self.assertEqual(len(desk.on_quote(quote("BTC-USD", "99", "100"))), 1)
            clock.now = 60.0
            self.assertEqual(desk.on_quote(quote("BTC-USD", "99", "100")), [])
            clock.now = 901.0
            self.assertEqual(len(desk.on_quote(quote("BTC-USD", "99", "100"))), 1)

    def test_a_partial_fill_lets_the_rest_go_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            [first] = desk.on_quote(quote("BTC-USD", "99", "100"))
            desk.note_fill("BTC-USD", first.quantity / 2)
            [rest] = desk.on_quote(quote("BTC-USD", "99", "100"))
        self.assertAlmostEqual(float(rest.quantity), float(first.quantity) / 2, places=5)

    def test_equities_wait_for_the_regular_session(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            self.assertEqual(desk.on_quote(quote("QQQ", "500", "500", session="extended")), [])
            [intent] = desk.on_quote(quote("QQQ", "500", "500"))
        self.assertEqual(intent.symbol, "QQQ")

    def test_holdings_seeded_before_any_price_are_sold_only_once_priced(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            self.assertEqual(desk.seed_positions({"SOL-USD": "0.05"}), 1)
            # No SOL price yet: account value unknown, nothing may be emitted.
            self.assertEqual(desk.on_quote(quote("BTC-USD", "99", "100")), [])
            [sell] = desk.on_quote(quote("SOL-USD", "200", "201"))
        self.assertEqual((sell.side, sell.quantity), (Side.SELL, D("0.05")))

    def test_allocation_runs_once_a_week_and_survives_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            events: list[dict] = []
            desk, _ = build(tmp, events=events)
            desk.on_quote(quote("BTC-USD", "99", "100", at=THU))
            restarted, _ = build(tmp, events=events)
            self.assertEqual(restarted.allocation_week, "2026-09-21")
            restarted.on_quote(quote("BTC-USD", "99", "100", at=FRI))
            restarted.on_quote(quote("BTC-USD", "99", "100", at=MON))
        weeks = [e["week"] for e in events if e["event"] == "desk_allocation"]
        self.assertEqual(weeks, ["2026-09-21", "2026-09-28"])

    def test_a_failing_member_loses_its_capital_and_the_desk_continues(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "desk").mkdir()
            (tmp / "desk" / "desk.json").write_text(
                json.dumps(
                    {
                        "allocations": {"benchmark": 0.4, "broken": 0.6},
                        "allocation_week": "2026-09-21",
                        "targets": {},
                    }
                ),
                encoding="utf-8",
            )
            desk, log = build(tmp, extra=[("broken", _Broken())])
            intents = desk.on_quote(quote("BTC-USD", "99", "100"))
        self.assertEqual(desk.allocations, {"benchmark": 1.0, "broken": 0.0})
        self.assertIn("desk_member_failed", [e["event"] for e in log])
        self.assertEqual(len(intents), 1)  # the benchmark still trades
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_desk.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentic_trading.desk.desk'`

- [ ] **Step 3: Implement `desk/desk.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_desk.py`
Expected: 8 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/agentic_trading/desk/desk.py tests/test_desk.py
git add src/agentic_trading/desk/desk.py tests/test_desk.py
git commit -m "feat(desk): strategy desk netting member books into account orders"
```

---
### Task 8: Wiring — config, factory, evidence rule, trial handover

**Files:**
- Modify: `src/agentic_trading/config.py` (allow `strategy = "desk"`; add `desk_members`, `desk_member_order_pct`; validation)
- Modify: `src/agentic_trading/cli.py` (`build_strategy` gains a `"desk"` branch; new `build_desk`)
- Modify: `src/agentic_trading/walkforward.py` (`STRATEGY_RULES["desk"] = "none"`; `rank_targets(rule="none")` returns `[]`)
- Modify: `src/agentic_trading/trial.py` (split out `_replay`; add `_member_book`, `seed_member_book`; `score_trial` reads the desk member book when the desk runs)
- Test: `tests/test_desk_wiring.py`

**Interfaces:**
- Consumes: everything from Tasks 2–7; `cli.build_strategy(config, name)` for the member strategies; `evidence._current_equity(state_dir) -> Decimal`; `execution.cost_model_for(state_dir)`; `journal.DecisionJournal(path).append`.
- Produces: `Config.desk_members: tuple[str, ...] = ("momentum_rotation", "trend_crypto", "benchmark")`, `Config.desk_member_order_pct: Decimal = Decimal("0.19")`, `DESK_MEMBER_NAMES = ("momentum_rotation", "trend_crypto", "benchmark")` in `config.py`; `cli.build_desk(config) -> StrategyDesk`; `trial.seed_member_book(config, book) -> bool`. Desk state lives under `<state_dir>/desk/`: `<member>.json`, `<member>_strategy.json`, `account.json`, `desk.json`.

- [ ] **Step 1: Write the failing tests**

```python
"""The desk wired into config, the CLI factory, evidence and the trial."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.config import load_config
from tests.test_runtime_daemon import _write_config


def _config(tmp: Path, *extra: str):
    return load_config(
        _write_config(
            tmp,
            extra=['strategy = "desk"', f'history_path = "{tmp / "bars"}"', *extra],
        )
    )


class DeskConfigTests(unittest.TestCase):
    def test_the_desk_loads_with_its_default_members(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name))
        self.assertEqual(config.strategy, "desk")
        self.assertEqual(
            config.desk_members, ("momentum_rotation", "trend_crypto", "benchmark")
        )
        self.assertEqual(config.desk_member_order_pct, Decimal("0.19"))

    def test_unknown_members_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(ValueError, "desk_members"):
                _config(Path(name), 'desk_members = ["benchmark", "moonshot"]')

    def test_the_benchmark_member_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(ValueError, "benchmark"):
                _config(Path(name), 'desk_members = ["momentum_rotation"]')


class DeskFactoryTests(unittest.TestCase):
    def test_build_strategy_returns_a_desk_with_its_members(self) -> None:
        from agentic_trading.cli import build_strategy
        from agentic_trading.desk.desk import StrategyDesk

        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name))
            desk = build_strategy(config)
            self.assertIsInstance(desk, StrategyDesk)
            self.assertEqual(
                [member.name for member in desk.members],
                ["momentum_rotation", "trend_crypto", "benchmark"],
            )
            # Member strategies save their own state under the desk directory.
            rotation = desk.members[0].strategy
            self.assertEqual(
                rotation.state_path,
                Path(config.state_dir) / "desk" / "momentum_rotation_strategy.json",
            )

    def test_the_evidence_engine_grades_no_rule_for_the_desk(self) -> None:
        from agentic_trading.walkforward import rank_targets, rule_for_strategy

        self.assertEqual(rule_for_strategy("desk"), "none")
        self.assertEqual(rank_targets({}, datetime.now(timezone.utc), rule="none"), [])


class TrialHandoverTests(unittest.TestCase):
    def test_the_running_trial_becomes_the_rotation_members_book(self) -> None:
        from agentic_trading import trial
        from agentic_trading.cli import build_strategy

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp)
            started = datetime(2026, 9, 23, 23, 20, tzinfo=timezone.utc)
            Path(config.state_dir).mkdir(parents=True, exist_ok=True)
            trial.start_trial(config, starting_equity=Decimal("49.9"), now=started)
            # In production the trial was started under the rotation, before
            # the desk existed; start_trial records the config's strategy.
            trial_path = Path(config.state_dir) / "trial.json"
            payload = json.loads(trial_path.read_text(encoding="utf-8"))
            payload["strategy"] = "momentum_rotation"
            trial_path.write_text(json.dumps(payload), encoding="utf-8")
            journal = Path(config.journal_dir)
            journal.mkdir(parents=True)
            record = {
                "event": "accepted",
                "mode": "shadow",
                "at": (started + timedelta(minutes=3)).isoformat(),
                "intent": {
                    "symbol": "SOL-USD",
                    "side": "buy",
                    "quantity": "0.05",
                    "ref_price": "190",
                },
            }
            (journal / "2026-09-23.jsonl").write_text(
                json.dumps(record) + "\n", encoding="utf-8"
            )

            desk = build_strategy(config)
            book = desk.members[0].book
            self.assertEqual(book.positions, {"SOL-USD": Decimal("0.05")})
            self.assertEqual(book.starting_equity, Decimal("49.9"))
            self.assertEqual(book.entries, 1)

            scored = trial.score_trial(config, now=started + timedelta(days=1))
        self.assertEqual(scored["entries"], 1)
        self.assertEqual([p["symbol"] for p in scored["positions"]], ["SOL-USD"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_desk_wiring.py`
Expected: FAIL (`ValueError: strategy must be ...` for the desk config)

- [ ] **Step 3: Config**

In `src/agentic_trading/config.py`:

1. Above the `Config` dataclass, add:

```python
DESK_MEMBER_NAMES = ("momentum_rotation", "trend_crypto", "benchmark")
```

2. Next to the `tape_dir` field (Task 1), add:

```python
    # The strategy desk: which strategies compete, and each member's per-order
    # share of its own paper book (the rotation was tested at 19%).
    desk_members: tuple[str, ...] = DESK_MEMBER_NAMES
    desk_member_order_pct: Decimal = Decimal("0.19")
```

3. In `__post_init__`, add `"desk"` to the allowed strategies tuple and message:

```python
        if self.strategy not in (
            "fixture",
            "spy_scalper",
            "llm",
            "trend_crypto",
            "momentum_rotation",
            "desk",
        ):
            raise ValueError(
                "strategy must be "
                "fixture|spy_scalper|llm|trend_crypto|momentum_rotation|desk"
            )
        unknown = [m for m in self.desk_members if m not in DESK_MEMBER_NAMES]
        if unknown:
            raise ValueError(
                f"desk_members has unknown members {unknown}; "
                f"choose from {list(DESK_MEMBER_NAMES)}"
            )
        if "benchmark" not in self.desk_members:
            raise ValueError("desk_members must include benchmark (the fallback)")
        if not (Decimal("0") < self.desk_member_order_pct <= Decimal("1")):
            raise ValueError("desk_member_order_pct must be in (0, 1]")
```

4. In `load_config`, next to `tape_dir=...`, add:

```python
        desk_members=tuple(
            str(name) for name in (raw.get("desk_members") or DESK_MEMBER_NAMES)
        ),
        desk_member_order_pct=Decimal(str(raw.get("desk_member_order_pct", "0.19"))),
```

- [ ] **Step 4: Evidence rule**

In `src/agentic_trading/walkforward.py`, change `STRATEGY_RULES` to:

```python
# Which ranking each strategy trades, so the evidence gate and the console
# grade the rule the daemon actually runs. The desk has no single rule: its
# members are judged by their live books, so the walk-forward grades nothing
# (zero trades keeps the promotion gate shut).
STRATEGY_RULES = {
    "trend_crypto": "trend",
    "momentum_rotation": "rotation",
    "desk": "none",
}
```

and at the top of `rank_targets`' body, before `if rule == "rotation":`, add:

```python
    if rule == "none":
        return []
```

- [ ] **Step 5: Factory**

In `src/agentic_trading/cli.py`, at the start of `build_strategy`'s body (right after `name = ...`), add:

```python
    if name == "desk":
        return build_desk(config)
```

and directly after the `build_strategy` function add:

```python
def build_desk(config: Config) -> Any:
    """The strategy desk, with every member's book and state under state/desk."""
    from agentic_trading import trial
    from agentic_trading.desk.benchmark import BenchmarkStrategy
    from agentic_trading.desk.book import MemberBook
    from agentic_trading.desk.desk import StrategyDesk
    from agentic_trading.desk.member import Member
    from agentic_trading.evidence import _current_equity
    from agentic_trading.execution import cost_model_for
    from agentic_trading.journal import DecisionJournal

    desk_dir = Path(config.state_dir) / "desk"
    journal = DecisionJournal(Path(config.journal_dir))
    equity = _current_equity(config.state_dir)
    members = []
    for name in config.desk_members:
        if name == "benchmark":
            strategy: Any = BenchmarkStrategy()
        else:
            strategy = build_strategy(config, name)
            strategy.state_path = desk_dir / f"{name}_strategy.json"
        book, reset = MemberBook.load(
            desk_dir / f"{name}.json", name=name, starting_equity=equity
        )
        if reset:
            journal.append({"event": "desk_member_reset", "member": name})
        if book.is_new and trial.seed_member_book(config, book):
            journal.append({"event": "desk_trial_adopted", "member": name})
        members.append(
            Member(name, strategy, book, order_pct=config.desk_member_order_pct)
        )
    account, _ = MemberBook.load(
        desk_dir / "account.json", name="account", starting_equity=equity
    )
    return StrategyDesk(
        members=members,
        account=account,
        costs=lambda: cost_model_for(config.state_dir),
        journal=journal.append,
        state_path=desk_dir / "desk.json",
        retry_seconds=config.rebalance_retry_seconds,
    )
```

(If `Any` is not yet imported in `cli.py`, add it to the existing `from typing import ...` line.)

- [ ] **Step 6: Trial handover**

In `src/agentic_trading/trial.py`, replace everything from the line `def score_trial(config: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:` down to, but **not including**, the line `    benchmark_return = 0.0` with:

```python
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
        lowest_cash = member.cash
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

```

The remainder of `score_trial` (from `benchmark_return = 0.0` to its `return {...}`) stays unchanged. It already uses `started`, `current`, `starting`, `book`, `cash`, `lowest_cash`, `entries`, `exits` and `positions`.

- [ ] **Step 7: Run the new and existing trial tests**

Run: `.venv/bin/python -m pytest -q tests/test_desk_wiring.py tests/test_momentum_rotation.py`
Expected: all pass (6 new + the existing trial test unchanged)

- [ ] **Step 8: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest -q
ruff check src/agentic_trading/config.py src/agentic_trading/cli.py src/agentic_trading/walkforward.py src/agentic_trading/trial.py tests/test_desk_wiring.py
git add src/agentic_trading/config.py src/agentic_trading/cli.py src/agentic_trading/walkforward.py src/agentic_trading/trial.py tests/test_desk_wiring.py
git commit -m "feat(desk): wire the desk into config, the CLI factory and the trial"
```

---
### Task 9: Launch the desk in shadow and verify it live

**Files:**
- Modify: `config/agentic.toml` (local, gitignored: never commit it)

**Interfaces:**
- Consumes: the whole desk (Tasks 1–8); systemd user units `agentic-trading.service` and `agentic-trading-dashboard.service`.

- [ ] **Step 1: Back up and edit the config**

```bash
cp config/agentic.toml config/agentic.toml.bak-desk-launch
```

Set these keys in `config/agentic.toml` (edit the existing lines; add the missing ones next to `shadow_full_size`):

```toml
strategy = "desk"
desk_members = ["momentum_rotation", "trend_crypto", "benchmark"]
desk_member_order_pct = "0.19"   # each member's per-order share of its own book
max_order_pct = "0.60"           # the account's per-order cap: one benchmark leg is 60%
max_order_hard_pct = "0.60"
max_orders_per_day = 20          # launch day nets several legs at once
max_open_positions = 8
tape_enabled = true
```

Keep `mode = "shadow"`, `autonomy = "assisted"`, `auto_arm = false` and `shadow_full_size = true` exactly as they are.

- [ ] **Step 2: Check the config loads and the desk builds**

Run:
```bash
.venv/bin/python -c "
from agentic_trading.config import load_config
from agentic_trading.cli import build_strategy
c = load_config('config/agentic.toml')
d = build_strategy(c)
print(c.strategy, [m.name for m in d.members], d.allocations)
print({m.name: (str(m.book.equity), dict(m.book.positions)) for m in d.members})
"
```
Expected: `desk ['momentum_rotation', 'trend_crypto', 'benchmark'] {'momentum_rotation': 0.0, 'trend_crypto': 0.0, 'benchmark': 1.0}`. The rotation member's positions equal the running trial's holdings (from `agentic-trading trial --config config/agentic.toml`).

- [ ] **Step 3: Restart and confirm health**

```bash
systemctl --user restart agentic-trading.service agentic-trading-dashboard.service
systemctl --user is-active agentic-trading.service agentic-trading-dashboard.service
```
Expected: `active` twice.

- [ ] **Step 4: Verify desk events and the tape**

Wait for a few quote cycles in the background (`until grep -q '"event":"desk_allocation"' data/journal/$(date -u +%F).jsonl; do sleep 5; done`), then run:

```bash
python3 - <<'EOF'
import json, datetime
day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
for line in open(f"data/journal/{day}.jsonl"):
    r = json.loads(line)
    if r.get("event") in ("desk_trial_adopted", "desk_allocation", "desk_targets",
                          "member_fill", "desk_member_failed", "tape_write_failed",
                          "accepted", "rejected", "kill_switch"):
        print(r.get("at", "")[11:19], r["event"], json.dumps(r)[:200])
EOF
ls data/tape/*/ | head
grep -o '"kill_switch": [a-z]*' data/state/risk_guard.json
```
Expected:
- `desk_trial_adopted` for `momentum_rotation` (first start only), one `desk_allocation` for this week with the benchmark at 1.0, `member_fill` events for the benchmark's first BTC buy, and `desk_targets`.
- Account-level `accepted` desk_rebalance orders (BTC now; QQQ at the next regular session), and a sell of any rotation-trial holdings in the account (the benchmark holds 100% until a member qualifies).
- No `desk_member_failed`, no `tape_write_failed`, `"kill_switch": false`.
- Tape files exist under `data/tape/<SYMBOL>/<today>.jsonl.gz`.

- [ ] **Step 5: Verify the trial still scores**

Run: `.venv/bin/agentic-trading trial --config config/agentic.toml`
Expected: same trial dates as before (ends 2026-10-23), the book now read from the rotation member (`positions` = the member's holdings) and no crash.

- [ ] **Step 6: Record the launch in memory**

Update `/home/doczeus/.claude/projects/-home-doczeus-Projects-Agnetic-TraDING/memory/project_strategy_desk.md`: the desk is live in shadow as of the launch date; rollback = restore `config/agentic.toml.bak-desk-launch` and restart; the first possible capital allocation is the first Monday after a member has 20 daily samples.

---

## Self-review checklist (run by the plan author)

- Spec §2 units → Tasks 1 (tape), 2 (book), 4 (member), 5 (allocator), 6 (netting), 7 (desk); the benchmark member → Task 3; config/factory/evidence/trial → Task 8; rollout §5 → Task 9.
- Spec §3.2 thresholds → Task 5 constants and tests (19 vs 20 samples, no entries, trails, t ≤ 1, 60% cap, hysteresis).
- Spec §3.3 event-only targets, max($1, 5%), sell-all at zero, regular-session equities, re-emission → Tasks 6 and 7.
- Spec §4 failure table → member failure (Tasks 4, 7), missing quote (Task 6 `unpriced`, Task 7 cash_pending), corrupt state (Task 2 reset, Task 8 `desk_member_reset`), benchmark fallback (Task 5), tape errors (Task 1).
- **Executed before handoff (2026-09-23):** every code block in Tasks 1–8 was applied to a scratch copy of the repo exactly as written. Result: 893 passed, 4 skipped, lint clean. The only failure was `test_windows_packaging` bootstrap, which needs `data/bars` (absent from the scratch copy; passes in the real repo). Dry-running caught one plan bug, fixed before handoff: the trial-handover test must record the trial under `momentum_rotation`, as production does.

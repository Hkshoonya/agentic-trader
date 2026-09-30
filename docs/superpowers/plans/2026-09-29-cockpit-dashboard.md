# Cockpit Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat operator console with a tabbed cockpit whose Overview shows plain-English story cards, an animated strategy race, animated tiles and a live ticker.

**Architecture:**
- **Server:** a new read-only module, `dashboard_desk.py`, builds one JSON view for `GET /api/desk` from the desk's member books, `desk.json`, the trial and a cached filter of journal events.
- **Charts:** a pure SVG helper module (`dashboard_charts_js.py`) that Node tests can run without a browser.
- **Cockpit:** a module (`dashboard_cockpit_js.py`) that renders the Overview and the Strategies member cards.
- **Page:** the existing page is split into four tabs. Every existing card keeps its element ids, so the existing script works unchanged.

**Tech Stack:** Python 3.12 stdlib `http.server`, inline JavaScript (ES2020) and SVG, CSS animations, `unittest`/pytest, Node (optional, for the chart tests), Playwright MCP for acceptance.

**Spec:** `docs/superpowers/specs/2026-09-29-cockpit-dashboard-design.md`

## Global Constraints

- The page loads no external resources: no `<script src>`, no `http(s)://` in `src`, `href` or `url()`, no `@import`. The CSP stays exactly `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'`.
- No chart library, no build step, no new Python or Node dependency.
- New JS/CSS modules are Python raw strings (`r"""..."""`), so `\b` and `\d` survive (`tests/test_console_language.py` forbids a literal backspace).
- `dashboard_js.SCRIPT` keeps every helper its tests pin (`const esc =`, `const perHundred =`, `const plainLuck =`, `const plainScore =`, `perHundred(...)` calls). Do not move or rename them.
- Every element id that `dashboard_js.SCRIPT` looks up must still exist in the page.
- Every dynamic value written into `innerHTML` goes through `esc(...)`.
- `/api/desk` never returns 500 for bad state; unreadable pieces degrade to `null` or `[]` plus a sentence.
- Refresh: existing endpoints every 2 s, `/api/desk` every 5 s, candidates every 30 s. Polling and `requestAnimationFrame` loops skip work while `document.hidden`.
- Commits: `git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit`. No co-author or AI mention. Never stage `data/bars/*` or `config/agentic.toml*`.
- After the suite grows, update the test count in `README.md` (line 6 badge, line 610) and `CONTRIBUTING.md` (line 33), then run `.venv/bin/python tools/check_doc_counts.py`.
- Run tests with `.venv/bin/python -m pytest`.

## Review Focus

1. **A member listed in `desk_members` with no book file yet** (e.g. a newly added `dip_reversal`) must appear with an empty race line and the reason "no paper book yet", not vanish or raise. Test: Task 1.
2. **A book sample with an unparseable equity** (`""`, `"NaN"`) must be skipped, not crash the endpoint or draw a line to zero. Test: Task 1.
3. **A journal line cut by a power loss** (null bytes, half a JSON object) must be skipped by the event cache. Test: Task 2.
4. **`/api/desk` failing while the page is open** (dashboard restarting) must keep the last picture and show the reconnecting dot. Test: Task 6 acceptance, with the route aborted in Playwright.
5. **Canvas cards on a hidden tab** (Orders' "Decisions per day") must draw at full width after switching to that tab, not at zero width. Test: Task 6 acceptance screenshot of the Orders tab.

---

### Task 1: Desk view: members, allocation and trial

**Files:**
- Create: `src/agentic_trading/dashboard_desk.py`
- Create: `tests/test_dashboard_desk.py`
- Modify: `docs/superpowers/specs/2026-09-29-cockpit-dashboard-design.md` (the non-desk fallback; see Step 6)

**Interfaces:**
- Consumes:
  - `agentic_trading.desk.book.MemberBook.load(path, *, name, starting_equity) -> (MemberBook, reset: bool)`, with `.starting_equity`, `.cash_pending`, `.equity`, `.positions`, `.prices`, `.samples: list[tuple[str, str]]`, `.entries`, `.exits`, `.is_new`
  - `agentic_trading.desk.allocator.allocate(records, *, benchmark, previous) -> Allocation(.weights, .reasons, .stats)`, `MemberRecord(name, samples, entries)`, `MIN_SAMPLES`, `min_t(members) -> float`
  - `agentic_trading.desk.benchmark.BENCHMARK_SHARES: dict[str, Decimal]`
  - `agentic_trading.trial.score_trial(config, *, now) -> dict`, `trial.TRIAL_DAYS`
- Produces:
  - `build_desk_view(config, events: list[dict], *, now: datetime | None = None) -> dict`, shaped as in the spec's JSON plus `"account": {"value": float | None, "series": [[day, value], ...]}`
  - `label(name: str) -> str`
  - `next_allocation(now: datetime) -> datetime`
  - `TICKER_SIZE = 30`
  - Task 2 adds `story`, `ticker_item` and `DeskEventCache` to the same module. In this task, `build_desk_view` returns `"story": {}` and `"ticker": []`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dashboard_desk.py`:

```python
"""/api/desk: the strategy desk as the cockpit shows it, read-only."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.config import load_config
from agentic_trading.dashboard_desk import build_desk_view, label, next_allocation
from agentic_trading.desk.book import MemberBook

NOW = datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)


def _config(tmp: Path, members: str = '["momentum_rotation", "benchmark"]', strategy: str = "desk"):
    from tests.test_runtime_daemon import _write_config

    bars = tmp / "bars"
    bars.mkdir(exist_ok=True)
    return load_config(
        _write_config(
            tmp,
            extra=[
                f'history_path = "{bars}"',
                f'strategy = "{strategy}"',
                f"desk_members = {members}",
            ],
        )
    )


def _book(desk: Path, name: str, *, samples, cash="0", positions=None, prices=None,
          entries=1, start="50", cash_pending=False) -> None:
    book = MemberBook(name, starting_equity=Decimal(start), path=desk / f"{name}.json")
    book.cash = Decimal(cash)
    book.positions = {s: Decimal(q) for s, q in (positions or {}).items()}
    book.prices = {s: Decimal(p) for s, p in (prices or {}).items()}
    book.samples = [(d, str(e)) for d, e in samples]
    book.entries = entries
    book.cash_pending = cash_pending
    book.save()


def _desk(tmp: Path) -> Path:
    config = _config(tmp)
    desk = Path(config.state_dir) / "desk"
    _book(desk, "momentum_rotation",
          samples=[("2026-09-25", "50.5"), ("2026-09-26", "51")],
          cash="1", positions={"AAPL": "0.2"}, prices={"AAPL": "250"}, entries=3)
    _book(desk, "benchmark",
          samples=[("2026-09-25", "50"), ("2026-09-26", "50.25")], cash="50.25")
    _book(desk, "account", samples=[("2026-09-25", "50"), ("2026-09-26", "50.1")],
          cash="50.1", entries=0)
    (desk / "desk.json").write_text(json.dumps({
        "allocations": {"momentum_rotation": 0.0, "benchmark": 1.0},
        "allocation_week": "2026-09-28",
    }))
    return tmp


class DeskViewTests(unittest.TestCase):
    def test_each_member_races_from_its_own_start(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            view = build_desk_view(config, [], now=NOW)
        self.assertTrue(view["enabled"])
        rot, bench = view["members"]
        self.assertEqual(rot["label"], "Momentum rotation")
        self.assertEqual(rot["series"], [["2026-09-25", 1.0], ["2026-09-26", 2.0]])
        self.assertEqual(rot["now_pct"], 2.0)
        self.assertEqual(rot["value"], 51.0)
        self.assertEqual(rot["holdings"], [{"symbol": "AAPL", "value": 50.0}])
        self.assertEqual(rot["entries"], 3)
        self.assertTrue(bench["is_benchmark"])
        self.assertEqual(bench["series"], [["2026-09-25", 0.0], ["2026-09-26", 0.5]])
        self.assertEqual(bench["now_pct"], 0.5)

    def test_qualification_comes_from_the_allocator_itself(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            rot = build_desk_view(config, [], now=NOW)["members"][0]
        self.assertEqual(rot["samples"], 2)
        self.assertEqual(rot["samples_needed"], 20)
        self.assertIsNone(rot["t"])
        self.assertEqual(rot["t_needed"], 1.0)
        self.assertEqual(rot["reason"], "2 daily samples (needs 20)")
        self.assertEqual(rot["weight"], 0.0)

    def test_allocation_carries_weights_legs_history_and_next_monday(self) -> None:
        events = [
            {"event": "desk_allocation", "week": "2026-09-21",
             "allocations": {"benchmark": 1.0}, "changed": False},
            {"event": "desk_allocation", "week": "2026-09-28",
             "allocations": {"benchmark": 1.0}, "changed": False},
        ]
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            allocation = build_desk_view(config, events, now=NOW)["allocation"]
        self.assertEqual(allocation["week"], "2026-09-28")
        self.assertEqual(allocation["weights"], {"momentum_rotation": 0.0, "benchmark": 1.0})
        self.assertEqual(allocation["legs"], {"QQQ": 0.6, "BTC-USD": 0.4})
        self.assertEqual([h["week"] for h in allocation["history"]], ["2026-09-21", "2026-09-28"])
        self.assertEqual(allocation["next_at"], "2026-10-05T00:00:00+00:00")

    def test_the_account_book_gives_the_account_tile(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            account = build_desk_view(config, [], now=NOW)["account"]
        self.assertEqual(account["value"], 50.1)
        self.assertEqual(account["series"], [["2026-09-25", 50.0], ["2026-09-26", 50.1]])

    def test_a_member_without_a_book_yet_is_shown_empty(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp, members='["momentum_rotation", "dip_reversal", "benchmark"]')
            view = build_desk_view(config, [], now=NOW)
        dip = next(m for m in view["members"] if m["name"] == "dip_reversal")
        self.assertEqual(dip["series"], [])
        self.assertIsNone(dip["now_pct"])
        self.assertEqual(dip["reason"], "no paper book yet")
        self.assertEqual(dip["t_needed"], 1.0)  # two non-benchmark members: unchanged bar

    def test_unparseable_samples_are_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp)
            _book(Path(config.state_dir) / "desk", "momentum_rotation",
                  samples=[("2026-09-25", "NaN"), ("2026-09-26", ""), ("2026-09-27", "51")],
                  cash="51")
            rot = build_desk_view(config, [], now=NOW)["members"][0]
        self.assertEqual(rot["series"], [["2026-09-27", 2.0]])

    def test_a_book_whose_cash_is_pending_has_no_live_point(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp)
            _book(Path(config.state_dir) / "desk", "momentum_rotation",
                  samples=[("2026-09-25", "50.5")], cash="0", cash_pending=True)
            rot = build_desk_view(config, [], now=NOW)["members"][0]
        self.assertIsNone(rot["now_pct"])
        self.assertIsNone(rot["value"])

    def test_corrupt_desk_state_degrades_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp)
            desk = Path(config.state_dir) / "desk"
            (desk / "desk.json").write_text("{not json")
            (desk / "momentum_rotation.json").write_text("\x00\x00")
            view = build_desk_view(config, [], now=NOW)
        self.assertEqual(view["allocation"]["weights"], {})
        self.assertEqual(view["members"][0]["reason"], "no paper book yet")

    def test_no_trial_means_a_null_trial(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            self.assertIsNone(build_desk_view(config, [], now=NOW)["trial"])

    def test_labels_and_the_next_monday(self) -> None:
        self.assertEqual(label("trend_crypto"), "Crypto trend")
        self.assertEqual(label("dip_reversal"), "Dip buyer")
        self.assertEqual(label("benchmark"), "Buy-and-hold")
        self.assertEqual(label("some_new_rule"), "Some new rule")
        monday = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(next_allocation(monday), datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(next_allocation(NOW), monday)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard_desk.py -q`

Expected: collection error, `ModuleNotFoundError: No module named 'agentic_trading.dashboard_desk'`.

- [ ] **Step 3: Implement `dashboard_desk.py`**

Create `src/agentic_trading/dashboard_desk.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard_desk.py -q`

Expected: `10 passed`.

If `test_a_member_without_a_book_yet_is_shown_empty` fails on `t_needed`: `min_t(2)` must be `1.0`, and `dip_reversal` must be accepted by config validation (it is in `DESK_MEMBER_CHOICES`).

- [ ] **Step 5: Run the dashboard and desk suites**

Run: `.venv/bin/python -m pytest tests/test_history_dashboard.py tests/test_console_language.py tests/test_desk_allocator.py -q`

Expected: all pass.

- [ ] **Step 6: Correct the spec's non-desk fallback**

No account-equity history exists for a non-desk strategy, so the spec's "race the account against the benchmark" cannot be built. In `docs/superpowers/specs/2026-09-29-cockpit-dashboard-design.md`, replace the bullet that starts `**Not the desk.**` with:

```markdown
- **Not the desk.** When `config.strategy != "desk"` the response is `{"enabled": false, "strategy": ...}` with empty members. The cockpit still shows the story cards and the trial. The race says it follows the strategy desk and points to the Orders tab (no account-equity history exists to race).
```

- [ ] **Step 7: Commit**

```bash
git add src/agentic_trading/dashboard_desk.py tests/test_dashboard_desk.py docs/superpowers/specs/2026-09-29-cockpit-dashboard-design.md
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): a read-only desk view for the cockpit"
```

### Task 2: Story sentences, ticker wording and the journal event cache

**Files:**
- Modify: `src/agentic_trading/dashboard_desk.py`
- Modify: `tests/test_dashboard_desk.py`

**Interfaces:**
- Consumes: Task 1's `build_desk_view`, `label`, `BENCHMARK`, `TICKER_SIZE`, `_float`, `_decimal`; `agentic_trading.types.DESK_ORDER_REASON`.
- Produces:
  - `ticker_item(record: dict) -> dict | None`, returning `{"at", "kind", "text"}` where kind is one of `fill`, `order`, `allocation`, `overruled` or `error`.
  - `story(members, allocation, account_value, ticker, *, symbols: int, now) -> dict` with keys `right_now`, `money`, `just_now`.
  - `DeskEventCache(journal_dir)` with `.journal_dir` and `.read(*, days=90) -> list[dict]` (oldest first).
  - `build_desk_view` now fills `"story"` and `"ticker"` (newest first, at most `TICKER_SIZE`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard_desk.py`, before `if __name__ == "__main__":`:

```python
from agentic_trading.dashboard_desk import DeskEventCache, story, ticker_item  # noqa: E402


def _member(name, now_pct, *, samples=3, bench=False):
    return {"name": name, "label": label(name), "is_benchmark": bench,
            "now_pct": now_pct, "samples": samples}


ALL_BENCH = {"weights": {"momentum_rotation": 0.0, "benchmark": 1.0}}


class TickerTests(unittest.TestCase):
    def test_each_event_kind_reads_as_a_sentence(self) -> None:
        cases = [
            ({"event": "member_fill", "member": "momentum_rotation", "side": "buy",
              "symbol": "AAPL", "quantity": "0.04", "price": "250"},
             "fill", "Momentum rotation bought AAPL $10.00 on paper"),
            ({"event": "accepted", "side": "sell", "symbol": "QQQ", "notional": "29.7",
              "mode": "shadow", "intent": {"reason": "desk_rebalance"}},
             "order", "Desk sold QQQ $29.70 for the account, on paper"),
            ({"event": "desk_allocation", "changed": True,
              "allocations": {"momentum_rotation": 0.4, "benchmark": 0.6}},
             "allocation", "New weekly allocation: Buy-and-hold 60%, Momentum rotation 40%"),
            ({"event": "desk_allocation", "changed": False, "allocations": {"benchmark": 1.0}},
             "allocation", "Weekly allocation checked: no change"),
            ({"event": "advisory_overruled", "layer": "llm", "symbol": "SOL-USD"},
             "overruled", "The AI veto objected to SOL-USD; the desk followed its evidence"),
            ({"event": "desk_member_failed", "member": "trend_crypto", "error": "boom"},
             "error", "Crypto trend hit an error and sits out today"),
            ({"event": "selfcheck", "healthy": False, "failures": [{"name": "data"}]},
             "error", "Health check found a problem in data"),
            ({"event": "kill_switch", "reason": "daily loss"},
             "error", "Kill switch engaged: trading stopped"),
        ]
        for record, kind, text in cases:
            with self.subTest(event=record["event"]):
                item = ticker_item({**record, "at": "2026-09-29T21:00:00+00:00"})
                self.assertEqual((item["kind"], item["text"]), (kind, text))
                self.assertEqual(item["at"], "2026-09-29T21:00:00+00:00")

    def test_ordinary_orders_and_healthy_checks_are_not_ticker_news(self) -> None:
        self.assertIsNone(ticker_item({"event": "accepted", "intent": {"reason": "trend_entry"}}))
        self.assertIsNone(ticker_item({"event": "selfcheck", "healthy": True}))
        self.assertIsNone(ticker_item({"event": "cycle_stats"}))

    def test_the_view_lists_the_newest_thirty_first(self) -> None:
        events = [
            {"event": "member_fill", "member": "benchmark", "side": "buy", "symbol": "QQQ",
             "quantity": "1", "price": str(i), "at": f"2026-09-29T{i // 60:02d}:{i % 60:02d}:00+00:00"}
            for i in range(1, 41)
        ]
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            ticker = build_desk_view(config, events, now=NOW)["ticker"]
        self.assertEqual(len(ticker), 30)
        self.assertIn("$40.00", ticker[0]["text"])


class StoryTests(unittest.TestCase):
    def _story(self, members, allocation=ALL_BENCH, account=49.45, ticker=(), now=NOW):
        return story(members, allocation, account, list(ticker), symbols=19, now=now)

    def test_right_now_names_the_leader_against_buy_and_hold(self) -> None:
        said = self._story([_member("momentum_rotation", 1.16), _member("trend_crypto", -0.96),
                            _member("benchmark", -0.8, bench=True)])
        self.assertEqual(said["right_now"], "Momentum rotation is beating buy-and-hold by +1.96 points.")

    def test_right_now_when_nobody_is_ahead(self) -> None:
        said = self._story([_member("momentum_rotation", -1.0), _member("benchmark", 0.5, bench=True)])
        self.assertEqual(
            said["right_now"],
            "No strategy is ahead of buy-and-hold yet; the closest is Momentum rotation (-1.50 points).",
        )

    def test_right_now_before_any_prices(self) -> None:
        said = self._story([_member("momentum_rotation", None), _member("benchmark", None, bench=True)])
        self.assertEqual(said["right_now"], "The race starts when every book has its first prices.")

    def test_money_while_nobody_has_earned_capital(self) -> None:
        said = self._story([_member("momentum_rotation", 1.0, samples=4),
                            _member("benchmark", 0.0, bench=True)])
        self.assertEqual(
            said["money"],
            "All $49.45 sits in buy-and-hold (60% QQQ, 40% BTC). No strategy has earned "
            "capital yet: the furthest along has 4 of 20 daily samples.",
        )

    def test_money_when_samples_are_enough_but_the_edge_is_not(self) -> None:
        said = self._story([_member("momentum_rotation", 1.0, samples=25),
                            _member("benchmark", 0.0, bench=True)])
        self.assertTrue(said["money"].endswith(
            "No strategy has earned capital yet: none has beaten buy-and-hold convincingly."))

    def test_money_when_capital_follows_a_winner(self) -> None:
        said = self._story(
            [_member("momentum_rotation", 3.0, samples=25), _member("benchmark", 0.0, bench=True)],
            allocation={"weights": {"momentum_rotation": 0.4, "benchmark": 0.6}},
        )
        self.assertEqual(said["money"],
                         "Capital follows the evidence: Momentum rotation 40%, buy-and-hold 60%.")

    def test_money_before_the_first_allocation(self) -> None:
        said = self._story([_member("benchmark", 0.0, bench=True)], allocation={"weights": {}})
        self.assertEqual(said["money"], "The desk has not made its first allocation yet.")

    def test_just_now_is_the_latest_news_or_the_quiet_watch(self) -> None:
        fresh = [{"at": "2026-09-29T21:30:00+00:00", "kind": "fill", "text": "Crypto trend bought SPY $9.44 on paper"}]
        stale = [{"at": "2026-09-28T01:00:00+00:00", "kind": "fill", "text": "old news"}]
        members = [_member("benchmark", 0.0, bench=True)]
        self.assertEqual(self._story(members, ticker=fresh)["just_now"],
                         "Crypto trend bought SPY $9.44 on paper.")
        self.assertEqual(self._story(members, ticker=stale)["just_now"],
                         "Watching 19 symbols; nothing needs doing right now.")

    def test_the_view_carries_the_story(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            said = build_desk_view(config, [], now=NOW)["story"]
        self.assertEqual(said["right_now"], "Momentum rotation is beating buy-and-hold by +1.50 points.")
        self.assertIn("All $50.10 sits in buy-and-hold", said["money"])

    def test_a_non_desk_bot_still_gets_a_story(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), strategy="trend_crypto")
            view = build_desk_view(config, [], now=NOW)
        self.assertFalse(view["enabled"])
        self.assertEqual(view["story"]["right_now"],
                         "The cockpit follows the strategy desk; this bot runs Crypto trend on its own.")
        self.assertEqual(view["story"]["money"], "Its orders and positions are on the Orders tab.")


class EventCacheTests(unittest.TestCase):
    def _journal(self, tmp: Path) -> Path:
        journal = tmp / "journal"
        journal.mkdir()
        lines = [
            json.dumps({"event": "member_fill", "member": "benchmark", "side": "buy",
                        "symbol": "QQQ", "quantity": "1", "price": "1", "at": "2026-09-28T01:00:00+00:00"}),
            json.dumps({"event": "stale_quotes_rejected", "at": "2026-09-28T01:00:01+00:00"}),
            '{"event": "member_fill", "memb',             # cut by a power loss
            "\x00\x00\x00",                               # a null-byte tail
            json.dumps({"event": "accepted", "intent": {"reason": "trend_entry"}, "at": "x"}),
            json.dumps({"event": "accepted", "intent": {"reason": "desk_rebalance"},
                        "side": "buy", "symbol": "QQQ", "notional": "1", "at": "2026-09-28T02:00:00+00:00"}),
        ]
        (journal / "2026-09-28.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (journal / "archive-2026-09-16-tif.jsonl").write_text(lines[0] + "\n", encoding="utf-8")
        return journal

    def test_it_keeps_only_desk_news_and_skips_broken_lines(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            events = DeskEventCache(self._journal(Path(name))).read()
        self.assertEqual([e["event"] for e in events], ["member_fill", "accepted"])

    def test_an_unchanged_file_is_not_re_read(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            journal = self._journal(Path(name))
            cache = DeskEventCache(journal)
            first = cache.read()
            from unittest import mock

            with mock.patch("agentic_trading.dashboard_desk._relevant_events") as parse:
                self.assertEqual(cache.read(), first)
            parse.assert_not_called()

    def test_a_missing_journal_dir_is_empty(self) -> None:
        self.assertEqual(DeskEventCache(Path("/nonexistent/journal")).read(), [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard_desk.py -q`

Expected: collection error, `ImportError: cannot import name 'DeskEventCache'`.

- [ ] **Step 3: Implement the ticker, the story and the cache**

In `src/agentic_trading/dashboard_desk.py`:

1. Add `import threading` to the imports.
2. Add `from agentic_trading.types import DESK_ORDER_REASON`.
3. Add the constants below after `LABELS`.
4. Add the functions and class below before `build_desk_view`.

```python
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
```

Then fill `"story"` and `"ticker"` in `build_desk_view`:

1. Right after `current = ...`, add:

```python
    ticker = [
        item for item in (ticker_item(record) for record in reversed(events)) if item
    ][:TICKER_SIZE]
    symbols = len(getattr(config, "effective_whitelist", ()) or ())
```

2. In the non-desk return, replace `"story": {},` and `"ticker": [],` with:

```python
            "story": {
                "right_now": "The cockpit follows the strategy desk; this bot runs "
                f"{label(str(getattr(config, 'strategy', '')))} on its own.",
                "money": "Its orders and positions are on the Orders tab.",
                "just_now": story([], None, None, ticker, symbols=symbols, now=current)["just_now"],
            },
            "ticker": ticker,
```

3. In the desk return, replace `"story": {},` and `"ticker": [],` with:

```python
        "story": story(members, allocation, account["value"], ticker, symbols=symbols, now=current),
        "ticker": ticker,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard_desk.py -q`

Expected: `26 passed` (10 from Task 1 plus 16 new; one test has 8 subtests).

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/dashboard_desk.py tests/test_dashboard_desk.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): plain-English story and ticker from desk journal events"
```

### Task 3: Serve `GET /api/desk`

**Files:**
- Modify: `src/agentic_trading/dashboard.py`: imports, `DashboardState.__init__` (around lines 323-336), a new method after `equity_curve` (around line 1118), and `_Handler.do_GET` (around line 1479).
- Modify: `tests/test_dashboard_desk.py`

**Interfaces:**
- Consumes: `build_desk_view(config, events, *, now=None)`, `DeskEventCache(journal_dir).read()`.
- Produces:
  - `DashboardState.desk() -> dict`: the view. On any exception it returns `{"enabled": False, "error": "<Type>: <message>", "members": [], "story": {}, "ticker": []}`, never raising.
  - Route `GET /api/desk`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard_desk.py`, before `if __name__ == "__main__":`:

```python
class DeskEndpointTests(unittest.TestCase):
    def _get(self, config, path: str) -> dict:
        import threading
        import urllib.request

        from agentic_trading.dashboard import serve

        server = serve(config, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}{path}"
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
                return json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()

    def test_the_cockpit_endpoint_serves_the_desk_view(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            journal = Path(config.journal_dir)
            journal.mkdir(parents=True, exist_ok=True)
            (journal / "2026-09-29.jsonl").write_text(json.dumps({
                "event": "member_fill", "member": "momentum_rotation", "side": "buy",
                "symbol": "AAPL", "quantity": "0.04", "price": "250",
                "at": "2026-09-29T21:00:00+00:00"}) + "\n", encoding="utf-8")
            payload = self._get(config, "/api/desk")
        self.assertTrue(payload["enabled"])
        self.assertEqual([m["name"] for m in payload["members"]], ["momentum_rotation", "benchmark"])
        self.assertEqual(payload["ticker"][0]["text"], "Momentum rotation bought AAPL $10.00 on paper")

    def test_a_failure_inside_the_view_is_reported_not_raised(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            with mock.patch("agentic_trading.dashboard.build_desk_view",
                            side_effect=RuntimeError("boom")):
                payload = self._get(config, "/api/desk")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["error"], "RuntimeError: boom")
        self.assertEqual(payload["members"], [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard_desk.py -q -k Endpoint`

Expected: 2 failed. The first fails with `HTTPError 404` (route missing). The second fails with `AttributeError: ... does not have the attribute 'build_desk_view'`.

- [ ] **Step 3: Implement the route**

In `src/agentic_trading/dashboard.py`:

1. Add the import next to the other `agentic_trading` imports:

```python
from agentic_trading.dashboard_desk import DeskEventCache, build_desk_view
```

2. At the end of `DashboardState.__init__`, add:

```python
        # Desk news for the cockpit's ticker, parsed once per changed journal.
        self._desk_events = DeskEventCache(self.journal_dir)
```

3. Add this method to `DashboardState`, directly after `equity_curve`:

```python
    def desk(self) -> dict[str, Any]:
        """The cockpit's view of the strategy desk (``/api/desk``)."""
        config = self.refresh_config()
        if self._desk_events.journal_dir != self.journal_dir:
            self._desk_events = DeskEventCache(self.journal_dir)
        try:
            return build_desk_view(config, self._desk_events.read())
        except Exception as exc:  # noqa: BLE001 — the console must not 500 on odd state
            return {
                "enabled": False,
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "members": [],
                "story": {},
                "ticker": [],
            }
```

4. In `_Handler.do_GET`, directly after the `/api/equity` branch, add:

```python
        if parsed.path == "/api/desk":
            self._json(self.state.desk())
            return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard_desk.py tests/test_history_dashboard.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/dashboard.py tests/test_dashboard_desk.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): serve the desk view at /api/desk"
```

### Task 4: Pure SVG chart helpers

**Files:**
- Create: `src/agentic_trading/dashboard_charts_js.py`
- Create: `tests/test_dashboard_charts.py`

**Interfaces:**
- Produces the Python string `CHARTS`, which defines the global `const Charts` with:
  - `scale(d0, d1, r0, r1) -> (v) => number` (a flat domain maps to the middle)
  - `extent(values, pad = 0.1) -> [lo, hi]` (always includes 0; ignores non-finite values; empty gives `[-1.2, 1.2]`)
  - `linePath(points, sx, sy) -> string` (`"M x y L x y ..."`; a non-finite point restarts the line with a new `M`; 1 decimal place)
  - `arcs(parts) -> [{name, value, start, end}]` (degrees clockwise from 12 o'clock; skips non-positive and non-finite values; the ends sum to 360)
  - `arcPath(cx, cy, r0, r1, start, end) -> string` (a closed donut segment; a full 360° is capped at 359.9 so its end point never rounds onto its start, which SVG would draw as nothing)
  - `tween(a, b, t) -> number` (ease-out cubic, `t` clamped to [0, 1])
  - `dayIndex(iso, originIso) -> number` (days between two ISO timestamps)
- Touches no DOM, so Node can run it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dashboard_charts.py`:

```python
"""The cockpit's chart helpers are pure: data in, numbers and SVG paths out."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipUnless(NODE, "node is not installed")
class ChartHelperTests(unittest.TestCase):
    def js(self, expression: str):
        from agentic_trading.dashboard_charts_js import CHARTS

        script = CHARTS + "\nconsole.log(JSON.stringify(" + expression + "));\n"
        done = subprocess.run(
            [NODE, "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_scale_maps_the_ends_and_a_flat_domain_to_the_middle(self) -> None:
        self.assertEqual(
            self.js("[Charts.scale(0, 10, 0, 100)(0), Charts.scale(0, 10, 0, 100)(10),"
                    " Charts.scale(0, 10, 100, 0)(2.5), Charts.scale(3, 3, 0, 100)(3)]"),
            [0, 100, 75, 50],
        )

    def test_extent_always_includes_zero_and_pads(self) -> None:
        lo, hi = self.js("Charts.extent([1, 2, 3])")
        self.assertAlmostEqual(lo, -0.3)
        self.assertAlmostEqual(hi, 3.3)
        self.assertEqual(self.js("Charts.extent([])"), [-1.2, 1.2])
        lo, hi = self.js("Charts.extent([NaN, null, -2, Infinity])")
        self.assertAlmostEqual(lo, -2.2)
        self.assertAlmostEqual(hi, 0.2)

    def test_line_path_handles_none_one_many_and_gaps(self) -> None:
        ident = "(v) => v"
        self.assertEqual(self.js(f"Charts.linePath([], {ident}, {ident})"), "")
        self.assertEqual(self.js(f"Charts.linePath([[1, 2]], {ident}, {ident})"), "M 1.0 2.0")
        self.assertEqual(
            self.js(f"Charts.linePath([[0, 0], [1, 1], [2, NaN], [3, 3]], {ident}, {ident})"),
            "M 0.0 0.0 L 1.0 1.0 M 3.0 3.0",
        )

    def test_arcs_cover_the_circle_and_skip_empty_parts(self) -> None:
        arcs = self.js(
            "Charts.arcs([{name: 'QQQ', value: 0.6}, {name: 'none', value: 0},"
            " {name: 'bad', value: NaN}, {name: 'BTC', value: 0.4}])"
        )
        self.assertEqual([a["name"] for a in arcs], ["QQQ", "BTC"])
        self.assertAlmostEqual(arcs[0]["start"], 0)
        self.assertAlmostEqual(arcs[0]["end"], 216)
        self.assertAlmostEqual(arcs[-1]["end"], 360)
        self.assertEqual(self.js("Charts.arcs([{name: 'x', value: 0}])"), [])

    def test_a_full_circle_arc_does_not_collapse(self) -> None:
        path = self.js("Charts.arcPath(50, 50, 30, 40, 0, 360)")
        self.assertTrue(path.startswith("M ") and path.endswith(" Z"))
        self.assertEqual(path.count(" A "), 2)
        start = path.split(" A ")[0]
        end_of_outer = path.split(" A ")[1].split(" L ")[0].split()[-2:]
        self.assertNotEqual(start.split()[1:], end_of_outer)

    def test_tween_eases_and_clamps(self) -> None:
        self.assertEqual(
            self.js("[Charts.tween(0, 10, 0), Charts.tween(0, 10, 1), Charts.tween(0, 10, 7),"
                    " Charts.tween(0, 10, -1)]"),
            [0, 10, 10, 0],
        )
        self.assertGreater(self.js("Charts.tween(0, 10, 0.5)"), 5)  # ease-out runs ahead

    def test_day_index_counts_days(self) -> None:
        self.assertEqual(
            self.js("Charts.dayIndex('2026-09-27', '2026-09-25')"), 2)
        self.assertAlmostEqual(
            self.js("Charts.dayIndex('2026-09-25T12:00:00+00:00', '2026-09-25')"), 0.5)


class ChartModuleTests(unittest.TestCase):
    def test_the_module_is_a_raw_string_without_control_bytes(self) -> None:
        from agentic_trading.dashboard_charts_js import CHARTS

        self.assertIn("const Charts =", CHARTS)
        self.assertNotIn("\b", CHARTS)
        self.assertNotIn("document.", CHARTS)  # pure: no DOM
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard_charts.py -q`

Expected: every test fails with `ModuleNotFoundError: No module named 'agentic_trading.dashboard_charts_js'`.

- [ ] **Step 3: Implement `dashboard_charts_js.py`**

Create `src/agentic_trading/dashboard_charts_js.py`:

```python
"""Pure SVG chart helpers for the cockpit: data in, numbers and path strings out.

Nothing here touches the DOM, so Node runs these directly in the tests. The
cockpit module draws with them.
"""

CHARTS = r"""
const Charts = (() => {
  // Map a value in [d0, d1] onto [r0, r1]; a flat domain maps to the middle.
  const scale = (d0, d1, r0, r1) => (v) =>
    d1 === d0 ? (r0 + r1) / 2 : r0 + (v - d0) * (r1 - r0) / (d1 - d0);

  // A padded [lo, hi] over the finite values, always including zero, so the
  // race is read against its starting line.
  const extent = (values, pad = 0.1) => {
    const finite = values.filter((v) => Number.isFinite(v));
    let lo = Math.min(0, ...finite), hi = Math.max(0, ...finite);
    if (lo === hi) { lo -= 1; hi += 1; }
    const room = (hi - lo) * pad;
    return [lo - room, hi + room];
  };

  // "M x y L x y ..." through the finite points; a gap starts a new segment.
  const linePath = (points, sx, sy) => {
    let out = '';
    let pen = false;
    for (const [x, y] of points) {
      if (!Number.isFinite(x) || !Number.isFinite(y)) { pen = false; continue; }
      const move = pen ? ' L ' : (out ? ' M ' : 'M ');
      out += move + sx(x).toFixed(1) + ' ' + sy(y).toFixed(1);
      pen = true;
    }
    return out;
  };

  // Donut segments in degrees, clockwise from 12 o'clock.
  const arcs = (parts) => {
    const clean = parts.filter((p) => Number.isFinite(p.value) && p.value > 0);
    const total = clean.reduce((sum, p) => sum + p.value, 0);
    if (total <= 0) return [];
    let at = 0;
    return clean.map((p) => {
      const sweep = (p.value / total) * 360;
      const arc = { name: p.name, value: p.value, start: at, end: at + sweep };
      at += sweep;
      return arc;
    });
  };

  // A closed donut segment between radii r0 and r1 around (cx, cy).
  const arcPath = (cx, cy, r0, r1, start, end) => {
    // 359.9, not 359.999: at two decimals the latter's end point equals its
    // start, and SVG draws an arc between identical points as nothing.
    const sweep = Math.min(end - start, 359.9);
    const rad = (deg) => (deg - 90) * Math.PI / 180;
    const pt = (r, deg) => [cx + r * Math.cos(rad(deg)), cy + r * Math.sin(rad(deg))];
    const large = sweep > 180 ? 1 : 0;
    const f = (n) => n.toFixed(2);
    const [ax, ay] = pt(r1, start);
    const [bx, by] = pt(r1, start + sweep);
    const [ix, iy] = pt(r0, start + sweep);
    const [jx, jy] = pt(r0, start);
    return 'M ' + f(ax) + ' ' + f(ay)
      + ' A ' + r1 + ' ' + r1 + ' 0 ' + large + ' 1 ' + f(bx) + ' ' + f(by)
      + ' L ' + f(ix) + ' ' + f(iy)
      + ' A ' + r0 + ' ' + r0 + ' 0 ' + large + ' 0 ' + f(jx) + ' ' + f(jy) + ' Z';
  };

  // Ease-out cubic from a to b; t is clamped to [0, 1].
  const tween = (a, b, t) => {
    const k = Math.min(1, Math.max(0, t));
    return a + (b - a) * (1 - Math.pow(1 - k, 3));
  };

  // Days between two ISO timestamps (dates alone are read as UTC midnight).
  const dayIndex = (iso, origin) => (Date.parse(iso) - Date.parse(origin)) / 86400000;

  return { scale, extent, linePath, arcs, arcPath, tween, dayIndex };
})();
"""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard_charts.py -q`

Expected: `8 passed`, or `1 passed, 7 skipped` where Node is absent. This machine has Node (`/usr/bin/nodejs`), so expect 8.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/dashboard_charts_js.py tests/test_dashboard_charts.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): pure SVG chart helpers for the cockpit"
```

### Task 5: The page: tabs, cockpit markup and styles

**Files:**
- Create: `src/agentic_trading/dashboard_css.py`
- Modify: `src/agentic_trading/dashboard_html.py`: body from `<body>` to the end of `_TEMPLATE`, plus the assembly line.
- Modify: `src/agentic_trading/dashboard_js.py`: the last six lines (the boot block), plus two new functions.
- Create: `tests/test_cockpit_page.py`

**Interfaces:**
- Consumes: `CHARTS` from Task 4.
- Produces:
  - `dashboard_css.CSS` (string).
  - Page element ids that Task 6 fills: `say-now`, `say-money`, `say-just`, `race`, `race-trial`, `race-tip`, `t-account`, `t-spark`, `t-trial`, `t-trial-ring`, `t-money`, `t-money-legend`, `t-next`, `ticker`, `tape`, `netdot`, `members`.
  - Global JS functions `showTab(name)` and `initTabs()`.
  - The `HTML` assembly line, which Task 6 extends with the cockpit script.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cockpit_page.py`:

```python
"""The cockpit page: four tabs, nothing loaded from outside, no card lost."""

from __future__ import annotations

import re
import unittest

from agentic_trading.dashboard_html import HTML
from agentic_trading.dashboard_js import SCRIPT

COCKPIT_IDS = (
    "say-now", "say-money", "say-just", "race", "race-trial", "race-tip",
    "t-account", "t-spark", "t-trial", "t-trial-ring", "t-money",
    "t-money-legend", "t-next", "ticker", "tape",
)


def _ids(markup: str) -> set[str]:
    return set(re.findall(r'id="([A-Za-z0-9_-]+)"', markup))


def _markup() -> str:
    return HTML.split("<script>", 1)[0]


def _section(name: str) -> str:
    return HTML.split(f'id="tab-{name}"', 1)[1].split("</section>", 1)[0]


class CockpitPageTests(unittest.TestCase):
    def test_the_page_loads_nothing_from_outside(self) -> None:
        self.assertNotRegex(HTML, r"<script[^>]+src=")
        self.assertNotRegex(HTML, r'(src|href)="(https?:)?//')
        self.assertNotRegex(HTML, r"url\([\"']?(https?:)?//")
        self.assertNotIn("@import", HTML)

    def test_four_tabs_open_on_the_overview(self) -> None:
        for tab in ("overview", "strategies", "orders", "health"):
            self.assertIn(f'id="tab-{tab}"', HTML)
            self.assertIn(f'data-tab="{tab}"', HTML)
        self.assertIn('<section class="tab on" id="tab-overview">', HTML)

    def test_every_element_the_existing_script_needs_is_still_on_the_page(self) -> None:
        wanted = set(re.findall(r"(?:getElementById|setBadge)\('([A-Za-z0-9_-]+)'", SCRIPT))
        wanted |= set(re.findall(r"querySelector\('#([A-Za-z0-9_-]+)", SCRIPT))
        self.assertGreater(len(wanted), 30)
        self.assertEqual(wanted - _ids(SCRIPT) - _ids(_markup()), set())

    def test_the_overview_holds_the_cockpit(self) -> None:
        self.assertIn(".cockpit{display:grid;grid-template-columns:1fr 2.2fr 1fr", HTML)
        overview = _section("overview")
        for element in COCKPIT_IDS:
            self.assertIn(f'id="{element}"', overview, element)

    def test_cards_sit_in_their_tabs(self) -> None:
        placed = {
            "strategies": ("members", "candidates", "universe", "evidence", "gate",
                           "frontier", "evolution", "proposals"),
            "orders": ("orders", "chart", "stream", "notional", "trades", "equity"),
            "health": ("agents", "health", "alerts", "account", "streak", "regimes"),
        }
        for tab, elements in placed.items():
            for element in elements:
                self.assertIn(f'id="{element}"', _section(tab), f"{element} in {tab}")

    def test_the_arm_control_and_link_dot_live_in_the_header(self) -> None:
        header = HTML.split("<header>", 1)[1].split("</header>", 1)[0]
        for element in ("arm", "arm-status", "netdot", "tabs"):
            self.assertIn(f'id="{element}"', header)
        self.assertNotIn("<h2>Order submission</h2>", HTML)

    def test_timers_skip_work_while_the_page_is_hidden(self) -> None:
        self.assertIn("if (!document.hidden) refresh();", SCRIPT)
        self.assertIn("if (!document.hidden) refreshCandidates();", SCRIPT)
        self.assertNotIn("setInterval(refresh, 2000)", SCRIPT)

    def test_the_chart_helpers_ride_in_the_page(self) -> None:
        self.assertIn("const Charts =", HTML)
        self.assertNotIn("__COCKPIT_CSS__", HTML)
        self.assertNotIn("__SCRIPT__", HTML)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cockpit_page.py -q`

Expected: several failures, e.g. `'id="tab-overview"' not found`. `test_the_page_loads_nothing_from_outside` and `test_every_element...` already pass.

- [ ] **Step 3: Create `dashboard_css.py`**

```python
"""Cockpit styles: tabs, the overview grid, the race, tiles and the ticker.

Added after the original console styles, so every existing card keeps its look
and only the new pieces (and the body font) are defined here.
"""

CSS = r"""
body{font:14px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
table,#stream,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;z-index:5}
.tabs{display:flex;gap:4px;margin-left:auto}
.tabs button{background:transparent;border:1px solid var(--line);color:var(--muted);padding:6px 14px;border-radius:999px;font:600 12px system-ui,sans-serif;cursor:pointer;transition:all .2s}
.tabs button:hover{color:var(--text);border-color:var(--accent)}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#04111f}
.tab{display:none}
.tab.on{display:block;animation:tabin .35s ease}
@keyframes tabin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.armbox{display:flex;align-items:center;gap:8px}
.netdot{width:9px;height:9px;border-radius:50%;background:var(--buy);animation:ping 2s infinite}
.netdot.lost{background:var(--warn);animation:none}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(53,208,127,.55)}70%{box-shadow:0 0 0 8px rgba(53,208,127,0)}100%{box-shadow:0 0 0 0 rgba(53,208,127,0)}}
/* The cockpit fits one 1440x900 screen: stories | race | tiles, ticker below. */
.cockpit{display:grid;grid-template-columns:1fr 2.2fr 1fr;gap:14px;padding:16px;height:calc(100vh - 150px);min-height:520px}
.stories,.tiles{display:flex;flex-direction:column;gap:12px;min-height:0}
.story{flex:1;background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--buy);border-radius:12px;padding:16px;display:flex;flex-direction:column;justify-content:center;min-height:0}
.story.money{border-left-color:var(--accent)}
.story.just{border-left-color:var(--warn)}
.story .k,.tile .k{color:var(--muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px}
.say{font-size:17px;font-weight:650;line-height:1.35}
.say.fade{animation:fadein .3s ease}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.race{display:flex;flex-direction:column;min-height:0;position:relative}
.racehead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
#race{flex:1;width:100%;min-height:0}
#race .zero{stroke:var(--line);stroke-dasharray:4 4}
#race .grid{fill:var(--muted);font-size:11px}
#race .line{fill:none;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
#race .line.bench{stroke-dasharray:6 5;stroke-width:2}
#race .hit{fill:none;stroke:transparent;stroke-width:14;cursor:pointer}
#race .end{font-size:12px;font-weight:700}
#race .lead{animation:beat 1.6s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
@keyframes beat{0%,100%{transform:scale(1)}50%{transform:scale(1.6)}}
#race .drawin{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.2s ease-out forwards}
@keyframes draw{to{stroke-dashoffset:0}}
#race .empty{fill:var(--muted);font-size:14px}
#race .dim{opacity:.25}
.tip{position:absolute;pointer-events:none;background:#0d1219;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;display:none;max-width:240px;z-index:3}
.tile{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;min-height:0;display:flex;flex-direction:column;justify-content:center}
.num{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}
.tile svg{width:100%;height:44px;display:block}
.tile.ring svg{height:90px}
.sweep{animation:sweep .9s ease-out;transform-origin:center;transform-box:fill-box}
@keyframes sweep{from{transform:rotate(-90deg) scale(.7);opacity:0}to{transform:none;opacity:1}}
.ticker{margin:0 16px 16px;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;white-space:nowrap;padding:10px 0}
.tape{display:inline-block;padding-left:100%;animation:scroll 60s linear infinite}
.ticker:hover .tape{animation-play-state:paused}
@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-100%)}}
.item{display:inline-block;margin-right:36px;font-size:13px}
.item .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle;background:var(--muted)}
.item.fill .dot{background:var(--buy)}
.item.order .dot{background:var(--accent)}
.item.allocation .dot{background:var(--shadow)}
.item.overruled .dot{background:var(--warn)}
.item.error .dot{background:var(--sell)}
.item.new{animation:flashin 1.2s ease}
@keyframes flashin{0%{color:#fff;text-shadow:0 0 12px var(--accent)}100%{color:inherit;text-shadow:none}}
.members{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.member{background:#0f151e;border:1px solid var(--line);border-top:3px solid var(--muted);border-radius:10px;padding:12px}
.member h3{margin:0 0 6px;font-size:15px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{background:#1b2431;border-radius:999px;padding:2px 9px;font-size:11px}
.progress{height:6px;background:#1b2431;border-radius:3px;overflow:hidden;margin:4px 0 8px}
.progress>div{height:100%;background:linear-gradient(90deg,var(--shadow),var(--accent));transition:width .6s ease}
@media(max-width:1100px){.cockpit{grid-template-columns:1fr;height:auto}.tabs{margin-left:0}}
"""
```

- [ ] **Step 4: Restructure the page in `dashboard_html.py`**

1. Change the imports at the top to:

```python
from agentic_trading.dashboard_charts_js import CHARTS
from agentic_trading.dashboard_css import CSS
from agentic_trading.dashboard_js import SCRIPT
```

2. Directly after the existing `</style>`, before `</head>`, insert:

```html
<style>
__COCKPIT_CSS__
</style>
```

3. Replace everything from `<body>` to the end of `_TEMPLATE` (the `</script></body></html>` line) with the markup below. It moves every existing card, unchanged, into its tab. The only card removed is "Order submission": its `#arm` and `#arm-status` move into the header.

```html
<body>
<header><h1>Agentic Trader</h1>
<span id="mode" class="badge shadow">shadow</span>
<span id="stage" class="badge stage">stage: shadow</span>
<span id="session" class="badge">session</span>
<span id="armed" class="badge">arming…</span>
<span id="kill" class="badge kill" style="display:none">kill switch</span>
<span class="netdot" id="netdot" title="connected"></span>
<span class="sub" id="generated"></span><span class="badge" id="pulse" style="display:none">—</span>
<nav class="tabs" id="tabs">
  <button data-tab="overview" class="on">Overview</button>
  <button data-tab="strategies">Strategies</button>
  <button data-tab="orders">Orders</button>
  <button data-tab="health">Health</button>
</nav>
<div class="armbox"><div id="arm" class="sub">checking…</div><span class="sub" id="arm-status"></span></div>
</header>

<section class="tab on" id="tab-overview">
<div class="cockpit">
  <div class="stories">
    <div class="story"><div class="k">Right now</div><div class="say" id="say-now">Reading the desk…</div></div>
    <div class="story money"><div class="k">Your money</div><div class="say" id="say-money">…</div></div>
    <div class="story just"><div class="k">Just now</div><div class="say" id="say-just">…</div></div>
  </div>
  <div class="card race">
    <div class="racehead"><h2>The race · every strategy against buy-and-hold</h2><span class="sub" id="race-trial"></span></div>
    <svg id="race" role="img" aria-label="Each strategy's return against buy-and-hold"></svg>
    <div class="tip" id="race-tip"></div>
  </div>
  <div class="tiles">
    <div class="tile"><div class="k">Account</div><div class="num" id="t-account">—</div><svg id="t-spark"></svg></div>
    <div class="tile ring"><div class="k">Trial</div><div class="num" id="t-trial">—</div><svg id="t-trial-ring" viewBox="0 0 100 100"></svg></div>
    <div class="tile ring"><div class="k">Where the money is</div><svg id="t-money" viewBox="0 0 100 100"></svg><div class="sub" id="t-money-legend"></div></div>
    <div class="tile"><div class="k">Next allocation</div><div class="num" id="t-next">—</div></div>
  </div>
</div>
<div class="ticker" id="ticker"><div class="tape" id="tape"></div></div>
</section>

<section class="tab" id="tab-strategies">
<main>
<div class="card span12"><h2>Strategies · each one's paper record against buy-and-hold</h2><div id="members" class="members"><div class="sub">reading the desk…</div></div></div>
<div class="card span12"><h2>Candidates · what the rule wants right now</h2>
  <div class="tablewrap"><table id="candidates">
    <thead><tr><th>symbol</th><th>how the price is moving</th><th>how wildly it moves</th><th>in the book</th><th>held back by</th><th>why</th></tr></thead>
    <tbody><tr><td colspan="6" class="sub">computing…</td></tr></tbody>
  </table></div>
  <div class="sub" id="candidates-note"></div>
</div>
<div class="card span12"><h2>Symbol scout · what it found, what it added, what it dropped</h2>
  <div class="tablewrap"><table id="universe">
    <thead><tr><th>symbol</th><th>how the price is moving</th><th>traded per day</th><th>cost to trade</th><th>decision</th><th>why</th></tr></thead>
    <tbody><tr><td colspan="6" class="sub">no scan yet</td></tr></tbody>
  </table></div>
  <div class="sub" id="universe-note"></div>
</div>
<div class="card span8"><h2>What sizing up costs · measured drawdown by per-order size</h2><canvas id="frontier"></canvas>
  <div class="legend"><span><i class="dot buy"></i>inside the 15% ceiling</span><span><i class="dot sell"></i>above it</span><span><i class="line"></i>15% gate</span><span class="sub" id="frontier-note"></span></div></div>
<div class="card span4"><h2>Promotion gate</h2><div id="gate"></div></div>
<div class="card span12"><h2>Walk-forward evidence · what the order size is justified by</h2><div id="evidence" class="sub">no evidence report yet — run: agentic-trading walkforward --config config/agentic.toml</div></div>
<div class="card span5"><h2>Evolution evidence</h2><div id="evolution" class="sub">no evolution run yet</div></div>
<div class="card span7"><h2>Proposed changes · the evolution agent proposes, you decide</h2><div id="proposals" class="sub">nothing proposed yet</div></div>
</main>
</section>

<section class="tab" id="tab-orders">
<main>
<div class="card span4"><h2>Account equity</h2><div class="metric" id="equity">—</div><div class="sub" id="equity-sub">—</div></div>
<div class="card span4"><h2>Daily notional used</h2><div class="metric" id="notional">—</div><div class="sub" id="notional-sub">—</div></div>
<div class="card span4"><h2>Checked / sent / refused</h2><div class="metric" id="trades">0</div><div class="sub" id="trades-sub">today (UTC — the strategy's day)</div></div>
<div class="card span12"><h2>Market &amp; order table</h2>
  <div class="tablewrap"><table id="orders">
    <thead><tr><th>time</th><th>status</th><th>what the checks said</th><th>symbol</th><th>side</th><th>type</th><th>session</th><th>size</th><th>value</th><th>price now</th><th>alerts</th><th>why</th></tr></thead>
    <tbody><tr><td colspan="12" class="sub">no decisions yet</td></tr></tbody>
  </table></div>
  <div class="sub" id="orders-count"></div>
  <div class="sub" id="orders-cadence"></div>
</div>
<div class="card span7"><h2>Decisions per day · accepted, placed, refused</h2><canvas id="chart"></canvas>
  <div class="legend"><span><i class="dot buy"></i>placed/accepted</span><span><i class="dot sell"></i>refused</span><span class="sub" id="activity-note"></span></div></div>
<div class="card span5"><h2>Live execution stream</h2><div id="stream"></div></div>
</main>
</section>

<section class="tab" id="tab-health">
<main>
<div class="card span7"><h2>Agents on duty</h2><div id="agents" class="sub">starting…</div><div id="alerts"></div><div id="health"></div></div>
<div class="card span5"><h2>Runtime &amp; P&amp;L</h2><div id="account" class="sub">starting…</div></div>
<div class="card span5"><h2>Promotion streak</h2><div class="metric" id="streak">0</div><div class="sub" id="streak-sub">assessments to next stage</div><div class="gauge" style="margin-top:8px"><div id="streak-bar"></div></div></div>
<div class="card span7"><h2>Market regimes</h2><div id="regimes" class="sub">no regime read yet</div></div>
</main>
</section>
<script>
__SCRIPT__
</script></body></html>
"""

HTML = _TEMPLATE.replace("__COCKPIT_CSS__", CSS).replace(
    "__SCRIPT__", CHARTS + "\n" + SCRIPT
)
```

(The `#regimes` element used to share the Evolution card. It now has its own Health card; the script only writes into it by id.)

Also delete the old `HTML = _TEMPLATE.replace("__SCRIPT__", SCRIPT)` line.

- [ ] **Step 5: Add the tab switcher and the hidden-page pause to `dashboard_js.py`**

Replace the last six lines of `SCRIPT` (from `refresh();` to the `window.addEventListener('resize', ...)` line) with:

```javascript
// Four tabs; the choice survives a reload. Canvas cards measure their width
// when drawn, so a newly shown tab is redrawn at once, not after the next tick.
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((s) => s.classList.toggle('on', s.id === 'tab-' + name));
  try { localStorage.setItem('tab', name); } catch (e) { /* storage blocked: default next time */ }
  window.dispatchEvent(new Event('resize'));
  refresh();
}

function initTabs() {
  document.querySelectorAll('#tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  let saved = 'overview';
  try { saved = localStorage.getItem('tab') || 'overview'; } catch (e) { /* storage blocked */ }
  if (!document.getElementById('tab-' + saved)) saved = 'overview';
  showTab(saved);
}

initTabs();
refreshCandidates();
setInterval(() => { if (!document.hidden) refresh(); }, 2000);
setInterval(() => { if (!document.hidden) refreshCandidates(); }, 30000);
window.addEventListener('resize', () => fetch('/api/equity').then(r => r.json()).then(drawChart));
```

- [ ] **Step 6: Run the page and language tests**

Run: `.venv/bin/python -m pytest tests/test_cockpit_page.py tests/test_console_language.py tests/test_history_dashboard.py -q`

Expected: all pass.

If `test_every_element_the_existing_script_needs_is_still_on_the_page` lists a missing id, add that element back into the tab where its card now lives. Never rename the id.

- [ ] **Step 7: Look at the page once**

Run: `.venv/bin/python -c "from agentic_trading.dashboard_html import HTML; open('/tmp/claude-1000/cockpit_check.html','w').write(HTML)"`

Then run `grep -c 'class="tab' /tmp/claude-1000/cockpit_check.html`.

Expected: `4`.

- [ ] **Step 8: Commit**

```bash
git add src/agentic_trading/dashboard_css.py src/agentic_trading/dashboard_html.py src/agentic_trading/dashboard_js.py tests/test_cockpit_page.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): four tabs, cockpit layout and styles; timers pause while hidden"
```

### Task 6: The cockpit script

**Files:**
- Create: `src/agentic_trading/dashboard_cockpit_js.py`
- Modify: `src/agentic_trading/dashboard_html.py` (the `HTML` assembly line)
- Create: `tests/test_cockpit_js.py`

**Interfaces:**
- Consumes:
  - `Charts` (Task 4)
  - `esc` (the global in `dashboard_js.SCRIPT`)
  - the element ids from Task 5
  - `GET /api/desk` (Tasks 1–3)
- Produces:
  - `COCKPIT`: a string defining the globals `CockpitFmt` (pure: `pct`, `money`, `countdown`, `spread`, `moneyParts`, `tickerKey`) and `Cockpit` (`start`, `poll`).
  - `COCKPIT_BOOT = "Cockpit.start();\n"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cockpit_js.py`:

```python
"""The cockpit script: pure formatting helpers, a script that parses, ids that exist."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest

NODE = shutil.which("node") or shutil.which("nodejs")


def _node(script: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, "-e", script], input=stdin, capture_output=True,
                          text=True, timeout=30)


@unittest.skipUnless(NODE, "node is not installed")
class CockpitFormatTests(unittest.TestCase):
    def js(self, expression: str):
        from agentic_trading.dashboard_charts_js import CHARTS
        from agentic_trading.dashboard_cockpit_js import COCKPIT

        done = _node(CHARTS + COCKPIT + "\nconsole.log(JSON.stringify(" + expression + "));\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_percent_and_money_read_like_a_ticker(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.pct(1.234), CockpitFmt.pct(-0.8), CockpitFmt.pct(0),"
                    " CockpitFmt.pct(null), CockpitFmt.money(49.451), CockpitFmt.money(NaN)]"),
            ["+1.23%", "−0.80%", "+0.00%", "—", "$49.45", "—"],
        )

    def test_countdown_scales_its_units(self) -> None:
        day, hour, minute = 86400000, 3600000, 60000
        self.assertEqual(
            self.js(f"[CockpitFmt.countdown(4*{day} + {hour} + 46*{minute}),"
                    f" CockpitFmt.countdown(2*{hour} + {minute}),"
                    f" CockpitFmt.countdown(46*{minute} + 10000),"
                    " CockpitFmt.countdown(0), CockpitFmt.countdown(-5)]"),
            ["4d 1h 46m", "2h 1m", "46m 10s", "due now", "due now"],
        )

    def test_end_labels_are_spread_apart_in_order_and_on_screen(self) -> None:
        self.assertEqual(self.js("CockpitFmt.spread([100, 102, 50], 14, 0, 200)"), [100, 114, 50])
        self.assertEqual(self.js("CockpitFmt.spread([195, 196], 14, 0, 200)"), [186, 200])
        self.assertEqual(self.js("CockpitFmt.spread([2, 3], 14, 10, 200)"), [10, 24])
        self.assertEqual(self.js("CockpitFmt.spread([], 14, 0, 200)"), [])

    def test_the_money_ring_shows_legs_until_a_strategy_is_funded(self) -> None:
        legs = {"QQQ": 0.6, "BTC-USD": 0.4}
        all_bench = json.dumps({"weights": {"momentum_rotation": 0, "benchmark": 1}, "legs": legs})
        funded = json.dumps({"weights": {"momentum_rotation": 0.4, "benchmark": 0.6}, "legs": legs})
        label = "(n) => n === 'benchmark' ? 'Buy-and-hold' : 'Momentum rotation'"
        self.assertEqual(self.js(f"CockpitFmt.moneyParts({all_bench}, {label})"),
                         [{"name": "QQQ", "value": 0.6}, {"name": "BTC", "value": 0.4}])
        self.assertEqual(self.js(f"CockpitFmt.moneyParts({funded}, {label})"),
                         [{"name": "Momentum rotation", "value": 0.4},
                          {"name": "Buy-and-hold", "value": 0.6}])
        self.assertEqual(self.js(f"CockpitFmt.moneyParts(null, {label})"), [])

    def test_the_ticker_key_changes_only_with_the_items(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.tickerKey([{at: 'a', text: 'x'}]) === CockpitFmt.tickerKey([{at: 'a', text: 'x'}]),"
                    " CockpitFmt.tickerKey([{at: 'a', text: 'x'}]) === CockpitFmt.tickerKey([{at: 'b', text: 'x'}])]"),
            [True, False],
        )

    def test_the_whole_page_script_parses(self) -> None:
        from agentic_trading.dashboard_html import HTML

        script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        done = _node("new Function(require('fs').readFileSync(0, 'utf8'));", stdin=script)
        self.assertEqual(done.returncode, 0, done.stderr)


class CockpitPageWiringTests(unittest.TestCase):
    def test_every_element_the_cockpit_draws_into_exists(self) -> None:
        from agentic_trading.dashboard_cockpit_js import COCKPIT
        from agentic_trading.dashboard_html import HTML

        wanted = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", COCKPIT))
        self.assertGreater(len(wanted), 10)
        markup = HTML.split("<script>", 1)[0]
        self.assertEqual({w for w in wanted if f'id="{w}"' not in markup}, set())

    def test_the_cockpit_boots_once_after_the_console_script(self) -> None:
        from agentic_trading.dashboard_html import HTML

        script = HTML.split("<script>", 1)[1]
        self.assertEqual(script.count("Cockpit.start();"), 1)
        self.assertLess(script.index("const esc ="), script.index("const Cockpit ="))
        self.assertLess(script.index("const Cockpit ="), script.index("Cockpit.start();"))

    def test_the_module_is_raw_and_escapes_what_it_writes(self) -> None:
        from agentic_trading.dashboard_cockpit_js import COCKPIT

        self.assertNotIn("\b", COCKPIT)
        self.assertNotIn("${", COCKPIT)  # concatenation only; every value passes esc()
        self.assertGreater(COCKPIT.count("esc("), 15)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cockpit_js.py -q`

Expected: every test fails with `ModuleNotFoundError: No module named 'agentic_trading.dashboard_cockpit_js'`.

- [ ] **Step 3: Implement `dashboard_cockpit_js.py`**

```python
"""The cockpit: story cards, the strategy race, tiles, the ticker, member cards.

It polls ``/api/desk`` every five seconds while the page is visible, keeps the
last good picture when a poll fails, and animates only what changed. The pure
formatting helpers live in ``CockpitFmt`` so Node can test them.
"""

COCKPIT = r"""
const CockpitFmt = (() => {
  const pct = (v) => (!Number.isFinite(v) ? '—'
    : (v < 0 ? '−' : '+') + Math.abs(v).toFixed(2) + '%');
  const money = (v) => (!Number.isFinite(v) ? '—' : '$' + v.toFixed(2));
  const countdown = (ms) => {
    if (!Number.isFinite(ms) || ms <= 0) return 'due now';
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm ' + (s % 60) + 's';
  };
  // Label y positions pushed at least `gap` apart, order kept, inside [lo, hi].
  const spread = (ys, gap, lo, hi) => {
    const order = ys.map((y, i) => [y, i]).sort((a, b) => a[0] - b[0]);
    const out = order.map(([y]) => y);
    for (let i = 1; i < out.length; i++) out[i] = Math.max(out[i], out[i - 1] + gap);
    if (out.length) out[out.length - 1] = Math.min(out[out.length - 1], hi);
    for (let i = out.length - 2; i >= 0; i--) out[i] = Math.min(out[i], out[i + 1] - gap);
    if (out.length && out[0] < lo) {
      const shift = lo - out[0];
      for (let i = 0; i < out.length; i++) out[i] += shift;
    }
    const result = new Array(ys.length);
    order.forEach(([, index], k) => { result[index] = out[k]; });
    return result;
  };
  // The money ring: member weights, or the benchmark's legs while no strategy
  // holds capital (a ring that says only "benchmark 100%" says nothing).
  const moneyParts = (allocation, labelOf) => {
    if (!allocation) return [];
    const weights = allocation.weights || {};
    const funded = Object.entries(weights).filter(([n, w]) => n !== 'benchmark' && w > 0);
    if (!funded.length) {
      return Object.entries(allocation.legs || {})
        .map(([symbol, w]) => ({ name: symbol.replace('-USD', ''), value: w }));
    }
    return Object.entries(weights).filter(([, w]) => w > 0)
      .map(([name, w]) => ({ name: labelOf(name), value: w }));
  };
  const tickerKey = (items) => (items || []).map((i) => i.at + '|' + i.text).join('\n');
  return { pct, money, countdown, spread, moneyParts, tickerKey };
})();

const Cockpit = (() => {
  const PALETTE = ['#35d07f', '#ffb020', '#4aa8ff', '#c77dff', '#ff5f6d'];
  const BENCH_COLOR = '#7b8a9e';
  const LEG_COLORS = ['#4aa8ff', '#6b7cff', '#35d07f', '#ffb020', '#c77dff'];
  let last = null;          // the last good /api/desk payload
  let raceDomain = null;    // the y-range the race settled on; the next glides from it
  let prevNow = {};         // each member's last "now" value, for the glide
  let drawnOnce = false;    // lines draw in on the first render only
  let tickerSeen = '';      // what is on the tape now
  let lastTickerAt = '';    // newest item already shown, so newer ones flash
  let nextAt = NaN;         // next allocation, epoch ms
  const shown = {};         // last value each counter settled on

  const $ = (id) => document.getElementById(id);
  const colorsFor = (members) => {
    const out = {};
    let i = 0;
    for (const m of members) out[m.name] = m.is_benchmark ? BENCH_COLOR : PALETTE[i++ % PALETTE.length];
    return out;
  };
  const shortTime = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  function say(id, text) {
    const el = $(id);
    if (!el || !text || el.textContent === text) return;
    el.classList.remove('fade');
    void el.offsetWidth;  // restart the fade animation
    el.classList.add('fade');
    el.textContent = text;
  }

  function count(id, value, fmt) {
    const el = $(id);
    if (!el) return;
    if (!Number.isFinite(value)) { el.textContent = '—'; delete shown[id]; return; }
    const from = Number.isFinite(shown[id]) ? shown[id] : value;
    shown[id] = value;
    if (from === value || document.hidden) { el.textContent = fmt(value); return; }
    const t0 = performance.now();
    const step = (now) => {
      const k = (now - t0) / 600;
      el.textContent = fmt(Charts.tween(from, value, k));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function raceLines(data) {
    const members = data.members || [];
    const days = members.flatMap((m) => (m.series || []).map((p) => p[0])).sort();
    const origin = days.length ? days[0] : String(data.as_of || '').slice(0, 10);
    const nowX = Math.max(Charts.dayIndex(data.as_of, origin), 0);
    const lines = members.map((m) => {
      const pts = (m.series || []).map(([d, v]) => [Charts.dayIndex(d, origin), v]);
      if (Number.isFinite(m.now_pct)) pts.push([nowX, m.now_pct]);
      return { m, pts };
    });
    return { nowX, lines };
  }

  function emptyRace(svg, W, H, text) {
    svg.innerHTML = '<text class="empty" x="' + (W / 2) + '" y="' + (H / 2)
      + '" text-anchor="middle">' + esc(text) + '</text>';
  }

  function drawRace(data, domain, animateIn) {
    const svg = $('race');
    if (!svg) return;
    const W = svg.clientWidth || 640, H = svg.clientHeight || 320;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    if (!data.enabled) {
      emptyRace(svg, W, H, 'The race follows the strategy desk; this bot trades on its own (see Orders).');
      return;
    }
    const { nowX, lines } = raceLines(data);
    if (!lines.some((l) => l.pts.length)) {
      emptyRace(svg, W, H, 'The race fills in one point per day.');
      return;
    }
    const pad = { l: 56, r: 180, t: 16, b: 24 };
    const sx = Charts.scale(0, Math.max(nowX, 1), pad.l, W - pad.r);
    const sy = Charts.scale(domain[0], domain[1], H - pad.b, pad.t);
    const colors = colorsFor(data.members);
    const racers = lines.filter((l) => !l.m.is_benchmark && Number.isFinite(l.m.now_pct));
    const leader = racers.length
      ? racers.reduce((a, b) => (b.m.now_pct > a.m.now_pct ? b : a)).m.name : null;
    const zero = sy(0).toFixed(1);
    let out = '<line class="zero" x1="' + pad.l + '" x2="' + (W - pad.r) + '" y1="' + zero + '" y2="' + zero + '"/>';
    for (const v of [domain[1], 0, domain[0]]) {
      out += '<text class="grid" x="' + (pad.l - 8) + '" y="' + (sy(v) + 4).toFixed(1)
        + '" text-anchor="end">' + esc(CockpitFmt.pct(v)) + '</text>';
    }
    const ends = [];
    for (const { m, pts } of lines) {
      const d = Charts.linePath(pts, sx, sy);
      if (!d) continue;
      const drawIn = animateIn && !m.is_benchmark;  // pathLength would stretch the benchmark's dashes
      out += '<path class="line' + (m.is_benchmark ? ' bench' : '') + (drawIn ? ' drawin' : '') + '"'
        + (drawIn ? ' pathLength="1"' : '') + ' data-name="' + esc(m.name) + '" d="' + d
        + '" stroke="' + colors[m.name] + '"/>';
      out += '<path class="hit" data-name="' + esc(m.name) + '" d="' + d + '"/>';
      const tip = pts[pts.length - 1];
      const x = sx(tip[0]), y = sy(tip[1]);
      out += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="4.5" fill="' + colors[m.name] + '"'
        + (m.name === leader ? ' class="lead"' : '') + '/>';
      ends.push({ m, x, y, value: tip[1] });
    }
    const placed = CockpitFmt.spread(ends.map((e) => e.y), 16, pad.t + 6, H - pad.b);
    ends.forEach((e, i) => {
      out += '<text class="end" x="' + (e.x + 10).toFixed(1) + '" y="' + (placed[i] + 4).toFixed(1)
        + '" fill="' + colors[e.m.name] + '">' + esc(e.m.label + ' ' + CockpitFmt.pct(e.value)) + '</text>';
    });
    svg.innerHTML = out;
  }

  function renderRace(data) {
    const lines = data.enabled ? raceLines(data).lines : [];
    const target = Charts.extent(lines.flatMap((l) => l.pts.map((p) => p[1])));
    const fromNow = prevNow;
    prevNow = Object.fromEntries((data.members || []).map((m) => [m.name, m.now_pct]));
    if (!drawnOnce || !raceDomain) {
      drawRace(data, target, true);
      raceDomain = target;
      drawnOnce = true;
      return;
    }
    const from = raceDomain;
    raceDomain = target;
    const moved = from[0] !== target[0] || from[1] !== target[1]
      || (data.members || []).some((m) => fromNow[m.name] !== m.now_pct);
    if (!moved || document.hidden) { drawRace(data, target, false); return; }
    const t0 = performance.now();
    const frame = (now) => {
      const k = Math.min(1, (now - t0) / 600);
      const members = (data.members || []).map((m) => ({
        ...m,
        now_pct: Number.isFinite(fromNow[m.name]) && Number.isFinite(m.now_pct)
          ? Charts.tween(fromNow[m.name], m.now_pct, k) : m.now_pct,
      }));
      const domain = [Charts.tween(from[0], target[0], k), Charts.tween(from[1], target[1], k)];
      drawRace({ ...data, members }, domain, false);
      if (k < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function installRaceHover() {
    const svg = $('race'), tip = $('race-tip');
    if (!svg || !tip) return;
    const clear = () => {
      tip.style.display = 'none';
      svg.querySelectorAll('path.line').forEach((p) => p.classList.remove('dim'));
    };
    svg.addEventListener('mousemove', (ev) => {
      const name = ev.target && ev.target.dataset ? ev.target.dataset.name : null;
      const m = name && last ? (last.members || []).find((x) => x.name === name) : null;
      if (!m) { clear(); return; }
      svg.querySelectorAll('path.line').forEach((p) => p.classList.toggle('dim', p.dataset.name !== m.name));
      const rows = (m.holdings || []).slice(0, 8)
        .map((h) => esc(h.symbol) + ' ' + esc(CockpitFmt.money(h.value))).join('<br>');
      tip.innerHTML = '<b>' + esc(m.label + ' ' + CockpitFmt.pct(m.now_pct)) + '</b><br>'
        + (rows || '<span class="sub">holds cash</span>');
      const box = svg.parentElement.getBoundingClientRect();
      tip.style.left = (ev.clientX - box.left + 14) + 'px';
      tip.style.top = (ev.clientY - box.top + 14) + 'px';
      tip.style.display = 'block';
    });
    svg.addEventListener('mouseleave', clear);
  }

  function renderTiles(data) {
    const account = data.account || {};
    count('t-account', account.value, CockpitFmt.money);
    const spark = $('t-spark');
    if (spark) {
      const series = (account.series || []).slice(-30).map((p, i) => [i, p[1]]);
      const values = series.map((p) => p[1]).filter(Number.isFinite);
      const W = spark.clientWidth || 200, H = 44;
      spark.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      if (values.length > 1) {
        const d = Charts.linePath(series, Charts.scale(0, series.length - 1, 2, W - 2),
          Charts.scale(Math.min(...values), Math.max(...values), H - 4, 4));
        const up = values[values.length - 1] >= values[0];
        spark.innerHTML = '<path d="' + d + '" fill="none" stroke-width="2" stroke="'
          + (up ? '#35d07f' : '#ff5f6d') + '"/>';
      } else {
        spark.innerHTML = '';
      }
    }
    const trial = data.trial, ring = $('t-trial-ring'), trialNum = $('t-trial');
    if (trial && Number.isFinite(trial.day)) {
      if (trial.verdict && trial.verdict !== 'running') {
        delete shown['t-trial'];
        if (trialNum) trialNum.textContent = trial.verdict === 'keep' ? 'Keep' : 'Kill';
      } else {
        count('t-trial', Math.max(0, trial.days - trial.day), (v) => Math.ceil(v) + ' days left');
      }
      if (ring) {
        const done = Math.min(360, (trial.day / trial.days) * 360);
        ring.innerHTML = '<path d="' + Charts.arcPath(50, 50, 36, 44, 0, 360) + '" fill="#1b2431"/>'
          + (done > 0 ? '<path class="sweep" d="' + Charts.arcPath(50, 50, 36, 44, 0, done) + '" fill="#4aa8ff"/>' : '')
          + '<text x="50" y="55" text-anchor="middle" fill="#dbe4f0" font-size="14">'
          + esc('day ' + Math.floor(trial.day)) + '</text>';
      }
    } else {
      count('t-trial', NaN, String);
      if (ring) ring.innerHTML = '';
    }
    const money = $('t-money');
    if (money) {
      const labelOf = (n) => { const m = (data.members || []).find((x) => x.name === n); return m ? m.label : n; };
      const parts = CockpitFmt.moneyParts(data.allocation, labelOf);
      const key = JSON.stringify(parts);
      if (money.dataset.key !== key) {
        money.dataset.key = key;
        const arcs = Charts.arcs(parts);
        const colour = (i) => LEG_COLORS[i % LEG_COLORS.length];
        const share = (a) => a.name + ' ' + Math.round(a.value * 100) + '%';
        money.innerHTML = '<g class="sweep">' + arcs.map((a, i) => '<path d="'
          + Charts.arcPath(50, 50, 30, 46, a.start, a.end) + '" fill="' + colour(i) + '"><title>'
          + esc(share(a)) + '</title></path>').join('') + '</g>';
        const legend = $('t-money-legend');
        if (legend) {
          legend.innerHTML = arcs.map((a, i) => '<span style="color:' + colour(i) + '">●</span> '
            + esc(share(a))).join(' · ');
        }
      }
    }
  }

  function tick() {
    if (document.hidden) return;
    const el = $('t-next');
    if (el) el.textContent = Number.isFinite(nextAt) ? CockpitFmt.countdown(nextAt - Date.now()) : '—';
  }

  function renderTicker(items) {
    const tape = $('tape');
    if (!tape) return;
    const key = CockpitFmt.tickerKey(items);
    if (key === tickerSeen) return;  // rebuilding would restart the scroll
    tickerSeen = key;
    if (!items.length) {
      tape.innerHTML = '<span class="item">' + esc('No desk news yet: fills, orders and allocations will scroll here.') + '</span>';
      return;
    }
    tape.innerHTML = items.map((i) => '<span class="item ' + esc(i.kind)
      + (lastTickerAt && i.at > lastTickerAt ? ' new' : '') + '"><i class="dot"></i>'
      + esc(shortTime(i.at) + '  ' + i.text) + '</span>').join('');
    lastTickerAt = items[0].at;
    tape.style.animationDuration = Math.max(30, items.length * 6) + 's';
  }

  function renderMembers(data) {
    const box = $('members');
    if (!box) return;
    if (!data.enabled) {
      box.innerHTML = '<div class="sub">' + esc('The desk is off: this bot runs '
        + (data.strategy || 'one strategy') + ' on its own.') + '</div>';
      return;
    }
    const members = data.members || [];
    const colors = colorsFor(members);
    const bench = members.find((m) => m.is_benchmark);
    box.innerHTML = members.map((m) => {
      const gap = !m.is_benchmark && bench && Number.isFinite(m.now_pct) && Number.isFinite(bench.now_pct)
        ? ' ' + CockpitFmt.pct(m.now_pct - bench.now_pct).replace('%', ' pts') + ' vs buy-and-hold' : '';
      const need = m.samples_needed || 20;
      const fill = Math.min(100, Math.round(((m.samples || 0) / need) * 100));
      const chips = (m.holdings || []).map((h) => '<span class="chip">'
        + esc(h.symbol + ' ' + CockpitFmt.money(h.value)) + '</span>').join('')
        || '<span class="sub">holds cash</span>';
      const edge = Number.isFinite(m.t) ? m.t.toFixed(2) : 'not yet';
      const rule = m.is_benchmark ? '' : '<div class="sub">' + esc('Daily samples ' + (m.samples || 0) + ' of ' + need)
        + '</div><div class="progress"><div style="width:' + fill + '%"></div></div><div class="sub">'
        + esc('Edge score ' + edge + ' (must beat ' + Number(m.t_needed).toFixed(2) + ')') + '</div>';
      return '<div class="member" style="border-top-color:' + colors[m.name] + '"><h3>' + esc(m.label) + '</h3>'
        + '<div class="num" style="font-size:20px">' + esc(CockpitFmt.pct(m.now_pct))
        + '<span class="sub" style="font-size:12px">' + esc(gap) + '</span></div>'
        + '<div class="sub">' + esc('Capital ' + Math.round((m.weight || 0) * 100) + '% · '
        + m.entries + ' buys, ' + m.exits + ' sells') + '</div>'
        + '<div class="chips">' + chips + '</div>' + rule
        + '<div class="sub">' + esc(m.reason || '') + '</div></div>';
    }).join('');
  }

  function setLink(ok) {
    const dot = $('netdot');
    if (!dot) return;
    dot.classList.toggle('lost', !ok);
    dot.title = ok ? 'connected' : 'reconnecting…';
  }

  function render(data) {
    const story = data.error
      ? { right_now: 'The cockpit could not read the desk (' + data.error + ').' }
      : (data.story || {});
    say('say-now', story.right_now);
    say('say-money', story.money);
    say('say-just', story.just_now);
    const trial = data.trial, head = $('race-trial');
    if (head) {
      head.textContent = trial ? 'Day ' + Math.floor(trial.day) + ' of ' + trial.days + ' · '
        + trial.label + ' trial · verdict ' + trial.ends_at : '';
    }
    renderRace(data);
    renderTiles(data);
    nextAt = data.allocation ? Date.parse(data.allocation.next_at) : NaN;
    tick();
    renderTicker(data.ticker || []);
    renderMembers(data);
  }

  async function poll() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/desk');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      last = data;
      setLink(true);
      render(data);
    } catch (e) {
      setLink(false);  // keep the last good picture on screen
    }
  }

  function start() {
    installRaceHover();
    poll();
    setInterval(poll, 5000);
    setInterval(tick, 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    window.addEventListener('resize', () => { if (last) drawRace(last, raceDomain || [-1, 1], false); });
  }

  return { start, poll };
})();
"""

COCKPIT_BOOT = "Cockpit.start();\n"
```

- [ ] **Step 4: Put the cockpit in the page**

In `src/agentic_trading/dashboard_html.py`:

1. Add the import `from agentic_trading.dashboard_cockpit_js import COCKPIT, COCKPIT_BOOT`.
2. Change the assembly to:

```python
HTML = _TEMPLATE.replace("__COCKPIT_CSS__", CSS).replace(
    "__SCRIPT__", "\n".join((CHARTS, SCRIPT, COCKPIT, COCKPIT_BOOT))
)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cockpit_js.py tests/test_cockpit_page.py tests/test_console_language.py -q`

Expected: all pass (`test_cockpit_js.py`: 9 tests, 6 of which need Node).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests -q -p no:randomly`

Expected: every test passes. The count is 940 plus this plan's new tests.

- [ ] **Step 7: Commit**

```bash
git add src/agentic_trading/dashboard_cockpit_js.py src/agentic_trading/dashboard_html.py tests/test_cockpit_js.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): the cockpit: story cards, animated race, tiles, ticker, member cards"
```

### Task 7: Live acceptance, docs and delivery

**Files:**
- Modify: `README.md` (line 6 badge, line 610) and `CONTRIBUTING.md` (line 33): the test count.
- Screenshots go to the session scratchpad. They are not committed.

**Interfaces:**
- Consumes: the running `agentic-trading-dashboard.service` on `http://127.0.0.1:8787/`.
- Produces: verified screenshots, the pushed branch, and a green `windows-build` run.

- [ ] **Step 1: Restart the dashboard on the new code**

Run: `systemctl --user restart agentic-trading-dashboard.service && sleep 3 && systemctl --user is-active agentic-trading-dashboard.service && curl -s http://127.0.0.1:8787/api/desk | .venv/bin/python -m json.tool | head -40`

Expected:
- `active`
- JSON with `"enabled": true`, three members (`momentum_rotation`, `trend_crypto`, `benchmark`), a non-empty `story`, and `trial.strategy` = `momentum_rotation`.

- [ ] **Step 2: Check the Overview in a real browser (Playwright MCP)**

1. Load the tools in one call: `ToolSearch("select:mcp__plugin_playwright_playwright__browser_navigate,mcp__plugin_playwright_playwright__browser_resize,mcp__plugin_playwright_playwright__browser_take_screenshot,mcp__plugin_playwright_playwright__browser_evaluate,mcp__plugin_playwright_playwright__browser_click,mcp__plugin_playwright_playwright__browser_console_messages,mcp__plugin_playwright_playwright__browser_run_code_unsafe")`.
2. Resize to 1440×900 and navigate to `http://127.0.0.1:8787/`. Wait 3 s.
3. Evaluate:

   ```javascript
   () => ({
     scroll: document.scrollingElement.scrollHeight <= window.innerHeight + 2,
     lines: document.querySelectorAll('#race path.line').length,
     say: document.getElementById('say-now').textContent,
     ring: document.querySelectorAll('#t-money path').length,
     tape: document.querySelectorAll('#tape .item').length,
     next: document.getElementById('t-next').textContent,
   })
   ```

   Expected:
   - `scroll: true` (the overview fits without scrolling)
   - `lines: 3`
   - `say` is a sentence (not `Reading the desk…`)
   - `ring >= 2`
   - `tape >= 1`
   - `next` looks like `4d 1h 46m`
4. Take a screenshot `cockpit-overview.png`.
5. Read the browser console messages. Expected: no errors.

If `scroll` is false, reduce `.cockpit`'s `height:calc(100vh - 150px)` in `dashboard_css.py` by the overflow the page reports. Then re-run Task 5's tests and repeat this step.

- [ ] **Step 3: Check every tab (Review Focus 5: canvases on hidden tabs)**

1. Click each tab button (`Strategies`, `Orders`, `Health`). Wait 2.5 s after each click and take a screenshot (`cockpit-strategies.png`, `cockpit-orders.png`, `cockpit-health.png`).
2. On Orders, evaluate `() => document.getElementById('chart').width`. Expected: greater than 300 (drawn at the card's width, not 0).
3. On Strategies, evaluate `() => document.querySelectorAll('#members .member').length`. Expected: `3`.
4. Click `Overview` again and reload. Expected: the page reopens on the tab saved last (Overview).

- [ ] **Step 4: Check the failure path (Review Focus 4)**

With `browser_run_code_unsafe`, abort `/api/desk`, wait past one poll, and confirm the picture stayed:

```javascript
async (page) => {
  const before = await page.textContent('#say-now');
  await page.route('**/api/desk', (route) => route.abort());
  await page.waitForTimeout(6500);
  const after = await page.textContent('#say-now');
  const lost = await page.getAttribute('#netdot', 'class');
  await page.unroute('**/api/desk');
  await page.waitForTimeout(6000);
  const back = await page.getAttribute('#netdot', 'class');
  return { kept: before === after, lost, back };
}
```

Expected:
- `kept: true`
- `lost` contains `lost`
- `back` is `netdot` (no `lost`)

- [ ] **Step 5: Update the documented test count and run everything**

1. Run `.venv/bin/python -m pytest tests -q -p no:randomly 2>&1 | tail -1` and note N (the passed count).
2. Replace the old count with N in `README.md` line 6 (`tests-<old>%20passing`) and line 610 (`<old> tests`), and in `CONTRIBUTING.md` line 33 (`# <old> tests`).
3. Run `.venv/bin/python tools/check_doc_counts.py`. Expected: `docs agree with the suite: N tests`.

- [ ] **Step 6: Commit, push and run the Windows build**

```bash
git add README.md CONTRIBUTING.md
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "docs: the suite count after the cockpit"
git push origin feat/momentum-rotation-trial
gh workflow run windows-build.yml --repo Hkshoonya/agentic-trader --ref feat/momentum-rotation-trial
```

Watch it: find the run with `gh run list --repo Hkshoonya/agentic-trader --workflow windows-build.yml --branch feat/momentum-rotation-trial --limit 1 --json databaseId -q '.[0].databaseId'`, then run `gh run watch <id> --repo Hkshoonya/agentic-trader --exit-status`.

Expected: success. The smoke test fetches `/api/summary` from the frozen dashboard, which must still answer.

---

## Self-Review (done while writing)

**Spec coverage:**

| Spec item | Task |
|---|---|
| Top bar with mode, stage, health dot, kill badge, arm control | 5 (the link dot is `#netdot`; the existing badges are kept) |
| Story cards with crossfade | 2 (sentences), 6 (`say`) |
| Race: lines, dashed benchmark, zero line, end labels, pulsing leader, trial heading, hover holdings, draw-in, glide, empty state | 6 |
| Tiles: account and sparkline, trial ring, money ring with legs fallback, next-allocation countdown, count-up tween | 6 |
| Ticker: scrolling, newest flash, hover pause | 5 (CSS), 6 |
| Strategies tab member cards with samples bar, t against `min_t`, reason, weight | 1 (data), 6 (cards) |
| Existing cards moved into tabs, restyled | 5 |
| `/api/desk` schema, rules, allocation history, trial, ticker events, story rules, non-desk fallback, degrade on bad state | 1, 2, 3 |
| Refresh cadence and pause while hidden | 5 (existing timers), 6 (desk poll) |
| Module split | 4, 5, 6 |
| Failure handling (keep last picture, reconnecting dot, skip NaN) | 4 (`linePath`), 6, 7 |
| Testing: Python, Node, page, Playwright | 1–7 |

**Adjustments made:**
- The spec's non-desk "race the account against the benchmark" is replaced. No account-equity history exists to race, so Task 1 Step 6 updates the spec: the story cards and trial are still shown, and the race explains itself.
- The spec's "Order submission" card becomes the header arm control, as the spec's own parenthetical says.

**Placeholder scan:** no TBD or TODO. Every code step shows its code.

**Name consistency:**
- `build_desk_view(config, events, *, now)`, `DeskEventCache.read(*, days)`, `ticker_item`, `story(..., symbols=, now=)`, `CHARTS`, `CSS`, `COCKPIT`, `COCKPIT_BOOT`, `Charts.*`, `CockpitFmt.*`, `Cockpit.start` are used identically across tasks.
- The element ids listed in Task 5 are exactly the ones Task 6 draws into, which Task 6's wiring test checks.

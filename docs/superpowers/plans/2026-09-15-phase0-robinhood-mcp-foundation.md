# Phase 0: Robinhood MCP Foundation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a local Python foundation that authenticates to Robinhood Trading MCP, snapshots tools, enforces RiskGuard, journals decisions, and runs shadow-mode end-to-end (review/simulate only — never place) using local quotes and a fixture strategy.

**Architecture:** `src/agentic_trading` package with clear layers — types/config → journal → RiskGuard → MCP client/broker facade → runtime/CLI. Strategies emit `OrderIntent` only. Phase 0 uses a `FixtureStrategy` for E2E; the existing `paper_scalper.py` stays untouched for offline replay (Phase 1 wires it as a plugin).

**Tech Stack:** Python 3.11+, stdlib + `httpx` (HTTP), no pytest plugins required (unittest or pytest), TOML/JSON config, Streamable HTTP MCP JSON-RPC, OAuth 2.1 + PKCE.

**Spec:** `docs/superpowers/specs/2026-09-15-agentic-robinhood-mcp-design.md`

---

## File structure (create)

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps (`httpx`), console script |
| `src/agentic_trading/__init__.py` | Package version |
| `src/agentic_trading/__main__.py` | `python -m agentic_trading` |
| `src/agentic_trading/types.py` | `OrderIntent`, `Side`, guard result types |
| `src/agentic_trading/config.py` | Load/validate runtime config (mode, caps, paths) |
| `src/agentic_trading/journal.py` | Append-only JSONL + idempotency lookup |
| `src/agentic_trading/risk.py` | `RiskGuard` |
| `src/agentic_trading/rh_mcp/client.py` | Streamable HTTP JSON-RPC + auth header |
| `src/agentic_trading/rh_mcp/oauth.py` | OAuth 2.1 PKCE (desktop browser flow) |
| `src/agentic_trading/rh_mcp/snapshot.py` | `tools/list` → dated snapshot file |
| `src/agentic_trading/broker.py` | Capability map + `get_equity` / `review_order` / `place_order` |
| `src/agentic_trading/strategies/fixture.py` | Emits canned intents for shadow soak/tests |
| `src/agentic_trading/quotes.py` | Read existing JSONL quote format |
| `src/agentic_trading/runtime.py` | Shadow loop orchestration |
| `src/agentic_trading/cli.py` | `run`, `status`, `flip-mode`, `reset-kill-switch`, `auth` |
| `config/agentic.example.toml` | Checked-in example (no secrets) |
| `tests/fixtures/tools_snapshot.json` | Fake `tools/list` for unit tests |
| `tests/test_types_config.py` | Intent/config validation |
| `tests/test_journal.py` | Journal append + idempotency |
| `tests/test_risk.py` | RiskGuard cases from spec |
| `tests/test_broker.py` | Adapter + fake MCP |
| `tests/test_runtime_shadow.py` | Shadow E2E, asserts zero place calls |
| `tests/fakes.py` | `FakeMcpClient` |

**Do not modify in Phase 0 (except maybe `.gitignore`):** `paper_scalper.py`, `reporting.py`, existing paper tests (must still pass).

**Update:** `.gitignore` — add `data/tools_snapshot*.json`, `data/journal/`, `.venv/`, token paths if under project, `config/agentic.toml` if operators copy secrets there.

---

### Task 1: Scaffold package + ignore secrets

**Files:**
- Create: `pyproject.toml`
- Create: `src/agentic_trading/__init__.py`
- Create: `src/agentic_trading/__main__.py`
- Modify: `.gitignore`
- Create: `config/agentic.example.toml`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "agentic-trading"
version = "0.1.0"
description = "Local Robinhood MCP trading agent (shadow-first)"
readme = "README.md"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
agentic-trading = "agentic_trading.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: Create package stubs**

```python
# src/agentic_trading/__init__.py
__version__ = "0.1.0"
```

```python
# src/agentic_trading/__main__.py
# cli.main is implemented in Task 7; stub until then so imports work.
def _stub_main(argv=None):
    raise SystemExit("CLI not implemented yet — continue Phase 0 tasks")

if __name__ == "__main__":
    try:
        from agentic_trading.cli import main
    except ImportError:
        main = _stub_main
    raise SystemExit(main())
```

- [ ] **Step 3: Extend `.gitignore`**

Append:

```
.venv/
config/agentic.toml
data/tools_snapshot*.json
data/journal/
data/state/
*.egg-info/
dist/
build/
.pytest_cache/
```

- [ ] **Step 4: Write example config**

```toml
# config/agentic.example.toml
mode = "shadow"  # shadow | live
symbol_whitelist = ["SPY"]
max_order_pct = "0.05"
daily_notional_pct = "0.20"
daily_loss_pct = "0.03"
max_open_positions = 1
equity_refresh_ticks = 30
equity_refresh_seconds = 60
timezone = "local"
quotes_path = "data/spy_quotes.jsonl"
journal_dir = "data/journal"
state_dir = "data/state"
tools_snapshot_path = "data/tools_snapshot.json"
token_path = "~/.config/agentic-trading/tokens.json"
mcp_url = "https://agent.robinhood.com/mcp/trading"
# capability names filled after first tools/list; leave empty to auto-map by heuristics + confirm
```

- [ ] **Step 5: Install editable + commit**

```bash
python3 -m pip install -e ".[dev]"
python3 -c "import agentic_trading; print(agentic_trading.__version__)"
```

Expected: `0.1.0`

```bash
git add pyproject.toml src/agentic_trading/__init__.py src/agentic_trading/__main__.py .gitignore config/agentic.example.toml
git commit -m "$(cat <<'EOF'
Scaffold agentic_trading package for Phase 0 foundation.

EOF
)"
```

---

### Task 2: Types + config loader (TDD)

**Files:**
- Create: `src/agentic_trading/types.py`
- Create: `src/agentic_trading/config.py`
- Create: `tests/test_types_config.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_types_config.py
from decimal import Decimal
from datetime import datetime, timezone
import unittest

from agentic_trading.types import OrderIntent, Side
from agentic_trading.config import load_config


class OrderIntentTests(unittest.TestCase):
    def test_notional_from_quantity_and_ref_price(self):
        intent = OrderIntent(
            decision_id="d1",
            symbol="spy",
            side=Side.BUY,
            quantity=Decimal("2"),
            ref_price=Decimal("100"),
            reason="test",
            created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(intent.symbol, "SPY")
        self.assertEqual(intent.resolved_notional(), Decimal("200"))

    def test_rejects_missing_notional_inputs(self):
        with self.assertRaises(ValueError):
            OrderIntent(
                decision_id="d1",
                symbol="SPY",
                side=Side.BUY,
                reason="x",
                created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
            )


class ConfigTests(unittest.TestCase):
    def test_load_example_defaults_shadow(self):
        cfg = load_config("config/agentic.example.toml")
        self.assertEqual(cfg.mode, "shadow")
        self.assertEqual(cfg.max_order_pct, Decimal("0.05"))
        self.assertEqual(cfg.symbol_whitelist, frozenset({"SPY"}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
python3 -m pytest tests/test_types_config.py -v
```

Expected: import / not found errors

- [ ] **Step 3: Implement `types.py` and `config.py`**

```python
# src/agentic_trading/types.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderIntent:
    decision_id: str
    symbol: str
    side: Side
    reason: str
    created_at: datetime
    notional_usd: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    ref_price: Optional[Decimal] = None
    metadata: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        if not self.decision_id:
            raise ValueError("decision_id required")
        # force notional resolution at construction
        self.resolved_notional()

    def resolved_notional(self) -> Decimal:
        if self.notional_usd is not None:
            n = Decimal(str(self.notional_usd))
        elif self.quantity is not None and self.ref_price is not None:
            n = Decimal(str(self.quantity)) * Decimal(str(self.ref_price))
        else:
            raise ValueError("need notional_usd or quantity+ref_price")
        if n <= 0 or not n.is_finite():
            raise ValueError("notional must be positive and finite")
        return n


def new_decision_id() -> str:
    return str(uuid4())
```

```python
# src/agentic_trading/config.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    mode: str
    symbol_whitelist: frozenset[str]
    max_order_pct: Decimal
    daily_notional_pct: Decimal
    daily_loss_pct: Decimal
    max_open_positions: int
    equity_refresh_ticks: int
    equity_refresh_seconds: int
    timezone: str
    quotes_path: Path
    journal_dir: Path
    state_dir: Path
    tools_snapshot_path: Path
    token_path: Path
    mcp_url: str

    def __post_init__(self) -> None:
        if self.mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow|live")


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    return Config(
        mode=raw["mode"],
        symbol_whitelist=frozenset(s.upper() for s in raw["symbol_whitelist"]),
        max_order_pct=Decimal(str(raw["max_order_pct"])),
        daily_notional_pct=Decimal(str(raw["daily_notional_pct"])),
        daily_loss_pct=Decimal(str(raw["daily_loss_pct"])),
        max_open_positions=int(raw["max_open_positions"]),
        equity_refresh_ticks=int(raw["equity_refresh_ticks"]),
        equity_refresh_seconds=int(raw["equity_refresh_seconds"]),
        timezone=str(raw.get("timezone", "local")),
        quotes_path=Path(raw["quotes_path"]),
        journal_dir=Path(raw["journal_dir"]),
        state_dir=Path(raw["state_dir"]),
        tools_snapshot_path=Path(raw["tools_snapshot_path"]),
        token_path=Path(raw["token_path"]).expanduser(),
        mcp_url=str(raw["mcp_url"]),
    )
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
python3 -m pytest tests/test_types_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/types.py src/agentic_trading/config.py tests/test_types_config.py
git commit -m "$(cat <<'EOF'
Add OrderIntent and runtime config loader.

EOF
)"
```

---

### Task 3: Decision journal

**Files:**
- Create: `src/agentic_trading/journal.py`
- Create: `tests/test_journal.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_journal.py
from pathlib import Path
import tempfile
import unittest

from agentic_trading.journal import DecisionJournal


class JournalTests(unittest.TestCase):
    def test_append_and_has_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            j.append({"decision_id": "abc", "event": "accepted"})
            self.assertTrue(j.has_decision("abc"))
            self.assertFalse(j.has_decision("nope"))
            lines = list(j.iter_today())
            self.assertEqual(len(lines), 1)
```

- [ ] **Step 2: Run — expect FAIL**

```bash
python3 -m pytest tests/test_journal.py -v
```

- [ ] **Step 3: Implement journal**

Use one JSONL file per local date: `journal_dir/YYYY-MM-DD.jsonl`. `append` writes one JSON object per line. `has_decision` scans today's file for matching `decision_id`. `iter_today` yields parsed dicts.

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/journal.py tests/test_journal.py
git commit -m "$(cat <<'EOF'
Add append-only decision journal with idempotency lookup.

EOF
)"
```

---

### Task 4: RiskGuard (core safety — thorough TDD)

**Files:**
- Create: `src/agentic_trading/risk.py`
- Create: `tests/test_risk.py`

Implement per spec: whitelist, notional caps, entry-only max positions, closes exempt from position cap but not from notional/kill/whitelist, shadow blocks place, kill-switch, short sells rejected, positions-read failure refuses entries.

- [ ] **Step 1: Write failing tests covering spec cases**

```python
# tests/test_risk.py
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from agentic_trading.risk import RiskGuard, PortfolioSnapshot, GuardDecision
from agentic_trading.types import OrderIntent, Side


def intent(side=Side.BUY, symbol="SPY", notional="50", decision_id="d1"):
    return OrderIntent(
        decision_id=decision_id,
        symbol=symbol,
        side=side,
        notional_usd=Decimal(notional),
        reason="t",
        created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )


class RiskGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = RiskGuard(
            mode="shadow",
            whitelist=frozenset({"SPY"}),
            max_order_pct=Decimal("0.05"),
            daily_notional_pct=Decimal("0.20"),
            daily_loss_pct=Decimal("0.03"),
            max_open_positions=1,
            baseline_equity=Decimal("1000"),
            current_equity=Decimal("1000"),
        )

    def test_rejects_unknown_symbol(self):
        d = self.guard.evaluate(intent(symbol="AAPL"), PortfolioSnapshot(open_positions=0, held={"SPY": Decimal("0")}))
        self.assertFalse(d.allowed)

    def test_rejects_over_max_order(self):
        # 5% of 1000 = 50; 51 should fail
        d = self.guard.evaluate(intent(notional="51"), PortfolioSnapshot(0, {}))
        self.assertFalse(d.allowed)

    def test_blocks_entry_when_max_positions_allows_close(self):
        snap = PortfolioSnapshot(open_positions=1, held={"SPY": Decimal("1")})
        entry = self.guard.evaluate(intent(side=Side.BUY, decision_id="e"), snap)
        close = self.guard.evaluate(intent(side=Side.SELL, notional="40", decision_id="c"), snap)
        self.assertFalse(entry.allowed)
        self.assertTrue(close.allowed)

    def test_rejects_sell_that_would_short(self):
        snap = PortfolioSnapshot(open_positions=0, held={})
        d = self.guard.evaluate(intent(side=Side.SELL, notional="10"), snap)
        self.assertFalse(d.allowed)

    def test_kill_switch_blocks_all_writes(self):
        self.guard.trip_kill_switch("loss")
        d = self.guard.evaluate(intent(), PortfolioSnapshot(0, {}))
        self.assertFalse(d.allowed)
        self.guard.reset_kill_switch()
        d2 = self.guard.evaluate(intent(decision_id="d2"), PortfolioSnapshot(0, {}))
        self.assertTrue(d2.allowed)

    def test_shadow_marks_would_place_not_place(self):
        d = self.guard.evaluate(intent(), PortfolioSnapshot(0, {}))
        self.assertTrue(d.allowed)
        self.assertTrue(d.would_place)
        self.assertFalse(d.may_place)

    def test_live_may_place(self):
        g = RiskGuard(
            mode="live",
            whitelist=frozenset({"SPY"}),
            max_order_pct=Decimal("0.05"),
            daily_notional_pct=Decimal("0.20"),
            daily_loss_pct=Decimal("0.03"),
            max_open_positions=1,
            baseline_equity=Decimal("1000"),
            current_equity=Decimal("1000"),
        )
        d = g.evaluate(intent(), PortfolioSnapshot(0, {}))
        self.assertTrue(d.may_place)

    def test_positions_read_failed_blocks_entry_not_verified_close(self):
        snap = PortfolioSnapshot(open_positions=0, held={}, positions_read_failed=True)
        entry = self.guard.evaluate(intent(decision_id="e"), snap)
        self.assertFalse(entry.allowed)
```

Expand tests as needed for daily notional accumulation and shadow realized-loss kill.

- [ ] **Step 2: Run — expect FAIL**

```bash
python3 -m pytest tests/test_risk.py -v
```

- [ ] **Step 3: Implement `RiskGuard`**

Key API:

```python
@dataclass
class PortfolioSnapshot:
    open_positions: int
    held: dict[str, Decimal]  # symbol -> quantity
    positions_read_failed: bool = False

@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    would_place: bool
    may_place: bool  # True only in live + allowed
    notional: Decimal

@dataclass
class ShadowBook:
    """Local would-place book for mode=shadow. Rebuild from today's journal on start."""
    held: dict[str, Decimal]
    realized_pnl: Decimal

    @classmethod
    def from_journal(cls, journal) -> "ShadowBook":
        """Replay today's accepted would-place buy/sell events into held + realized_pnl."""
        ...

    def apply_accepted(self, intent: OrderIntent) -> None:
        """Update held on buy; on sell reduce held and add realized_pnl if cost basis known.
        Phase 0 may track average cost on buys for realized PnL on sells.
        """
        ...

    def as_snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(open_positions=len([q for q in self.held.values() if q > 0]), held=dict(self.held))

class RiskGuard:
    def evaluate(self, intent: OrderIntent, snapshot: PortfolioSnapshot) -> GuardDecision: ...
    def record_accepted(self, intent: OrderIntent) -> None: ...  # daily notional
    def update_equity(self, equity: Decimal) -> None: ...
    def note_shadow_realized(self, realized_pnl: Decimal) -> None:
        """Shadow kill: if realized_pnl <= -daily_loss_pct * baseline_equity, trip kill."""
        ...
    def trip_kill_switch(self, reason: str) -> None: ...
    def reset_kill_switch(self) -> None: ...
    def persist(self, state_dir: Path) -> None: ...
    def load(self, state_dir: Path) -> None: ...
```

Closing sell verification: `side=SELL` requires `held.get(symbol, 0) > 0`. Prefer quantity on intent when present; if only notional, treat as close attempt only when held > 0 (Phase 0 fixture must supply quantity on sells).

**Shadow book ownership (Phase 0):** `ShadowBook` lives beside RiskGuard (same module or `shadow_book.py`). Runtime in shadow mode:
1. On start: `book = ShadowBook.from_journal(journal)` for today
2. On each evaluate: pass `book.as_snapshot()` (not broker positions)
3. On accepted would-place: `book.apply_accepted(intent)` then `guard.note_shadow_realized(book.realized_pnl)`
4. Live mode: ignore ShadowBook for position counts; use `broker.get_positions()`

Add RiskGuard tests:
- `test_shadow_book_rebuild_blocks_second_entry`
- `test_shadow_realized_loss_trips_kill`

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/risk.py tests/test_risk.py
git commit -m "$(cat <<'EOF'
Implement RiskGuard with equity caps and entry/close rules.

EOF
)"
```

---

### Task 5: Fake MCP + broker facade + snapshot

**Files:**
- Create: `tests/fakes.py`
- Create: `tests/fixtures/tools_snapshot.json`
- Create: `src/agentic_trading/rh_mcp/__init__.py`
- Create: `src/agentic_trading/rh_mcp/snapshot.py`
- Create: `src/agentic_trading/broker.py`
- Create: `tests/test_broker.py`

- [ ] **Step 1: Write fixture snapshot**

Minimal tools array including at least:

- `review_equity_order` (inputSchema with symbol, side, notional or quantity fields — keep flexible)
- `get_account` (or similarly named) returning equity
- `place_equity_order` (write — must never be called in shadow tests)

Exact argument names in the fixture are placeholders; broker maps logical ops → tool names from snapshot.

- [ ] **Step 2: Write failing broker tests**

```python
def test_review_calls_review_tool_not_place():
    fake = FakeMcpClient(snapshot)
    broker = Broker(fake, capability_map)
    broker.review_order(symbol="SPY", side="buy", notional_usd="50")
    assert fake.calls[0].name == "review_equity_order"
    assert all(c.name != "place_equity_order" for c in fake.calls)

def test_broker_place_is_callable_but_recorded():
    # Broker.place_order exists for live; it must NOT refuse based on mode (runtime gates).
    fake = FakeMcpClient(snapshot)
    broker = Broker(fake, capability_map)
    broker.place_order(symbol="SPY", side="buy", notional_usd="50")
    assert any(c.name == "place_equity_order" for c in fake.calls)
```
# Shadow zero-place assertion lives in tests/test_runtime_shadow.py (Task 7), not in Broker.


- [ ] **Step 3: Implement `FakeMcpClient`, snapshot writer, `Broker`**

```python
class Broker:
    def __init__(self, client, tools: list[dict], capability_map: dict[str, str] | None = None): ...
    def get_equity(self) -> Decimal: ...
    def get_positions(self) -> PortfolioSnapshot: ...
    def review_order(self, **kwargs) -> dict: ...
    def place_order(self, **kwargs) -> dict: ...  # exists for live; runtime gates
```

Capability auto-map heuristics (Phase 0): prefer exact names `review_equity_order`, then first tool whose name contains `review` and `equity`; similarly for `place` and `account`/`equity`/`portfolio`. Persist chosen map next to snapshot.

- [ ] **Step 4: Tests PASS**

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/rh_mcp tests/fakes.py tests/fixtures tests/test_broker.py src/agentic_trading/broker.py
git commit -m "$(cat <<'EOF'
Add MCP tool snapshot handling and broker facade with fakes.

EOF
)"
```

---

### Task 6: Quotes reader + fixture strategy

**Files:**
- Create: `src/agentic_trading/quotes.py`
- Create: `src/agentic_trading/strategies/__init__.py`
- Create: `src/agentic_trading/strategies/fixture.py`
- Create: `tests/test_quotes_fixture.py`

- [ ] **Step 1: Quotes reader** — parse existing JSONL lines (same fields as `paper_scalper` / README). Skip malformed lines; yield dicts with Decimal bid/ask.

- [ ] **Step 2: `FixtureStrategy`** — given a list of intents (or emit buy then sell based on simple counter), `on_quote(quote) -> list[OrderIntent]` (0 or 1). Must set `ref_price`/`quantity` or `notional_usd`.

- [ ] **Step 3: Tests** — read first line of `data/spy_quotes.jsonl` if present; fixture emits valid intents.

- [ ] **Step 4: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add JSONL quote reader and fixture strategy for shadow soak.

EOF
)"
```

---

### Task 7: Runtime shadow loop + CLI

**Files:**
- Create: `src/agentic_trading/runtime.py`
- Create: `src/agentic_trading/cli.py`
- Create: `tests/test_runtime_shadow.py`

- [ ] **Step 1: Shadow runtime algorithm**

```
load config (force or default mode=shadow)
open journal, load RiskGuard state
connect broker: tests inject FakeMcpClient; CLI uses real MCP client only if token_path exists and is readable, else fail with clear "run auth" (CI always injects fake)
snapshot tools at start
equity = broker.get_equity(); set baseline if needed
for each quote in feed:
  maybe refresh equity
  update kill from equity/shadow PnL rules
  intents = strategy.on_quote(quote)
  for intent in intents:
    if journal.has_decision(intent.decision_id): skip
    if mode == shadow:
      snap = shadow_book.as_snapshot()
    else:
      snap = broker.get_positions()
    decision = guard.evaluate(intent, snap)
    if not decision.allowed: journal reject; continue
    review = broker.review_order(...)
    journal {would_place:true, review, mode:shadow, ...}
    guard.record_accepted(intent)
    if mode == shadow:
      shadow_book.apply_accepted(intent)
      guard.note_shadow_realized(shadow_book.realized_pnl)
    elif decision.may_place and os.environ.get("AGENTIC_ALLOW_LIVE") == "1":
      broker.place_order(...)
    # NEVER call place_order when mode==shadow
handle SIGINT: stop after current intent
```

- [ ] **Step 2: CLI commands**

- `agentic-trading auth` — run PKCE flow, save tokens (manual / optional in CI)
- `agentic-trading run --config config/agentic.toml` — shadow loop
- `agentic-trading status` — print mode, kill, daily notional, equity baseline
- `agentic-trading flip-mode shadow|live` — persist mode in `state_dir/mode` (live flip allowed but runtime must still refuse place until Phase 1+ operator checklist; **Phase 0 runtime refuses `place_order` unless `mode==live` AND env `AGENTIC_ALLOW_LIVE=1`**)
- `agentic-trading reset-kill-switch`

- [ ] **Step 3: Integration test**

Use `FakeMcpClient` + fixture strategy + temp journal. Assert:

- ≥1 journal line with `would_place: true`
- `place_equity_order` call count == 0
- existing paper tests still pass: `python3 -m pytest tests/test_paper_scalper.py tests/test_reporting_cli.py -v` (or unittest discovery as currently used)

- [ ] **Step 4: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add shadow runtime loop and operator CLI.

EOF
)"
```

---

### Task 8: Real MCP client + OAuth (manual path; CI stays fake)

**Files:**
- Create: `src/agentic_trading/rh_mcp/client.py`
- Create: `src/agentic_trading/rh_mcp/oauth.py`
- Modify: `src/agentic_trading/cli.py` (`auth`, `snapshot-tools`)
- Create: `docs/superpowers/plans/manual-oauth-checklist.md` (short)

- [ ] **Step 1: Implement Streamable HTTP JSON-RPC client**

Methods: `initialize`, `tools/list`, `tools/call`. Send `Authorization: Bearer <token>`. On 401: refresh once via oauth module; else raise `AuthFailed`.

Verify OAuth URLs at implement time against Robinhood docs / discovery (spec open point). Candidate endpoints from public toolkit notes (confirm before hardcoding):

- authorize: `https://robinhood.com/oauth`
- token: `https://api.robinhood.com/oauth2/token/`
- register: `https://agent.robinhood.com/oauth/trading/register`
- PKCE: S256

- [ ] **Step 2: `auth` CLI** — open browser / print URL, local redirect listener or paste code, store tokens mode `0600`.

- [ ] **Step 3: `snapshot-tools` CLI** — authenticated `tools/list` → `data/tools_snapshot.json` (+ dated copy).

- [ ] **Step 4: No CI test hits network. Document manual checklist.**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add Robinhood MCP HTTP client and desktop OAuth PKCE flow.

EOF
)"
```

---

### Task 9: README Phase 0 section + final verification

**Files:**
- Modify: `README.md` (add Phase 0 section; keep paper trading section intact)
- Update: `.gitignore` if any new paths appeared

- [ ] **Step 1: Document**

- Install: `pip install -e ".[dev]"`
- Copy `config/agentic.example.toml` → `config/agentic.toml`
- Paper mode unchanged
- Shadow: `agentic-trading run --config config/agentic.toml`
- Auth + snapshot-tools for real MCP
- Explicit: default is shadow; live requires mode flip **and** `AGENTIC_ALLOW_LIVE=1`
- Risk / disclosure blurb

- [ ] **Step 2: Run full test suite**

```bash
python3 -m pytest tests/ -v
python3 paper_scalper.py --quotes data/spy_quotes.jsonl --config config.json --output /tmp/agentic-paper-check
```

Expected: all pytest green; paper experiment still runs.

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
Document Phase 0 shadow foundation usage in README.

EOF
)"
```

---

## Out of scope (do not implement in this plan)

- Porting `paper_scalper.py` into `strategies/spy_scalper.py` (Phase 1)
- LLM multi-asset strategy (Phase 2)
- Calling `place_order` in default CI or default CLI path without dual live gates

## Success criteria checklist (from spec)

- [ ] `tools/list` snapshot path works (fake in tests; real via CLI)
- [ ] Shadow run journals `would_place` + review; zero place calls
- [ ] RiskGuard tests: over-cap, wrong-symbol, kill-switch, missing-notional, entry blocked / close allowed
- [ ] Existing paper scalper tests pass
- [ ] CLI: status, flip-mode, reset-kill-switch
)

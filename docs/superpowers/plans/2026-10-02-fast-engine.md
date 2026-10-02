# Fast Engine (Switchboard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A crypto "switchboard" runs inside the venues service on live Alpaca prices. It reads each coin's market every minute, switches between three playbooks, and trades a paper book at honest Alpaca costs, with a Coinbase comparison book alongside. The strategy desk judges it weekly but never funds it.

**Architecture:**
- **Where it runs:** a new package, `agentic_trading.fast`, holds pure pieces (bars, regime, playbooks, fills, mirror, switchboard). A `FastEngine` wraps them and subscribes to the venues `TickBus` inside `run_venues`.
- **What it writes:**
  - its member book to `data/state/desk/switchboard.json`;
  - its engine state and Coinbase mirror to `data/state/fast/`;
  - a status file, `data/state/fast.json`, every second;
  - its journal to `data/journal/fast-<date>.jsonl`.
- **How the desk sees it:** the trader's desk reads the book through a `ReadOnlyBook` / `ReadOnlyMember` and holds its weight at 0 (`UNFUNDED`).
- **Other ways in:** `fast replay` runs the same `Switchboard` over recorded ticks or historical bars. The console shows it through `/api/fast`, a Switchboard card and ticker lines.

**Tech Stack:** Python 3.12, asyncio, `Decimal` money, alpaca-py `CryptoHistoricalDataClient` (bar replay only), hand-built dashboard JS/CSS (inline, CSP-safe), unittest under pytest with the network tripwire.

**Spec:** `docs/superpowers/specs/2026-10-02-fast-engine-design.md`

## Global Constraints

- **Commits:** authored only as Hkshoonya: `git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit …`. No AI mention, no co-author trailers.
- **Never stage** `data/bars/*`, `data/stream/*`, `config/agentic.toml*` or `config/secrets.toml`.
- **No real orders:** the switchboard never calls `Venue.submit`.
- **No network in tests:** no test reaches the network (the tripwire in `tests/conftest.py` stays on). Bar fetching takes an injected client.
- **Defaults:**
  - Alpaca fee `0.0025` per side; Coinbase fee `0.006` per side.
  - Fill delay `250` ms; cost gate `3` × round-trip cost; `3` positions maximum; cooldown `5` minutes; daily loss stop `0.03`.
  - Symbols `BTC/USD`, `ETH/USD`, `SOL/USD`.
- **Regime thresholds:**
  - Efficiency ratio ≥ `0.35` is trending; ≤ `0.20` is choppy. The ratio is measured over `30` bars.
  - Fewer than `60` bars means warming.
  - Squeeze: realized volatility in the bottom `20%` of the last 24 h, with at least `240` bars of history.
  - Precedence: squeeze, then trending, then choppy.
- **Playbooks:**
  - **Breakout:** above the 30-bar high; trailing stop 1.5 × ATR(30); expected move 2 × ATR.
  - **Pullback:** close above EMA60, low at or below EMA20, price above the last bar's high; target the 60-bar high; stop at the dip low − 0.25 × ATR.
  - **Squeeze break:** above the 60-bar range high; stop at the range midpoint; expected move the range height.
- **Fees validation:** a fee outside 0–0.02 is refused at startup.
- **Unfunded:** `UNFUNDED = frozenset({"switchboard"})` is a code constant. There is no config switch.
- **Run tests with** `.venv/bin/python -m pytest …`. The venv has no pip; use `uv pip install --python .venv/bin/python …`.

## Review Focus

1. **A restart between the book save and the engine save** leaves a holding with no saved trade, or a trade with no holding. The holding must still be managed (it gets a ±2% stop and target) and the phantom trade dropped. Test: Task 6.
2. **Late or out-of-order ticks** (an exchange time older than the open minute) must fold into the open bar, never create a past bar. Test: Task 2.
3. **A price that gaps through a stop** must fill at the real bid, not at the stop price. Test: Task 5.
4. **A crossed or one-sided quote** (ask < bid, or a missing side) must never be used for a fill or for the cost gate. Test: Tasks 4 and 5.
5. **A book too small for the $1 minimum** (cash tied up in positions) must report an unfilled entry, not a phantom trade. Test: Tasks 4 and 5.

## File Map

| File | Status | Responsibility |
|---|---|---|
| `src/agentic_trading/fast/__init__.py` | create | package docstring |
| `src/agentic_trading/fast/settings.py` | create | `FastConfig`, `load_fast_config` |
| `src/agentic_trading/fast/costs.py` | create | `FeeOnlyCosts` for `MemberBook` |
| `src/agentic_trading/fast/bars.py` | create | `Bar`, `MinuteBars` |
| `src/agentic_trading/fast/regime.py` | create | `read_regime` and constants |
| `src/agentic_trading/fast/playbooks.py` | create | `Plan`, `Trade`, `atr`, `ema`, the entries, `exit_reason`, `ENTRIES` |
| `src/agentic_trading/fast/fills.py` | create | `Order`, `Fill`, `usable_quote`, `PaperFiller` |
| `src/agentic_trading/fast/mirror.py` | create | `CoinbaseMirror` |
| `src/agentic_trading/fast/switchboard.py` | create | `Event`, `tick_price`, `Switchboard` |
| `src/agentic_trading/fast/store.py` | create | `FastStore` |
| `src/agentic_trading/fast/service.py` | create | `FastJournal`, `FastEngine`, `fast_problem` |
| `src/agentic_trading/fast/replay.py` | create | `Report`, `replay`, `recorded_ticks`, `bar_ticks`, `fetch_bars`, `median_spread` |
| `src/agentic_trading/fast/cli.py` | create | `fast replay` / `fast status` |
| `src/agentic_trading/venues/daemon.py` | modify | `run_venues(engine=…)`, `fast_loop`, `main_run` builds the engine |
| `src/agentic_trading/desk/book.py` | modify | `ReadOnlyBook` |
| `src/agentic_trading/desk/member.py` | modify | `ReadOnlyMember` |
| `src/agentic_trading/desk/allocator.py` | modify | `UNFUNDED`, `hold_unfunded` |
| `src/agentic_trading/desk/desk.py` | modify | refresh read-only books; hold unfunded weights; `would_earn` |
| `src/agentic_trading/cli.py` | modify | `build_desk` switchboard branch; `fast` parser and route |
| `src/agentic_trading/config.py` | modify | `"switchboard"` in `DESK_MEMBER_CHOICES` |
| `src/agentic_trading/dashboard_fast.py` | create | `fast_view` |
| `src/agentic_trading/dashboard.py` | modify | `DashboardState.fast()`, `/api/fast` |
| `src/agentic_trading/dashboard_desk.py` | modify | label, fast ticker events, read `fast-*.jsonl`, order by time |
| `src/agentic_trading/dashboard_html.py`, `dashboard_css.py`, `dashboard_cockpit_js.py` | modify | Switchboard card |
| `tests/fast_support.py` | create | shared tick and bar builders |
| `tests/test_fast_*.py` | create | the tests per task |

---

### Task 1: Settings and the fee-only cost model

**Files:**
- Create: `src/agentic_trading/fast/__init__.py`, `src/agentic_trading/fast/settings.py`, `src/agentic_trading/fast/costs.py`
- Test: `tests/test_fast_settings.py`

**Interfaces:**
- Produces:
  - `FastConfig` (frozen dataclass). Fields: `enabled: bool`, `symbols: tuple[str, ...]`, `alpaca_fee: Decimal`, `coinbase_fee: Decimal`, `fill_delay_ms: int`, `cost_gate_multiple: Decimal`, `max_positions: int`, `cooldown_minutes: float`, `daily_loss_stop: Decimal`.
  - `load_fast_config(path) -> FastConfig`.
  - `FeeOnlyCosts(per_side_fee: Decimal)` with `.for_symbol(symbol)`, which returns an object with `per_side_bps` and `fee_per_order`.

- [ ] **Step 1: Write the failing test** — `tests/test_fast_settings.py`:

```python
"""[fast] settings are strict, and the fee-only cost model charges only the fee."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.costs import FeeOnlyCosts
from agentic_trading.fast.settings import FastConfig, load_fast_config


def _load(text: str) -> FastConfig:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "agentic.toml"
        path.write_text(text, encoding="utf-8")
        return load_fast_config(path)


class SettingsTests(unittest.TestCase):
    def test_off_by_default_with_the_spec_defaults(self) -> None:
        config = _load("")
        self.assertFalse(config.enabled)
        self.assertEqual(config.symbols, ("BTC/USD", "ETH/USD", "SOL/USD"))
        self.assertEqual((config.alpaca_fee, config.coinbase_fee), (Decimal("0.0025"), Decimal("0.006")))
        self.assertEqual((config.fill_delay_ms, config.max_positions), (250, 3))
        self.assertEqual((config.cost_gate_multiple, config.daily_loss_stop), (Decimal("3"), Decimal("0.03")))
        self.assertEqual(config.cooldown_minutes, 5.0)

    def test_a_full_table_parses(self) -> None:
        config = _load('[fast]\nenabled = true\nsymbols = ["btc/usd"]\nalpaca_fee = "0.0015"\n'
                       'fill_delay_ms = 500\nmax_positions = 1\ncooldown_minutes = 2\n')
        self.assertTrue(config.enabled)
        self.assertEqual(config.symbols, ("BTC/USD",))
        self.assertEqual(config.alpaca_fee, Decimal("0.0015"))
        self.assertEqual((config.fill_delay_ms, config.max_positions, config.cooldown_minutes), (500, 1, 2.0))

    def test_mistakes_are_refused_with_the_key_named(self) -> None:
        cases = {
            '[fast]\nenabeld = true\n': "unknown keys",
            '[fast]\nenabled = "false"\n': "fast.enabled",
            '[fast]\nalpaca_fee = "0.05"\n': "fast.alpaca_fee",
            '[fast]\ncoinbase_fee = "-0.001"\n': "fast.coinbase_fee",
            '[fast]\nsymbols = ["BTC-USD"]\n': "fast.symbols",
            '[fast]\nsymbols = []\n': "fast.symbols",
            '[fast]\nmax_positions = 0\n': "fast.max_positions",
            '[fast]\nfill_delay_ms = "250"\n': "fast.fill_delay_ms",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, needle):
                    _load(text)


class FeeOnlyCostsTests(unittest.TestCase):
    def test_a_book_pays_the_fee_and_nothing_else(self) -> None:
        book = MemberBook("switchboard", starting_equity=Decimal("50"))
        costs = FeeOnlyCosts(Decimal("0.0025"))
        quantity = book.buy("BTC/USD", Decimal("10"), Decimal("100"), costs)
        self.assertAlmostEqual(float(quantity), 10 / (100 * 1.0025), places=5)  # rounded down to 6 dp
        self.assertEqual(book.cash, Decimal("50") - quantity * Decimal("100") * Decimal("1.0025"))
        self.assertEqual(costs.for_symbol("ETH/USD").fee_per_order, Decimal("0"))
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_settings.py -q`
Expected: collection ERROR, `ModuleNotFoundError: No module named 'agentic_trading.fast'`.

- [ ] **Step 3: Implement**

`src/agentic_trading/fast/__init__.py`:

```python
"""The fast engine: a crypto switchboard trading a paper book on live ticks."""
```

`src/agentic_trading/fast/settings.py`:

```python
"""The ``[fast]`` table of ``agentic.toml``: the switchboard's settings.

Off unless ``enabled = true``. As in ``[venues]``, a misspelled key is an
error. Booleans must be real TOML booleans, because ``enabled = "false"`` (a
string) would otherwise read as true.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_CRYPTO = re.compile(r"^[A-Z]{2,10}/USD$")
MAX_FEE = Decimal("0.02")


@dataclass(frozen=True)
class FastConfig:
    enabled: bool = False
    symbols: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")
    alpaca_fee: Decimal = Decimal("0.0025")
    coinbase_fee: Decimal = Decimal("0.006")
    fill_delay_ms: int = 250
    cost_gate_multiple: Decimal = Decimal("3")
    max_positions: int = 3
    cooldown_minutes: float = 5.0
    daily_loss_stop: Decimal = Decimal("0.03")


def _decimal(name: str, value: Any, low: Decimal, high: Decimal) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"fast.{name} must be a number") from None
    if not number.is_finite() or not low <= number <= high:
        raise ValueError(f"fast.{name} must be between {low} and {high}")
    return number


def _whole(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"fast.{name} must be a whole number from {low} to {high}")
    return value


def load_fast_config(path: Path | str) -> FastConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("fast") or {}
    if not isinstance(raw, dict):
        raise ValueError("[fast] must be a table")
    known = {f.name for f in fields(FastConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"[fast] has unknown keys {unknown}; known: {sorted(known)}")
    values: dict[str, Any] = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError("fast.enabled must be true or false, without quotes")
        values["enabled"] = raw["enabled"]
    if "symbols" in raw:
        symbols = raw["symbols"]
        if not isinstance(symbols, list) or not symbols:
            raise ValueError("fast.symbols must be a non-empty list")
        cleaned = tuple(str(item).strip().upper() for item in symbols)
        bad = [s for s in cleaned if not _CRYPTO.match(s)]
        if bad:
            raise ValueError(f"fast.symbols must look like BTC/USD: {bad}")
        values["symbols"] = cleaned
    for key in ("alpaca_fee", "coinbase_fee"):
        if key in raw:
            values[key] = _decimal(key, raw[key], Decimal("0"), MAX_FEE)
    if "cost_gate_multiple" in raw:
        values["cost_gate_multiple"] = _decimal(
            "cost_gate_multiple", raw["cost_gate_multiple"], Decimal("1"), Decimal("20"))
    if "daily_loss_stop" in raw:
        values["daily_loss_stop"] = _decimal(
            "daily_loss_stop", raw["daily_loss_stop"], Decimal("0.001"), Decimal("0.5"))
    if "fill_delay_ms" in raw:
        values["fill_delay_ms"] = _whole("fill_delay_ms", raw["fill_delay_ms"], 0, 5000)
    if "max_positions" in raw:
        values["max_positions"] = _whole("max_positions", raw["max_positions"], 1, 10)
    if "cooldown_minutes" in raw:
        minutes = raw["cooldown_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 <= minutes <= 1440:
            raise ValueError("fast.cooldown_minutes must be from 0 to 1440")
        values["cooldown_minutes"] = float(minutes)
    return FastConfig(**values)
```

`src/agentic_trading/fast/costs.py`:

```python
"""A ``MemberBook`` cost model that charges only a venue's fee.

The switchboard fills at the real ask (buys) or bid (sells), so the spread is
already in the price. The book must add the fee and nothing else; the desk's
usual cost model would charge an estimated spread a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class _FeeModel:
    per_side_bps: Decimal
    fee_per_order: Decimal = Decimal("0")


class FeeOnlyCosts:
    def __init__(self, per_side_fee: Decimal) -> None:
        self._model = _FeeModel(Decimal(str(per_side_fee)) * Decimal("10000"))

    def for_symbol(self, symbol: str) -> _FeeModel:
        return self._model
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_settings.py -q`
Expected: `4 passed` (and 8 subtests passed).

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/__init__.py src/agentic_trading/fast/settings.py src/agentic_trading/fast/costs.py tests/test_fast_settings.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): strict [fast] settings and a fee-only cost model"
```

---

### Task 2: Minute bars and the market reading

**Files:**
- Create: `src/agentic_trading/fast/bars.py`, `src/agentic_trading/fast/regime.py`, `tests/fast_support.py`
- Test: `tests/test_fast_regime.py`

**Interfaces:**
- Produces:
  - `Bar(minute: datetime, open: float, high: float, low: float, close: float)`, a mutable dataclass.
  - `MinuteBars(keep=1440)`, whose `.add(symbol, price: float, at: datetime) -> Optional[Bar]` returns the bar the minute just completed. Also `.closed(symbol) -> list[Bar]`.
  - Constants `WARMING`, `TRENDING`, `SQUEEZE`, `CHOPPY`, `UNCLEAR` and `STAND_ASIDE = {WARMING, CHOPPY, UNCLEAR}`.
  - `efficiency_ratio(closes)`, `realized_vol(closes)`, and `read_regime(bars) -> str`.
  - `tests/fast_support.py`, holding `T0`, `quote(symbol, bid, ask, at, venue="alpaca")`, `trade(symbol, price, at)` and `bars_from(closes, start=T0, wick=0.0)`.

- [ ] **Step 1: Write the failing test.** First the shared builders, `tests/fast_support.py`:

```python
"""Shared builders for the fast-engine tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agentic_trading.fast.bars import Bar
from agentic_trading.venues.model import Tick

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # a Monday


def quote(symbol: str, bid, ask, at: datetime, *, venue: str = "alpaca") -> Tick:
    return Tick(venue, symbol, Decimal(str(bid)), Decimal(str(ask)), None, None, at, at)


def trade(symbol: str, price, at: datetime, *, venue: str = "alpaca") -> Tick:
    return Tick(venue, symbol, None, None, Decimal(str(price)), Decimal("0.01"), at, at)


def bars_from(closes, *, start: datetime = T0, wick: float = 0.0) -> list[Bar]:
    """One bar per close; each opens at the previous close."""
    out, previous = [], float(closes[0])
    for i, close in enumerate(closes):
        close = float(close)
        out.append(Bar(start + timedelta(minutes=i), previous,
                       max(previous, close) + wick, min(previous, close) - wick, close))
        previous = close
    return out
```

`tests/test_fast_regime.py`:

```python
"""Ticks become minute bars; bars become one of five market readings."""

from __future__ import annotations

import unittest
from datetime import timedelta

from agentic_trading.fast.bars import MinuteBars
from agentic_trading.fast.regime import (
    CHOPPY, SQUEEZE, TRENDING, UNCLEAR, WARMING, efficiency_ratio, read_regime,
)
from tests.fast_support import T0, bars_from


class BarTests(unittest.TestCase):
    def test_a_minute_rollover_returns_the_completed_bar(self) -> None:
        bars = MinuteBars()
        for seconds, price in ((5, 100.0), (30, 101.0), (50, 99.0)):
            self.assertIsNone(bars.add("BTC/USD", price, T0 + timedelta(seconds=seconds)))
        done = bars.add("BTC/USD", 100.5, T0 + timedelta(minutes=1, seconds=2))
        self.assertEqual((done.minute, done.open, done.high, done.low, done.close), (T0, 100.0, 101.0, 99.0, 99.0))
        self.assertEqual(len(bars.closed("BTC/USD")), 1)

    def test_a_late_tick_folds_into_the_open_bar(self) -> None:
        bars = MinuteBars()
        bars.add("BTC/USD", 100.0, T0 + timedelta(minutes=1, seconds=1))
        self.assertIsNone(bars.add("BTC/USD", 105.0, T0 + timedelta(seconds=59)))  # older minute
        self.assertEqual(bars.closed("BTC/USD"), [])
        done = bars.add("BTC/USD", 101.0, T0 + timedelta(minutes=2))
        self.assertEqual((done.minute, done.high), (T0 + timedelta(minutes=1), 105.0))

    def test_only_the_newest_bars_are_kept(self) -> None:
        bars = MinuteBars(keep=3)
        for i in range(6):
            bars.add("ETH/USD", 10.0 + i, T0 + timedelta(minutes=i))
        self.assertEqual([b.close for b in bars.closed("ETH/USD")], [12.0, 13.0, 14.0])


class RegimeTests(unittest.TestCase):
    def test_efficiency_ratio(self) -> None:
        self.assertEqual(efficiency_ratio([1, 2, 3, 4]), 1.0)
        self.assertEqual(efficiency_ratio([1, 2, 1, 2, 1]), 0.0)
        self.assertEqual(efficiency_ratio([5]), 0.0)

    def test_too_little_history_is_warming(self) -> None:
        self.assertEqual(read_regime(bars_from([100] * 59)), WARMING)

    def test_a_steady_climb_is_trending(self) -> None:
        self.assertEqual(read_regime(bars_from([100 + i for i in range(100)])), TRENDING)

    def test_back_and_forth_is_choppy(self) -> None:
        self.assertEqual(read_regime(bars_from([100 + (i % 2) for i in range(100)])), CHOPPY)

    def test_two_up_one_down_is_unclear(self) -> None:
        closes, price = [], 100.0
        for i in range(100):
            price += 2 if i % 2 == 0 else -1
            closes.append(price)
        self.assertEqual(read_regime(bars_from(closes)), UNCLEAR)  # ER = 15/45 = 0.33

    def test_calm_after_a_wild_day_is_a_squeeze_even_when_choppy(self) -> None:
        wild = [100 + 2 * (i % 2) for i in range(260)]
        calm = [100 + 0.01 * (i % 2) for i in range(40)]
        self.assertEqual(read_regime(bars_from(wild + calm)), SQUEEZE)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_regime.py -q`
Expected: collection ERROR, `No module named 'agentic_trading.fast.bars'`.

- [ ] **Step 3: Implement.** First `src/agentic_trading/fast/bars.py`:

```python
"""One-minute OHLC bars per symbol, built from tick prices.

Bars are keyed on the exchange's time. A late tick (its minute is older than the
open bar's) folds into the open bar; it never creates a past bar. A minute
with no ticks has no bar; gaps are not filled.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Bar:
    minute: datetime
    open: float
    high: float
    low: float
    close: float


class MinuteBars:
    def __init__(self, keep: int = 1440) -> None:
        self.keep = keep
        self._closed: dict[str, deque[Bar]] = {}
        self._open: dict[str, Bar] = {}

    def add(self, symbol: str, price: float, at: datetime) -> Optional[Bar]:
        minute = at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        current = self._open.get(symbol)
        if current is None:
            self._open[symbol] = Bar(minute, price, price, price, price)
            return None
        if minute <= current.minute:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
            return None
        self._closed.setdefault(symbol, deque(maxlen=self.keep)).append(current)
        self._open[symbol] = Bar(minute, price, price, price, price)
        return current

    def closed(self, symbol: str) -> list[Bar]:
        return list(self._closed.get(symbol, ()))
```

`src/agentic_trading/fast/regime.py`:

```python
"""Each coin's market, read once a minute: trending, squeeze, choppy or unclear.

* **efficiency ratio** (Kaufman): net move / total path over 30 bars.
  At least 0.35 is trending; at most 0.20 is choppy.
* **squeeze**: the 30-bar realized volatility sits in the bottom 20% of the
  day's rolling values (needs at least 240 bars of history).
* Precedence: squeeze, then trending, then choppy; anything else is unclear.
  Fewer than 60 bars is warming.

Choppy, unclear and warming all mean "stand aside". In chop, a ~0.5% round-trip
cost eats every small win.
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

from agentic_trading.fast.bars import Bar

WARMING, TRENDING, SQUEEZE, CHOPPY, UNCLEAR = "warming", "trending", "squeeze", "choppy", "unclear"
STAND_ASIDE = frozenset({WARMING, CHOPPY, UNCLEAR})
ER_BARS = 30
ER_TREND = 0.35
ER_CHOP = 0.20
MIN_BARS = 60
SQUEEZE_HISTORY = 240
SQUEEZE_SHARE = 0.20
VOL_STEP = 10


def efficiency_ratio(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        return 0.0
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return 0.0 if path == 0 else abs(closes[-1] - closes[0]) / path


def realized_vol(closes: Sequence[float]) -> float:
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return statistics.pstdev(returns) if len(returns) >= 2 else 0.0


def read_regime(bars: Sequence[Bar]) -> str:
    if len(bars) < MIN_BARS:
        return WARMING
    closes = [bar.close for bar in bars]
    window = ER_BARS + 1  # 30 moves need 31 closes
    if len(closes) >= SQUEEZE_HISTORY:
        now = realized_vol(closes[-window:])
        history = [realized_vol(closes[i - window:i]) for i in range(len(closes), window - 1, -VOL_STEP)]
        if now > 0 and sum(1 for v in history if v < now) / len(history) < SQUEEZE_SHARE:
            return SQUEEZE
    ratio = efficiency_ratio(closes[-window:])
    if ratio >= ER_TREND:
        return TRENDING
    if ratio <= ER_CHOP:
        return CHOPPY
    return UNCLEAR
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_regime.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/bars.py src/agentic_trading/fast/regime.py tests/fast_support.py tests/test_fast_regime.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): minute bars and the per-coin market reading"
```

---

### Task 3: The three playbooks and their exits

**Files:**
- Create: `src/agentic_trading/fast/playbooks.py`
- Test: `tests/test_fast_playbooks.py`

**Interfaces:**
- Consumes: `Bar` (Task 2); the `TRENDING` and `SQUEEZE` constants.
- Produces:
  - `Plan(playbook: str, stop: float, target: Optional[float], expected_move: float)`, frozen.
  - `Trade(symbol, playbook, entry: float, stop: float, target: Optional[float], quantity: Decimal, opened_at: datetime, high: float)`, mutable, with `.to_dict()` and `Trade.from_dict(raw)`.
  - `atr(bars, n=30) -> float` and `ema(values, n) -> float`.
  - `breakout_entry`, `pullback_entry` and `squeeze_entry`, each `(bars, price) -> Optional[Plan]`.
  - `exit_reason(trade, bars, price) -> Optional[str]`.
  - Constants `BREAKOUT`, `PULLBACK`, `SQUEEZE_BREAK`, `RECOVERED`, and `ENTRIES: dict[str, tuple[tuple[str, Callable], ...]]`.

- [ ] **Step 1: Write the failing test** — `tests/test_fast_playbooks.py`:

```python
"""Each playbook's entry, stop and target, from scripted bars."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agentic_trading.fast.playbooks import (
    BREAKOUT, ENTRIES, PULLBACK, RECOVERED, SQUEEZE_BREAK, Trade, atr, breakout_entry, ema,
    exit_reason, pullback_entry, squeeze_entry,
)
from agentic_trading.fast.regime import SQUEEZE, TRENDING
from tests.fast_support import T0, bars_from


def _trade(playbook, entry, stop, target=None):
    return Trade("BTC/USD", playbook, entry, stop, target, Decimal("0.1"), T0, entry)


class IndicatorTests(unittest.TestCase):
    def test_atr_and_ema(self) -> None:
        self.assertEqual(atr(bars_from([100, 104] * 20)), 4.0)
        self.assertEqual(atr(bars_from([100])), 0.0)
        self.assertEqual(ema([5.0] * 10, 3), 5.0)
        self.assertGreater(ema([float(i) for i in range(50)], 5), ema([float(i) for i in range(50)], 20))


class EntryTests(unittest.TestCase):
    def test_breakout_needs_a_new_30_bar_high(self) -> None:
        bars = bars_from([100, 104] * 20)
        self.assertIsNone(breakout_entry(bars, 103.9))
        plan = breakout_entry(bars, 105.0)
        self.assertEqual(plan.playbook, BREAKOUT)
        self.assertAlmostEqual(plan.stop, 105.0 - 1.5 * 4.0)
        self.assertIsNone(plan.target)
        self.assertAlmostEqual(plan.expected_move, 8.0)
        self.assertIsNone(breakout_entry(bars[:20], 200.0))  # too little history

    def test_pullback_buys_the_turn_after_a_dip_to_the_fast_average(self) -> None:
        closes = [100.0 + i for i in range(79)] + [174.0]
        fast = ema(closes, 20)
        bars = bars_from(closes, wick=0.5)
        bars[-1].low, bars[-1].high = fast - 0.1, 175.0
        self.assertIsNone(pullback_entry(bars, 174.5))  # not yet above the dip bar's high
        plan = pullback_entry(bars, 175.5)
        self.assertEqual(plan.playbook, PULLBACK)
        self.assertAlmostEqual(plan.target, 178.5)  # the 60-bar high (close 178 + wick)
        self.assertLess(plan.stop, bars[-1].low)
        self.assertAlmostEqual(plan.expected_move, 178.5 - 175.5)

    def test_squeeze_break_targets_the_range_height(self) -> None:
        bars = bars_from([100.0 + 0.1 * (i % 2) for i in range(60)])
        self.assertIsNone(squeeze_entry(bars, 100.05))
        plan = squeeze_entry(bars, 100.2)
        self.assertEqual(plan.playbook, SQUEEZE_BREAK)
        self.assertAlmostEqual(plan.stop, 100.05)
        self.assertAlmostEqual(plan.expected_move, 0.1)
        self.assertAlmostEqual(plan.target, 100.3)

    def test_the_playbooks_per_regime(self) -> None:
        self.assertEqual([name for name, _ in ENTRIES[TRENDING]], [BREAKOUT, PULLBACK])
        self.assertEqual([name for name, _ in ENTRIES[SQUEEZE]], [SQUEEZE_BREAK])


class ExitTests(unittest.TestCase):
    def test_the_breakout_stop_trails_up_and_never_down(self) -> None:
        bars = bars_from([100, 104] * 20)  # ATR 4 -> trail 6
        trade = _trade(BREAKOUT, 100.0, 94.0)
        self.assertIsNone(exit_reason(trade, bars, 110.0))
        self.assertAlmostEqual(trade.stop, 104.0)
        self.assertIsNone(exit_reason(trade, bars, 105.0))
        self.assertAlmostEqual(trade.stop, 104.0)  # unchanged on the way down
        self.assertEqual(exit_reason(trade, bars, 103.9), "trailing stop")

    def test_fixed_stop_and_target_playbooks(self) -> None:
        for playbook in (PULLBACK, SQUEEZE_BREAK, RECOVERED):
            with self.subTest(playbook=playbook):
                trade = _trade(playbook, 100.0, 98.0, 103.0)
                self.assertIsNone(exit_reason(trade, [], 101.0))
                self.assertEqual(exit_reason(trade, [], 103.0), "target")
                self.assertEqual(exit_reason(_trade(playbook, 100.0, 98.0, 103.0), [], 97.5), "stop")

    def test_a_trade_round_trips_through_a_dict(self) -> None:
        trade = _trade(PULLBACK, 100.0, 98.0, 103.0)
        self.assertEqual(Trade.from_dict(trade.to_dict()), trade)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_playbooks.py -q`
Expected: collection ERROR, `No module named 'agentic_trading.fast.playbooks'`.

- [ ] **Step 3: Implement** `src/agentic_trading/fast/playbooks.py`:

```python
"""Three long-only playbooks: what to buy, where to get out.

* **breakout** (trending): price clears the 30-bar high. It exits on a
  trailing stop 1.5 x ATR under the highest price since entry.
* **pullback** (trending): a dip to the 20-bar EMA while the close holds above
  the 60-bar EMA, bought when price clears the dip bar's high. It targets the
  60-bar high, with a stop under the dip.
* **squeeze break** (squeeze): price clears a quiet 60-bar range. The stop is
  the range middle and the target one range height higher.

``expected_move`` feeds the switchboard's cost gate. A trade stays with the
playbook that opened it until that playbook's exit fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from agentic_trading.fast.bars import Bar
from agentic_trading.fast.regime import SQUEEZE, TRENDING

BREAKOUT, PULLBACK, SQUEEZE_BREAK, RECOVERED = "breakout", "pullback", "squeeze_break", "recovered"
ATR_BARS = 30
BREAKOUT_BARS = 30
RANGE_BARS = 60
TRAIL_ATR = 1.5
BREAKOUT_MOVE_ATR = 2.0
PULLBACK_STOP_ATR = 0.25


@dataclass(frozen=True)
class Plan:
    playbook: str
    stop: float
    target: Optional[float]
    expected_move: float


@dataclass
class Trade:
    symbol: str
    playbook: str
    entry: float
    stop: float
    target: Optional[float]
    quantity: Decimal
    opened_at: datetime
    high: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "playbook": self.playbook, "entry": self.entry,
            "stop": self.stop, "target": self.target, "quantity": str(self.quantity),
            "opened_at": self.opened_at.isoformat(), "high": self.high,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Trade":
        return cls(
            str(raw["symbol"]), str(raw["playbook"]), float(raw["entry"]), float(raw["stop"]),
            None if raw.get("target") is None else float(raw["target"]),
            Decimal(str(raw["quantity"])), datetime.fromisoformat(str(raw["opened_at"])),
            float(raw["high"]),
        )


def atr(bars: Sequence[Bar], n: int = ATR_BARS) -> float:
    window = list(bars)[-(n + 1):]
    if len(window) < 2:
        return 0.0
    ranges = [
        max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close))
        for prior, bar in zip(window, window[1:])
    ]
    return sum(ranges) / len(ranges)


def ema(values: Sequence[float], n: int) -> float:
    k = 2.0 / (n + 1)
    average = float(values[0])
    for value in values[1:]:
        average = float(value) * k + average * (1 - k)
    return average


def breakout_entry(bars: Sequence[Bar], price: float) -> Optional[Plan]:
    if len(bars) < BREAKOUT_BARS:
        return None
    level = max(bar.high for bar in bars[-BREAKOUT_BARS:])
    size = atr(bars)
    if price <= level or size <= 0:
        return None
    return Plan(BREAKOUT, price - TRAIL_ATR * size, None, BREAKOUT_MOVE_ATR * size)


def pullback_entry(bars: Sequence[Bar], price: float) -> Optional[Plan]:
    if len(bars) < RANGE_BARS:
        return None
    closes = [bar.close for bar in bars[-240:]]
    last = bars[-1]
    if last.close <= ema(closes, 60) or last.low > ema(closes, 20) or price <= last.high:
        return None
    target = max(bar.high for bar in bars[-RANGE_BARS:])
    stop = last.low - PULLBACK_STOP_ATR * atr(bars)
    if target <= price or stop >= price:
        return None
    return Plan(PULLBACK, stop, target, target - price)


def squeeze_entry(bars: Sequence[Bar], price: float) -> Optional[Plan]:
    if len(bars) < RANGE_BARS:
        return None
    window = bars[-RANGE_BARS:]
    high, low = max(bar.high for bar in window), min(bar.low for bar in window)
    if price <= high or high <= low:
        return None
    height = high - low
    return Plan(SQUEEZE_BREAK, (high + low) / 2, price + height, height)


ENTRIES: dict[str, tuple[tuple[str, Callable[[Sequence[Bar], float], Optional[Plan]]], ...]] = {
    TRENDING: ((BREAKOUT, breakout_entry), (PULLBACK, pullback_entry)),
    SQUEEZE: ((SQUEEZE_BREAK, squeeze_entry),),
}


def exit_reason(trade: Trade, bars: Sequence[Bar], price: float) -> Optional[str]:
    """Why the trade should close at ``price`` now, or None. Ratchets trailing stops."""
    trade.high = max(trade.high, price)
    if trade.playbook == BREAKOUT:
        size = atr(bars)
        if size > 0:
            trade.stop = max(trade.stop, trade.high - TRAIL_ATR * size)
        return "trailing stop" if price <= trade.stop else None
    if price <= trade.stop:
        return "stop"
    if trade.target is not None and price >= trade.target:
        return "target"
    return None
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_playbooks.py -q`
Expected: `8 passed` (and 3 subtests passed).

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/playbooks.py tests/test_fast_playbooks.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): breakout, pullback and squeeze-break playbooks with their exits"
```

---

### Task 4: Honest paper fills and the Coinbase mirror

**Files:**
- Create: `src/agentic_trading/fast/fills.py`, `src/agentic_trading/fast/mirror.py`
- Test: `tests/test_fast_fills.py`

**Interfaces:**
- Consumes:
  - `FeeOnlyCosts` (Task 1).
  - `MemberBook` (`desk/book.py`): `buy(symbol, notional, price, costs, minimum)`, `sell(symbol, quantity, price, costs)`, `mark(prices, stamp)`, `.positions`, `.cash`, `.equity`.
  - `Tick` (`venues/model.py`).
- Produces:
  - `usable_quote(tick) -> bool` (both sides present, positive, not crossed).
  - `Order(symbol, side, decided_at, notional=Decimal(0), quantity=Decimal(0))`.
  - `Fill(symbol, side, price: Decimal, quantity: Decimal, at: datetime)`.
  - `PaperFiller(book, fee, *, delay, minimum=Decimal("1"))` with `.submit(order)` and `.on_tick(tick) -> list[Fill]`. A fill may have `quantity == 0`, meaning the order could not fill.
  - `CoinbaseMirror(book, fee, *, max_age=timedelta(seconds=5))` with `.on_tick(tick)`, `.mark(symbol, fallback_price, now)`, `.copy(fill, now) -> bool`, `.unpriced: int`, `.book`, and the static `product(symbol) -> str`.

- [ ] **Step 1: Write the failing test** — `tests/test_fast_fills.py`:

```python
"""Paper fills use the real ask/bid after the delay; the mirror re-prices at Coinbase."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.fills import Fill, Order, PaperFiller, usable_quote
from agentic_trading.fast.mirror import CoinbaseMirror
from tests.fast_support import T0, quote, trade

D = Decimal
MS = timedelta(milliseconds=1)


def _filler(cash="50"):
    book = MemberBook("switchboard", starting_equity=D(cash))
    return book, PaperFiller(book, D("0.0025"), delay=250 * MS)


class FillerTests(unittest.TestCase):
    def test_a_buy_fills_at_the_first_ask_after_the_delay(self) -> None:
        book, filler = _filler()
        filler.submit(Order("BTC/USD", "buy", T0, notional=D("10")))
        self.assertEqual(filler.on_tick(quote("BTC/USD", 99.9, 100, T0 + 100 * MS)), [])
        self.assertEqual(filler.on_tick(quote("ETH/USD", 9, 10, T0 + 300 * MS)), [])  # other coin
        [fill] = filler.on_tick(quote("BTC/USD", 100.9, 101, T0 + 300 * MS))
        self.assertEqual((fill.side, fill.price), ("buy", D("101")))
        self.assertAlmostEqual(float(fill.quantity), 10 / (101 * 1.0025), places=5)  # rounded down to 6 dp
        self.assertEqual(book.cash, D("50") - fill.quantity * D("101") * D("1.0025"))

    def test_a_sell_gets_the_bid_less_the_fee(self) -> None:
        book, filler = _filler()
        book.buy("BTC/USD", D("10"), D("100"), filler.costs)
        held, before = book.positions["BTC/USD"], book.cash
        filler.submit(Order("BTC/USD", "sell", T0, quantity=held))
        [fill] = filler.on_tick(quote("BTC/USD", 110, 110.2, T0 + 300 * MS))
        self.assertEqual((fill.price, fill.quantity), (D("110"), held))
        self.assertNotIn("BTC/USD", book.positions)
        self.assertEqual(book.cash, before + held * D("110") * D("0.9975"))

    def test_trades_and_crossed_quotes_never_fill(self) -> None:
        _, filler = _filler()
        filler.submit(Order("BTC/USD", "buy", T0, notional=D("10")))
        self.assertEqual(filler.on_tick(trade("BTC/USD", 100, T0 + 300 * MS)), [])
        self.assertEqual(filler.on_tick(quote("BTC/USD", 101, 100, T0 + 400 * MS)), [])
        self.assertFalse(usable_quote(quote("BTC/USD", 101, 100, T0)))
        self.assertTrue(usable_quote(quote("BTC/USD", 100, 100, T0)))
        self.assertEqual(len(filler.on_tick(quote("BTC/USD", 99.9, 100, T0 + 500 * MS))), 1)

    def test_an_order_under_the_minimum_reports_an_empty_fill(self) -> None:
        book, filler = _filler(cash="0.5")
        filler.submit(Order("BTC/USD", "buy", T0, notional=D("0.5")))
        [fill] = filler.on_tick(quote("BTC/USD", 99.9, 100, T0 + 300 * MS))
        self.assertEqual(fill.quantity, D("0"))
        self.assertEqual(book.positions, {})


class MirrorTests(unittest.TestCase):
    def _mirror(self):
        return CoinbaseMirror(MemberBook("switchboard@coinbase", starting_equity=D("50")), D("0.006"))

    def test_a_buy_and_a_sell_are_copied_at_coinbase_prices(self) -> None:
        mirror = self._mirror()
        self.assertEqual(CoinbaseMirror.product("BTC/USD"), "BTC-USD")
        mirror.on_tick(quote("BTC-USD", 100.0, 100.5, T0, venue="coinbase"))
        self.assertTrue(mirror.copy(Fill("BTC/USD", "buy", D("100"), D("0.1"), T0), T0 + timedelta(seconds=1)))
        held = mirror.book.positions["BTC/USD"]
        self.assertAlmostEqual(float(held), 10 / (100.5 * 1.006), places=5)
        mirror.on_tick(quote("BTC-USD", 110.0, 110.4, T0 + timedelta(seconds=9), venue="coinbase"))
        self.assertTrue(mirror.copy(Fill("BTC/USD", "sell", D("110"), D("0.1"), T0), T0 + timedelta(seconds=10)))
        self.assertEqual(mirror.book.positions, {})
        self.assertEqual(mirror.unpriced, 0)

    def test_a_missing_or_old_quote_is_unpriced(self) -> None:
        mirror = self._mirror()
        buy = Fill("BTC/USD", "buy", D("100"), D("0.1"), T0)
        self.assertFalse(mirror.copy(buy, T0))
        mirror.on_tick(quote("BTC-USD", 100.0, 100.5, T0, venue="coinbase"))
        self.assertFalse(mirror.copy(buy, T0 + timedelta(seconds=6)))
        self.assertEqual(mirror.unpriced, 2)
        # a sell with nothing copied to sell is not counted twice
        self.assertFalse(mirror.copy(Fill("BTC/USD", "sell", D("100"), D("0.1"), T0), T0))
        self.assertEqual(mirror.unpriced, 2)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_fills.py -q`
Expected: collection ERROR, `No module named 'agentic_trading.fast.fills'`.

- [ ] **Step 3: Implement.** First `src/agentic_trading/fast/fills.py`:

```python
"""Paper fills that do not flatter the strategy.

A decision fills at the first usable quote that arrives at least ``delay``
(250 ms) after it, which models an order's trip to the venue. Buys pay the ask
and sells get the bid, plus the venue fee. A trade print, or a crossed or
one-sided quote, never fills. An order the book cannot afford (under the $1
minimum) comes back as a fill of quantity 0, so the caller knows it failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from agentic_trading.desk.book import MIN_NOTIONAL, MemberBook
from agentic_trading.fast.costs import FeeOnlyCosts
from agentic_trading.venues.model import Tick

ZERO = Decimal("0")


def usable_quote(tick: Tick) -> bool:
    return (tick.bid is not None and tick.ask is not None
            and tick.bid > 0 and tick.ask > 0 and tick.ask >= tick.bid)


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # "buy" or "sell"
    decided_at: datetime
    notional: Decimal = ZERO
    quantity: Decimal = ZERO


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    at: datetime


class PaperFiller:
    def __init__(self, book: MemberBook, fee: Decimal, *, delay: timedelta,
                 minimum: Decimal = MIN_NOTIONAL) -> None:
        self.book = book
        self.costs = FeeOnlyCosts(fee)
        self.delay = delay
        self.minimum = minimum
        self.pending: list[Order] = []

    def submit(self, order: Order) -> None:
        self.pending.append(order)

    def on_tick(self, tick: Tick) -> list[Fill]:
        if not usable_quote(tick):
            return []
        fills: list[Fill] = []
        waiting: list[Order] = []
        for order in self.pending:
            if order.symbol != tick.symbol or tick.received_at - order.decided_at < self.delay:
                waiting.append(order)
                continue
            if order.side == "buy":
                price = tick.ask
                quantity = self.book.buy(order.symbol, order.notional, price, self.costs, self.minimum)
            else:
                price = tick.bid
                quantity = self.book.sell(order.symbol, order.quantity, price, self.costs)
            fills.append(Fill(order.symbol, order.side, price, quantity, tick.received_at))
        self.pending = waiting
        return fills
```

`src/agentic_trading/fast/mirror.py`:

```python
"""The same trades, priced at Coinbase: a comparison book the desk never judges.

Each Alpaca-book fill is copied at Coinbase's bid or ask at that moment, with
Coinbase's fee. A copy is **unpriced** (counted, not guessed) when Coinbase has
no quote or its quote is older than ``max_age``, or when the book cannot
afford the buy.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.costs import FeeOnlyCosts
from agentic_trading.fast.fills import Fill, usable_quote
from agentic_trading.venues.model import Tick

ZERO = Decimal("0")


class CoinbaseMirror:
    def __init__(self, book: MemberBook, fee: Decimal, *, max_age: timedelta = timedelta(seconds=5)) -> None:
        self.book = book
        self.costs = FeeOnlyCosts(fee)
        self.max_age = max_age
        self.quotes: dict[str, Tick] = {}
        self.unpriced = 0

    @staticmethod
    def product(symbol: str) -> str:
        return symbol.replace("/", "-")

    def on_tick(self, tick: Tick) -> None:
        if usable_quote(tick):
            self.quotes[tick.symbol] = tick

    def mark(self, symbol: str, fallback: Decimal, now: datetime) -> None:
        quote = self.quotes.get(self.product(symbol))
        price = (quote.bid + quote.ask) / 2 if quote is not None else fallback
        self.book.mark({symbol: price}, now)

    def copy(self, fill: Fill, now: datetime) -> bool:
        if fill.side == "sell" and self.book.positions.get(fill.symbol.upper(), ZERO) <= 0:
            return False  # its buy was never copied, and that was counted already
        quote = self.quotes.get(self.product(fill.symbol))
        if quote is None or abs(now - quote.received_at) > self.max_age:
            self.unpriced += 1
            return False
        if fill.side == "buy":
            quantity = self.book.buy(fill.symbol, fill.price * fill.quantity, quote.ask, self.costs)
        else:
            held = self.book.positions.get(fill.symbol.upper(), ZERO)
            quantity = self.book.sell(fill.symbol, held, quote.bid, self.costs)
        if quantity <= 0:
            self.unpriced += 1
            return False
        return True
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_fills.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/fills.py src/agentic_trading/fast/mirror.py tests/test_fast_fills.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): delayed ask/bid paper fills and the Coinbase comparison book"
```

---

### Task 5: The switchboard

**Files:**
- Create: `src/agentic_trading/fast/switchboard.py`
- Test: `tests/test_fast_switchboard.py`

**Interfaces:**
- Consumes:
  - `FastConfig` (Task 1).
  - `MinuteBars`, `read_regime`, `STAND_ASIDE`, `WARMING` (Task 2).
  - `ENTRIES`, `Plan`, `Trade`, `exit_reason` (Task 3).
  - `PaperFiller`, `Order`, `Fill`, `usable_quote` (Task 4).
  - `CoinbaseMirror` (Task 4).
- Produces:
  - `Event(kind, symbol, at, text, data)` with `.to_record()`. The record is `{"at", "event", "symbol", "text", **data}`.
  - Event kinds: `fast_entry`, `fast_exit`, `fast_regime`, `fast_skipped`, `fast_halted`, `fast_unfilled`. Task 6 adds `fast_recovered` and Task 7 adds `fast_failed` and `fast_reset`.
  - `tick_price(tick) -> Optional[Decimal]`.
  - `Switchboard(config, book, *, mirror=None, regime_reader=read_regime)`, with:
    - `.on_tick(tick) -> list[Event]` and `.status() -> dict`;
    - attributes `.book`, `.mirror`, `.bars`, `.regimes`, `.trades`, `.entering`, `.exiting`, `.cooldown_until`, `.filler`, `.skipped_total`, `.day`, `.day_start_equity`, `.halted_day`.
  - `fast_exit` data has `playbook`, `reason`, `price`, `quantity` and `net_pct`; `fast_entry` data has `playbook`, `regime`, `price`, `quantity`, `stop`, `target`, `expected_move_pct` and `cost_pct`.

- [ ] **Step 1: Write the failing test** — `tests/test_fast_switchboard.py`:

```python
"""The switchboard's rules, driven tick by tick."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.regime import CHOPPY, SQUEEZE, TRENDING, UNCLEAR, WARMING
from agentic_trading.fast.settings import FastConfig
from agentic_trading.fast.switchboard import Switchboard
from tests.fast_support import T0, quote

D = Decimal
SEC, MS = timedelta(seconds=1), timedelta(milliseconds=1)
CFG = FastConfig(enabled=True, symbols=("BTC/USD", "ETH/USD"))
WIDE = [100, 104] * 16          # 31 closed bars: ATR 4, 30-bar high 104
OPEN = T0 + timedelta(minutes=31)  # the minute still open after seeding


def _board(config=CFG, equity="300", mirror=None):
    book = MemberBook("switchboard", starting_equity=D(equity))
    return Switchboard(config, book, mirror=mirror, regime_reader=lambda bars: TRENDING)


def _seed(board, symbol="BTC/USD", closes=WIDE):
    for i, close in enumerate(closes):
        board.bars.add(symbol, float(close), T0 + timedelta(minutes=i))
    board.regimes[symbol] = TRENDING


def _enter(board, symbol="BTC/USD", at=OPEN):
    """Warm the feed, break out above 104, and fill 300 ms later."""
    board.on_tick(quote(symbol, 102.9, 103.1, at + SEC))   # first price: never an entry
    board.on_tick(quote(symbol, 104.9, 105.1, at + 2 * SEC))
    return board.on_tick(quote(symbol, 105.0, 105.2, at + 2 * SEC + 300 * MS))


def _exit_through_a_gap(board):
    board.on_tick(quote("BTC/USD", 90.0, 90.2, OPEN + 3 * SEC))
    return board.on_tick(quote("BTC/USD", 89.5, 89.7, OPEN + 3 * SEC + 300 * MS))


class EntryTests(unittest.TestCase):
    def test_a_breakout_fills_at_the_ask_with_a_third_of_the_book(self) -> None:
        board = _board()
        _seed(board)
        [entry] = [e for e in _enter(board) if e.kind == "fast_entry"]
        self.assertEqual((entry.data["playbook"], entry.data["price"]), ("breakout", "105.2"))
        self.assertIn("expecting", entry.text)
        trade = board.trades["BTC/USD"]
        self.assertEqual((trade.playbook, trade.entry), ("breakout", 105.2))
        self.assertAlmostEqual(float(trade.quantity * D("105.2") * D("1.0025")), 100.0, places=3)

    def test_choppy_unclear_and_warming_stand_aside(self) -> None:
        for regime in (CHOPPY, UNCLEAR, WARMING):
            with self.subTest(regime=regime):
                board = _board()
                _seed(board)
                board.regimes["BTC/USD"] = regime
                _enter(board)
                self.assertEqual((board.trades, board.filler.pending), ({}, []))

    def test_the_cost_gate_skips_small_moves_and_reports_once_a_minute(self) -> None:
        board = _board()
        _seed(board, closes=[100, 100.4] * 16)  # ATR 0.4: expects 0.8, under 3 x ~0.52
        board.on_tick(quote("BTC/USD", 100.3, 100.31, OPEN + SEC))
        board.on_tick(quote("BTC/USD", 100.49, 100.51, OPEN + 2 * SEC))
        board.on_tick(quote("BTC/USD", 100.49, 100.51, OPEN + 3 * SEC))
        self.assertEqual((board.trades, board.filler.pending, board.skipped_total), ({}, [], 1))
        events = board.on_tick(quote("BTC/USD", 100.49, 100.51, OPEN + timedelta(minutes=1, seconds=1)))
        [skip] = [e for e in events if e.kind == "fast_skipped"]
        self.assertIn("cost gate", skip.text)

    def test_the_position_cap(self) -> None:
        board = _board(FastConfig(enabled=True, symbols=("BTC/USD", "ETH/USD"), max_positions=1))
        _seed(board)
        _seed(board, "ETH/USD")
        _enter(board)
        board.on_tick(quote("ETH/USD", 102.9, 103.1, OPEN + 5 * SEC))
        board.on_tick(quote("ETH/USD", 104.9, 105.1, OPEN + 6 * SEC))
        self.assertEqual(board.filler.pending, [])
        self.assertNotIn("ETH/USD", board.trades)

    def test_the_first_price_after_a_quiet_feed_never_enters(self) -> None:
        board = _board()
        _seed(board)
        board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + SEC))
        board.on_tick(quote("BTC/USD", 104.9, 105.1, OPEN + 32 * SEC))  # 31 s of silence
        self.assertEqual(board.filler.pending, [])
        board.on_tick(quote("BTC/USD", 104.9, 105.1, OPEN + 33 * SEC))
        self.assertEqual(len(board.filler.pending), 1)

    def test_a_crossed_quote_is_never_used(self) -> None:
        board = _board()
        _seed(board)
        board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + SEC))
        board.on_tick(quote("BTC/USD", 105.2, 105.0, OPEN + 2 * SEC))
        self.assertEqual(board.filler.pending, [])

    def test_an_entry_too_small_to_fill_is_reported_not_traded(self) -> None:
        board = _board(equity="2")  # a third of $2 is under the $1 minimum
        _seed(board)
        events = _enter(board)
        self.assertIn("fast_unfilled", [e.kind for e in events])
        self.assertEqual(board.trades, {})


class ExitTests(unittest.TestCase):
    def test_an_open_trade_keeps_its_playbook_when_the_reading_changes(self) -> None:
        board = _board()
        _seed(board)
        _enter(board)
        board.regimes["BTC/USD"] = SQUEEZE
        board.on_tick(quote("BTC/USD", 98.0, 98.2, OPEN + 3 * SEC))  # under the 99.2 trail
        self.assertEqual(board.exiting["BTC/USD"], "trailing stop")
        self.assertEqual(board.trades["BTC/USD"].playbook, "breakout")

    def test_a_gap_through_the_stop_fills_at_the_real_bid(self) -> None:
        board = _board()
        _seed(board)
        _enter(board)
        [done] = [e for e in _exit_through_a_gap(board) if e.kind == "fast_exit"]
        self.assertEqual(done.data["price"], "89.5")
        self.assertLess(done.data["net_pct"], -14)
        self.assertEqual(board.trades, {})

    def test_a_cooldown_follows_every_exit(self) -> None:
        # the gap loss is ~5% of the book: loosen the daily stop so only the cooldown is tested
        board = _board(FastConfig(enabled=True, symbols=("BTC/USD", "ETH/USD"), daily_loss_stop=D("0.5")))
        _seed(board)
        _enter(board)
        _exit_through_a_gap(board)
        self.assertEqual(board.cooldown_until["BTC/USD"], OPEN + 3 * SEC + 300 * MS + timedelta(minutes=5))
        soon = OPEN + timedelta(minutes=2)
        board.on_tick(quote("BTC/USD", 105.9, 106.1, soon))
        board.on_tick(quote("BTC/USD", 105.9, 106.1, soon + SEC))
        self.assertEqual(board.filler.pending, [])
        later = OPEN + timedelta(minutes=6)
        board.on_tick(quote("BTC/USD", 106.9, 107.1, later))
        board.on_tick(quote("BTC/USD", 106.9, 107.1, later + SEC))
        self.assertEqual(len(board.filler.pending), 1)

    def test_the_daily_loss_stop_lasts_until_the_next_utc_day(self) -> None:
        board = _board()
        _seed(board)
        board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + SEC))  # the day starts at $300
        board.book.cash -= D("12")  # down 4%
        events = board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + 2 * SEC))
        self.assertIn("fast_halted", [e.kind for e in events])
        board.on_tick(quote("BTC/USD", 104.9, 105.1, OPEN + 3 * SEC))
        self.assertEqual(board.filler.pending, [])
        self.assertTrue(board.status()["halted"])
        tomorrow = OPEN + timedelta(days=1)
        board.on_tick(quote("BTC/USD", 105.9, 106.1, tomorrow))
        board.on_tick(quote("BTC/USD", 105.9, 106.1, tomorrow + SEC))
        self.assertFalse(board.status()["halted"])
        self.assertEqual(len(board.filler.pending), 1)


class MirrorAndStatusTests(unittest.TestCase):
    def test_coinbase_ticks_feed_the_mirror_and_status_shows_both_books(self) -> None:
        mirror = CoinbaseMirror(MemberBook("switchboard@coinbase", starting_equity=D("300")), D("0.006"))
        board = _board(mirror=mirror)
        _seed(board)
        board.on_tick(quote("BTC-USD", 105.0, 105.3, OPEN + 2 * SEC, venue="coinbase"))
        _enter(board)
        self.assertIn("BTC/USD", mirror.book.positions)
        status = board.status()
        coin = status["coins"][0]
        self.assertEqual((coin["symbol"], coin["regime"], coin["trade"]["playbook"]),
                         ("BTC/USD", "trending", "breakout"))
        self.assertEqual(status["mirror"]["unpriced"], 0)
        self.assertIsNotNone(status["book"]["return_pct"])
        self.assertTrue(status["coins"][1]["standing_aside"])  # ETH is still warming
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_switchboard.py -q`
Expected: collection ERROR, `No module named 'agentic_trading.fast.switchboard'`.

- [ ] **Step 3: Implement** `src/agentic_trading/fast/switchboard.py`:

```python
"""The switchboard: for each coin, read the market, pick a playbook, act on each price.

A paper desk member that runs inside the venues service:
* It reads each coin's market once a minute (regime.py). On every price, it
  lets that regime's playbooks propose an entry.
* It keeps an open trade with the playbook that opened it until that
  playbook's own exit fires.
* Fills come from PaperFiller: the real ask or bid, 250 ms after the
  decision, plus the Alpaca fee. A CoinbaseMirror copies each fill at
  Coinbase prices for comparison.

Every rule here is about not paying fees for nothing. It stands aside unless
the market is trending or squeezed, and enters only when the expected move is
several times the round-trip cost. It enters on no price that follows 30 s of
silence, stops entering for the day after a 3% loss, and cools down after
each exit.

The time base is each tick's ``received_at``, so a replay of recorded ticks
makes the same decisions the live run made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.bars import Bar, MinuteBars
from agentic_trading.fast.fills import Fill, Order, PaperFiller, usable_quote
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.playbooks import ENTRIES, Plan, Trade, exit_reason
from agentic_trading.fast.regime import STAND_ASIDE, WARMING, read_regime
from agentic_trading.fast.settings import FastConfig
from agentic_trading.venues.model import Tick

STALE_AFTER = timedelta(seconds=30)
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Event:
    kind: str
    symbol: str
    at: datetime
    text: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "event": self.kind, "symbol": self.symbol,
                "text": self.text, **self.data}


def tick_price(tick: Tick) -> Optional[Decimal]:
    """The mid of a usable quote, else a positive trade price, else None."""
    if usable_quote(tick):
        return (tick.bid + tick.ask) / 2
    if tick.last is not None and tick.last > 0:
        return tick.last
    return None


def _book_view(book: MemberBook) -> dict[str, Any]:
    start, equity = book.starting_equity, book.equity
    return {
        "equity": str(equity.quantize(CENT)),
        "return_pct": round(float(equity / start - 1) * 100, 3) if start > 0 else None,
        "entries": book.entries,
        "exits": book.exits,
    }


class Switchboard:
    def __init__(
        self,
        config: FastConfig,
        book: MemberBook,
        *,
        mirror: Optional[CoinbaseMirror] = None,
        regime_reader: Callable[[Sequence[Bar]], str] = read_regime,
    ) -> None:
        self.config = config
        self.book = book
        self.mirror = mirror
        self.read_regime = regime_reader
        self.fee = float(config.alpaca_fee)
        self.filler = PaperFiller(book, config.alpaca_fee, delay=timedelta(milliseconds=config.fill_delay_ms))
        self.bars = MinuteBars()
        self.regimes: dict[str, str] = {symbol: WARMING for symbol in config.symbols}
        self.trades: dict[str, Trade] = {}
        self.entering: dict[str, Plan] = {}
        self.exiting: dict[str, str] = {}
        self.cooldown_until: dict[str, datetime] = {}
        self.quotes: dict[str, Tick] = {}
        self.last_seen: dict[str, datetime] = {}
        self.skipped: dict[str, int] = {}
        self._skip_minute: dict[str, datetime] = {}
        self.skipped_total = 0
        self.day = ""
        self.day_start_equity = book.equity
        self.halted_day = ""

    # -- the tick path -----------------------------------------------------

    def on_tick(self, tick: Tick) -> list[Event]:
        if tick.venue == "coinbase":
            if self.mirror is not None:
                self.mirror.on_tick(tick)
            return []
        if tick.venue != "alpaca" or tick.symbol not in self.config.symbols:
            return []
        price = tick_price(tick)
        if price is None:
            return []
        symbol, now = tick.symbol, tick.received_at
        previous = self.last_seen.get(symbol)
        self.last_seen[symbol] = now
        if usable_quote(tick):
            self.quotes[symbol] = tick
        events = self._roll_day(now)
        self.book.mark({symbol: price}, now)
        if self.mirror is not None:
            self.mirror.mark(symbol, price, now)
        for fill in self.filler.on_tick(tick):
            events.extend(self._on_fill(fill))
        if self.bars.add(symbol, float(price), tick.exchange_at) is not None:
            events.extend(self._on_bar(symbol, now))
        value = float(price)
        trade = self.trades.get(symbol)
        if trade is not None:
            if symbol not in self.exiting:
                reason = exit_reason(trade, self.bars.closed(symbol), value)
                if reason:
                    self.exiting[symbol] = reason
                    self.filler.submit(Order(symbol, "sell", now, quantity=trade.quantity))
        elif symbol not in self.entering:
            if previous is not None and now - previous <= STALE_AFTER:
                events.extend(self._consider_entry(symbol, value, now))
        return events

    def _roll_day(self, now: datetime) -> list[Event]:
        day = now.astimezone(timezone.utc).date().isoformat()
        if day != self.day:
            self.day = day
            self.day_start_equity = self.book.equity
        start = self.day_start_equity
        if self.halted_day != day and start > 0 and self.book.equity <= start * (1 - self.config.daily_loss_stop):
            self.halted_day = day
            drop = float(1 - self.book.equity / start) * 100
            return [Event("fast_halted", "*", now,
                          f"The switchboard is down {drop:.1f}% today: no new entries until tomorrow (UTC)",
                          {"drop_pct": round(drop, 2)})]
        return []

    def _on_bar(self, symbol: str, now: datetime) -> list[Event]:
        events: list[Event] = []
        regime = self.read_regime(self.bars.closed(symbol))
        was = self.regimes.get(symbol, WARMING)
        if regime != was:
            self.regimes[symbol] = regime
            events.append(Event("fast_regime", symbol, now, f"{symbol} is now {regime} (was {was})",
                                {"regime": regime, "was": was}))
        count = self.skipped.pop(symbol, 0)
        if count:
            noun = "setup" if count == 1 else "setups"
            events.append(Event(
                "fast_skipped", symbol, now,
                f"{symbol}: {count} {noun} skipped by the cost gate "
                f"(expected move under {self.config.cost_gate_multiple}x the round-trip cost)",
                {"count": count}))
        return events

    def _consider_entry(self, symbol: str, price: float, now: datetime) -> list[Event]:
        regime = self.regimes.get(symbol, WARMING)
        if regime in STAND_ASIDE or self.halted_day == self.day:
            return []
        if now < self.cooldown_until.get(symbol, now):
            return []
        if len(self.trades) + len(self.entering) >= self.config.max_positions:
            return []
        quote = self.quotes.get(symbol)
        if quote is None:
            return []
        bars = self.bars.closed(symbol)
        plan = next((p for _, entry in ENTRIES.get(regime, ()) if (p := entry(bars, price)) is not None), None)
        if plan is None:
            return []
        round_trip = 2 * self.fee * price + float(quote.ask - quote.bid)
        if plan.expected_move < float(self.config.cost_gate_multiple) * round_trip:
            minute = now.replace(second=0, microsecond=0)
            if self._skip_minute.get(symbol) != minute:  # one count per coin per minute
                self._skip_minute[symbol] = minute
                self.skipped[symbol] = self.skipped.get(symbol, 0) + 1
                self.skipped_total += 1
            return []
        notional = min(self.book.equity / self.config.max_positions, self.book.cash)
        self.entering[symbol] = plan
        self.filler.submit(Order(symbol, "buy", now, notional=notional))
        return []

    def _on_fill(self, fill: Fill) -> list[Event]:
        symbol, now = fill.symbol, fill.at
        if fill.side == "buy":
            plan = self.entering.pop(symbol, None)
            if plan is None:
                return []
            if fill.quantity <= 0:
                return [Event("fast_unfilled", symbol, now,
                              f"{symbol}: the {plan.playbook} entry was too small for the book to fill",
                              {"playbook": plan.playbook})]
            entry = float(fill.price)
            self.trades[symbol] = Trade(symbol, plan.playbook, entry, plan.stop, plan.target,
                                        fill.quantity, now, entry)
            if self.mirror is not None:
                self.mirror.copy(fill, now)
            quote = self.quotes.get(symbol)
            spread = float((quote.ask - quote.bid) / quote.ask) if quote is not None else 0.0
            cost_pct = (2 * self.fee + spread) * 100
            move_pct = plan.expected_move / entry * 100
            regime = self.regimes.get(symbol, WARMING)
            text = (f"Bought ${float(fill.price * fill.quantity):,.2f} of {symbol} at {fill.price} "
                    f"({plan.playbook}, {regime}): stop {plan.stop:,.2f}, expecting {move_pct:+.2f}% "
                    f"against {cost_pct:.2f}% round-trip cost")
            return [Event("fast_entry", symbol, now, text, {
                "playbook": plan.playbook, "regime": regime, "price": str(fill.price),
                "quantity": str(fill.quantity), "stop": round(plan.stop, 6),
                "target": None if plan.target is None else round(plan.target, 6),
                "expected_move_pct": round(move_pct, 3), "cost_pct": round(cost_pct, 3),
            })]
        reason = self.exiting.pop(symbol, "exit")
        trade = self.trades.pop(symbol, None)
        if trade is None:
            return []
        self.cooldown_until[symbol] = now + timedelta(minutes=self.config.cooldown_minutes)
        if fill.quantity <= 0:
            return [Event("fast_unfilled", symbol, now, f"{symbol}: the exit found nothing to sell",
                          {"playbook": trade.playbook})]
        if self.mirror is not None:
            self.mirror.copy(fill, now)
        net = float(fill.price) * (1 - self.fee) / (trade.entry * (1 + self.fee)) - 1
        net_pct = round(net * 100, 3)
        text = f"Sold {symbol} at {fill.price} ({reason}, {trade.playbook}): {net_pct:+.2f}% after fees"
        return [Event("fast_exit", symbol, now, text, {
            "playbook": trade.playbook, "reason": reason, "price": str(fill.price),
            "quantity": str(fill.quantity), "net_pct": net_pct,
        })]

    # -- reporting -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        coins = []
        for symbol in self.config.symbols:
            regime = self.regimes.get(symbol, WARMING)
            trade = self.trades.get(symbol)
            row: dict[str, Any] = {"symbol": symbol, "regime": regime,
                                   "standing_aside": trade is None and regime in STAND_ASIDE, "trade": None}
            if trade is not None:
                price = self.book.prices.get(symbol.upper())
                row["trade"] = {
                    "playbook": trade.playbook, "entry": trade.entry, "stop": round(trade.stop, 6),
                    "target": None if trade.target is None else round(trade.target, 6),
                    "pnl_pct": None if price is None else round((float(price) / trade.entry - 1) * 100, 3),
                    "opened_at": trade.opened_at.isoformat(),
                }
            coins.append(row)
        mirror = None
        if self.mirror is not None:
            mirror = {**_book_view(self.mirror.book), "unpriced": self.mirror.unpriced}
        return {
            "coins": coins,
            "book": _book_view(self.book),
            "mirror": mirror,
            "skipped": self.skipped_total,
            "halted": bool(self.day) and self.halted_day == self.day,
        }
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_switchboard.py -q`
Expected: `12 passed` (and 3 subtests passed).

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/switchboard.py tests/test_fast_switchboard.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): the switchboard — regime-picked playbooks, cost gate, cooldowns and a daily stop"
```

---

### Task 6: The store (restarts, corrupt files, orphan holdings)

**Files:**
- Create: `src/agentic_trading/fast/store.py`
- Modify: `src/agentic_trading/fast/switchboard.py` (add `RECOVER_BAND`, `to_state`, `restore`)
- Test: `tests/test_fast_store.py`

**Interfaces:**
- Consumes:
  - `Switchboard`, `Event` (Task 5).
  - `Trade`, `RECOVERED` (Task 3).
  - `MemberBook.load(path, *, name, starting_equity) -> (book, broken)`.
  - `jsonio.write_text` and `jsonio.dumps`.
- Produces:
  - `FastStore(state_dir)` with:
    - paths `.book_path` (`desk/switchboard.json`), `.mirror_path` (`fast/mirror.json`) and `.engine_path` (`fast/engine.json`);
    - `.starting_equity() -> Decimal`;
    - `.load(now) -> (book, mirror_book, state: dict, notes: list[str])`;
    - `.save(board)`.
  - `Switchboard.to_state() -> dict` and `Switchboard.restore(state, now) -> list[Event]` (`fast_recovered` events).

- [ ] **Step 1: Write the failing test** — `tests/test_fast_store.py`:

```python
"""The switchboard survives restarts; broken files are moved aside, never fatal."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.regime import TRENDING
from agentic_trading.fast.store import FastStore
from agentic_trading.fast.switchboard import Switchboard
from tests.fast_support import T0
from tests.test_fast_switchboard import CFG, OPEN, _enter, _seed

D = Decimal


def _board_from(store: FastStore, now=T0):
    book, mirror_book, state, notes = store.load(now)
    board = Switchboard(CFG, book, mirror=CoinbaseMirror(mirror_book, D("0.006")),
                        regime_reader=lambda bars: TRENDING)
    return board, state, notes


class StoreTests(unittest.TestCase):
    def test_an_open_trade_and_its_cooldowns_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = FastStore(name)
            board, _, _ = _board_from(store)
            _seed(board)
            _enter(board)
            board.cooldown_until["ETH/USD"] = OPEN + timedelta(minutes=4)
            store.save(board)
            self.assertTrue(store.book_path.is_file())
            again, state, notes = _board_from(store, OPEN + timedelta(minutes=1))
            self.assertEqual(again.restore(state, OPEN + timedelta(minutes=1)), [])
        self.assertEqual(notes, [])
        trade = again.trades["BTC/USD"]
        self.assertEqual((trade.playbook, trade.entry), ("breakout", 105.2))
        self.assertEqual(trade.quantity, again.book.positions["BTC/USD"])
        self.assertEqual(again.cooldown_until["ETH/USD"], OPEN + timedelta(minutes=4))
        self.assertEqual(again.day, board.day)

    def test_the_book_starts_at_the_desk_members_equity(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = FastStore(name)
            self.assertEqual(store.starting_equity(), D("50"))
            (Path(name) / "desk").mkdir()
            (Path(name) / "desk" / "account.json").write_text(json.dumps({"starting_equity": "49.9"}))
            self.assertEqual(store.starting_equity(), D("49.9"))
            book, _, _, _ = store.load(T0)
        self.assertEqual(book.starting_equity, D("49.9"))

    def test_unreadable_files_are_moved_aside_and_a_fresh_start_is_noted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = FastStore(name)
            store.book_path.parent.mkdir(parents=True)
            store.book_path.write_text("not json")
            store.engine_path.parent.mkdir(parents=True)
            store.engine_path.write_text("[1, 2]")
            book, _, state, notes = store.load(T0)
            moved = sorted(p.name for p in Path(name).rglob("*.corrupt-*"))
        self.assertEqual(state, {})
        self.assertTrue(book.is_new)
        self.assertEqual(len(notes), 2)
        self.assertEqual(moved, ["engine.json.corrupt-20261005T120000Z", "switchboard.json.corrupt-20261005T120000Z"])

    def test_a_holding_without_a_trade_is_recovered_and_a_phantom_trade_dropped(self) -> None:
        book = MemberBook("switchboard", starting_equity=D("300"))
        book.positions = {"BTC/USD": D("0.5")}
        book.prices = {"BTC/USD": D("100")}
        board = Switchboard(CFG, book)
        phantom = {"symbol": "ETH/USD", "playbook": "pullback", "entry": 10.0, "stop": 9.0, "target": 11.0,
                   "quantity": "1", "opened_at": T0.isoformat(), "high": 10.0}
        events = board.restore({"trades": [phantom], "skipped_total": 4}, T0)
        self.assertEqual(sorted(board.trades), ["BTC/USD"])
        recovered = board.trades["BTC/USD"]
        self.assertEqual((recovered.playbook, recovered.stop, recovered.target), ("recovered", 98.0, 102.0))
        self.assertEqual([e.kind for e in events], ["fast_recovered"])
        self.assertEqual(board.skipped_total, 4)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_store.py -q`
Expected: collection ERROR, `No module named 'agentic_trading.fast.store'`.

- [ ] **Step 3: Implement.** First `src/agentic_trading/fast/store.py`:

```python
"""Where the switchboard's paper life survives restarts.

* ``data/state/desk/switchboard.json``: the member book the desk judges, in
  the ordinary MemberBook format.
* ``data/state/fast/mirror.json``: the Coinbase comparison book.
* ``data/state/fast/engine.json``: open trades, cooldowns, the day's start
  equity and counters.

Writes are atomic (``jsonio.write_text``). An unreadable file is moved aside as
``<name>.corrupt-<UTC stamp>`` and a fresh one starts. The caller journals
that; it is never a crash.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agentic_trading import jsonio
from agentic_trading.desk.book import MemberBook

DEFAULT_EQUITY = Decimal("50")
BOOK_NAME, MIRROR_NAME = "switchboard", "switchboard@coinbase"


class FastStore:
    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.book_path = self.state_dir / "desk" / "switchboard.json"
        self.mirror_path = self.state_dir / "fast" / "mirror.json"
        self.engine_path = self.state_dir / "fast" / "engine.json"

    def starting_equity(self) -> Decimal:
        """The desk members' starting equity, so the race is fair; else $50."""
        try:
            raw = json.loads((self.state_dir / "desk" / "account.json").read_text(encoding="utf-8"))
            value = Decimal(str(raw["starting_equity"]))
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
            return DEFAULT_EQUITY
        return value if value.is_finite() and value > 0 else DEFAULT_EQUITY

    def load(self, now: datetime) -> tuple[MemberBook, MemberBook, dict[str, Any], list[str]]:
        notes: list[str] = []
        book = self._book(self.book_path, BOOK_NAME, self.starting_equity(), now, notes)
        mirror = self._book(self.mirror_path, MIRROR_NAME, book.starting_equity, now, notes)
        state: dict[str, Any] = {}
        if self.engine_path.is_file():
            try:
                raw = json.loads(self.engine_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
                state = raw
            except (OSError, ValueError):
                self._aside(self.engine_path, now, notes)
        return book, mirror, state, notes

    def save(self, board: Any) -> None:
        board.book.save()
        if board.mirror is not None:
            board.mirror.book.save()
        jsonio.write_text(self.engine_path, jsonio.dumps(board.to_state(), indent=2) + "\n")

    def _book(self, path: Path, name: str, equity: Decimal, now: datetime, notes: list[str]) -> MemberBook:
        book, broken = MemberBook.load(path, name=name, starting_equity=equity)
        if broken:
            self._aside(path, now, notes)
        return book

    @staticmethod
    def _aside(path: Path, now: datetime, notes: list[str]) -> None:
        target = path.with_name(f"{path.name}.corrupt-{now:%Y%m%dT%H%M%SZ}")
        try:
            path.rename(target)
            notes.append(f"{path.name} was unreadable: moved to {target.name}, starting fresh")
        except OSError as exc:
            notes.append(f"{path.name} was unreadable and could not be moved aside ({exc.strerror})")
```

In `src/agentic_trading/fast/switchboard.py`:
1. Change the playbooks import to `from agentic_trading.fast.playbooks import ENTRIES, RECOVERED, Plan, Trade, exit_reason`.
2. Add `RECOVER_BAND = 0.02` under `STALE_AFTER`.
3. Add these two methods to `Switchboard`, just above `# -- reporting`:

```python
    # -- persistence ----------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "trades": [trade.to_dict() for trade in self.trades.values()],
            "cooldown_until": {s: at.isoformat() for s, at in self.cooldown_until.items()},
            "day": self.day,
            "day_start_equity": str(self.day_start_equity),
            "halted_day": self.halted_day,
            "skipped_total": self.skipped_total,
        }

    def restore(self, state: dict[str, Any], now: datetime) -> list[Event]:
        """Pick up where a saved run left off. The book is the truth for holdings.

        A saved trade with no holding behind it is dropped. A holding with no
        saved trade (a crash between the book save and the engine save) is
        managed as ``recovered`` with a 2% stop and target, so it is never
        left unmanaged.
        """
        trades: dict[str, Trade] = {}
        for raw in state.get("trades") or []:
            try:
                trade = Trade.from_dict(raw)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            held = self.book.positions.get(trade.symbol.upper(), Decimal("0"))
            if trade.symbol in self.config.symbols and held > 0:
                trade.quantity = held
                trades[trade.symbol] = trade
        events: list[Event] = []
        for symbol, held in self.book.positions.items():
            price = float(self.book.prices.get(symbol) or 0)
            if symbol in trades or symbol not in self.config.symbols or held <= 0 or price <= 0:
                continue
            trades[symbol] = Trade(symbol, RECOVERED, price, price * (1 - RECOVER_BAND),
                                   price * (1 + RECOVER_BAND), held, now, price)
            events.append(Event("fast_recovered", symbol, now,
                                f"{symbol}: a holding with no saved trade is now managed with a "
                                f"{RECOVER_BAND:.0%} stop and target", {"quantity": str(held)}))
        self.trades = trades
        for symbol, raw in (state.get("cooldown_until") or {}).items():
            try:
                self.cooldown_until[str(symbol)] = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
        self.day = str(state.get("day") or "")
        try:
            self.day_start_equity = Decimal(str(state.get("day_start_equity", self.book.equity)))
        except ArithmeticError:
            self.day_start_equity = self.book.equity
        self.halted_day = str(state.get("halted_day") or "")
        try:
            self.skipped_total = int(state.get("skipped_total") or 0)
        except (TypeError, ValueError):
            self.skipped_total = 0
        return events
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_store.py tests/test_fast_switchboard.py -q`
Expected: `16 passed` (and 3 subtests passed).

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/store.py src/agentic_trading/fast/switchboard.py tests/test_fast_store.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): atomic store, corrupt files moved aside, orphan holdings recovered"
```

---

### Task 7: The engine inside the venues service

**Files:**
- Create: `src/agentic_trading/fast/service.py`
- Modify: `src/agentic_trading/venues/daemon.py` (`run_venues` gains `engine`, `fast_status_every`, `fast_save_every` and a `fast_loop`; `main_run` builds the engine)
- Test: `tests/test_fast_service.py`

**Interfaces:**
- Consumes:
  - `Switchboard` (Tasks 5–6); `FastStore` (Task 6); `CoinbaseMirror` (Task 4); `FastConfig` and `load_fast_config` (Task 1).
  - `run_venues(...)`, `TickBus.subscribe()` and `HealthBoard.set_venue(name, **fields)` from venues.
- Produces:
  - `FastJournal(journal_dir).append(record)`, which writes `fast-<YYYY-MM-DD>.jsonl`.
  - `FastEngine(config, *, state_dir, journal_dir, now=None)`, with `.board`, `.failed: str`, `.recent` (a deque of records), `.on_tick(tick)`, `.save()`, `.status(now) -> dict` and `.write_status(now)` (writes `state_dir/fast.json`).
  - `fast_problem(config, venues_config, sources) -> str`; `""` means OK.
  - `run_venues(..., engine=None, fast_status_every=1.0, fast_save_every=10.0)`.
  - Health rows: `venues.json` gains a venue row `{"name": "switchboard", "mode": "paper", "status": "ok"|"error", "last_error": …}`.

- [ ] **Step 1: Write the failing test** — `tests/test_fast_service.py`:

```python
"""The switchboard runs on the venues bus; its failure never stops the feeds."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from agentic_trading.fast.service import FastEngine, fast_problem
from agentic_trading.fast.settings import FastConfig
from agentic_trading.venues.daemon import Backoff, run_venues
from agentic_trading.venues.settings import VenuesConfig
from tests.fast_support import T0, quote
from tests.test_venues_daemon import FakeSource

BTC_ONLY = FastConfig(enabled=True, symbols=("BTC/USD",))


def _source():
    ticks = [quote("BTC/USD", 100, 100.1, T0 + timedelta(seconds=i)) for i in range(3)]
    return FakeSource([("emit_block", ticks)], key="alpaca_crypto", venue="alpaca", always_open=True)


def _engine(tmp: Path) -> FastEngine:
    return FastEngine(BTC_ONLY, state_dir=tmp / "state", journal_dir=tmp / "journal", now=T0)


def _run(tmp: Path, engine: FastEngine) -> dict:
    async def scenario():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.4, stop.set)
        await run_venues(
            state_dir=tmp / "state", venues_config=VenuesConfig(stream_dir=tmp / "stream"),
            credentials={}, venues={}, sources=[_source()], stop=stop, now=lambda: T0,
            stock_open=lambda t: True, health_every=0.02, account_every=0.05,
            backoff_factory=lambda: Backoff(base=0.01, cap=0.02, jitter=0), rate_delay=0.01,
            stop_grace=0.2, engine=engine, fast_status_every=0.02, fast_save_every=0.05,
        )
    asyncio.run(scenario())
    return json.loads((tmp / "state" / "venues.json").read_text())


def _row(state: dict) -> dict:
    return next(v for v in state["venues"] if v["name"] == "switchboard")


class ServiceTests(unittest.TestCase):
    def test_the_engine_reads_the_bus_and_writes_its_status_and_book(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            state = _run(tmp, _engine(tmp))
            fast = json.loads((tmp / "state" / "fast.json").read_text())
            book_saved = (tmp / "state" / "desk" / "switchboard.json").is_file()
        self.assertTrue(fast["enabled"])
        self.assertEqual((fast["failed"], fast["coins"][0]["symbol"]), ("", "BTC/USD"))
        self.assertEqual((_row(state)["mode"], _row(state)["status"]), ("paper", "ok"))
        self.assertTrue(book_saved)

    def test_a_strategy_error_stops_only_the_engine(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            engine = _engine(tmp)

            def boom(tick):
                raise RuntimeError("boom in a playbook")

            engine.board.on_tick = boom
            state = _run(tmp, engine)
            recorded = (tmp / "stream" / "alpaca" / "BTCUSD" / "2026-10-05.jsonl.gz").exists()
            journal = (tmp / "journal" / "fast-2026-10-05.jsonl").read_text()
        self.assertEqual(_row(state)["status"], "error")
        self.assertIn("boom in a playbook", _row(state)["last_error"])
        self.assertTrue(recorded)  # the feeds and the recorder kept going
        self.assertIn('"event":"fast_failed"', journal)

    def test_a_corrupt_engine_file_is_journaled_as_a_fresh_start(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "state" / "fast").mkdir(parents=True)
            (tmp / "state" / "fast" / "engine.json").write_text("{broken")
            engine = _engine(tmp)
            journal = (tmp / "journal" / "fast-2026-10-05.jsonl").read_text()
        self.assertEqual(engine.recent[0]["event"], "fast_reset")
        self.assertIn("engine.json was unreadable", journal)

    def test_misconfiguration_is_named_before_the_service_starts(self) -> None:
        venues = VenuesConfig()
        crypto = [NS(key="alpaca_crypto")]
        self.assertIn("DOGE/USD", fast_problem(FastConfig(enabled=True, symbols=("BTC/USD", "DOGE/USD")), venues, crypto))
        self.assertIn("Alpaca crypto stream", fast_problem(BTC_ONLY, venues, [NS(key="coinbase")]))
        self.assertEqual(fast_problem(BTC_ONLY, venues, crypto), "")
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_service.py -q`
Expected: collection ERROR, `No module named 'agentic_trading.fast.service'`.

- [ ] **Step 3: Implement.** First `src/agentic_trading/fast/service.py`:

```python
"""The switchboard inside the venues service: ticks in; book, status and journal out.

``run_venues`` hands every bus tick to ``FastEngine.on_tick``. An exception
from the switchboard marks the engine failed (health shows it, the book is
saved) and the engine stops consuming. The price feeds and the recorder keep
running; they never share a call stack with strategy code.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agentic_trading import jsonio
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.settings import FastConfig
from agentic_trading.fast.store import FastStore
from agentic_trading.fast.switchboard import Switchboard
from agentic_trading.venues.model import Tick

RECENT = 20


class FastJournal:
    """``fast-<date>.jsonl`` beside the trader's journals; dated readers skip it by name."""

    def __init__(self, journal_dir: Path | str) -> None:
        self.journal_dir = Path(journal_dir)
        self.errors = 0

    def append(self, record: dict[str, Any]) -> None:
        day = str(record.get("at") or "")[:10] or datetime.now(timezone.utc).date().isoformat()
        try:
            self.journal_dir.mkdir(parents=True, exist_ok=True)
            with (self.journal_dir / f"fast-{day}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except OSError:
            self.errors += 1  # a full disk must not stop the switchboard


def fast_problem(config: FastConfig, venues_config: Any, sources: list[Any]) -> str:
    """Why the switchboard cannot run with this configuration, or "" when it can."""
    missing = [s for s in config.symbols if s not in venues_config.alpaca_crypto_symbols]
    if missing:
        return f"symbols {missing} are not in [venues] alpaca_crypto_symbols, so no prices would arrive for them"
    if not any(getattr(source, "key", "") == "alpaca_crypto" for source in sources):
        return ("the switchboard needs the Alpaca crypto stream: Alpaca keys and "
                "use_alpaca_live (or use_alpaca_paper) in [venues]")
    return ""


class FastEngine:
    def __init__(self, config: FastConfig, *, state_dir: Path | str, journal_dir: Path | str,
                 now: Optional[datetime] = None) -> None:
        current = now or datetime.now(timezone.utc)
        self.config = config
        self.state_dir = Path(state_dir)
        self.store = FastStore(state_dir)
        book, mirror_book, state, notes = self.store.load(current)
        self.board = Switchboard(config, book, mirror=CoinbaseMirror(mirror_book, config.coinbase_fee))
        self.journal = FastJournal(journal_dir)
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
        self.failed = ""
        self.save_error = ""
        for note in notes:
            self._record({"at": current.isoformat(), "event": "fast_reset", "symbol": "*", "text": note})
        for event in self.board.restore(state, current):
            self._record(event.to_record())

    def on_tick(self, tick: Tick) -> None:
        if self.failed:
            return
        try:
            events = self.board.on_tick(tick)
        except Exception as exc:  # noqa: BLE001 - a strategy bug must not touch the feeds
            self.failed = f"{type(exc).__name__}: {exc}"[:200]
            self._record({"at": tick.received_at.isoformat(), "event": "fast_failed",
                          "symbol": tick.symbol, "text": f"The switchboard stopped: {self.failed}"})
            self.save()
            return
        traded = False
        for event in events:
            self._record(event.to_record())
            traded = traded or event.kind in ("fast_entry", "fast_exit")
        if traded:
            self.save()

    def save(self) -> None:
        try:
            self.store.save(self.board)
            self.save_error = ""
        except OSError as exc:
            self.save_error = f"could not save: {exc.strerror}"

    def status(self, now: datetime) -> dict[str, Any]:
        return {
            "enabled": True,
            "as_of": now.isoformat(),
            "failed": self.failed,
            "save_error": self.save_error,
            **self.board.status(),
            "recent": list(reversed(self.recent)),
        }

    def write_status(self, now: datetime) -> None:
        try:
            jsonio.write_text(self.state_dir / "fast.json", jsonio.dumps(self.status(now), indent=2) + "\n")
        except OSError:
            pass  # the console shows the file as stale; the switchboard keeps trading

    def _record(self, record: dict[str, Any]) -> None:
        self.recent.append(record)
        self.journal.append(record)
```

Then in `src/agentic_trading/venues/daemon.py`:

1. In `run_venues`'s signature, replace
   `                     stop_grace: float = 5.0) -> None:` with:

```python
                     stop_grace: float = 5.0, engine: Optional[Any] = None,
                     fast_status_every: float = 1.0, fast_save_every: float = 10.0) -> None:
```

2. Directly after the line `    queue = bus.subscribe()`, add:

```python
    fast_queue = bus.subscribe() if engine is not None else None
```

3. Directly above `    def stop_quietly(source: Any) -> None:`, add:

```python
    async def fast_loop() -> None:
        """The switchboard's own subscriber: a slow strategy drops only its own ticks."""
        if engine is None or fast_queue is None:
            return
        loop = asyncio.get_running_loop()
        next_status = next_save = loop.time()
        while not (stop.is_set() and fast_queue.empty()):
            try:
                tick = await asyncio.wait_for(fast_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                tick = None
            if tick is not None:
                engine.on_tick(tick)
            moment = loop.time()
            if moment >= next_status:
                engine.write_status(clock())
                board.set_venue("switchboard", mode="paper", status="error" if engine.failed else "ok",
                                last_error=engine.failed)
                next_status = moment + fast_status_every
            if moment >= next_save:
                engine.save()
                next_save = moment + fast_save_every

```

4. In the `asyncio.gather(` call, change `stopper(), record_loop(), health_loop(), account_loop(),` to `stopper(), record_loop(), health_loop(), account_loop(), fast_loop(),`.

5. Replace
```python
    finally:
        recorder.flush(now=clock())
```
with
```python
    finally:
        if engine is not None:
            engine.save()
            engine.write_status(clock())
            board.set_venue("switchboard", mode="paper", status="error" if engine.failed else "ok",
                            last_error=engine.failed)
        recorder.flush(now=clock())
```

6. In `main_run`, directly after the block that ends `print("venues: no keys for any enabled venue; see config/secrets.example.toml")` / `return 2`, add:

```python
    from agentic_trading.fast.service import FastEngine, fast_problem
    from agentic_trading.fast.settings import load_fast_config

    try:
        fast_config = load_fast_config(config_path)
    except ValueError as exc:
        print(f"fast: {exc}")
        return 2
    engine = None
    if fast_config.enabled:
        problem = fast_problem(fast_config, venues_config, sources)
        if problem:
            print(f"fast: {problem}")
            return 2
        engine = FastEngine(fast_config, state_dir=config.state_dir, journal_dir=config.journal_dir)
```

and change the `run_venues(...)` call inside `main()` to pass `engine=engine`:

```python
        await run_venues(state_dir=config.state_dir, venues_config=venues_config, credentials=credentials,
                         venues=venues, sources=sources, stop=stop, engine=engine)
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_service.py tests/test_venues_daemon.py -q`
Expected: all pass (4 new, plus the existing daemon tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/service.py src/agentic_trading/venues/daemon.py tests/test_fast_service.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): the switchboard runs on the venues bus, isolated from the feeds"
```

---

### Task 8: The desk judges the switchboard but never funds it

**Files:**
- Modify:
  - `src/agentic_trading/desk/book.py` (add `ReadOnlyBook`)
  - `src/agentic_trading/desk/member.py` (add `ReadOnlyMember`)
  - `src/agentic_trading/desk/allocator.py` (add `UNFUNDED`, `hold_unfunded`)
  - `src/agentic_trading/desk/desk.py` (refresh read-only books; hold unfunded weights; `would_earn`)
  - `src/agentic_trading/cli.py` (`build_desk` switchboard branch)
  - `src/agentic_trading/config.py` (`DESK_MEMBER_CHOICES`)
- Test: `tests/test_fast_desk.py`

**Interfaces:**
- Consumes: the book file the venues service writes (Task 6): `data/state/desk/switchboard.json` in `MemberBook` format.
- Produces:
  - `ReadOnlyBook(MemberBook)`: `mark()` returns False and changes nothing, `save()` does nothing, and `.refresh()` re-reads the file.
  - `ReadOnlyMember(name, book)`.
  - `UNFUNDED = frozenset({"switchboard"})`.
  - `hold_unfunded(weights, *, benchmark) -> (held_weights, would_earn)`.
  - The `desk_allocation` journal event gains `"would_earn": {"switchboard": w}` whenever the switchboard is a member.

- [ ] **Step 1: Write the failing test** — `tests/test_fast_desk.py`:

```python
"""The desk reads the switchboard's book, never writes it, and never funds it."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agentic_trading.backtest import CostModel
from agentic_trading.desk.allocator import UNFUNDED, Allocation, hold_unfunded
from agentic_trading.desk.benchmark import BenchmarkStrategy
from agentic_trading.desk.book import MemberBook, ReadOnlyBook
from agentic_trading.desk.desk import StrategyDesk
from agentic_trading.desk.member import Member, ReadOnlyMember
from tests.test_desk import quote

D = Decimal
FREE = CostModel(D("0"), D("0"), D("0"))
NOW = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)


def _owner(path: Path) -> MemberBook:
    """The venues service's side: it owns and writes the switchboard's book."""
    book = MemberBook("switchboard", starting_equity=D("50"), path=path)
    book.samples = [("2026-09-22", "50.5")]
    book.entries = 2
    book.save()
    return book


class ReadOnlyBookTests(unittest.TestCase):
    def test_marks_and_saves_are_ignored_and_refresh_reads_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "switchboard.json"
            owner = _owner(path)
            before = path.read_text()
            reader, broken = ReadOnlyBook.load(path, name="switchboard", starting_equity=D("50"))
            self.assertFalse(broken)
            self.assertFalse(reader.mark({"BTC/USD": D("100")}, NOW))
            reader.save()
            self.assertEqual(path.read_text(), before)
            owner.samples.append(("2026-09-23", "51"))
            owner.save()
            reader.refresh()
            self.assertEqual((len(reader.samples), reader.entries, reader.path), (2, 2, path))
            path.write_text("garbage")
            reader.refresh()
            self.assertEqual(reader.samples, [])


class UnfundedTests(unittest.TestCase):
    def test_hold_unfunded_moves_the_weight_to_the_benchmark(self) -> None:
        self.assertIn("switchboard", UNFUNDED)
        held, would = hold_unfunded({"switchboard": 0.4, "momentum_rotation": 0.2, "benchmark": 0.4},
                                    benchmark="benchmark")
        self.assertEqual(held, {"switchboard": 0.0, "momentum_rotation": 0.2, "benchmark": 0.8})
        self.assertEqual(would, {"switchboard": 0.4})
        self.assertEqual(hold_unfunded({"benchmark": 1.0}, benchmark="benchmark"), ({"benchmark": 1.0}, {}))

    def test_the_desk_records_what_the_switchboard_would_earn_and_funds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk_dir = Path(name) / "desk"
            path = desk_dir / "switchboard.json"
            _owner(path)
            before = path.read_text()
            bench, _ = MemberBook.load(desk_dir / "benchmark.json", name="benchmark", starting_equity=D("50"))
            reader, _ = ReadOnlyBook.load(path, name="switchboard", starting_equity=D("50"))
            account, _ = MemberBook.load(desk_dir / "account.json", name="account", starting_equity=D("50"))
            log: list[dict] = []
            desk = StrategyDesk(
                members=[Member("benchmark", BenchmarkStrategy(), bench, order_pct=D("0.19")),
                         ReadOnlyMember("switchboard", reader)],
                account=account, costs=lambda: FREE, journal=log.append, state_path=desk_dir / "desk.json",
            )
            qualified = Allocation({"switchboard": 0.6, "benchmark": 0.4}, True, {"switchboard": "beats buy-and-hold"}, {})
            with patch("agentic_trading.desk.desk.allocate", return_value=qualified):
                desk.on_quote(quote("BTC-USD", "99", "100"))
            after = path.read_text()
        self.assertEqual(desk.allocations["switchboard"], 0.0)
        self.assertEqual(desk.allocations["benchmark"], 1.0)
        [event] = [e for e in log if e["event"] == "desk_allocation"]
        self.assertEqual(event["would_earn"], {"switchboard": 0.6})
        self.assertEqual(after, before)  # the desk saved its state but never this file


class WiringTests(unittest.TestCase):
    def test_switchboard_is_a_read_only_desk_member(self) -> None:
        from agentic_trading.cli import build_strategy
        from tests.test_desk_wiring import _config

        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name),
                             'desk_members = ["momentum_rotation", "trend_crypto", "switchboard", "benchmark"]')
            desk = build_strategy(config)
        member = next(m for m in desk.members if m.name == "switchboard")
        self.assertIsInstance(member, ReadOnlyMember)
        self.assertIsInstance(member.book, ReadOnlyBook)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_desk.py -q`
Expected: collection ERROR, `ImportError: cannot import name 'UNFUNDED'`.

- [ ] **Step 3: Implement**

1. Append to `src/agentic_trading/desk/book.py`:

```python


class ReadOnlyBook(MemberBook):
    """A member book another process owns: the switchboard's, written by the
    venues service. The desk reads it, and never marks or writes it."""

    def mark(self, prices: dict[str, Decimal], stamp: datetime) -> bool:
        return False

    def save(self) -> None:
        return None

    def refresh(self) -> None:
        """Re-read the owner's latest file. An unreadable file reads as empty."""
        if self.path is None:
            return
        fresh, _ = MemberBook.load(self.path, name=self.name, starting_equity=self.starting_equity)
        for key, value in vars(fresh).items():
            if key != "path":
                setattr(self, key, value)
```

2. Append to `src/agentic_trading/desk/member.py`:

```python


class _Silent:
    """The strategy of a member that trades elsewhere: it never asks for orders here."""

    def on_quote(self, quote: dict[str, Any]) -> list[Any]:
        return []


class ReadOnlyMember(Member):
    """A desk member whose trading happens in another process (the switchboard
    runs inside the venues service). The desk judges its book and never trades for it."""

    def __init__(self, name: str, book: MemberBook) -> None:
        super().__init__(name, _Silent(), book, order_pct=Decimal("0"))
```

3. In `src/agentic_trading/desk/allocator.py`, add below `BASE_MEMBERS = 2`:

```python
# Members the desk judges but may not fund yet. Their trades happen on paper in
# another process, and no live order path exists for them. Lifting this is a
# code change in the live sub-project, never a config switch.
UNFUNDED = frozenset({"switchboard"})


def hold_unfunded(weights: dict[str, float], *, benchmark: str) -> tuple[dict[str, float], dict[str, float]]:
    """Move unfunded members' weight to the benchmark; report what they would have earned."""
    held = dict(weights)
    would: dict[str, float] = {}
    for name in sorted(UNFUNDED):
        if name not in held:
            continue
        weight = held[name]
        would[name] = round(weight, 6)
        if weight > 0:
            held[benchmark] = round(held.get(benchmark, 0.0) + weight, 6)
            held[name] = 0.0
    return held, would
```

4. In `src/agentic_trading/desk/desk.py`:
   - Change the import to `from agentic_trading.desk.allocator import MemberRecord, allocate, hold_unfunded`.
   - Directly after
     ```python
             if week == self.allocation_week:
                 return []
     ```
     add:
     ```python
             for member in self.members:
                 refresh = getattr(member.book, "refresh", None)
                 if callable(refresh):
                     refresh()  # a book another process writes (the switchboard's)
     ```
   - Replace `        weights = self._drop_failed(result.weights)` with:
     ```python
             weights, would_earn = hold_unfunded(self._drop_failed(result.weights), benchmark=self.benchmark)
     ```
   - In the returned `desk_allocation` dict, directly after `                "stats": result.stats,`, add:
     ```python
                     **({"would_earn": would_earn} if would_earn else {}),
     ```

5. In `src/agentic_trading/config.py`, change `DESK_MEMBER_CHOICES = DESK_MEMBER_NAMES + ("dip_reversal",)` to:

```python
DESK_MEMBER_CHOICES = DESK_MEMBER_NAMES + ("dip_reversal", "switchboard")
```

6. In `src/agentic_trading/cli.py`, in `build_desk`:
   - Change `    from agentic_trading.desk.book import MemberBook` to `    from agentic_trading.desk.book import MemberBook, ReadOnlyBook`.
   - Change `    from agentic_trading.desk.member import Member` to `    from agentic_trading.desk.member import Member, ReadOnlyMember`.
   - Directly before `        if name == "benchmark":`, add:

```python
        if name == "switchboard":  # trades in the venues service; the desk only reads its book
            book, reset = ReadOnlyBook.load(desk_dir / f"{name}.json", name=name, starting_equity=equity)
            if reset:
                journal.append({"event": "desk_member_reset", "member": name})
            members.append(ReadOnlyMember(name, book))
            continue
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_desk.py tests/test_desk.py tests/test_desk_wiring.py tests/test_desk_allocator.py tests/test_desk_members.py tests/test_dashboard_desk.py -q`
Expected: all pass (4 new).

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/desk/book.py src/agentic_trading/desk/member.py src/agentic_trading/desk/allocator.py src/agentic_trading/desk/desk.py src/agentic_trading/cli.py src/agentic_trading/config.py tests/test_fast_desk.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(desk): judge the switchboard read-only; unfunded members' weight stays in the benchmark"
```

---

### Task 9: Replay and the `fast` command

**Files:**
- Create: `src/agentic_trading/fast/replay.py`, `src/agentic_trading/fast/cli.py`
- Modify: `src/agentic_trading/cli.py` (parser and route), `.gitignore` (`data/fastbars/`)
- Test: `tests/test_fast_replay.py`

**Interfaces:**
- Consumes:
  - `Switchboard`, `tick_price`, `Event` (Task 5); `CoinbaseMirror` (Task 4); `FastStore.starting_equity()` (Task 6); `FastEngine` (Task 7, used by the parity test).
  - `load_venues_config(path).stream_dir`.
- Produces:
  - `Report` with `.lines()`, `.ticks`, `.playbooks` and `.events`.
  - `replay(ticks, config, *, label, start="", end="", starting_equity=Decimal("50")) -> Report`.
  - `recorded_ticks(stream_dir, symbols, start: date, end: date) -> Iterator[Tick]`.
  - `bar_ticks(rows: dict[str, list[dict]], spread: Decimal) -> Iterator[Tick]`.
  - `fetch_bars(symbols, start, end, cache_dir, *, fetch=_alpaca_minutes) -> dict[str, list[dict]]`.
  - `median_spread(stream_dir, symbols) -> Decimal`.
  - `add_fast_parser(sub)`, `dispatch_fast(args, *, fetch=None, today=None) -> int`, `cmd_replay(...)`, `cmd_status(config_path)` and `status_lines(data) -> list[str]`.

- [ ] **Step 1: Write the failing test** — `tests/test_fast_replay.py`:

```python
"""Replay runs the live code: same ticks, same decisions; bars are labelled approximate."""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

from agentic_trading.fast.cli import cmd_replay, status_lines
from agentic_trading.fast.replay import bar_ticks, fetch_bars, recorded_ticks, replay
from agentic_trading.fast.service import FastEngine
from agentic_trading.fast.settings import FastConfig
from tests.fast_support import T0, quote
from tests.test_runtime_daemon import _write_config

CFG = FastConfig(enabled=True, symbols=("BTC/USD",))
DAY = T0.date()


def _market() -> list:
    """150 minutes: a steady climb (1.5/min) that trips a breakout, then a slide that stops it out."""
    ticks = []
    for minute in range(150):
        base = 20 + 1.5 * minute if minute < 100 else 170 - 3 * (minute - 100)
        for second, wiggle in ((0, 0.0), (15, 0.3), (30, -0.2), (45, 0.1)):
            at = T0 + timedelta(minutes=minute, seconds=second)
            price = base + wiggle
            ticks.append(quote("BTC-USD", round(price - 0.1, 2), round(price + 0.1, 2), at, venue="coinbase"))
            ticks.append(quote("BTC/USD", round(price - 0.05, 2), round(price + 0.05, 2), at))
    return ticks


def _record(stream: Path, ticks: list) -> None:
    for venue in ("alpaca", "coinbase"):
        folder = stream / venue / "BTCUSD"
        folder.mkdir(parents=True)
        with gzip.open(folder / f"{DAY.isoformat()}.jsonl.gz", "wt", encoding="utf-8") as handle:
            for tick in ticks:
                if tick.venue == venue:
                    handle.write(json.dumps(tick.to_row()) + "\n")


def _seen(events) -> list:
    return [(e.kind, e.at.isoformat(), e.text) for e in events]


class ReplayTests(unittest.TestCase):
    def test_replay_is_deterministic_and_matches_the_live_engine(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            _record(tmp / "stream", _market())
            first = replay(recorded_ticks(tmp / "stream", CFG.symbols, DAY, DAY), CFG, label="exact")
            second = replay(recorded_ticks(tmp / "stream", CFG.symbols, DAY, DAY), CFG, label="exact")
            engine = FastEngine(CFG, state_dir=tmp / "state", journal_dir=tmp / "journal", now=T0)
            for tick in recorded_ticks(tmp / "stream", CFG.symbols, DAY, DAY):
                engine.on_tick(tick)
            live = [json.loads(line) for line in (tmp / "journal" / f"fast-{DAY.isoformat()}.jsonl").read_text().splitlines()]
        kinds = [e.kind for e in first.events]
        self.assertIn("fast_entry", kinds)
        self.assertIn("fast_exit", kinds)
        self.assertEqual(_seen(first.events), _seen(second.events))
        self.assertEqual([(r["event"], r["at"], r["text"]) for r in live], _seen(first.events))
        self.assertEqual(first.ticks, 1200)
        self.assertIn("breakout", first.playbooks)
        self.assertIsNotNone(first.coinbase_return_pct)

    def test_bars_become_four_prices_with_coinbase_first(self) -> None:
        rows = {"BTC/USD": [{"t": T0.isoformat(), "o": "10", "h": "12", "l": "9", "c": "11"},
                            {"t": (T0 + timedelta(minutes=1)).isoformat(), "o": "11", "h": "13", "l": "8", "c": "9"}]}
        ticks = list(bar_ticks(rows, Decimal("0")))
        self.assertEqual([(t.venue, t.symbol) for t in ticks[:2]], [("coinbase", "BTC-USD"), ("alpaca", "BTC/USD")])
        alpaca = [t for t in ticks if t.venue == "alpaca"]
        self.assertEqual([float(t.bid) for t in alpaca], [10, 9, 12, 11, 11, 13, 8, 9])  # up bar: low first
        self.assertEqual(alpaca[1].received_at - alpaca[0].received_at, timedelta(seconds=15))

    def test_bars_are_cached_per_day_and_fetched_once(self) -> None:
        calls = []

        def fake(symbol, start, end):
            calls.append((symbol, start, end))
            return [NS(timestamp=T0 + timedelta(days=d, minutes=m), open=1, high=2, low=0.5, close=1.5)
                    for d in range(2) for m in range(3)]

        with tempfile.TemporaryDirectory() as name:
            first = fetch_bars(("BTC/USD",), DAY, DAY + timedelta(days=1), name, fetch=fake)
            again = fetch_bars(("BTC/USD",), DAY, DAY + timedelta(days=1), name, fetch=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(first["BTC/USD"]), 6)
        self.assertEqual(first, again)


class CommandTests(unittest.TestCase):
    def _config(self, tmp: Path) -> Path:
        path = _write_config(tmp)
        path.write_text(path.read_text() + f'\n[venues]\nstream_dir = "{tmp / "stream"}"\n'
                        '\n[fast]\nsymbols = ["BTC/USD"]\n')
        return path

    def test_bar_replay_says_approximate_and_skips_unfinished_days(self) -> None:
        def fake(symbol, start, end):
            return [NS(timestamp=T0 + timedelta(minutes=m), open=20 + 1.5 * m, high=20.3 + 1.5 * m,
                       low=19.8 + 1.5 * m, close=20.1 + 1.5 * m) for m in range(120)]

        with tempfile.TemporaryDirectory() as name:
            path = self._config(Path(name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_replay(str(path), "bars", DAY.isoformat(), (DAY + timedelta(days=3)).isoformat(),
                                  fetch=fake, today=DAY + timedelta(days=1))
        self.assertEqual(code, 0)
        self.assertIn("approximate", out.getvalue())
        self.assertIn(f"{DAY.isoformat()} → {DAY.isoformat()}", out.getvalue())  # clamped to finished days

    def test_recorded_replay_with_nothing_recorded_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self._config(Path(name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_replay(str(path), "recorded", DAY.isoformat(), DAY.isoformat())
        self.assertEqual(code, 1)
        self.assertIn("no prices", out.getvalue())

    def test_status_reads_in_plain_words(self) -> None:
        lines = status_lines({
            "as_of": T0.isoformat(), "failed": "", "book": {"equity": "50.10", "return_pct": 0.2},
            "mirror": {"return_pct": -0.4},
            "coins": [{"symbol": "BTC/USD", "regime": "trending", "standing_aside": False,
                       "trade": {"playbook": "breakout", "entry": 105.2, "stop": 99.2, "pnl_pct": 0.5}},
                      {"symbol": "ETH/USD", "regime": "choppy", "standing_aside": True, "trade": None}],
            "recent": [{"at": T0.isoformat(), "text": "Bought $33.00 of BTC/USD"}],
        })
        text = "\n".join(lines)
        self.assertIn("breakout open at 105.2", text)
        self.assertIn("ETH/USD: choppy · standing aside", text)
        self.assertIn("-0.40% at Coinbase costs", text)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/test_fast_replay.py -q`
Expected: collection ERROR, `No module named 'agentic_trading.fast.cli'`.

- [ ] **Step 3: Implement.** First `src/agentic_trading/fast/replay.py`:

```python
"""Replay the switchboard over past prices, with the same code the live service runs.

Two sources:
* **recorded** (exact): the venues recorder's gzip files under
  ``data/stream``. Alpaca and Coinbase ticks are merged in arrival order.
* **bars** (approximate): Alpaca's historical 1-minute crypto bars. Each bar
  becomes four prices 15 s apart: the open; then the low before the high for
  an up bar (the high before the low for a down bar); then the close. They use
  the median spread seen in recordings. The order of prices inside a minute
  is a guess, so the report says "approximate".
"""

from __future__ import annotations

import gzip
import heapq
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.settings import FastConfig
from agentic_trading.fast.switchboard import Event, Switchboard, tick_price
from agentic_trading.venues.model import Tick

DEFAULT_SPREAD = Decimal("0.0010")  # 10 bps until something has been recorded
STEP = timedelta(seconds=15)
SPREAD_SAMPLE = 5000


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


@dataclass
class Report:
    label: str
    start: str
    end: str
    ticks: int
    playbooks: dict[str, dict[str, Any]]
    alpaca_return_pct: float
    coinbase_return_pct: Optional[float]
    hold_return_pct: Optional[float]
    skipped: int
    unpriced: int
    events: list[Event] = field(default_factory=list, repr=False)

    def lines(self) -> list[str]:
        out = [f"switchboard replay ({self.label}) {self.start} → {self.end}: {self.ticks:,} prices"]
        if not self.playbooks:
            out.append("  no trades")
        for name, stats in sorted(self.playbooks.items()):
            out.append(f"  {name}: {stats['trades']} trades, {stats['wins']} won, "
                       f"average win {_pct(stats['avg_win_pct'])}, average loss {_pct(stats['avg_loss_pct'])}")
        out.append(f"  at Alpaca costs: {_pct(self.alpaca_return_pct)}")
        out.append(f"  at Coinbase costs: {_pct(self.coinbase_return_pct)} ({self.unpriced} trades not copied)")
        out.append(f"  just holding the same coins: {_pct(self.hold_return_pct)}")
        out.append(f"  setups skipped by the cost gate: {self.skipped}")
        return out


def _return(book: MemberBook) -> float:
    return round(float(book.equity / book.starting_equity - 1) * 100, 3)


def replay(ticks: Iterable[Tick], config: FastConfig, *, label: str, start: str = "", end: str = "",
           starting_equity: Decimal = Decimal("50")) -> Report:
    book = MemberBook("switchboard", starting_equity=starting_equity)
    mirror = CoinbaseMirror(MemberBook("switchboard@coinbase", starting_equity=starting_equity), config.coinbase_fee)
    board = Switchboard(config, book, mirror=mirror)
    first: dict[str, Decimal] = {}
    last: dict[str, Decimal] = {}
    events: list[Event] = []
    exits: dict[str, list[float]] = {}
    count = 0
    for tick in ticks:
        count += 1
        if tick.venue == "alpaca" and tick.symbol in config.symbols:
            price = tick_price(tick)
            if price is not None:
                first.setdefault(tick.symbol, price)
                last[tick.symbol] = price
        for event in board.on_tick(tick):
            events.append(event)
            if event.kind == "fast_exit":
                exits.setdefault(event.data["playbook"], []).append(float(event.data["net_pct"]))
    playbooks: dict[str, dict[str, Any]] = {}
    for name, results in exits.items():
        wins = [r for r in results if r > 0]
        losses = [r for r in results if r <= 0]
        playbooks[name] = {
            "trades": len(results), "wins": len(wins),
            "avg_win_pct": round(statistics.fmean(wins), 3) if wins else None,
            "avg_loss_pct": round(statistics.fmean(losses), 3) if losses else None,
        }
    holds = [float(last[s] / first[s] - 1) * 100 for s in first if first[s] > 0]
    copied_nothing = book.entries > 0 and mirror.book.entries == 0
    return Report(
        label, start, end, count, playbooks, _return(book),
        None if copied_nothing else _return(mirror.book),
        round(statistics.fmean(holds), 3) if holds else None,
        board.skipped_total, mirror.unpriced, events,
    )


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _row_tick(row: Any) -> Optional[Tick]:
    if not isinstance(row, dict):
        return None

    def number(key: str) -> Optional[Decimal]:
        value = row.get(key)
        return None if value in (None, "") else Decimal(str(value))

    try:
        return Tick(str(row["venue"]), str(row["symbol"]), number("bid"), number("ask"), number("last"),
                    number("size"), datetime.fromisoformat(str(row["exchange_at"])),
                    datetime.fromisoformat(str(row["received_at"])))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def _read_rows(path: Path) -> Iterator[Any]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (OSError, EOFError):
        return  # a file cut short by a crash: keep what was read


def recorded_ticks(stream_dir: Path | str, symbols: Iterable[str], start: date, end: date) -> Iterator[Tick]:
    """Every recorded Alpaca and Coinbase tick for ``symbols``, a day at a time, in arrival order."""
    folders = [s.replace("/", "") for s in symbols]
    for day in _days(start, end):
        ticks: list[Tick] = []
        # Coinbase first: at equal arrival times the stable sort keeps it ahead, so the
        # mirror holds that moment's Coinbase quote when the Alpaca tick fills.
        for venue in ("coinbase", "alpaca"):
            for folder in folders:
                path = Path(stream_dir) / venue / folder / f"{day.isoformat()}.jsonl.gz"
                if path.is_file():
                    ticks.extend(t for t in map(_row_tick, _read_rows(path)) if t is not None)
        ticks.sort(key=lambda t: t.received_at)
        yield from ticks


def median_spread(stream_dir: Path | str, symbols: Iterable[str]) -> Decimal:
    """The median relative Alpaca spread in the newest recording, else 10 bps."""
    spreads: list[float] = []
    for folder in (s.replace("/", "") for s in symbols):
        files = sorted((Path(stream_dir) / "alpaca" / folder).glob("*.jsonl.gz"))
        if not files:
            continue
        for index, row in enumerate(_read_rows(files[-1])):
            if index >= SPREAD_SAMPLE:
                break
            tick = _row_tick(row)
            if tick is not None and tick.bid and tick.ask and tick.ask >= tick.bid > 0:
                spreads.append(float((tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2)))
    return Decimal(str(round(statistics.median(spreads), 6))) if spreads else DEFAULT_SPREAD


def _bar_prices(row: dict[str, Any]) -> list[Decimal]:
    o, h, l, c = (Decimal(str(row[key])) for key in ("o", "h", "l", "c"))
    return [o, l, h, c] if c >= o else [o, h, l, c]


def _symbol_ticks(symbol: str, rows: list[dict[str, Any]], spread: Decimal) -> Iterator[Tick]:
    product = CoinbaseMirror.product(symbol)
    half = spread / 2
    for row in rows:
        try:
            at = datetime.fromisoformat(str(row["t"]))
            prices = _bar_prices(row)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        for index, price in enumerate(prices):
            when = at + STEP * index
            bid, ask = price * (1 - half), price * (1 + half)
            # Coinbase first, so the mirror holds this moment's quote when the Alpaca tick fills
            yield Tick("coinbase", product, bid, ask, None, None, when, when)
            yield Tick("alpaca", symbol, bid, ask, None, None, when, when)


def bar_ticks(rows: dict[str, list[dict[str, Any]]], spread: Decimal) -> Iterator[Tick]:
    return heapq.merge(*(_symbol_ticks(s, r, spread) for s, r in rows.items()), key=lambda t: t.received_at)


def _alpaca_minutes(symbol: str, start: datetime, end: datetime) -> list[Any]:  # pragma: no cover - network
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    request = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end)
    response = CryptoHistoricalDataClient().get_crypto_bars(request)
    return list(response.data.get(symbol, []))


def fetch_bars(symbols: Iterable[str], start: date, end: date, cache_dir: Path | str, *,
               fetch: Callable[[str, datetime, datetime], list[Any]] = _alpaca_minutes) -> dict[str, list[dict[str, Any]]]:
    """1-minute bars per symbol for ``start``..``end``, cached as one file per symbol per day.

    Only days with no cache file are fetched (one request per symbol). A day
    is cached even when it has no bars, so a quiet day is not requested again.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        folder = Path(cache_dir) / symbol.replace("/", "")
        missing = [d for d in _days(start, end) if not (folder / f"{d.isoformat()}.json").is_file()]
        if missing:
            begin = datetime.combine(missing[0], time.min, tzinfo=timezone.utc)
            stop = datetime.combine(missing[-1] + timedelta(days=1), time.min, tzinfo=timezone.utc)
            by_day: dict[str, list[dict[str, Any]]] = {d.isoformat(): [] for d in missing}
            for bar in fetch(symbol, begin, stop):
                at = bar.timestamp.astimezone(timezone.utc)
                if at.date().isoformat() in by_day:
                    by_day[at.date().isoformat()].append({
                        "t": at.isoformat(), "o": str(bar.open), "h": str(bar.high),
                        "l": str(bar.low), "c": str(bar.close)})
            folder.mkdir(parents=True, exist_ok=True)
            for key, rows in by_day.items():
                (folder / f"{key}.json").write_text(json.dumps(rows), encoding="utf-8")
        rows_all: list[dict[str, Any]] = []
        for day in _days(start, end):
            try:
                rows_all.extend(json.loads((folder / f"{day.isoformat()}.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        out[symbol] = rows_all
    return out
```

`src/agentic_trading/fast/cli.py`:

```python
"""``agentic-trading fast``: replay the switchboard over past prices, or show its status."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def add_fast_parser(sub: Any) -> None:
    parser = sub.add_parser("fast", help="The switchboard: replay it over past prices, or show its status")
    actions = parser.add_subparsers(dest="fast_action", required=True)
    replay_p = actions.add_parser("replay", help="Run the switchboard over recorded prices or past 1-minute bars")
    replay_p.add_argument("--config", required=True)
    replay_p.add_argument("--source", choices=("recorded", "bars"), default="recorded")
    replay_p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    replay_p.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    status_p = actions.add_parser("status", help="What the running switchboard is doing, in plain words")
    status_p.add_argument("--config", required=True)


def dispatch_fast(args: Any, *, fetch: Optional[Callable[..., Any]] = None, today: Optional[date] = None) -> int:
    if args.fast_action == "replay":
        return cmd_replay(args.config, args.source, args.start, args.end, fetch=fetch, today=today)
    return cmd_status(args.config)


def cmd_replay(config_path: str, source: str, start_text: str, end_text: str, *,
               fetch: Optional[Callable[..., Any]] = None, today: Optional[date] = None) -> int:
    from agentic_trading.config import load_config
    from agentic_trading.fast.replay import bar_ticks, fetch_bars, median_spread, recorded_ticks, replay
    from agentic_trading.fast.settings import load_fast_config
    from agentic_trading.fast.store import FastStore
    from agentic_trading.venues.settings import load_venues_config

    try:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    except ValueError:
        print("fast replay: dates must look like 2026-10-01")
        return 2
    if end < start:
        print("fast replay: --to is before --from")
        return 2
    config = load_config(config_path)
    fast = load_fast_config(config_path)
    venues = load_venues_config(config_path)
    if source == "recorded":
        ticks: Any = recorded_ticks(venues.stream_dir, fast.symbols, start, end)
        label = "exact, recorded prices"
    else:
        end = min(end, (today or datetime.now(timezone.utc).date()) - timedelta(days=1))
        if end < start:
            print("fast replay: bars exist only for finished days; pick an earlier --from")
            return 2
        cache = Path(config.state_dir).parent / "fastbars"
        rows = fetch_bars(fast.symbols, start, end, cache, **({"fetch": fetch} if fetch else {}))
        ticks = bar_ticks(rows, median_spread(venues.stream_dir, fast.symbols))
        label = "approximate, 1-minute bars"
    report = replay(ticks, fast, label=label, start=start.isoformat(), end=end.isoformat(),
                    starting_equity=FastStore(config.state_dir).starting_equity())
    if report.ticks == 0:
        print(f"fast replay: no prices between {start} and {end}")
        return 1
    print("\n".join(report.lines()))
    return 0


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):+.2f}%"


def status_lines(data: dict[str, Any]) -> list[str]:
    head = f"switchboard as of {data.get('as_of')}"
    if data.get("failed"):
        head += f" — STOPPED: {data['failed']}"
    book, mirror = data.get("book") or {}, data.get("mirror") or {}
    lines = [head, f"  paper book ${book.get('equity')} ({_pct(book.get('return_pct'))} at Alpaca costs; "
                   f"{_pct(mirror.get('return_pct'))} at Coinbase costs)"]
    for coin in data.get("coins") or []:
        trade = coin.get("trade")
        if trade:
            what = (f"{trade.get('playbook')} open at {trade.get('entry')}, stop {trade.get('stop')}, "
                    f"{_pct(trade.get('pnl_pct'))}")
        elif coin.get("standing_aside"):
            what = "standing aside"
        else:
            what = "watching for a setup"
        lines.append(f"  {coin.get('symbol')}: {coin.get('regime')} · {what}")
    for record in (data.get("recent") or [])[:5]:
        lines.append(f"  {str(record.get('at', ''))[11:19]} {record.get('text', '')}")
    return lines


def cmd_status(config_path: str) -> int:
    from agentic_trading.config import load_config

    config = load_config(config_path)
    try:
        data = json.loads((Path(config.state_dir) / "fast.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("the switchboard has not run yet: set [fast] enabled = true and restart agentic-trading-venues")
        return 1
    print("\n".join(status_lines(data)))
    return 0
```

In `src/agentic_trading/cli.py`, directly after the line `    add_venues_parser(sub)`, add:

```python
    from agentic_trading.fast.cli import add_fast_parser

    add_fast_parser(sub)
```

and directly after the `venues` route (the line `        return dispatch_venues(args)`), add:

```python
    if args.command == "fast":
        from agentic_trading.fast.cli import dispatch_fast

        return dispatch_fast(args)
```

Append `data/fastbars/` to `.gitignore` on its own line.

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/python -m pytest tests/test_fast_replay.py -q`
Expected: `6 passed`.

Also run: `.venv/bin/agentic-trading fast --help`
Expected: usage text listing `replay` and `status`.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/fast/replay.py src/agentic_trading/fast/cli.py src/agentic_trading/cli.py .gitignore tests/test_fast_replay.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(fast): replay over recorded ticks or past bars, and fast replay/status"
```

---

### Task 10: The console: `/api/fast`, the Switchboard card and the ticker

**Files:**
- Create: `src/agentic_trading/dashboard_fast.py`
- Modify:
  - `src/agentic_trading/dashboard.py` (`DashboardState.fast()`, the `/api/fast` route)
  - `src/agentic_trading/dashboard_desk.py` (label, fast ticker events, reading `fast-*.jsonl`, ordering by time)
  - `src/agentic_trading/dashboard_html.py`, `src/agentic_trading/dashboard_css.py`, `src/agentic_trading/dashboard_cockpit_js.py`
- Test: `tests/test_dashboard_fast.py`; append to `tests/test_cockpit_js.py` (`CockpitFormatTests`) and `tests/test_cockpit_page.py` (`CockpitPageTests`)

**Interfaces:**
- Consumes:
  - `data/state/fast.json` (Task 7: `enabled`, `as_of`, `failed`, `coins[]`, `book`, `mirror`, `skipped`, `halted`, `recent[]`).
  - Journal `desk_allocation.would_earn` (Task 8); `fast_entry`/`fast_exit` records with `text` (Task 5).
- Produces:
  - `fast_view(state_dir, events=(), *, now=None) -> dict`.
  - `GET /api/fast`.
  - `CockpitFmt.regimeWord`, `fastCompare`, `fundedBadge` and `tradeLine`.
  - The `#fast` card on the Strategies tab; the ticker kind `fast`.

- [ ] **Step 1: Write the failing tests.** First `tests/test_dashboard_fast.py`:

```python
"""/api/fast: whitelisted switchboard state; fast events reach the ticker in time order."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.dashboard_desk import DeskEventCache, label, ticker_item
from agentic_trading.dashboard_fast import fast_view

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _state(folder: Path, as_of: datetime) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fast.json").write_text(json.dumps({
        "enabled": True, "as_of": as_of.isoformat(), "failed": "", "halted": False, "skipped": 3,
        "secret": "LEAK-top",
        "coins": [{"symbol": "BTC/USD", "regime": "trending", "standing_aside": False, "api_key": "LEAK-coin",
                   "trade": {"playbook": "breakout", "entry": 105.2, "stop": 99.2, "target": None,
                             "pnl_pct": 0.5, "opened_at": as_of.isoformat(), "note": "LEAK-trade"}}],
        "book": {"equity": "50.25", "return_pct": 0.5, "entries": 1, "exits": 0, "cash": "LEAK-book"},
        "mirror": {"equity": "49.80", "return_pct": -0.4, "unpriced": 1},
        "recent": [{"at": as_of.isoformat(), "event": "fast_entry", "symbol": "BTC/USD",
                    "text": "Bought $16.67 of BTC/USD", "quantity": "LEAK-recent"}],
    }))


class FastViewTests(unittest.TestCase):
    def test_no_switchboard_means_disabled_with_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            view = fast_view(name, now=T0)
        self.assertFalse(view["enabled"])
        self.assertIn("[fast] enabled = true", view["note"])

    def test_only_whitelisted_fields_pass_and_would_earn_comes_from_the_desk(self) -> None:
        events = [{"event": "desk_allocation", "would_earn": {"switchboard": 0.42}}]
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            view = fast_view(name, events, now=T0 + timedelta(seconds=2))
        self.assertNotIn("LEAK", json.dumps(view))
        self.assertFalse(view["stale"])
        self.assertEqual(view["coins"][0]["trade"]["playbook"], "breakout")
        self.assertEqual((view["book"]["return_pct"], view["mirror"]["unpriced"]), (0.5, 1))
        self.assertEqual(view["would_earn"], 0.42)

    def test_an_old_file_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            self.assertTrue(fast_view(name, now=T0 + timedelta(minutes=2))["stale"])

    def test_the_route_serves_it(self) -> None:
        from agentic_trading.config import load_config
        from agentic_trading.dashboard import serve
        from tests.test_runtime_daemon import _write_config

        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name)))
            _state(Path(config.state_dir), datetime.now(timezone.utc))
            server = serve(config, host="127.0.0.1", port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/fast"
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["coins"][0]["symbol"], "BTC/USD")


class TickerTests(unittest.TestCase):
    def test_fast_trades_are_ticker_news_and_merge_in_time_order(self) -> None:
        item = ticker_item({"event": "fast_entry", "at": T0.isoformat(), "text": "Bought $16.67 of BTC/USD"})
        self.assertEqual((item["kind"], item["text"]), ("fast", "Bought $16.67 of BTC/USD"))
        self.assertEqual(label("switchboard"), "Switchboard")
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "2026-10-05.jsonl").write_text(json.dumps(
                {"event": "desk_allocation", "at": "2026-10-05T11:00:00+00:00", "changed": False}) + "\n")
            (folder / "fast-2026-10-05.jsonl").write_text(
                json.dumps({"event": "fast_entry", "at": "2026-10-05T10:00:00.500000+00:00", "text": "in"}) + "\n"
                + json.dumps({"event": "fast_exit", "at": "2026-10-05T12:00:00.250000+00:00", "text": "out"}) + "\n")
            events = DeskEventCache(folder).read()
        self.assertEqual([e["event"] for e in events], ["fast_entry", "desk_allocation", "fast_exit"])
```

Append to `CockpitFormatTests` in `tests/test_cockpit_js.py` (before `class CockpitPageWiringTests`):

```python
    def test_switchboard_words(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.regimeWord('choppy'), CockpitFmt.regimeWord('trending'), CockpitFmt.regimeWord('odd')]"),
            ["choppy · standing aside", "trending", "unknown"],
        )
        self.assertEqual(
            self.js("CockpitFmt.fastCompare({book: {return_pct: 0.42}, mirror: {return_pct: -0.31, unpriced: 2}})"),
            "the same trades: +0.42% at Alpaca, −0.31% at Coinbase (2 not copied)",
        )
        self.assertEqual(self.js("CockpitFmt.fastCompare({book: {return_pct: null}})"), "no trades yet")
        self.assertEqual(
            self.js("[CockpitFmt.fundedBadge(null), CockpitFmt.fundedBadge(0.42)]"),
            ["judged only — not yet funded", "judged only — not yet funded · would earn 42%"],
        )
        self.assertEqual(
            self.js("[CockpitFmt.tradeLine({standing_aside: true, trade: null}),"
                    " CockpitFmt.tradeLine({standing_aside: false, trade: null}),"
                    " CockpitFmt.tradeLine({trade: {playbook: 'breakout', entry: 105.2, stop: 99.2, pnl_pct: 0.5}})]"),
            ["standing aside", "watching for a setup", "breakout open at 105.2 · stop 99.2 · +0.50%"],
        )
```

Append to `CockpitPageTests` in `tests/test_cockpit_page.py` (before `class CockpitSizingTests`):

```python
    def test_the_switchboard_card_is_on_the_strategies_tab(self) -> None:
        self.assertIn('id="fast"', _section("strategies"))
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard_fast.py tests/test_cockpit_js.py tests/test_cockpit_page.py -q`
Expected: collection ERROR for `tests/test_dashboard_fast.py` (`No module named 'agentic_trading.dashboard_fast'`). Run the other two files alone and expect exactly 2 failures: `test_switchboard_words` and `test_the_switchboard_card_is_on_the_strategies_tab`.

- [ ] **Step 3: Implement.** First `src/agentic_trading/dashboard_fast.py`:

```python
"""The switchboard as the console sees it: named fields from ``data/state/fast.json``.

Only named fields pass through, so nothing the service writes later can reach
the page by accident. ``would_earn`` comes from the desk's latest weekly
allocation event, not from the service.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

COIN_FIELDS = ("symbol", "regime", "standing_aside")
TRADE_FIELDS = ("playbook", "entry", "stop", "target", "pnl_pct", "opened_at")
BOOK_FIELDS = ("equity", "return_pct", "entries", "exits")
MIRROR_FIELDS = ("equity", "return_pct", "unpriced")
RECENT_FIELDS = ("at", "event", "symbol", "text")
FRESH = timedelta(seconds=30)
NOTE = "the switchboard is not running: set [fast] enabled = true, then restart agentic-trading-venues"


def _pick(raw: Any, names: tuple[str, ...]) -> Optional[dict[str, Any]]:
    return {name: raw.get(name) for name in names} if isinstance(raw, dict) else None


def _would_earn(events: Iterable[dict[str, Any]]) -> Optional[float]:
    for record in reversed(list(events)):
        if record.get("event") == "desk_allocation" and isinstance(record.get("would_earn"), dict):
            try:
                return float(record["would_earn"].get("switchboard"))
            except (TypeError, ValueError):
                return None
    return None


def fast_view(state_dir: Path | str, events: Iterable[dict[str, Any]] = (), *,
              now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    try:
        data = json.loads((Path(state_dir) / "fast.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"enabled": False, "note": NOTE}
    try:
        as_of: Optional[datetime] = datetime.fromisoformat(str(data.get("as_of")))
    except ValueError:
        as_of = None
    if as_of is not None and as_of.tzinfo is None:
        as_of = None
    coins = []
    for raw in data.get("coins") or []:
        if isinstance(raw, dict):
            coin = _pick(raw, COIN_FIELDS) or {}
            coin["trade"] = _pick(raw.get("trade"), TRADE_FIELDS)
            coins.append(coin)
    recent = [_pick(r, RECENT_FIELDS) for r in (data.get("recent") or []) if isinstance(r, dict)][:10]
    return {
        "enabled": True,
        "as_of": data.get("as_of"),
        "stale": as_of is None or current - as_of > FRESH,
        "failed": str(data.get("failed") or ""),
        "halted": bool(data.get("halted")),
        "skipped": data.get("skipped"),
        "coins": coins,
        "book": _pick(data.get("book"), BOOK_FIELDS),
        "mirror": _pick(data.get("mirror"), MIRROR_FIELDS),
        "recent": recent,
        "would_earn": _would_earn(events),
    }
```

In `src/agentic_trading/dashboard.py`:
- Add `from agentic_trading.dashboard_fast import fast_view` directly above `from agentic_trading.dashboard_venues import venues_view`.
- Add this method directly above `    def venues(self) -> dict[str, Any]:`:

```python
    def fast(self) -> dict[str, Any]:
        """The switchboard's live state (``/api/fast``)."""
        self.refresh_config()
        if self._desk_events.journal_dir != self.journal_dir:
            self._desk_events = DeskEventCache(self.journal_dir)
        return fast_view(self.state_dir, self._desk_events.read())

```

- Directly after the `/api/venues` route's `            return`, add:

```python
        if parsed.path == "/api/fast":
            self._json(self.state.fast())
            return
```

In `src/agentic_trading/dashboard_desk.py`:
- In `LABELS`, add `    "switchboard": "Switchboard",` after `"dip_reversal": "Dip buyer",`.
- In `TICKER_EVENTS`, add `"fast_entry",` and `"fast_exit",` after `"kill_switch",`.
- In `ticker_item`, directly before `    else:  # kill_switch`, add:

```python
    elif event in ("fast_entry", "fast_exit"):
        kind = "fast"
        text = str(record.get("text") or "")[:200]
```

- In `DeskEventCache.read`, replace
```python
            paths = sorted(
                (p for p in self.journal_dir.glob("*.jsonl") if p.name[:1].isdigit()),
                key=lambda p: p.name,
            )[-days:]
```
with
```python
            paths = sorted(
                (p for p in self.journal_dir.glob("*.jsonl")
                 if p.name[:1].isdigit() or p.name.startswith("fast-")),
                key=lambda p: p.name.removeprefix("fast-"),
            )[-2 * days:]
```
  and replace the method's final `        return events` with:
```python
        # The trader's and the switchboard's journals interleave: order by time.
        events.sort(key=lambda record: str(record.get("at") or ""))
        return events
```

In `src/agentic_trading/dashboard_html.py`, directly after the line containing `id="members"`, add:

```html
<div class="card span12"><h2>Switchboard · fast crypto playbooks on live prices (paper)</h2><div id="fast" class="fast"><div class="sub">reading the switchboard…</div></div></div>
```

In `src/agentic_trading/dashboard_css.py`, directly above the line starting `/* Stacked: the page scrolls`, add:

```css
.fast{display:grid;gap:8px}
.fhead{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.fbadge{padding:2px 10px;border-radius:999px;border:1px solid var(--shadow);color:var(--shadow);font-size:.85em}
.fcoin{display:flex;gap:14px;flex-wrap:wrap;align-items:center;padding:6px 0;border-bottom:1px dashed #1a2330}
.fcoin b{min-width:90px}
.fchip{display:inline-block;padding:1px 9px;border-radius:999px;background:#1b2431;color:var(--muted);font-size:.85em}
.fchip.trending{color:var(--buy)}
.fchip.squeeze{color:var(--accent)}
.fchip.choppy,.fchip.unclear{color:var(--warn)}
.frecent{margin:4px 0 0;padding-left:18px;color:var(--muted)}
.frecent li{margin:2px 0}
.item.fast .dot{background:var(--accent)}
```

In `src/agentic_trading/dashboard_cockpit_js.py`:
1. Directly above the line `  return { pct, money, countdown, spread, moneyParts, tickerKey, accept, healthLevel, sampleTime, venueLevel, worst };`, add:

```js
  // The switchboard card's words.
  const REGIME_WORDS = { trending: 'trending', squeeze: 'squeeze', choppy: 'choppy · standing aside',
    unclear: 'unclear · standing aside', warming: 'warming up' };
  const regimeWord = (regime) => REGIME_WORDS[regime] || 'unknown';
  const signedPct = (x) => (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(2) + '%';
  const fastCompare = (view) => {
    const a = view && view.book, c = view && view.mirror;
    if (!a || a.return_pct == null) return 'no trades yet';
    let text = 'the same trades: ' + signedPct(a.return_pct) + ' at Alpaca';
    if (c && c.return_pct != null) text += ', ' + signedPct(c.return_pct) + ' at Coinbase';
    if (c && c.unpriced) text += ' (' + c.unpriced + ' not copied)';
    return text;
  };
  const fundedBadge = (would) => 'judged only — not yet funded'
    + (would == null ? '' : ' · would earn ' + Math.round(would * 100) + '%');
  const tradeLine = (coin) => {
    const t = coin && coin.trade;
    if (!t) return coin && coin.standing_aside ? 'standing aside' : 'watching for a setup';
    return t.playbook + ' open at ' + t.entry + ' · stop ' + t.stop
      + (t.pnl_pct == null ? '' : ' · ' + signedPct(t.pnl_pct));
  };
```

   and change that return line to:

```js
  return { pct, money, countdown, spread, moneyParts, tickerKey, accept, healthLevel, sampleTime, venueLevel, worst,
    regimeWord, fastCompare, fundedBadge, tradeLine };
```

2. Directly after the end of `pollVenues` (the lines `    renderVenues(lastVenues);` / `    applyDot();` / `  }`), add:

```js

  function renderFast(view) {
    const box = $('fast');
    if (!box) return;
    if (!view || !view.enabled) {
      box.innerHTML = '<div class="sub">' + esc((view && view.note) || 'the switchboard is not running') + '</div>';
      return;
    }
    const head = '<div class="fhead"><span class="fbadge">' + esc(CockpitFmt.fundedBadge(view.would_earn))
      + '</span><span class="sub">' + esc(CockpitFmt.fastCompare(view)) + '</span></div>';
    let notes = '';
    if (view.failed) notes += '<div class="sub">' + esc('stopped: ' + view.failed) + '</div>';
    else if (view.stale) notes += '<div class="sub">' + esc('the switchboard has not reported for a while') + '</div>';
    if (view.halted) notes += '<div class="sub">' + esc('daily loss stop: no new entries until tomorrow (UTC)') + '</div>';
    const coins = (view.coins || []).map((c) => '<div class="fcoin"><b>' + esc(c.symbol) + '</b><span class="fchip '
      + esc(c.regime) + '">' + esc(CockpitFmt.regimeWord(c.regime)) + '</span><span>' + esc(CockpitFmt.tradeLine(c))
      + '</span></div>').join('');
    const recent = (view.recent || []).map((r) => '<li>' + esc(r.text) + '</li>').join('');
    box.innerHTML = head + notes + coins
      + (recent ? '<ul class="frecent">' + recent + '</ul>' : '<div class="sub">no decisions yet</div>');
  }

  async function pollFast() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/fast');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      renderFast(await response.json());
    } catch (e) {
      // keep the last picture; the header dot already shows a lost console
    }
  }
```

3. Directly after `    setInterval(pollVenues, 5000);` in `start()`, add:

```js
    pollFast();
    setInterval(pollFast, 2000);
```

- [ ] **Step 4: Run them to see them pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard_fast.py tests/test_cockpit_js.py tests/test_cockpit_page.py tests/test_console_language.py tests/test_history_dashboard.py tests/test_dashboard_desk.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/dashboard_fast.py src/agentic_trading/dashboard.py src/agentic_trading/dashboard_desk.py src/agentic_trading/dashboard_html.py src/agentic_trading/dashboard_css.py src/agentic_trading/dashboard_cockpit_js.py tests/test_dashboard_fast.py tests/test_cockpit_js.py tests/test_cockpit_page.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): the Switchboard card, /api/fast and fast trades in the ticker"
```

---

### Task 11: Packaging, docs and live acceptance

**Files:**
- Modify: `windows/AgenticTrader.spec` (`hiddenimports`), `README.md` (a "Fast engine" section and the test count), `CONTRIBUTING.md` (the test count)
- Local only, never committed: `config/agentic.toml`, backed up first

**Interfaces:**
- Consumes: everything above.
- Produces: the switchboard running live on paper inside `agentic-trading-venues`; the desk carrying `switchboard` as a read-only member; a pushed `feat/fast-engine` branch with a PR based on `feat/venues`; a green windows-build.

- [ ] **Step 1: Packaging and docs**

In `windows/AgenticTrader.spec`, add these directly after `    "agentic_trading.dashboard_venues",`:

```python
    "agentic_trading.fast.cli",
    "agentic_trading.fast.service",
    "agentic_trading.fast.replay",
    "agentic_trading.dashboard_fast",
```

In `README.md`, directly above `## How it decides`, add:

```markdown
### Fast engine (the switchboard)

The switchboard is a paper desk member that trades BTC, ETH and SOL on live Alpaca prices inside the
venues service.
- **Reading the market:** every minute it classifies each coin as trending, squeeze, choppy or unclear.
  It runs a breakout or pullback playbook in a trend and a squeeze-break playbook in a squeeze, and
  stands aside otherwise.
- **Only when worth the fees:** it enters only when the expected move is at least 3× the round-trip cost.
- **Honest fills:** at the real ask or bid, 250 ms after each decision, with Alpaca's fee. The same
  trades are also priced at Coinbase costs for comparison.
- **Judged, not funded:** the desk judges its book like any member's, but holds its weight at 0.
  Funding it would need a live order path that does not exist yet.

1. Add `[fast]` with `enabled = true` to `config/agentic.toml`, and add `"switchboard"` to `desk_members`.
2. Restart `agentic-trading-venues` (it runs the switchboard) and `agentic-trading` (the desk reads it).
3. `agentic-trading fast status --config config/agentic.toml` shows what it is doing.
4. `agentic-trading fast replay --config config/agentic.toml --source bars --from 2026-07-01 --to 2026-09-30`
   replays it over past 1-minute bars (approximate). `--source recorded` replays recorded prices exactly.
```

- [ ] **Step 2: Full suite and documentation count**

1. Run `.venv/bin/python -m pytest tests -q -p no:randomly 2>&1 | tail -1` and note N (the passed count).
2. Update the count to N in `README.md` line 6 (`tests-<old>%20passing`), in the README's `tests/ … <old> tests` line, and in `CONTRIBUTING.md` (`# <old> tests, all must pass`).
3. Run `.venv/bin/python tools/check_doc_counts.py`. Expected: `docs agree with the suite: N tests`.

Commit:

```bash
git add windows/AgenticTrader.spec README.md CONTRIBUTING.md
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "build(fast): Windows bundling and the switchboard's setup docs"
```

- [ ] **Step 3: Turn it on locally (config is never committed)**

```bash
cp config/agentic.toml config/agentic.toml.bak-fast
```

Append to `config/agentic.toml`:

```toml

[fast]
enabled = true
```

In the same file, change `desk_members = ["momentum_rotation", "trend_crypto", "benchmark"]` to
`desk_members = ["momentum_rotation", "trend_crypto", "switchboard", "benchmark"]`.

Run: `.venv/bin/python -c "from agentic_trading.config import load_config; from agentic_trading.fast.settings import load_fast_config; print(load_config('config/agentic.toml').desk_members, load_fast_config('config/agentic.toml').enabled)"`
Expected: `('momentum_rotation', 'trend_crypto', 'switchboard', 'benchmark') True`.

Then restart both services:

```bash
systemctl --user restart agentic-trading-venues agentic-trading
```

- [ ] **Step 4: 30 minutes live (acceptance 1)**

Wait 30 minutes with a `run_in_background` until-loop that checks `systemctl --user is-active agentic-trading-venues` every 15 s; never use a foreground sleep. Then run:

```bash
systemctl --user is-active agentic-trading-venues agentic-trading
.venv/bin/agentic-trading fast status --config config/agentic.toml
.venv/bin/python -c "import json; d=json.load(open('data/state/venues.json')); print([v for v in d['venues'] if v['name']=='switchboard'])"
grep -c '"event":"fast_regime"' data/journal/fast-$(date -u +%F).jsonl
grep -n "desk_member_reset\|switchboard" data/journal/$(date -u +%F).jsonl | tail -3
```

Expected:
- both services `active`;
- `fast status` lists BTC/USD, ETH/USD and SOL/USD, each with a reading. The first hour shows `warming`, since the regime needs 60 bars. That is correct, not a failure.
- the switchboard health row is `status: ok`;
- at least 3 `fast_regime` lines (each coin leaves `warming` within about 61 minutes; if 30 minutes is too early, re-check at 65 minutes);
- the trader process loaded the member with no errors.

Any entries and exits carry plain reasons in `fast-<date>.jsonl`.

- [ ] **Step 5: The failure path (acceptance 2)**

This is covered by `tests/test_fast_service.py::ServiceTests::test_a_strategy_error_stops_only_the_engine`, which marks the engine `error` while the recorder keeps writing. Run it once more by name and confirm it passes. Breaking the live service on purpose is not needed.

- [ ] **Step 6: Replay over ~90 days of bars (acceptance 3)**

Run: `.venv/bin/agentic-trading fast replay --config config/agentic.toml --source bars --from $(date -u -d '91 days ago' +%F) --to $(date -u -d 'yesterday' +%F)`

Expected:
- the header reads `switchboard replay (approximate, 1-minute bars)`;
- per-playbook lines, or `no trades`;
- the results at Alpaca costs, at Coinbase costs, and for just holding the same coins.

This fetches from Alpaca's free market-data API (no keys) and caches the bars under `data/fastbars/`, which git ignores. Report the numbers to the user exactly as printed, with no spin. If the switchboard trails holding, say so.

- [ ] **Step 7: The desk's judgement (acceptance 4)**

`tests/test_fast_desk.py::UnfundedTests` proves `would_earn` and the zero weight. The first live weekly allocation with the switchboard happens at the next Monday 00:00 UTC. Check it then with:

`grep '"event": *"desk_allocation"' data/journal/*.jsonl | tail -1`

Expect a `"would_earn": {"switchboard": …}` entry, with `"switchboard": 0.0` in `allocations`. Record that the check is pending until then.

- [ ] **Step 8: The console (acceptance 5)**

1. Run `systemctl --user restart agentic-trading-dashboard.service`.
2. With Playwright: resize to 1440×900, open `http://127.0.0.1:8787/`, click the `Strategies` tab, wait 4 s, and screenshot `.playwright-mcp/switchboard-card.png`. Read the screenshot.
3. Evaluate `() => document.querySelectorAll('#fast .fcoin').length`. Expected: `3`.
4. Run `tools/cockpit_size_sweep.js` with `browser_run_code_unsafe` (filename). Expected: `pass: true`.
5. Check that the console has no errors (`browser_console_messages`, level error).

- [ ] **Step 9: Push, open the PR, run the Windows build**

First run the secret-leak check over the branch diff, printing only the count:

```bash
git diff feat/venues..HEAD > /tmp/fast-branch.diff
.venv/bin/python - <<'EOF'
from pathlib import Path
from agentic_trading.venues.secrets import load_credentials
creds = load_credentials(Path("config/secrets.toml"))
needles = [n for c in creds.values() for n in [c.key, c.secret] + [l.strip() for l in c.secret.splitlines() if len(l.strip()) > 16 and not l.startswith("-----")] if n]
text = Path("/tmp/fast-branch.diff").read_text(errors="ignore")
print("secret occurrences in the branch diff:", sum(n in text for n in needles))
EOF
git diff --name-only feat/venues..HEAD | grep -E "secrets.toml$|agentic.toml|data/" || echo "no forbidden paths"
```

Expected: `secret occurrences in the branch diff: 0` and `no forbidden paths`.

```bash
git push -u origin feat/fast-engine
gh pr create --repo Hkshoonya/agentic-trader --base feat/venues --head feat/fast-engine \
  --title "Fast engine: a crypto switchboard on live ticks, paper-judged by the desk" \
  --body "Adds the switchboard: per-coin market readings, three playbooks (breakout, pullback, squeeze break), a 3x cost gate, honest delayed ask/bid fills at Alpaca costs with a Coinbase comparison book, replay over recorded ticks or past bars, a read-only unfunded desk member (would_earn recorded, weight held at 0), and a Switchboard card on the console. No real orders. Spec: docs/superpowers/specs/2026-10-02-fast-engine-design.md"
gh workflow run windows-build.yml --repo Hkshoonya/agentic-trader --ref feat/fast-engine
```

Watch the run: `gh run watch <id> --repo Hkshoonya/agentic-trader --exit-status`. Take the id from `gh run list --repo Hkshoonya/agentic-trader --branch feat/fast-engine --limit 1`. Expected: success.

---

## Self-Review (done while writing)

**Spec coverage:**

| Spec item | Task |
|---|---|
| `[fast]` config, fee range refused | 1 |
| `bars.py`, `regime.py` (thresholds, precedence, warming) | 2 |
| `playbooks.py` (three playbooks, trailing stop, expected move) | 3 |
| `fills.py` (250 ms delay, ask/bid, fee, $1 minimum), `costs.py` | 1, 4 |
| `mirror.py` (Coinbase prices and fee, unpriced) | 4 |
| `switchboard.py` (cost gate, the trade keeps its playbook, cap, 1/3 sizing, cooldown, daily stop, stale feed) | 5 |
| `store.py` (atomic, corrupt moved aside, restart) | 6 |
| `service.py` (TickBus subscriber, isolation, `fast.json`, journal, health row) | 7 |
| `ReadOnlyMember`, `UNFUNDED`, `would_earn`, `min_t` counting the switchboard | 8 (`min_t` counts every non-benchmark record in `allocate`, unchanged) |
| `replay.py` (recorded exact, bars approximate, parity), CLI `fast replay` / `fast status` | 9 |
| `/api/fast`, the Switchboard card, ticker, race line (automatic from the book) | 10 |
| Failure table | 5 (stale feed), 4 (unpriced), 6 (corrupt), 7 (exception, misconfiguration) |
| Acceptance 1–5 | 11 |

**Adjustments to the spec, carried as rulings for the executor's ledger:**
- The squeeze-break playbook gets a target of entry + range height, so it has an exit besides its stop. The spec gave only the stop and the expected move.
- In replay, a bar's four prices are open, then low, then high, then close for an up bar (high before low for a down bar). Coinbase ticks precede Alpaca ticks at equal times, so the mirror is priced at the same moment.
- `fast_problem` also refuses symbols missing from `[venues] alpaca_crypto_symbols`. Spec: "fast enabled without crypto symbols".
- The switchboard's health row is a venue row named `switchboard` (mode `paper`) in `venues.json`, which the Venues card already renders.
- A holding with no saved trade becomes a `recovered` trade with a ±2% stop and target (Review Focus 1). The spec does not cover it.
- Bars are cached at `data/fastbars/` (`state_dir.parent / "fastbars"`). `.gitignore` gains it.

**Placeholder scan:** none. Every step has its code or exact command.

**Name consistency:**
- `FastConfig`, `load_fast_config`, `FeeOnlyCosts`, `Bar`, `MinuteBars`, `read_regime`, `STAND_ASIDE`, `Plan`, `Trade`, `ENTRIES`, `exit_reason`, `Order`, `Fill`, `usable_quote`, `PaperFiller`, `CoinbaseMirror`, `Event`, `tick_price`, `Switchboard.on_tick/status/to_state/restore`, `FastStore.load/save/starting_equity`, `FastJournal`, `FastEngine.on_tick/save/status/write_status/failed/recent/board`, `fast_problem`, `replay`, `recorded_ticks`, `bar_ticks`, `fetch_bars`, `median_spread`, `ReadOnlyBook.refresh`, `ReadOnlyMember`, `UNFUNDED`, `hold_unfunded`, `fast_view`, `CockpitFmt.regimeWord/fastCompare/fundedBadge/tradeLine`.
- Each is spelled identically wherever it appears.

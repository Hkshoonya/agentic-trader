# Agent Swarm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily job breeds, screens, ages, blends and culls a population of daily-bar strategy recipes, and keeps one blended paper book that the desk judges as the unfunded member `swarm`.

**Architecture:**
- **New package `src/agentic_trading/swarm/`.** It runs as `agentic-trading swarm step` from a systemd timer.
- **One engine.** Every agent is a recipe (data) run through the existing `walkforward.simulate()`. The birth screen and forward life use the same engine; forward life counts only bars after birth.
- **One desk member.** The swarm's own book (`data/state/desk/swarm.json`) blends the contributing agents' targets. The desk reads it as a `ReadOnlyMember`, exactly as it reads the switchboard.

**Tech Stack:** Python 3.11+, stdlib only (plus the repo's existing modules), unittest-style tests run by pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-agent-swarm-design.md`

## Global Constraints

**Commits and git:**
- Commit only as `git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit`. No AI mention, no trailers.
- Never stage `data/bars/*`, `data/stream/*`, `config/agentic.toml*` or `config/secrets.toml`.
- Work in the worktree `/home/doczeus/Projects/agentic-trading-swarm` (branch `feat/swarm`). Run tests there with `.venv`, using the main checkout's venv: `"/home/doczeus/Projects/Agnetic TraDING/.venv/bin/python" -m pytest tests -q`. `pyproject` puts `src` on `pythonpath`, so the worktree's `src` is imported. Check once with `python -c "import agentic_trading,sys;print(agentic_trading.__file__)"` run from the worktree.

**Tests:**
- No test may touch the network (the `tests/conftest.py` tripwire). The LLM is only ever the `FakeLlmClient`.
- The README and CONTRIBUTING test counts must equal the suite (`tools/check_doc_counts.py`). They are updated in Task 13.

**Fixed decisions from the spec:**
- **Funding:** `UNFUNDED` becomes `frozenset({"switchboard", "swarm"})`. It's a code constant, never config.
- **Benchmark:** 60% QQQ / 40% BTC (`BTCUSD` in `data/bars`, `BTC-USD` in desk books).
- **Population:** max 24 living agents, ≤ 12 screens per step, nursery 20 forward days, cull after 60, drawdown death at 25%.
- **LLM scout:** off by default, ≤ 3 proposals per ISO week, 15 s timeout.
- **Screen:** profitable after costs on the 3 years before birth, ≥ 20 trades, drawdown < 30%, return correlation with a living agent ≤ 0.9.
- **Stale bars:** the step does nothing when the newest QQQ or BTCUSD bar is more than 4 days older than today (UTC).

**Plan rulings (the spec left these open; recorded here):**
- **P1. The quarter cull runs on Mondays (UTC), not every step.** A daily cull would shrink the mature pool to 3 within a week. It also only takes agents whose forward excess is ≤ 0.
- **P2. A step's `as_of` is the last day known to be finished.** That is `min(newest BTCUSD bar date − 1, today − 1)`: a day counts only once a later BTC bar exists. History sync merges the still-forming daily candle, so the newest bar may be partial even when it is yesterday's, for example if the last sync ran at 23:30 UTC. A later bar also means a sync ran after the US close, so equities are final too. Every bar dated after `as_of` is dropped.
- **P3. Birth and forward life:**
  - An agent screened at a step is born on `as_of + 1`.
  - It is screened on bars dated ≤ `as_of`.
  - Its forward life starts at its birth date.
- **P4. An agent's target weights:**
  - Start from `targets_as_of(...)` at `day`, which uses bars strictly before `day`.
  - Each selected symbol gets `per_order_pct`, or `min(per_order_pct, per_order_pct / TARGET_VOL × weight)` when `inverse_vol` is set.
  - The total is scaled down to 1 if it is over.
  - This mirrors `simulate()`'s sizing with `max_position_pct = per_order_pct` and `max_gross = 1`.
- **P5. Each step's RNG is `random.Random(f"{seed}:{as_of}:{trials}")`.** It is reproducible without storing RNG state.

## Review Focus

1. **A half-formed daily bar.** BTCUSD's newest bar may still be moving, whether it is today's or yesterday's with no sync since. Nothing may read it (P2). Task 10 has a test.
2. **The host was off for days.** Each missed day must be replayed in order, exactly as daily runs would: cull with that day's records and weekday, then blend and trade, with one sample per day. A Monday must not be skipped, and a later death must not be applied to an earlier day. Task 8 and Task 10 have tests.
3. **A recipe whose universe lacks its regime symbol** (rotation on `crypto` has no SPY; reversal on `crypto` is refused). It must hold cash, not crash. Task 2 and Task 4 have tests.
4. **Running the step twice on the same day.** The second run is a no-op: no new trials, no second book sample. Task 10 has a test.
5. **Corrupt state.** A broken `population.json` moves aside, and the trial count never goes down (taken from `lineage.json`). Task 7 has a test.

## File Structure

| File | Responsibility |
|---|---|
| `src/agentic_trading/walkforward.py` (modify) | Rule parameters flow `simulate` → `targets_as_of` → `rank_targets` → rankers |
| `src/agentic_trading/swarm/__init__.py` | Empty |
| `swarm/settings.py` | `[swarm]` config |
| `swarm/recipe.py` | `Recipe`, `validate`, choices, `rule_kwargs`, `sim_kwargs`, `universe_series` |
| `swarm/breed.py` | `immigrant`, `mutate`, `crossover` |
| `swarm/life.py` | Date and series helpers, `Record`, `forward_record`, `benchmark_returns`, `agent_weights`, `Agent` |
| `swarm/screen.py` | `screen`, `correlation`, `duplicate_of` |
| `swarm/blend.py` | `contributing`, `shares`, `blend_weights`, `cull` |
| `swarm/store.py` | `SwarmState`, `SwarmStore` (atomic JSON, move-aside, lock) |
| `swarm/book.py` | `advance`: one day of the swarm member book |
| `swarm/scout.py` | `Scout`: the optional LLM proposals |
| `swarm/step.py` | `run_step`: the daily orchestration and `swarm.json` |
| `swarm/cli.py` | `swarm step`, `swarm status` |
| `fast/service.py` (modify) | `FastJournal(prefix=…)`, reused as the swarm journal |
| `desk/allocator.py`, `config.py`, `cli.py` (modify) | Desk integration |
| `dashboard_swarm.py`, `dashboard.py`, `dashboard_desk.py`, `dashboard_html.py`, `dashboard_css.py`, `dashboard_cockpit_js.py` (modify/create) | The Swarm card |
| `deploy/agentic-trading-swarm{,.service,.timer}` | Daily timer |
| `tests/swarm_support.py`, `tests/test_swarm_*.py` | Tests |

---

### Task 1: Rule parameters through the walk-forward

**Files:**
- Modify: `src/agentic_trading/walkforward.py`. Change `rank_targets` (~line 189), `rank_rotation` (~283), `rank_reversal` (~379), `targets_as_of` (~466) and `simulate` (~509).
- Create: `tests/swarm_support.py`
- Test: `tests/test_walkforward_params.py`

**Interfaces:**
- **Produces, new keyword arguments, all optional, whose defaults reproduce today exactly:**
  - `rank_rotation(series, when, *, max_positions=5, books: Optional[Mapping[str, tuple[str, int, int, int]]] = None)`
  - `rank_reversal(series, when, *, max_positions=5, book: Optional[tuple[str, int, int, int, int]] = None)`
  - `rank_targets(..., books=None, reversal=None)`
  - `targets_as_of(..., books=None, reversal=None)`
  - `simulate(..., horizons=(50, 100, 200, 252), min_vote=0.5, books=None, reversal=None)`
- **Produces test helpers** (`tests/swarm_support.py`): `D0`, `daily(symbol, closes, *, start=D0, weekdays_only=False)`, `path(n, *, start=100.0, step=0.004, wiggle=0.01, phase=0.0)`, `universe(n=420)`

- [ ] **Step 1: Write the test helpers**

```python
# tests/swarm_support.py
"""Daily bars for swarm tests: deterministic, no network, no files."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from agentic_trading.history import Bar

D0 = date(2024, 1, 1)


def daily(symbol: str, closes: list[float], *, start: date = D0, weekdays_only: bool = False) -> list[Bar]:
    bars, day = [], start
    for close in closes:
        while weekdays_only and day.weekday() >= 5:
            day += timedelta(days=1)
        price = Decimal(str(round(close, 6)))
        when = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        bars.append(Bar(symbol, when, price, price, price, price, Decimal("1000")))
        day += timedelta(days=1)
    return bars


def path(n: int, *, start: float = 100.0, step: float = 0.004, wiggle: float = 0.01, phase: float = 0.0) -> list[float]:
    """A drifting price with a deterministic wobble (never a straight line)."""
    return [start * (1 + step) ** i * (1 + wiggle * math.sin(i / 3 + phase)) for i in range(n)]


def universe(n: int = 420) -> dict[str, list[Bar]]:
    """Four equities (weekdays) and three coins (every day), all rising at different speeds."""
    return {
        "SPY": daily("SPY", path(n, step=0.001), weekdays_only=True),
        "QQQ": daily("QQQ", path(n, step=0.0012, phase=1), weekdays_only=True),
        "AAPL": daily("AAPL", path(n, step=0.002, phase=2, wiggle=0.03), weekdays_only=True),
        "MSFT": daily("MSFT", path(n, step=-0.0005, phase=3, wiggle=0.03), weekdays_only=True),
        "BTCUSD": daily("BTCUSD", path(n, start=30000, step=0.002, phase=4, wiggle=0.04)),
        "ETHUSD": daily("ETHUSD", path(n, start=2000, step=0.003, phase=5, wiggle=0.05)),
        "SOLUSD": daily("SOLUSD", path(n, start=50, step=-0.001, phase=6, wiggle=0.06)),
    }
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_walkforward_params.py
"""The rankers take their parameters from the caller; the defaults are today's rules."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agentic_trading.walkforward import (
    REVERSAL_BOOK, ROTATION_BOOKS, rank_reversal, rank_rotation, simulate, targets_as_of,
)
from tests.swarm_support import universe

WHEN = datetime(2025, 2, 3, tzinfo=timezone.utc)  # a Monday, ~400 bars in


class DefaultParityTests(unittest.TestCase):
    def test_explicit_defaults_select_exactly_what_no_arguments_select(self) -> None:
        series = universe()
        self.assertEqual(rank_rotation(series, WHEN), rank_rotation(series, WHEN, books=ROTATION_BOOKS))
        self.assertEqual(rank_reversal(series, WHEN), rank_reversal(series, WHEN, book=REVERSAL_BOOK))
        for rule in ("trend", "rotation", "reversal"):
            with self.subTest(rule=rule):
                self.assertEqual(targets_as_of(series, WHEN, rule=rule),
                                 targets_as_of(series, WHEN, rule=rule, books=None, reversal=None))


class ParameterTests(unittest.TestCase):
    def test_rotation_top_and_lookback_come_from_the_caller(self) -> None:
        series = universe()
        books = {"equity": ("SPY", 50, 20, 1), "crypto": ("BTCUSD", 50, 20, 1)}
        chosen = [r["symbol"] for r in rank_rotation(series, WHEN, books=books) if r["selected"]]
        self.assertLessEqual(len(chosen), 2)  # one per book at most
        default = [r["symbol"] for r in rank_rotation(series, WHEN) if r["selected"]]
        self.assertGreaterEqual(len(default), len(chosen))

    def test_reversal_top_comes_from_the_caller(self) -> None:
        series = universe()
        rows = rank_reversal(series, WHEN, book=("SPY", 50, 50, 5, 1))
        self.assertLessEqual(sum(1 for r in rows if r["selected"]), 1)

    def test_simulate_passes_trend_horizons_through(self) -> None:
        series = universe(200)  # too short for the default 252-bar horizon
        start = datetime(2024, 4, 1, tzinfo=timezone.utc)
        end = datetime(2024, 7, 1, tzinfo=timezone.utc)
        none, _ = simulate(series, start=start, end=end, rule="trend", per_order_pct=0.2, max_position_pct=0.2)
        some, _ = simulate(series, start=start, end=end, rule="trend", per_order_pct=0.2, max_position_pct=0.2,
                           horizons=(5, 10, 20, 40))
        self.assertEqual(none, [])
        self.assertGreater(len(some), 0)
```

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `"/home/doczeus/Projects/Agnetic TraDING/.venv/bin/python" -m pytest tests/test_walkforward_params.py -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'books'`.

- [ ] **Step 4: Implement**

In `walkforward.py`, make these edits. Each signature gains its parameters; the body changes are shown.

1. `rank_rotation`:
   - Add `books: Optional[Mapping[str, tuple[str, int, int, int]]] = None` after `max_positions`.
   - Change the loop header to `for book, (regime_symbol, regime_bars, lookback, top) in (books or ROTATION_BOOKS).items():`.
2. `rank_reversal`:
   - Add `book: Optional[tuple[str, int, int, int, int]] = None`.
   - Change the first line of the body to `regime_symbol, regime_bars, trend_bars, lookback, top = book or REVERSAL_BOOK`.
3. `rank_targets`:
   - Add `books: Optional[Mapping[str, tuple[str, int, int, int]]] = None, reversal: Optional[tuple[str, int, int, int, int]] = None`.
   - Change the two dispatch lines to:
     ```python
     if rule == "rotation":
         return rank_rotation(series, when, max_positions=max_positions, books=books)
     if rule == "reversal":
         return rank_reversal(series, when, max_positions=max_positions, book=reversal)
     ```
4. `targets_as_of`:
   - Add the same two parameters.
   - Pass `books=books, reversal=reversal` into `rank_targets(...)`.
5. `simulate`:
   - Add `horizons: tuple[int, ...] = (50, 100, 200, 252), min_vote: float = 0.5, books: Optional[Mapping[str, tuple[str, int, int, int]]] = None, reversal: Optional[tuple[str, int, int, int, int]] = None` after `rule`.
   - Change its `targets_as_of` call to:
     ```python
     targets = targets_as_of(series, when, horizons=horizons, min_vote=min_vote, max_positions=max_positions,
                             rule=rule, books=books, reversal=reversal)
     ```
6. Add `Mapping` to the `typing` import.

- [ ] **Step 5: Run the new tests and the whole suite**

Run: `... -m pytest tests -q`
Expected: all pass. That includes the existing walk-forward, rotation and reversal tests, which prove the defaults are unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/agentic_trading/walkforward.py tests/swarm_support.py tests/test_walkforward_params.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(walkforward): rankers take their parameters from the caller; defaults are today's rules"
```

---

### Task 2: `[swarm]` settings and the recipe

**Files:**
- Create: `src/agentic_trading/swarm/__init__.py` (empty), `swarm/settings.py`, `swarm/recipe.py`
- Test: `tests/test_swarm_recipe.py`

**Interfaces:**
- Consumes: Task 1's `simulate`/`targets_as_of` keyword arguments.
- **Produces:**
  - `SwarmConfig` (fields below) and `load_swarm_config(path) -> SwarmConfig`.
  - Constants: `FAMILIES`, `UNIVERSES`, `CHOICES`, `SIZING`, `HORIZON_SETS`.
  - `Recipe` (frozen), with `.id`, `.name`, `.content()`, `.to_dict()`, `.param(key)`, `.replace(**changes)`.
  - `validate(raw: dict, *, origin="immigrant", parents=()) -> Recipe`.
  - `rule_kwargs(recipe) -> dict` (for `targets_as_of`) and `sim_kwargs(recipe) -> dict` (for `simulate`).
  - `universe_series(series, universe) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_recipe.py
"""Recipes are data: validated, hashed, and turned into walk-forward arguments."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.swarm.recipe import (
    Recipe, rule_kwargs, sim_kwargs, universe_series, validate,
)
from agentic_trading.swarm.settings import SwarmConfig, load_swarm_config
from tests.swarm_support import universe

TREND = {"family": "trend", "params": {"horizons": [20, 50, 100, 200], "min_vote": 0.75, "max_positions": 3},
         "universe": "all", "per_order_pct": 0.2, "inverse_vol": True}


def _load(text: str) -> SwarmConfig:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "agentic.toml"
        path.write_text(text, encoding="utf-8")
        return load_swarm_config(path)


class SettingsTests(unittest.TestCase):
    def test_off_by_default_with_the_spec_numbers(self) -> None:
        config = _load("")
        self.assertFalse(config.enabled)
        self.assertEqual((config.max_agents, config.screens_per_day, config.nursery_days, config.cull_after_days),
                         (24, 12, 20, 60))
        self.assertEqual(config.max_drawdown, Decimal("0.25"))
        self.assertEqual((config.llm_scout, config.llm_proposals_per_week, config.seed), (False, 3, 7))

    def test_mistakes_are_refused_with_the_key_named(self) -> None:
        for text, needle in {'[swarm]\nenabeld = true\n': "unknown keys",
                             '[swarm]\nenabled = "true"\n': "swarm.enabled",
                             '[swarm]\nmax_agents = 0\n': "swarm.max_agents",
                             '[swarm]\nmax_drawdown = "0.9"\n': "swarm.max_drawdown",
                             '[swarm]\nllm_scout = 1\n': "swarm.llm_scout"}.items():
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, needle):
                _load(text)


class RecipeTests(unittest.TestCase):
    def test_a_valid_recipe_round_trips_and_its_id_is_its_content(self) -> None:
        recipe = validate(TREND)
        self.assertEqual(validate(recipe.to_dict()).id, recipe.id)
        self.assertEqual(len(recipe.id), 8)
        self.assertTrue(recipe.name.startswith("trend-"))
        other = validate({**TREND, "per_order_pct": 0.25})
        self.assertNotEqual(other.id, recipe.id)
        self.assertEqual(validate(TREND, origin="scout").id, recipe.id)  # origin is not content

    def test_bad_recipes_are_refused_with_the_problem_named(self) -> None:
        cases = {
            "family": {**TREND, "family": "astrology"},
            "universe": {**TREND, "universe": "forex"},
            "min_vote": {**TREND, "params": {**TREND["params"], "min_vote": 0.6}},
            "unknown": {**TREND, "params": {**TREND["params"], "leverage": 3}},
            "missing": {**TREND, "params": {"min_vote": 0.75}},
            "per_order_pct": {**TREND, "per_order_pct": 0.9},
            "equities only": {"family": "reversal", "params": {"regime_ma": 200, "trend_ma": 200, "lookback": 5,
                                                               "top": 3, "max_positions": 3},
                              "universe": "crypto", "per_order_pct": 0.2, "inverse_vol": False},
        }
        for needle, raw in cases.items():
            with self.subTest(needle=needle), self.assertRaisesRegex(ValueError, needle):
                validate(raw)

    def test_walk_forward_arguments_per_family(self) -> None:
        trend = validate(TREND)
        self.assertEqual(rule_kwargs(trend), {"rule": "trend", "horizons": (20, 50, 100, 200), "min_vote": 0.75,
                                              "max_positions": 3})
        self.assertEqual(sim_kwargs(trend)["per_order_pct"], 0.2)
        self.assertEqual(sim_kwargs(trend)["max_position_pct"], 0.2)
        self.assertTrue(sim_kwargs(trend)["inverse_vol"])
        rotation = validate({"family": "rotation", "universe": "all", "per_order_pct": 0.2, "inverse_vol": False,
                             "params": {"equity_ma": 200, "equity_lookback": 63, "equity_top": 3, "crypto_ma": 100,
                                        "crypto_lookback": 60, "crypto_top": 2, "max_positions": 5}})
        self.assertEqual(rule_kwargs(rotation)["books"],
                         {"equity": ("SPY", 200, 63, 3), "crypto": ("BTCUSD", 100, 60, 2)})

    def test_universe_slices(self) -> None:
        series = universe(30)
        self.assertEqual(set(universe_series(series, "crypto")), {"BTCUSD", "ETHUSD", "SOLUSD"})
        self.assertEqual(set(universe_series(series, "equity")), {"SPY", "QQQ", "AAPL", "MSFT"})
        self.assertEqual(len(universe_series(series, "all")), 7)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_recipe.py -q`
Expected: FAIL with `ModuleNotFoundError: agentic_trading.swarm`.

- [ ] **Step 3: Implement `swarm/settings.py`**

```python
"""The ``[swarm]`` table of ``agentic.toml``: the swarm's population rules.

Off unless ``enabled = true``. A misspelled key is an error, and booleans must
be real TOML booleans, as in ``[fast]``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentic_trading.fast.settings import _decimal, _whole


@dataclass(frozen=True)
class SwarmConfig:
    enabled: bool = False
    max_agents: int = 24
    screens_per_day: int = 12
    nursery_days: int = 20
    cull_after_days: int = 60
    max_drawdown: Decimal = Decimal("0.25")
    llm_scout: bool = False
    llm_proposals_per_week: int = 3
    seed: int = 7


WHOLE = {"max_agents": (1, 100), "screens_per_day": (1, 100), "nursery_days": (1, 365),
         "cull_after_days": (1, 3650), "llm_proposals_per_week": (0, 50), "seed": (0, 2**31 - 1)}


def _swarm(call: Any, *args: Any) -> Any:
    """The fast table's checks, with their messages naming ``swarm.``."""
    try:
        return call(*args)
    except ValueError as exc:
        raise ValueError(str(exc).replace("fast.", "swarm.", 1)) from None


def load_swarm_config(path: Path | str) -> SwarmConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("swarm") or {}
    if not isinstance(raw, dict):
        raise ValueError("[swarm] must be a table")
    known = {f.name for f in fields(SwarmConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"[swarm] has unknown keys {unknown}; known: {sorted(known)}")
    values: dict[str, Any] = {}
    for key in ("enabled", "llm_scout"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"swarm.{key} must be true or false, without quotes")
            values[key] = raw[key]
    for key, (low, high) in WHOLE.items():
        if key in raw:
            values[key] = _swarm(_whole, key, raw[key], low, high)
    if "max_drawdown" in raw:
        values["max_drawdown"] = _swarm(_decimal, "max_drawdown", raw["max_drawdown"], Decimal("0.05"), Decimal("0.8"))
    return SwarmConfig(**values)
```

- [ ] **Step 4: Implement `swarm/recipe.py`**

```python
"""A recipe: one daily-bar rule, written as data and never as code.

A recipe names a family the walk-forward already trades (trend, rotation or
reversal), that family's parameters chosen from fixed lists, which symbols it may
hold, and how big each position is. Its id is a hash of exactly that content,
so the same idea always has the same id, however it was found.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable

from agentic_trading.history import Bar
from agentic_trading.orders import is_crypto_symbol

FAMILIES = ("trend", "rotation", "reversal")
UNIVERSES = ("crypto", "equity", "all")
HORIZON_SETS: tuple[tuple[int, ...], ...] = ((10, 20, 50, 100), (20, 50, 100, 200), (50, 100, 200, 252),
                                             (100, 200, 252, 300))
POSITIONS = (1, 2, 3, 4, 5, 6, 8)
CHOICES: dict[str, dict[str, tuple[Any, ...]]] = {
    "trend": {"horizons": HORIZON_SETS, "min_vote": (0.5, 0.75, 1.0), "max_positions": POSITIONS},
    "rotation": {"equity_ma": (50, 100, 150, 200), "equity_lookback": (20, 40, 63, 90, 126),
                 "equity_top": (1, 2, 3, 4, 5), "crypto_ma": (50, 100, 150, 200),
                 "crypto_lookback": (20, 30, 60, 90), "crypto_top": (1, 2, 3, 4), "max_positions": POSITIONS},
    "reversal": {"regime_ma": (100, 150, 200), "trend_ma": (100, 150, 200), "lookback": (3, 5, 10),
                 "top": (1, 2, 3, 4, 5), "max_positions": (1, 2, 3, 4, 5)},
}
SIZING: dict[str, tuple[Any, ...]] = {"per_order_pct": (0.1, 0.15, 0.2, 0.25, 0.33), "inverse_vol": (False, True)}
ORIGINS = ("immigrant", "mutation", "crossover", "scout")


@dataclass(frozen=True)
class Recipe:
    family: str
    params: tuple[tuple[str, Any], ...]  # sorted (key, value) pairs; tuples, so hashable
    universe: str
    per_order_pct: float
    inverse_vol: bool
    origin: str = "immigrant"
    parents: tuple[str, ...] = ()

    def param(self, key: str) -> Any:
        return dict(self.params)[key]

    def content(self) -> dict[str, Any]:
        return {"family": self.family, "params": {k: list(v) if isinstance(v, tuple) else v for k, v in self.params},
                "universe": self.universe, "per_order_pct": self.per_order_pct, "inverse_vol": self.inverse_vol}

    @property
    def id(self) -> str:
        text = json.dumps(self.content(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

    @property
    def name(self) -> str:
        return f"{self.family}-{self.id[:4]}"

    def to_dict(self) -> dict[str, Any]:
        return {**self.content(), "origin": self.origin, "parents": list(self.parents)}

    def replace(self, **changes: Any) -> "Recipe":
        return replace(self, **changes)


def _choice(name: str, value: Any, allowed: tuple[Any, ...]) -> Any:
    if isinstance(value, list):
        value = tuple(value)
    if isinstance(value, bool) != any(isinstance(a, bool) for a in allowed) or value not in allowed:
        shown = [list(a) if isinstance(a, tuple) else a for a in allowed]
        raise ValueError(f"{name} must be one of {shown}, not {value!r}")
    return value


def validate(raw: Any, *, origin: str | None = None, parents: Iterable[str] | None = None) -> Recipe:
    if not isinstance(raw, dict):
        raise ValueError("a recipe must be an object")
    family = raw.get("family")
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {list(FAMILIES)}, not {family!r}")
    universe = raw.get("universe")
    if universe not in UNIVERSES:
        raise ValueError(f"universe must be one of {list(UNIVERSES)}, not {universe!r}")
    if family == "reversal" and universe == "crypto":
        raise ValueError("reversal trades equities only; its universe cannot be crypto")
    params = raw.get("params")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    allowed = CHOICES[family]
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise ValueError(f"unknown params {unknown} for {family}")
    missing = sorted(set(allowed) - set(params))
    if missing:
        raise ValueError(f"missing params {missing} for {family}")
    cleaned = tuple(sorted((key, _choice(key, params[key], allowed[key])) for key in allowed))
    per_order_pct = _choice("per_order_pct", raw.get("per_order_pct"), SIZING["per_order_pct"])
    inverse_vol = _choice("inverse_vol", raw.get("inverse_vol"), SIZING["inverse_vol"])
    chosen_origin = origin or str(raw.get("origin") or "immigrant")
    if chosen_origin not in ORIGINS:
        raise ValueError(f"origin must be one of {list(ORIGINS)}")
    chosen_parents = tuple(str(p) for p in (parents if parents is not None else raw.get("parents") or ()))
    return Recipe(family, cleaned, universe, float(per_order_pct), bool(inverse_vol), chosen_origin, chosen_parents)


def rule_kwargs(recipe: Recipe) -> dict[str, Any]:
    """Arguments for ``targets_as_of``/``rank_targets``: which rule, with which parameters."""
    p = dict(recipe.params)
    if recipe.family == "trend":
        return {"rule": "trend", "horizons": tuple(p["horizons"]), "min_vote": p["min_vote"],
                "max_positions": p["max_positions"]}
    if recipe.family == "rotation":
        return {"rule": "rotation", "max_positions": p["max_positions"],
                "books": {"equity": ("SPY", p["equity_ma"], p["equity_lookback"], p["equity_top"]),
                          "crypto": ("BTCUSD", p["crypto_ma"], p["crypto_lookback"], p["crypto_top"])}}
    return {"rule": "reversal", "max_positions": p["max_positions"],
            "reversal": ("SPY", p["regime_ma"], p["trend_ma"], p["lookback"], p["top"])}


def sim_kwargs(recipe: Recipe) -> dict[str, Any]:
    """Arguments for ``simulate``: the rule plus its sizing (P4)."""
    return {**rule_kwargs(recipe), "per_order_pct": recipe.per_order_pct,
            "max_position_pct": recipe.per_order_pct, "inverse_vol": recipe.inverse_vol, "max_gross": 1.0}


def universe_series(series: dict[str, list[Bar]], universe: str) -> dict[str, list[Bar]]:
    if universe == "all":
        return dict(series)
    crypto = universe == "crypto"
    return {s: bars for s, bars in series.items() if is_crypto_symbol(s) == crypto}
```

- [ ] **Step 5: Run the tests**

Run: `... -m pytest tests/test_swarm_recipe.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agentic_trading/swarm tests/test_swarm_recipe.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): [swarm] settings and recipes as validated, hashed data"
```

---
### Task 3: Breeding

**Files:**
- Create: `src/agentic_trading/swarm/breed.py`
- Test: `tests/test_swarm_breed.py`

**Interfaces:**
- Consumes: `Recipe`, `validate`, `FAMILIES`, `UNIVERSES`, `CHOICES` and `SIZING` from Task 2.
- **Produces:**
  - `immigrant(rng: random.Random) -> Recipe` (origin `immigrant`)
  - `mutate(parent: Recipe, rng) -> Recipe` (origin `mutation`, parents `(parent.id,)`)
  - `crossover(a: Recipe, b: Recipe, rng) -> Recipe` (origin `crossover`, parents `(a.id, b.id)`; different families fall back to `mutate(a, rng)`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_breed.py
"""Breeding stays inside the recipe space and is reproducible from its seed."""

from __future__ import annotations

import random
import unittest

from agentic_trading.swarm.breed import crossover, immigrant, mutate
from agentic_trading.swarm.recipe import validate


class BreedTests(unittest.TestCase):
    def test_immigrants_are_always_valid_and_reproducible(self) -> None:
        for seed in range(200):
            recipe = immigrant(random.Random(seed))
            self.assertEqual(validate(recipe.to_dict()).id, recipe.id)
            self.assertEqual(recipe.origin, "immigrant")
        self.assertEqual(immigrant(random.Random(5)).id, immigrant(random.Random(5)).id)

    def test_a_mutation_changes_one_or_two_things_and_names_its_parent(self) -> None:
        parent = immigrant(random.Random(1))
        for seed in range(100):
            child = mutate(parent, random.Random(seed))
            self.assertEqual((child.family, child.origin, child.parents), (parent.family, "mutation", (parent.id,)))
            before, after = parent.content(), child.content()
            changed = [k for k in ("universe", "per_order_pct", "inverse_vol") if before[k] != after[k]]
            changed += [k for k in before["params"] if before["params"][k] != after["params"][k]]
            self.assertIn(len(changed), (1, 2))

    def test_crossover_takes_each_gene_from_a_parent(self) -> None:
        rng = random.Random(3)
        a = immigrant(rng)
        b = next(r for r in (immigrant(random.Random(s)) for s in range(500)) if r.family == a.family and r.id != a.id)
        child = crossover(a, b, random.Random(9))
        self.assertEqual((child.origin, child.parents), ("crossover", (a.id, b.id)))
        for key, value in child.content()["params"].items():
            self.assertIn(value, (a.content()["params"][key], b.content()["params"][key]))

    def test_crossing_families_falls_back_to_a_mutation(self) -> None:
        a = next(immigrant(random.Random(s)) for s in range(100) if immigrant(random.Random(s)).family == "trend")
        b = next(immigrant(random.Random(s)) for s in range(100) if immigrant(random.Random(s)).family == "rotation")
        self.assertEqual(crossover(a, b, random.Random(2)).origin, "mutation")
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_breed.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `swarm/breed.py`**

```python
"""New recipes from old ones: mutation, crossover and random immigrants.

Every choice comes from the recipe's fixed lists, so a child is always a
valid recipe. The caller seeds the RNG (P5), so a step can be replayed.
"""

from __future__ import annotations

import random
from typing import Any

from agentic_trading.swarm.recipe import CHOICES, FAMILIES, SIZING, UNIVERSES, Recipe, validate


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _universes(family: str) -> list[str]:
    return [u for u in UNIVERSES if not (family == "reversal" and u == "crypto")]


def immigrant(rng: random.Random) -> Recipe:
    family = rng.choice(FAMILIES)
    raw = {"family": family,
           "params": {key: _plain(rng.choice(options)) for key, options in CHOICES[family].items()},
           "universe": rng.choice(_universes(family)),
           "per_order_pct": rng.choice(SIZING["per_order_pct"]),
           "inverse_vol": rng.choice(SIZING["inverse_vol"])}
    return validate(raw, origin="immigrant", parents=())


def mutate(parent: Recipe, rng: random.Random) -> Recipe:
    raw = parent.content()
    genes = [("params", key) for key in CHOICES[parent.family]] + [
        ("top", "per_order_pct"), ("top", "inverse_vol"), ("top", "universe")]
    for where, key in rng.sample(genes, k=rng.choice((1, 2))):
        if where == "params":
            options = [_plain(o) for o in CHOICES[parent.family][key]]
            current = raw["params"][key]
            raw["params"][key] = rng.choice([o for o in options if o != current])
        else:
            options = _universes(parent.family) if key == "universe" else list(SIZING[key])
            raw[key] = rng.choice([o for o in options if o != raw[key]])
    return validate(raw, origin="mutation", parents=(parent.id,))


def crossover(a: Recipe, b: Recipe, rng: random.Random) -> Recipe:
    if a.family != b.family:
        return mutate(a, rng)
    left, right = a.content(), b.content()
    raw = {"family": a.family,
           "params": {key: rng.choice((left["params"][key], right["params"][key])) for key in left["params"]},
           "universe": rng.choice((left["universe"], right["universe"])),
           "per_order_pct": rng.choice((left["per_order_pct"], right["per_order_pct"])),
           "inverse_vol": rng.choice((left["inverse_vol"], right["inverse_vol"]))}
    return validate(raw, origin="crossover", parents=(a.id, b.id))
```

- [ ] **Step 4: Run the tests**

Run: `... -m pytest tests/test_swarm_breed.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/swarm/breed.py tests/test_swarm_breed.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): mutation, crossover and immigrants inside the recipe space"
```

---

### Task 4: An agent's forward life

**Files:**
- Create: `src/agentic_trading/swarm/life.py`
- Test: `tests/test_swarm_life.py`

**Interfaces:**
- Consumes: `simulate`/`targets_as_of`/`TARGET_VOL` (Task 1 and existing); `Recipe`, `sim_kwargs`, `rule_kwargs`, `universe_series` and `validate` (Task 2).
- **Produces, helpers:**
  - `as_datetime(day: date) -> datetime` (UTC midnight)
  - `through(series, day: date) -> dict`: bars dated ≤ `day`
  - `window_dates(series, start: date, end: date) -> list[date]`: exactly the days `simulate` walks
  - `daily_returns(curve: list[float], days: int) -> list[float]`
  - `drawdown_pct(returns: Iterable[float]) -> float`
- **Produces, the record and the agent:**
  - `Record` (frozen): `days: tuple[str, ...]`, `returns`, `excess`, `trades: int`, `drawdown_pct: float`, `return_pct: float`; property `excess_pct`; method `upto(day_iso) -> Record`
  - `forward_record(recipe, series, birth: date, as_of: date, *, costs, cash) -> Record`
  - `benchmark_returns(series, days: list[date]) -> list[float]`
  - `agent_weights(recipe, series, day: date) -> dict[str, float]`, keyed by `data/bars` symbols such as `BTCUSD`
  - `Agent` (mutable dataclass): `recipe: Recipe`, `born: str`, `signature: dict[str, float]`, `errored: str = ""`, `record: Record = Record()`; `to_row() -> dict`, `Agent.from_row(row) -> Agent`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_life.py
"""An agent's record: forward only, same engine as the screen, judged against 60/40."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from agentic_trading.backtest import CostModel
from agentic_trading.swarm.life import (
    Agent, Record, agent_weights, benchmark_returns, forward_record, through, window_dates,
)
from agentic_trading.swarm.recipe import validate
from agentic_trading.walkforward import simulate
from tests.swarm_support import D0, daily, universe

TREND = validate({"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
                  "universe": "all", "per_order_pct": 0.2, "inverse_vol": False})
BIRTH = D0 + timedelta(days=300)


class LifeTests(unittest.TestCase):
    def test_through_drops_every_bar_after_the_day(self) -> None:
        series = through(universe(40), D0 + timedelta(days=9))
        self.assertTrue(all(b.start.date() <= D0 + timedelta(days=9) for bars in series.values() for b in bars))
        self.assertEqual(len(series["BTCUSD"]), 10)

    def test_window_dates_are_the_days_simulate_walks(self) -> None:
        from agentic_trading.swarm.life import as_datetime
        series = universe(200)
        start, end = D0 + timedelta(days=120), D0 + timedelta(days=180)
        _, curve = simulate(series, start=as_datetime(start), end=as_datetime(end), costs=CostModel())
        self.assertEqual(len(curve), len(window_dates(series, start, end)) + 2)

    def test_the_record_starts_at_birth_and_is_repeatable(self) -> None:
        series = universe(420)
        as_of = BIRTH + timedelta(days=60)
        record = forward_record(TREND, series, BIRTH, as_of, costs=CostModel(), cash=50)
        self.assertEqual(record.days[0], BIRTH.isoformat())
        self.assertLessEqual(record.days[-1], as_of.isoformat())
        self.assertEqual(len(record.returns), len(record.excess))
        self.assertEqual(record, forward_record(TREND, series, BIRTH, as_of, costs=CostModel(), cash=50))
        self.assertEqual(forward_record(TREND, series, BIRTH, BIRTH - timedelta(days=1), costs=CostModel(), cash=50),
                         Record())

    def test_upto_keeps_only_earlier_days(self) -> None:
        record = forward_record(TREND, universe(420), BIRTH, BIRTH + timedelta(days=40), costs=CostModel(), cash=50)
        cut = record.upto((BIRTH + timedelta(days=10)).isoformat())
        self.assertEqual(len(cut.days), 10)
        self.assertTrue(all(d < (BIRTH + timedelta(days=10)).isoformat() for d in cut.days))

    def test_the_benchmark_is_sixty_forty_and_a_closed_market_counts_zero(self) -> None:
        saturday = date(2024, 1, 6)
        series = {"QQQ": daily("QQQ", [100, 101], start=date(2024, 1, 4)),  # Thu, Fri
                  "BTCUSD": daily("BTCUSD", [100, 102, 104], start=date(2024, 1, 4))}  # Thu, Fri, Sat
        friday, = benchmark_returns(series, [date(2024, 1, 5)])
        self.assertAlmostEqual(friday, 0.6 * 0.01 + 0.4 * 0.02)
        weekend, = benchmark_returns(series, [saturday])
        self.assertAlmostEqual(weekend, 0.4 * (104 / 102 - 1))

    def test_agent_weights_follow_the_sizing_and_never_exceed_the_book(self) -> None:
        day = D0 + timedelta(days=400)
        weights = agent_weights(TREND, universe(420), day)
        self.assertTrue(weights)
        self.assertTrue(all(abs(w - 0.2) < 1e-9 for w in weights.values()))
        self.assertLessEqual(sum(weights.values()), 1.0 + 1e-9)
        crypto = validate({**TREND.content(), "universe": "crypto"})
        self.assertTrue(all(s.endswith("USD") for s in agent_weights(crypto, universe(420), day)))

    def test_a_rotation_without_its_regime_symbol_holds_nothing_in_that_book(self) -> None:
        rotation = validate({"family": "rotation", "universe": "crypto", "per_order_pct": 0.2, "inverse_vol": False,
                             "params": {"equity_ma": 50, "equity_lookback": 20, "equity_top": 3, "crypto_ma": 50,
                                        "crypto_lookback": 20, "crypto_top": 2, "max_positions": 5}})
        weights = agent_weights(rotation, universe(420), D0 + timedelta(days=400))
        self.assertTrue(all(s.endswith("USD") for s in weights))

    def test_an_agent_row_round_trips(self) -> None:
        agent = Agent(TREND, BIRTH.isoformat(), {"2024-10-01": 0.01})
        again = Agent.from_row(agent.to_row())
        self.assertEqual((again.recipe.id, again.born, again.signature, again.errored),
                         (TREND.id, BIRTH.isoformat(), {"2024-10-01": 0.01}, ""))
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_life.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `swarm/life.py`**

```python
"""An agent's forward life: the same ``simulate`` as its screen, on bars after its birth only.

A recipe is frozen when it is born. Its record is rebuilt each step from its
birth to the step's last finished day, so a restarted machine loses nothing and
the record can always be reproduced from ``data/bars``. Its excess is measured
against the desk's benchmark, 60% QQQ / 40% BTC, so swarm numbers line up with
the allocator's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from agentic_trading.history import Bar
from agentic_trading.swarm.recipe import Recipe, rule_kwargs, sim_kwargs, universe_series, validate
from agentic_trading.walkforward import TARGET_VOL, simulate, targets_as_of

BENCHMARK = (("QQQ", 0.6), ("BTCUSD", 0.4))


def as_datetime(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)


def through(series: dict[str, list[Bar]], day: date) -> dict[str, list[Bar]]:
    trimmed = {s: [b for b in bars if b.start.date() <= day] for s, bars in series.items()}
    return {s: bars for s, bars in trimmed.items() if bars}


def window_dates(series: dict[str, list[Bar]], start: date, end: date) -> list[date]:
    """The days ``simulate`` walks for this series and window (its own rule, repeated)."""
    days = {bar.start.date() for bars in series.values() for bar in bars}
    return sorted(d for d in days if start <= d <= end)


def daily_returns(curve: list[float], days: int) -> list[float]:
    """``simulate``'s curve is [cash, one mark per day..., cash after closing]; returns per day."""
    values = curve[: days + 1]
    return [values[i + 1] / values[i] - 1 if values[i] > 0 else 0.0 for i in range(len(values) - 1)]


def drawdown_pct(returns: Iterable[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for r in returns:
        equity *= 1 + r
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak if peak > 0 else 0.0)
    return round(worst * 100, 3)


@dataclass(frozen=True)
class Record:
    days: tuple[str, ...] = ()
    returns: tuple[float, ...] = ()
    excess: tuple[float, ...] = ()
    trades: int = 0
    drawdown_pct: float = 0.0
    return_pct: float = 0.0

    @property
    def excess_pct(self) -> float:
        return round(math.fsum(self.excess) * 100, 3)

    def upto(self, day: str) -> "Record":
        """The record as it stood before ``day`` (ISO date): what a past day could have known."""
        keep = sum(1 for d in self.days if d < day)
        returns = self.returns[:keep]
        return Record(self.days[:keep], returns, self.excess[:keep], self.trades,
                      drawdown_pct(returns), _total_pct(returns))


def _total_pct(returns: Iterable[float]) -> float:
    return round((math.prod(1 + r for r in returns) - 1) * 100, 3)


def benchmark_returns(series: dict[str, list[Bar]], days: list[date]) -> list[float]:
    out = [0.0] * len(days)
    for symbol, share in BENCHMARK:
        closes = sorted((b.start.date(), float(b.close)) for b in series.get(symbol, []) if b.close > 0)
        index, previous = 0, None
        table = dict(closes)
        for position, day in enumerate(days):
            while index < len(closes) and closes[index][0] < day:
                previous = closes[index][1]
                index += 1
            if day in table and previous:
                out[position] += share * (table[day] / previous - 1)
    return out


def forward_record(recipe: Recipe, series: dict[str, list[Bar]], birth: date, as_of: date, *,
                   costs: Any, cash: Any) -> Record:
    if as_of < birth:
        return Record()
    scoped = universe_series(through(series, as_of), recipe.universe)
    days = window_dates(scoped, birth, as_of)
    trades, curve = simulate(scoped, start=as_datetime(birth), end=as_datetime(as_of), costs=costs,
                             starting_cash=float(cash), **sim_kwargs(recipe))
    if not curve:
        return Record()
    returns = daily_returns(curve, len(days))
    bench = benchmark_returns(series, days[: len(returns)])
    return Record(tuple(d.isoformat() for d in days[: len(returns)]), tuple(returns),
                  tuple(r - b for r, b in zip(returns, bench)), len(trades),
                  drawdown_pct(returns), _total_pct(returns))


def agent_weights(recipe: Recipe, series: dict[str, list[Bar]], day: date) -> dict[str, float]:
    """What the agent would hold on ``day``, as fractions of its book (P4)."""
    targets = targets_as_of(universe_series(series, recipe.universe), as_datetime(day), **rule_kwargs(recipe))
    size = recipe.per_order_pct
    weights = {s: (min(size, size / TARGET_VOL * w) if recipe.inverse_vol else size) for s, w in targets.items()}
    weights = {s: w for s, w in weights.items() if w > 0}
    total = sum(weights.values())
    return {s: w / total for s, w in weights.items()} if total > 1 else weights


@dataclass
class Agent:
    recipe: Recipe
    born: str
    signature: dict[str, float] = field(default_factory=dict)
    errored: str = ""
    record: Record = field(default_factory=Record)

    def to_row(self) -> dict[str, Any]:
        return {"recipe": self.recipe.to_dict(), "born": self.born, "signature": self.signature,
                "errored": self.errored}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Agent":
        return cls(validate(row["recipe"]), str(row["born"]),
                   {str(k): float(v) for k, v in (row.get("signature") or {}).items()},
                   str(row.get("errored") or ""))
```

- [ ] **Step 4: Run the tests**

Run: `... -m pytest tests/test_swarm_life.py -q`
Expected: PASS. If `test_window_dates_are_the_days_simulate_walks` fails, `window_dates` must be changed to match `simulate`'s rule, never the other way round.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/swarm/life.py tests/test_swarm_life.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): forward records from birth, judged against 60/40, and agents' target weights"
```

---
### Task 5: The birth screen

**Files:**
- Create: `src/agentic_trading/swarm/screen.py`
- Test: `tests/test_swarm_screen.py`

**Interfaces:**
- Consumes: `through`, `window_dates`, `daily_returns`, `drawdown_pct`, `as_datetime` and `Agent` (Task 4); `sim_kwargs` and `universe_series` (Task 2).
- **Produces:**
  - `Screen` (frozen): `passed: bool`, `reason: str`, `trades: int`, `return_pct: float`, `drawdown_pct: float`, `signature: dict[str, float]`
  - `screen(recipe, series, birth: date, *, costs, cash) -> Screen`
  - `correlation(a: dict[str, float], b: dict[str, float], min_overlap: int = 60) -> Optional[float]`
  - `duplicate_of(signature, living: Iterable[Agent]) -> Optional[str]` (the living agent's `recipe.name`)
- Constants: `SCREEN_YEARS = 3`, `MIN_TRADES = 20`, `MAX_DRAWDOWN_PCT = 30.0`, `DUPLICATE_CORRELATION = 0.9`, `SIGNATURE_DAYS = 120`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_screen.py
"""The birth screen: an in-sample filter on bars before birth, plus a near-duplicate check."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from agentic_trading.backtest import CostModel
from agentic_trading.swarm.life import Agent
from agentic_trading.swarm.recipe import validate
from agentic_trading.swarm.screen import correlation, duplicate_of, screen
from tests.swarm_support import D0, universe

TREND = validate({"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
                  "universe": "all", "per_order_pct": 0.2, "inverse_vol": False})
BIRTH = D0 + timedelta(days=400)


def _fake(trades: int, curve: list[float]):
    return mock.patch("agentic_trading.swarm.screen.simulate", return_value=([{}] * trades, curve))


class ScreenTests(unittest.TestCase):
    def test_bars_on_or_after_birth_change_nothing(self) -> None:
        series = universe(420)
        before = screen(TREND, series, BIRTH, costs=CostModel(), cash=50)
        spiked = dict(series)
        spiked["BTCUSD"] = [replace(b, close=b.close * Decimal("50")) if b.start.date() >= BIRTH else b
                            for b in series["BTCUSD"]]
        self.assertEqual(screen(TREND, spiked, BIRTH, costs=CostModel(), cash=50), before)
        self.assertTrue(all(day < BIRTH.isoformat() for day in before.signature))

    def test_each_threshold_on_both_sides(self) -> None:
        good = [50.0] + [50.0 + i * 0.1 for i in range(1, 40)] + [54.0]
        cases = [
            (20, good, True, "passed"),
            (19, good, False, "only 19 trades"),
            (20, [50.0, 50.0, 49.0, 49.9], False, "lost"),
            (20, [50.0, 60.0, 42.1, 61.0], True, "passed"),  # a 29.8% fall is inside the limit
            (20, [50.0, 60.0, 41.9, 61.0], False, "drawdown 30%"),  # 30.2% is not
        ]
        for trades, curve, passed, needle in cases:
            with self.subTest(trades=trades, curve=curve[:4]), _fake(trades, curve):
                result = screen(TREND, universe(60), BIRTH, costs=CostModel(), cash=50)
                self.assertEqual(result.passed, passed, result.reason)
                self.assertIn(needle, result.reason)

    def test_no_history_fails_plainly(self) -> None:
        with _fake(0, []):
            self.assertIn("no history", screen(TREND, universe(10), BIRTH, costs=CostModel(), cash=50).reason)


class DuplicateTests(unittest.TestCase):
    def test_correlation_needs_overlap(self) -> None:
        a = {f"2025-01-{d:02d}": (d % 7 - 3) / 100 for d in range(1, 32)}
        self.assertIsNone(correlation(a, a))  # 31 shared days < 60
        long = {f"d{i:03d}": ((i * 7) % 11 - 5) / 100 for i in range(100)}
        self.assertAlmostEqual(correlation(long, long), 1.0)
        self.assertAlmostEqual(correlation(long, {k: -v for k, v in long.items()}), -1.0)
        self.assertIsNone(correlation(long, {k: 0.0 for k in long}))  # no variance

    def test_a_near_copy_of_a_living_agent_is_named(self) -> None:
        long = {f"d{i:03d}": ((i * 7) % 11 - 5) / 100 for i in range(100)}
        living = [Agent(TREND, "2025-01-01", long)]
        self.assertEqual(duplicate_of({k: v * 1.01 for k, v in long.items()}, living), TREND.name)
        self.assertIsNone(duplicate_of({k: -v for k, v in long.items()}, living))
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_screen.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `swarm/screen.py`**

```python
"""The birth screen: a filter, never evidence.

A recipe must have made money after costs over the three years before its
birth, with enough trades to mean something and a survivable drawdown. That
history is the same one every other recipe was screened on, so passing proves
nothing; only forward results count. The screen just stops obvious losers and
near-copies of living agents from taking a slot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from agentic_trading.history import Bar
from agentic_trading.swarm.life import Agent, as_datetime, daily_returns, drawdown_pct, through, window_dates
from agentic_trading.swarm.recipe import Recipe, sim_kwargs, universe_series
from agentic_trading.walkforward import simulate

SCREEN_YEARS = 3
MIN_TRADES = 20
MAX_DRAWDOWN_PCT = 30.0
DUPLICATE_CORRELATION = 0.9
SIGNATURE_DAYS = 120
MIN_OVERLAP = 60


@dataclass(frozen=True)
class Screen:
    passed: bool
    reason: str
    trades: int = 0
    return_pct: float = 0.0
    drawdown_pct: float = 0.0
    signature: dict[str, float] = field(default_factory=dict)


def screen(recipe: Recipe, series: dict[str, list[Bar]], birth: date, *, costs: Any, cash: Any) -> Screen:
    last = birth - timedelta(days=1)
    start = last - timedelta(days=365 * SCREEN_YEARS)
    scoped = universe_series(through(series, last), recipe.universe)
    days = window_dates(scoped, start, last)
    trades, curve = simulate(scoped, start=as_datetime(start), end=as_datetime(last), costs=costs,
                             starting_cash=float(cash), **sim_kwargs(recipe))
    if not curve:
        return Screen(False, "no history to screen on")
    returns = daily_returns(curve, len(days))
    tail = list(zip(days[: len(returns)], returns))[-SIGNATURE_DAYS:]
    signature = {d.isoformat(): r for d, r in tail}
    total = round((curve[-1] / curve[0] - 1) * 100, 3) if curve[0] > 0 else 0.0
    worst = _curve_drawdown(curve)
    result = dict(trades=len(trades), return_pct=total, drawdown_pct=worst, signature=signature)
    if len(trades) < MIN_TRADES:
        return Screen(False, f"only {len(trades)} trades in {SCREEN_YEARS} years (needs {MIN_TRADES})", **result)
    if curve[-1] <= curve[0]:
        return Screen(False, f"lost {total:+.1f}% after costs", **result)
    if worst >= MAX_DRAWDOWN_PCT:
        return Screen(False, f"drawdown {worst:.0f}% (limit {MAX_DRAWDOWN_PCT:.0f}%)", **result)
    return Screen(True, "passed", **result)


def _curve_drawdown(curve: list[float]) -> float:
    returns = [curve[i + 1] / curve[i] - 1 if curve[i] > 0 else 0.0 for i in range(len(curve) - 1)]
    return drawdown_pct(returns)


def correlation(a: dict[str, float], b: dict[str, float], min_overlap: int = MIN_OVERLAP) -> Optional[float]:
    shared = sorted(set(a) & set(b))
    if len(shared) < min_overlap:
        return None
    x = [a[k] for k in shared]
    y = [b[k] for k in shared]
    mx, my = math.fsum(x) / len(x), math.fsum(y) / len(y)
    sxy = math.fsum((p - mx) * (q - my) for p, q in zip(x, y))
    sxx = math.fsum((p - mx) ** 2 for p in x)
    syy = math.fsum((q - my) ** 2 for q in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def duplicate_of(signature: dict[str, float], living: Iterable[Agent]) -> Optional[str]:
    for agent in living:
        value = correlation(signature, agent.signature)
        if value is not None and value > DUPLICATE_CORRELATION:
            return agent.recipe.name
    return None
```

About the threshold test's curves:
- The two drawdown curves fall 29.8% and 30.2% from their peak of 60.
- Both end above 50, so the "lost" rule never fires first.

- [ ] **Step 4: Run the tests**

Run: `... -m pytest tests/test_swarm_screen.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/swarm/screen.py tests/test_swarm_screen.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): the birth screen (a filter, never evidence) and the near-duplicate check"
```

---

### Task 6: Blending and culling

**Files:**
- Create: `src/agentic_trading/swarm/blend.py`
- Test: `tests/test_swarm_blend.py`

**Interfaces:**
- Consumes: `Agent`, `Record` and `agent_weights` (Task 4).
- **Produces:**
  - `TRAILING_DAYS = 60`, `BENCHMARK_WEIGHTS = {"QQQ": 0.6, "BTCUSD": 0.4}`
  - `contributing(agent, *, nursery_days) -> bool`
  - `shares(agents, *, nursery_days) -> dict[str, float]` (keyed by `recipe.id`, summing to 1, or empty)
  - `blend_weights(agents, agent_shares, series, day: date) -> dict[str, float]` (keyed by `data/bars` symbols)
  - `cull(agents, *, today: date, max_drawdown_pct: float, cull_after_days: int) -> list[tuple[Agent, str]]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_blend.py
"""Who earns a share of the swarm's book, and who dies."""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from agentic_trading.swarm.blend import BENCHMARK_WEIGHTS, blend_weights, contributing, cull, shares
from agentic_trading.swarm.life import Agent, Record
from agentic_trading.swarm.recipe import validate

BASE = {"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
        "universe": "all", "per_order_pct": 0.2, "inverse_vol": False}


def _agent(days: int, daily_excess: float, *, vol: float = 0.01, drawdown: float = 0.0, per_order: float = 0.2,
           errored: str = "") -> Agent:
    returns = tuple(vol if i % 2 else -vol for i in range(days))
    record = Record(tuple(f"d{i:04d}" for i in range(days)), returns, tuple([daily_excess] * days), 10, drawdown, 0.0)
    recipe = validate({**BASE, "per_order_pct": per_order})
    return Agent(recipe, "2025-01-01", {}, errored, record)


MONDAY, TUESDAY = date(2026, 10, 5), date(2026, 10, 6)


class ContributingTests(unittest.TestCase):
    def test_the_nursery_negative_excess_and_errors_get_nothing(self) -> None:
        self.assertFalse(contributing(_agent(19, 0.001), nursery_days=20))
        self.assertTrue(contributing(_agent(20, 0.001), nursery_days=20))
        self.assertFalse(contributing(_agent(80, -0.001), nursery_days=20))
        self.assertFalse(contributing(_agent(80, 0.001, errored="boom"), nursery_days=20))

    def test_shares_are_inverse_volatility_and_sum_to_one(self) -> None:
        calm = _agent(30, 0.001, vol=0.01, per_order=0.1)
        wild = _agent(30, 0.001, vol=0.03, per_order=0.25)
        got = shares([calm, wild, _agent(5, 0.01)], nursery_days=20)
        self.assertAlmostEqual(sum(got.values()), 1.0)
        self.assertAlmostEqual(got[calm.recipe.id] / got[wild.recipe.id], 3.0)

    def test_no_contributor_means_the_benchmark(self) -> None:
        self.assertEqual(blend_weights([_agent(5, 0.01)], {}, {}, MONDAY), BENCHMARK_WEIGHTS)

    def test_the_blend_is_the_share_weighted_sum_of_agent_targets(self) -> None:
        a, b = _agent(30, 0.001, per_order=0.1), _agent(30, 0.001, per_order=0.25)
        fake = {a.recipe.id: {"SPY": 0.5, "BTCUSD": 0.5}, b.recipe.id: {"BTCUSD": 1.0}}
        with mock.patch("agentic_trading.swarm.blend.agent_weights", side_effect=lambda r, s, d: fake[r.id]):
            got = blend_weights([a, b], {a.recipe.id: 0.25, b.recipe.id: 0.75}, {}, MONDAY)
        self.assertEqual(got, {"SPY": 0.125, "BTCUSD": 0.875})


class CullTests(unittest.TestCase):
    def test_a_deep_drawdown_dies_any_day(self) -> None:
        dying = _agent(10, 0.0, drawdown=26.0)
        [(agent, reason)] = cull([dying, _agent(10, 0.0, drawdown=10.0, per_order=0.1)], today=TUESDAY,
                                 max_drawdown_pct=25.0, cull_after_days=60)
        self.assertIs(agent, dying)
        self.assertIn("drawdown 26%", reason)

    def test_the_bottom_quarter_dies_on_mondays_only_and_only_when_losing(self) -> None:
        agents = [_agent(70, e, per_order=p) for e, p in ((-0.002, 0.1), (-0.001, 0.15), (0.001, 0.2),
                                                           (0.002, 0.25), (0.003, 0.33))]
        self.assertEqual(cull(agents, today=TUESDAY, max_drawdown_pct=25.0, cull_after_days=60), [])
        [(agent, reason)] = cull(agents, today=MONDAY, max_drawdown_pct=25.0, cull_after_days=60)
        self.assertIs(agent, agents[0])
        self.assertIn("bottom quarter", reason)
        winners = [_agent(70, 0.001, per_order=p) for p in (0.1, 0.15, 0.2, 0.25)]
        self.assertEqual(cull(winners, today=MONDAY, max_drawdown_pct=25.0, cull_after_days=60), [])

    def test_fewer_than_four_mature_agents_are_never_quartered(self) -> None:
        agents = [_agent(70, -0.001, per_order=p) for p in (0.1, 0.15, 0.2)] + [_agent(30, -0.01, per_order=0.25)]
        self.assertEqual(cull(agents, today=MONDAY, max_drawdown_pct=25.0, cull_after_days=60), [])
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_blend.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `swarm/blend.py`**

```python
"""The swarm's book, and the cull.

An agent earns a share of the book only after 20 forward days, and only while
its last 60 forward days beat the benchmark. Shares are inverse to each
contributor's own forward volatility, so a wild agent doesn't drown the calm
ones. With no contributor, the book holds the benchmark, so the swarm's excess
comes from its agents alone.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Iterable

from agentic_trading.history import Bar
from agentic_trading.swarm.life import Agent, agent_weights

TRAILING_DAYS = 60
BENCHMARK_WEIGHTS = {"QQQ": 0.6, "BTCUSD": 0.4}
MONDAY = 0


def contributing(agent: Agent, *, nursery_days: int) -> bool:
    record = agent.record
    return (not agent.errored and len(record.days) >= nursery_days
            and math.fsum(record.excess[-TRAILING_DAYS:]) > 0)


def _vol(returns: tuple[float, ...]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = math.fsum(returns) / len(returns)
    return math.sqrt(math.fsum((r - mean) ** 2 for r in returns) / len(returns))


def shares(agents: Iterable[Agent], *, nursery_days: int) -> dict[str, float]:
    raw = {}
    for agent in agents:
        vol = _vol(agent.record.returns)
        if contributing(agent, nursery_days=nursery_days) and vol > 0:
            raw[agent.recipe.id] = 1 / vol
    total = math.fsum(raw.values())
    return {key: value / total for key, value in raw.items()} if total > 0 else {}


def blend_weights(agents: Iterable[Agent], agent_shares: dict[str, float], series: dict[str, list[Bar]],
                  day: date) -> dict[str, float]:
    if not agent_shares:
        return dict(BENCHMARK_WEIGHTS)
    total: dict[str, float] = {}
    for agent in agents:
        share = agent_shares.get(agent.recipe.id, 0.0)
        if share <= 0:
            continue
        for symbol, weight in agent_weights(agent.recipe, series, day).items():
            total[symbol] = total.get(symbol, 0.0) + share * weight
    return {symbol: round(weight, 9) for symbol, weight in total.items() if weight > 0}


def cull(agents: Iterable[Agent], *, today: date, max_drawdown_pct: float,
         cull_after_days: int) -> list[tuple[Agent, str]]:
    alive = list(agents)
    deaths = [(a, f"drawdown {a.record.drawdown_pct:.0f}% (limit {max_drawdown_pct:.0f}%)")
              for a in alive if a.record.drawdown_pct >= max_drawdown_pct]
    if today.weekday() != MONDAY:  # P1: the quarter cull is weekly
        return deaths
    dead = {id(a) for a, _ in deaths}
    mature = [a for a in alive if id(a) not in dead and not a.errored and len(a.record.days) >= cull_after_days]
    if len(mature) < 4:
        return deaths
    mature.sort(key=lambda a: (a.record.excess_pct, a.recipe.id))
    for agent in mature[: len(mature) // 4]:
        if agent.record.excess_pct <= 0:
            deaths.append((agent, f"bottom quarter after {len(agent.record.days)} days: "
                                  f"{agent.record.excess_pct:+.1f}% vs the benchmark"))
    return deaths
```

- [ ] **Step 4: Run the tests**

Run: `... -m pytest tests/test_swarm_blend.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/swarm/blend.py tests/test_swarm_blend.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): inverse-vol shares for proven agents, the benchmark otherwise, and the cull"
```

---
### Task 7: The store and the swarm journal

**Files:**
- Create: `src/agentic_trading/swarm/store.py`
- Modify: `src/agentic_trading/fast/service.py`. `FastJournal` gains `prefix`.
- Test: `tests/test_swarm_store.py`

**Interfaces:**
- Consumes: `Agent` (Task 4); `FastStore.move_aside(path, now, notes)` (static, existing); `jsonio.write_text`/`jsonio.dumps`.
- **Produces:**
  - `SwarmState` (dataclass): `living: list[Agent]`, `lineage: dict[str, dict]`, `trials: int`, `last_step: str`, `scout: dict` (keys `week`, `spent`, `last_rationale`)
  - `SwarmStore(state_dir)` with these attributes and methods:
    - paths: `book_path` (`state/desk/swarm.json`) and `status_path` (`state/swarm.json`)
    - `load(now) -> tuple[SwarmState, list[str]]`
    - `save(state)`
    - `write_status(dict)`
    - `read_status() -> dict`
    - `lock(now) -> bool`
    - `unlock()`
  - `FastJournal(journal_dir, prefix="fast")` writes `<prefix>-<day>.jsonl`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_store.py
"""The swarm's state survives restarts, corruption and a second step at once."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.fast.service import FastJournal
from agentic_trading.swarm.life import Agent
from agentic_trading.swarm.recipe import validate
from agentic_trading.swarm.store import SwarmState, SwarmStore

NOW = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)
TREND = validate({"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
                  "universe": "all", "per_order_pct": 0.2, "inverse_vol": False})


class StoreTests(unittest.TestCase):
    def test_a_round_trip_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            state = SwarmState([Agent(TREND, "2026-10-05", {"2026-10-01": 0.01})],
                               {TREND.id: {"recipe": TREND.to_dict(), "born": "2026-10-05"}}, 7, "2026-10-04",
                               {"week": "2026-W41", "spent": 1, "last_rationale": "why"})
            store.save(state)
            again, notes = store.load(NOW)
        self.assertEqual(notes, [])
        self.assertEqual([a.recipe.id for a in again.living], [TREND.id])
        self.assertEqual((again.trials, again.last_step, again.scout["spent"]), (7, "2026-10-04", 1))

    def test_a_corrupt_file_moves_aside_and_the_trial_count_never_falls(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            store.save(SwarmState([Agent(TREND, "2026-10-05")], {TREND.id: {}}, 9, "2026-10-04", {}))
            (Path(name) / "swarm" / "population.json").write_text("{not json")
            (Path(name) / "swarm" / "ledger.json").write_text("[]")
            state, notes = store.load(NOW)
            moved = sorted(p.name for p in (Path(name) / "swarm").glob("*.corrupt-*"))
        self.assertEqual(state.living, [])
        self.assertEqual(state.trials, 9)  # from lineage.json
        self.assertEqual(len(moved), 2)
        self.assertEqual(len(notes), 2)

    def test_the_lock_admits_one_step_and_a_stale_lock_is_taken_over(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            self.assertTrue(store.lock(NOW))
            self.assertFalse(SwarmStore(Path(name)).lock(NOW + timedelta(minutes=5)))
            self.assertTrue(SwarmStore(Path(name)).lock(NOW + timedelta(hours=4)))  # the first one died
            store.unlock()
            self.assertTrue(store.lock(NOW))

    def test_status_reads_back_and_a_missing_one_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            self.assertEqual(store.read_status(), {})
            store.write_status({"as_of": "2026-10-04"})
            self.assertEqual(store.read_status(), {"as_of": "2026-10-04"})


class JournalTests(unittest.TestCase):
    def test_the_fast_journal_takes_a_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            FastJournal(Path(name), prefix="swarm").append({"event": "swarm_birth", "at": "2026-10-05T00:30:00+00:00"})
            FastJournal(Path(name)).append({"event": "fast_entry", "at": "2026-10-05T00:30:00+00:00"})
            names = sorted(p.name for p in Path(name).iterdir())
        self.assertEqual(names, ["fast-2026-10-05.jsonl", "swarm-2026-10-05.jsonl"])
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_store.py -q`
Expected: FAIL with `ModuleNotFoundError` (store) and `TypeError` (prefix).

- [ ] **Step 3: Give `FastJournal` a prefix**

In `src/agentic_trading/fast/service.py`:
- Change `__init__(self, journal_dir: Path | str) -> None:` to `__init__(self, journal_dir: Path | str, prefix: str = "fast") -> None:` and add `self.prefix = prefix`.
- In `append`, change `f"fast-{day}.jsonl"` to `f"{self.prefix}-{day}.jsonl"`.
- Change the docstring to `"""``<prefix>-<date>.jsonl`` beside the trader's journals ("fast" or "swarm"); dated readers skip it by name."""`.

- [ ] **Step 4: Implement `swarm/store.py`**

```python
"""Where the swarm survives restarts.

``data/state/swarm/`` holds:
- ``population.json``: the living agents;
- ``lineage.json``: every agent ever born, with its parents and how it died, and the trial count;
- ``ledger.json``: the trial count and the last finished step;
- ``scout.json``: the LLM scout's spending this week.

``data/state/desk/swarm.json`` is the member book the desk judges, and
``data/state/swarm.json`` is the dashboard's picture.

Writes are atomic. An unreadable file is moved aside, as the fast engine does, and
the trial count is the larger of the ledger's and the lineage's, so it never falls.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agentic_trading import jsonio
from agentic_trading.fast.store import FastStore
from agentic_trading.swarm.life import Agent

STALE_LOCK_SECONDS = 3 * 3600


@dataclass
class SwarmState:
    living: list[Agent] = field(default_factory=list)
    lineage: dict[str, dict[str, Any]] = field(default_factory=dict)
    trials: int = 0
    last_step: str = ""
    scout: dict[str, Any] = field(default_factory=dict)


class SwarmStore:
    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.dir = self.state_dir / "swarm"
        self.population_path = self.dir / "population.json"
        self.lineage_path = self.dir / "lineage.json"
        self.ledger_path = self.dir / "ledger.json"
        self.scout_path = self.dir / "scout.json"
        self.lock_path = self.dir / "step.lock"
        self.book_path = self.state_dir / "desk" / "swarm.json"
        self.status_path = self.state_dir / "swarm.json"

    def _read(self, path: Path, now: datetime, notes: list[str]) -> Optional[dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            return raw
        except (OSError, ValueError):
            FastStore.move_aside(path, now, notes)
            return None

    def load(self, now: datetime) -> tuple[SwarmState, list[str]]:
        notes: list[str] = []
        population = self._read(self.population_path, now, notes) or {}
        lineage = self._read(self.lineage_path, now, notes) or {}
        ledger = self._read(self.ledger_path, now, notes) or {}
        scout = self._read(self.scout_path, now, notes) or {}
        living = []
        for row in population.get("agents") or []:
            try:
                living.append(Agent.from_row(row))
            except (KeyError, TypeError, ValueError):
                notes.append("an unreadable agent was dropped from population.json")
        trials = max(_count(ledger.get("trials")), _count(lineage.get("trials")))
        agents = lineage.get("agents") if isinstance(lineage.get("agents"), dict) else {}
        return SwarmState(living, agents, trials, str(ledger.get("last_step") or ""), scout), notes

    def _write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        jsonio.write_text(path, jsonio.dumps(payload, indent=2) + "\n")

    def save(self, state: SwarmState) -> None:
        self._write(self.population_path, {"agents": [a.to_row() for a in state.living]})
        self._write(self.lineage_path, {"trials": state.trials, "agents": state.lineage})
        self._write(self.ledger_path, {"trials": state.trials, "last_step": state.last_step})
        self._write(self.scout_path, state.scout)

    def write_status(self, status: dict[str, Any]) -> None:
        self._write(self.status_path, status)

    def read_status(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def lock(self, now: datetime) -> bool:
        """One step at a time. A lock older than three hours belonged to a step that died."""
        self.dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                handle = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    taken = datetime.fromisoformat(self.lock_path.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    taken = None
                if taken is not None and (now - taken).total_seconds() < STALE_LOCK_SECONDS:
                    return False
                self.lock_path.unlink(missing_ok=True)
                continue
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(now.isoformat())
            return True
        return False

    def unlock(self) -> None:
        self.lock_path.unlink(missing_ok=True)


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
```

- [ ] **Step 5: Run the tests and the fast suite**

Run: `... -m pytest tests/test_swarm_store.py tests -q -k "swarm or fast"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agentic_trading/swarm/store.py src/agentic_trading/fast/service.py tests/test_swarm_store.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): atomic state with move-aside, a never-falling trial count, a step lock; journal prefix"
```

---

### Task 8: The swarm's member book

**Files:**
- Create: `src/agentic_trading/swarm/book.py`
- Test: `tests/test_swarm_book.py`

**Interfaces:**
- Consumes: `MemberBook` (`mark`, `buy`, `sell`, `weights`, `equity`, `positions`, `samples`) and `MIN_NOTIONAL` from `desk/book.py`; `broker_symbol` from `desk/member.py`.
- **Produces:**
  - `REBALANCE_GAP = Decimal("0.05")`
  - `closes_on(series, day: date) -> dict[str, Decimal]` (keyed by desk symbols, e.g. `BTC-USD`)
  - `advance(book, day: date, weights: dict[str, float], closes: dict[str, Decimal], costs) -> dict[str, int]` (`{"sold": n, "bought": n}`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_book.py
"""One day of the swarm's book: mark at the close, then trade toward the blend."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from agentic_trading.backtest import CostModel
from agentic_trading.desk.book import MemberBook
from agentic_trading.swarm.book import advance, closes_on
from tests.swarm_support import daily

D = Decimal
SAT = date(2024, 1, 6)


def _book() -> MemberBook:
    return MemberBook("swarm", starting_equity=D("50"))


class BookTests(unittest.TestCase):
    def test_closes_are_keyed_the_desk_way_and_only_for_that_day(self) -> None:
        series = {"BTCUSD": daily("BTCUSD", [100, 101], start=date(2024, 1, 5)),
                  "SPY": daily("SPY", [400], start=date(2024, 1, 5))}
        self.assertEqual(closes_on(series, SAT), {"BTC-USD": D("101")})
        self.assertEqual(closes_on(series, date(2024, 1, 5)), {"BTC-USD": D("100"), "SPY": D("400")})

    def test_the_first_day_buys_the_targets_and_the_same_targets_trade_nothing(self) -> None:
        book = _book()
        prices = {"QQQ": D("400"), "BTC-USD": D("60000")}
        first = advance(book, date(2024, 1, 5), {"QQQ": 0.6, "BTCUSD": 0.4}, prices, CostModel())
        self.assertEqual(first, {"sold": 0, "bought": 2})
        self.assertAlmostEqual(book.weights()["QQQ"], 0.6, delta=0.01)
        again = advance(book, date(2024, 1, 6) + timedelta(days=2), {"QQQ": 0.6, "BTCUSD": 0.4}, prices, CostModel())
        self.assertEqual(again, {"sold": 0, "bought": 0})

    def test_a_dropped_target_is_sold_and_a_closed_market_waits(self) -> None:
        book = _book()
        advance(book, date(2024, 1, 5), {"QQQ": 0.5, "BTCUSD": 0.5}, {"QQQ": D("400"), "BTC-USD": D("60000")},
                CostModel())
        result = advance(book, SAT, {"BTCUSD": 1.0}, {"BTC-USD": D("60000")}, CostModel())
        self.assertIn("QQQ", book.positions)  # no QQQ close on a Saturday: it waits
        self.assertEqual(result["sold"], 0)
        result = advance(book, date(2024, 1, 8), {"BTCUSD": 1.0}, {"QQQ": D("401"), "BTC-USD": D("60000")},
                         CostModel())
        self.assertNotIn("QQQ", book.positions)
        self.assertEqual(result["sold"], 1)

    def test_each_day_in_order_leaves_one_sample(self) -> None:
        book = _book()
        for offset in range(5):
            advance(book, date(2024, 1, 1) + timedelta(days=offset), {"BTCUSD": 0.4},
                    {"BTC-USD": D(str(60000 + offset * 100))}, CostModel())
        self.assertEqual([d for d, _ in book.samples], ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_book.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `swarm/book.py`**

```python
"""One day of the swarm's book, the record the desk judges.

The book is marked at the day's closes. That closes the previous day's sample,
just as the desk's own books sample. The book then trades toward the blend:
- a position more than 5 points over its target is sold down;
- one under its target by more than 5 points is bought up.

A symbol with no close that day (a stock on a Saturday) waits. Missed days are
replayed one by one by the caller, so a machine that was off still gets one
sample per day.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from agentic_trading.desk.book import MIN_NOTIONAL, MemberBook
from agentic_trading.desk.member import broker_symbol
from agentic_trading.history import Bar

REBALANCE_GAP = Decimal("0.05")
ZERO = Decimal("0")


def closes_on(series: dict[str, list[Bar]], day: date) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for symbol, bars in series.items():
        for bar in reversed(bars):
            if bar.start.date() == day:
                if bar.close > 0:
                    out[broker_symbol(symbol)] = Decimal(str(bar.close))
                break
            if bar.start.date() < day:
                break
    return out


def advance(book: MemberBook, day: date, weights: dict[str, float], closes: dict[str, Decimal],
            costs: Any) -> dict[str, int]:
    book.mark(closes, datetime.combine(day, time(23, 59), tzinfo=timezone.utc))
    targets = {broker_symbol(s): Decimal(str(w)) for s, w in weights.items() if w > 0}
    sold = bought = 0
    if book.equity <= 0:
        return {"sold": sold, "bought": bought}
    current = book.weights()
    for symbol in sorted(book.positions):
        price = closes.get(symbol)
        have = Decimal(str(current.get(symbol, 0.0)))
        want = targets.get(symbol, ZERO)
        if price is None or have <= 0:
            continue
        if want == 0 or have - want > REBALANCE_GAP:
            quantity = book.positions[symbol] * (have - want) / have
            if book.sell(symbol, quantity, price, costs) > 0:
                sold += 1
    equity, current = book.equity, book.weights()
    for symbol, want in sorted(targets.items()):
        price = closes.get(symbol)
        have = Decimal(str(current.get(symbol, 0.0)))
        if price is None or want - have <= REBALANCE_GAP:
            continue
        if book.buy(symbol, (want - have) * equity, price, costs, MIN_NOTIONAL) > 0:
            bought += 1
    return {"sold": sold, "bought": bought}
```

- [ ] **Step 4: Run the tests**

Run: `... -m pytest tests/test_swarm_book.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/swarm/book.py tests/test_swarm_book.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): the member book marks at each close and trades toward the blend"
```

---
### Task 9: The LLM scout

**Files:**
- Create: `src/agentic_trading/swarm/scout.py`
- Test: `tests/test_swarm_scout.py`

**Interfaces:**
- Consumes: `validate`, `CHOICES`, `SIZING`, `FAMILIES` and `UNIVERSES` (Task 2); `FakeLlmClient` (`llm/client.py`; `complete(system, user) -> str`).
- **Produces:**
  - `TIMEOUT = 15.0`
  - `week_key(day: date) -> str` (e.g. `"2026-W41"`)
  - `Scout(client, budget: int)` with `.propose(memory: dict, *, today: date, living: list[dict], trials: int) -> tuple[Optional[Recipe], str]`. It mutates `memory` (`week`, `spent`, `last_rationale`). Every call that reaches the client counts against the budget, whatever comes back.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_scout.py
"""The scout proposes; the schema decides; the budget is a hard limit."""

from __future__ import annotations

import json
import unittest
from datetime import date

from agentic_trading.llm.client import FakeLlmClient
from agentic_trading.swarm.scout import Scout, week_key

GOOD = {"recipe": {"family": "trend", "params": {"horizons": [20, 50, 100, 200], "min_vote": 0.75,
                                                 "max_positions": 3},
                   "universe": "crypto", "per_order_pct": 0.2, "inverse_vol": True},
        "rationale": "Coins trend   harder than stocks;\nask for three of four horizons."}
MONDAY = date(2026, 10, 5)


class ScoutTests(unittest.TestCase):
    def test_a_valid_proposal_becomes_a_scout_recipe(self) -> None:
        memory: dict = {}
        client = FakeLlmClient(json.dumps(GOOD))
        recipe, note = Scout(client, 3).propose(memory, today=MONDAY, living=[], trials=12)
        self.assertEqual((recipe.family, recipe.origin), ("trend", "scout"))
        self.assertEqual(note, "Coins trend harder than stocks; ask for three of four horizons.")
        self.assertEqual((memory["week"], memory["spent"], memory["last_rationale"]), ("2026-W41", 1, note))
        system, user = client.calls[0]
        self.assertIn("max_positions", system)
        self.assertEqual(json.loads(user)["recipes_tried_so_far"], 12)

    def test_fenced_json_is_accepted(self) -> None:
        recipe, _ = Scout(FakeLlmClient("```json\n" + json.dumps(GOOD) + "\n```"), 3).propose(
            {}, today=MONDAY, living=[], trials=0)
        self.assertIsNotNone(recipe)

    def test_bad_answers_are_refused_and_still_counted(self) -> None:
        bad = {**GOOD, "recipe": {**GOOD["recipe"], "per_order_pct": 0.9}}
        for text, needle in (("not json", "refused"), (json.dumps(bad), "per_order_pct"),
                             (json.dumps({"rationale": "x"}), "refused")):
            with self.subTest(text=text[:20]):
                memory: dict = {}
                recipe, note = Scout(FakeLlmClient(text), 3).propose(memory, today=MONDAY, living=[], trials=0)
                self.assertIsNone(recipe)
                self.assertIn(needle, note)
                self.assertEqual(memory["spent"], 1)

    def test_the_weekly_budget_and_its_reset(self) -> None:
        memory = {"week": "2026-W41", "spent": 3}
        client = FakeLlmClient(json.dumps(GOOD))
        recipe, note = Scout(client, 3).propose(memory, today=MONDAY, living=[], trials=0)
        self.assertIsNone(recipe)
        self.assertIn("spent", note)
        self.assertEqual(client.calls, [])
        recipe, _ = Scout(client, 3).propose(memory, today=date(2026, 10, 12), living=[], trials=0)
        self.assertIsNotNone(recipe)
        self.assertEqual((memory["week"], memory["spent"]), ("2026-W42", 1))

    def test_an_unreachable_model_is_a_plain_note(self) -> None:
        def boom(system: str, user: str) -> str:
            raise TimeoutError("slow")
        recipe, note = Scout(FakeLlmClient(boom), 3).propose({}, today=MONDAY, living=[], trials=0)
        self.assertIsNone(recipe)
        self.assertIn("could not be reached (TimeoutError)", note)

    def test_the_client_gets_the_short_timeout(self) -> None:
        client = FakeLlmClient("{}")
        client.timeout = 30.0
        Scout(client, 3)
        self.assertEqual(client.timeout, 15.0)
        self.assertEqual(week_key(MONDAY), "2026-W41")
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_scout.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `swarm/scout.py`**

```python
"""The LLM scout: an optional source of new recipes, and a story worth reading.

It is shown the recipe schema, the living agents and how many recipes have
been tried. It answers with one recipe and one sentence of reasoning. The
schema decides: anything that doesn't validate is refused. Each call costs one
of the week's proposals, whatever comes back. The scout never decides
anything else. Its recipes face the same screen, nursery and cull as every
other.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from agentic_trading.swarm.recipe import CHOICES, FAMILIES, SIZING, UNIVERSES, Recipe, validate

TIMEOUT = 15.0


def _shown(options: tuple[Any, ...]) -> list[Any]:
    return [list(o) if isinstance(o, tuple) else o for o in options]


SYSTEM = (
    "You propose ONE daily-bar trading recipe for a paper-trading swarm. Reply with JSON only, shaped "
    '{"recipe": {"family": ..., "params": {...}, "universe": ..., "per_order_pct": ..., "inverse_vol": ...}, '
    '"rationale": "<one sentence>"}. Every value must come from these lists: '
    + json.dumps({"family": list(FAMILIES), "universe": list(UNIVERSES),
                  "params_by_family": {f: {k: _shown(v) for k, v in CHOICES[f].items()} for f in FAMILIES},
                  "per_order_pct": _shown(SIZING["per_order_pct"]), "inverse_vol": _shown(SIZING["inverse_vol"])})
    + " A reversal recipe's universe cannot be crypto. Prefer ideas unlike the living agents."
)


def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _strip_fences(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    return body.strip()


class Scout:
    def __init__(self, client: Any, budget: int) -> None:
        self.client = client
        self.budget = budget
        if hasattr(client, "timeout"):
            client.timeout = TIMEOUT

    def propose(self, memory: dict[str, Any], *, today: date, living: list[dict[str, Any]],
                trials: int) -> tuple[Optional[Recipe], str]:
        week = week_key(today)
        if memory.get("week") != week:
            memory["week"], memory["spent"] = week, 0
        if int(memory.get("spent") or 0) >= self.budget:
            return None, f"the scout's {self.budget} proposals for {week} are spent"
        memory["spent"] = int(memory.get("spent") or 0) + 1
        user = json.dumps({"living_agents": living, "recipes_tried_so_far": trials})
        try:
            text = self.client.complete(SYSTEM, user)
        except Exception as exc:  # noqa: BLE001 — any failure of an optional advisor is a note
            return None, f"the scout could not be reached ({type(exc).__name__})"
        try:
            raw = json.loads(_strip_fences(str(text)))
            recipe = validate(raw["recipe"], origin="scout", parents=())
        except (ValueError, KeyError, TypeError) as exc:
            return None, f"the scout's proposal was refused: {str(exc)[:120]}"
        rationale = " ".join(str(raw.get("rationale") or "").split())[:240]
        memory["last_rationale"] = rationale
        return recipe, rationale
```

Two notes:
- `"not json"` raises `json.JSONDecodeError`, which is a `ValueError`, so the note says "refused". A missing `"recipe"` raises `KeyError`, also "refused".
- The `per_order_pct` case's note contains `per_order_pct must be one of`.

- [ ] **Step 4: Run the tests**

Run: `... -m pytest tests/test_swarm_scout.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/swarm/scout.py tests/test_swarm_scout.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): the optional LLM scout — schema-bound, weekly budget, never decides"
```

---
### Task 10: The daily step and its CLI

**Files:**
- Create: `src/agentic_trading/swarm/step.py`, `src/agentic_trading/swarm/cli.py`
- Modify: `src/agentic_trading/cli.py` (register the parser next to `add_fast_parser`, ~line 1203, and dispatch next to `fast`, ~line 1310); `windows/AgenticTrader.spec` (hiddenimports)
- Test: `tests/test_swarm_step.py`

**Interfaces:**
- Consumes everything above, plus:
  - `load_series(config)` (`evidence.py`)
  - `cost_model_for(state_dir)` (`execution.py`)
  - `FastStore(state_dir).starting_equity()`
  - `MemberBook.load`
  - `build_llm_client()` and `FakeLlmClient` (`llm/client.py`)
- **Produces:**
  - `StepResult(code: int, message: str)`
  - `finished_day(series, today) -> Optional[date]`
  - `stale_note(series, today) -> str`
  - `run_step(config, swarm, *, now=None, series=None, costs=None, cash=None, scout_client=None) -> StepResult`
  - `data/state/swarm.json` with these keys:
    - `as_of`, `stepped_at`, `stale`, `note`, `trials`, `alive`
    - `book{equity, return_pct}`
    - `agents[{id, name, family, universe, origin, born, forward_days, excess_pct, return_pct, drawdown_pct, state, share, errored}]`, where `state` is one of `nursery`, `contributing`, `waiting`, `errored`
    - `recent[{at, event, text}]` (≤ 20, newest first)
    - `scout{enabled, spent, budget, last_rationale}`
  - Journal events (`swarm-<day>.jsonl`): `swarm_birth`, `swarm_death`, `swarm_rejected`, `swarm_agent_error`, `swarm_scout`, `swarm_stale_bars`, `swarm_store_reset`, `swarm_step`
  - CLI: `add_swarm_parser(sub)`, `dispatch_swarm(args) -> int`, `status_lines(data) -> list[str]`

**Order inside one step:**
1. Lock.
2. Stale check.
3. Load the state.
4. Skip if `as_of` is already done.
5. Trim the series to `as_of` (P2).
6. Rebuild the records through `as_of`.
7. For every day since the last step, in order:
   - cull with records through that day and that day's weekday;
   - then shares and the blend from records before that day;
   - then trade at that day's close.
8. Breed and screen into free slots, once, at `as_of`.
9. Extend the signatures.
10. Save; write the status; journal `swarm_step`.
11. Unlock.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_step.py
"""A whole daily step: seeding, catching up, refusing stale or repeated work, ageing."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic_trading.backtest import CostModel
from agentic_trading.config import load_config
from agentic_trading.desk.book import MemberBook
from agentic_trading.swarm.screen import Screen
from agentic_trading.swarm.settings import SwarmConfig
from agentic_trading.swarm.step import finished_day, run_step
from agentic_trading.swarm.store import SwarmStore
from tests.swarm_support import D0, universe
from tests.test_runtime_daemon import _write_config

SERIES = universe(500)
LAST = SERIES["BTCUSD"][-1].start.date()
SWARM = SwarmConfig(enabled=True, max_agents=6, screens_per_day=3)
PASS = Screen(True, "passed", 25, 5.0, 10.0, {})


def _now(day):  # half past midnight on ``day``
    return datetime.combine(day, time(0, 30), tzinfo=timezone.utc)


class StepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(_write_config(Path(self.tmp.name)))
        self.store = SwarmStore(self.config.state_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _step(self, day, series=SERIES, swarm=SWARM, **kw):
        return run_step(self.config, swarm, now=_now(day), series=series, costs=CostModel(), cash=50, **kw)

    def _events(self) -> list[dict]:
        out = []
        for path in sorted(Path(self.config.journal_dir).glob("swarm-*.jsonl")):
            out += [json.loads(line) for line in path.read_text().splitlines()]
        return out

    def test_a_day_counts_only_once_a_later_bar_exists(self) -> None:
        self.assertEqual(finished_day(SERIES, LAST), LAST - timedelta(days=1))  # today's bar is forming
        self.assertEqual(finished_day(SERIES, LAST + timedelta(days=1)), LAST - timedelta(days=1))  # no sync since
        self.assertEqual(finished_day(SERIES, LAST + timedelta(days=2)), LAST - timedelta(days=1))

    def test_stale_bars_do_nothing_but_say_so(self) -> None:
        result = self._step(LAST + timedelta(days=9))
        self.assertEqual(result.code, 0)
        self.assertEqual([e["event"] for e in self._events()], ["swarm_stale_bars"])
        self.assertTrue(self.store.read_status()["stale"])
        self.assertEqual(self.store.load(_now(LAST))[0].trials, 0)

    def test_the_first_step_seeds_a_population_and_a_second_run_changes_nothing(self) -> None:
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self.assertEqual(self._step(LAST + timedelta(days=1)).code, 0)
            state, _ = self.store.load(_now(LAST))
            births = [e for e in self._events() if e["event"] == "swarm_birth"]
            self.assertEqual((len(state.living), state.trials, len(births)), (3, 3, 3))
            as_of = LAST - timedelta(days=1)
            self.assertEqual(state.last_step, as_of.isoformat())
            self.assertTrue(all(a.born == LAST.isoformat() for a in state.living))
            status = self.store.read_status()
            self.assertEqual((status["as_of"], status["alive"], status["trials"]), (as_of.isoformat(), 3, 3))
            self.assertEqual({a["state"] for a in status["agents"]}, {"nursery"})
            book_before = self.store.book_path.read_text()
            again = self._step(LAST + timedelta(days=1))
        self.assertIn("already", again.message)
        self.assertEqual(self.store.load(_now(LAST))[0].trials, 3)
        self.assertEqual(self.store.book_path.read_text(), book_before)

    def test_a_missed_stretch_is_replayed_one_day_at_a_time(self) -> None:
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=4)] for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=3), series=early)
            self._step(LAST + timedelta(days=1))
        book, _ = MemberBook.load(self.store.book_path, name="swarm", starting_equity=50)
        days = [d for d, _ in book.samples]  # first step: as_of LAST-5; second: LAST-4 .. LAST-1
        self.assertEqual(days, [(LAST - timedelta(days=5 - i)).isoformat() for i in range(4)])

    def test_a_replay_culls_each_missed_day_with_that_days_weekday(self) -> None:
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=9)] for s, bars in SERIES.items()}
        seen = []

        def spy(agents, *, today, **kw):
            seen.append(today)
            return []

        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=8), series=early)
            with mock.patch("agentic_trading.swarm.step.cull", side_effect=spy):
                self._step(LAST + timedelta(days=1))
        expected = [LAST - timedelta(days=9 - i) for i in range(9)]  # LAST-9 .. LAST-1, a Monday among them
        self.assertEqual(seen, expected)
        self.assertIn(0, {d.weekday() for d in seen})

    def test_an_erroring_agent_sits_out_and_the_rest_carry_on(self) -> None:
        from agentic_trading.swarm import life
        real = life.forward_record
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=1))
            victim = self.store.load(_now(LAST))[0].living[0].recipe.id

            def flaky(recipe, *args, **kw):
                if recipe.id == victim:
                    raise ArithmeticError("bad bar")
                return real(recipe, *args, **kw)

            with mock.patch("agentic_trading.swarm.step.forward_record", side_effect=flaky):
                self._step(LAST + timedelta(days=1))
        errors = [e for e in self._events() if e["event"] == "swarm_agent_error"]
        self.assertEqual(len(errors), 1)
        states = {a["id"]: a["state"] for a in self.store.read_status()["agents"]}
        self.assertEqual(states[victim], "errored")

    def test_a_held_lock_means_another_step_is_running(self) -> None:
        self.assertTrue(self.store.lock(_now(LAST + timedelta(days=1))))  # held by a step running right now
        self.assertIn("another swarm step", self._step(LAST + timedelta(days=1)).message)

    def test_agents_age_out_of_the_nursery_over_weeks(self) -> None:
        start = LAST - timedelta(days=25)
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            for offset in range(26):
                day = start + timedelta(days=offset)
                window = {s: [b for b in bars if b.start.date() <= day] for s, bars in SERIES.items()}
                self._step(day + timedelta(days=1), series=window)
        status = self.store.read_status()
        oldest = max(a["forward_days"] for a in status["agents"])
        self.assertGreaterEqual(oldest, 20)
        self.assertTrue({a["state"] for a in status["agents"]} & {"contributing", "waiting"})
        state, _ = self.store.load(_now(LAST))
        self.assertGreaterEqual(state.trials, len(state.living))  # the ledger never undercounts

    def test_an_unpatched_step_runs_the_real_screen(self) -> None:
        result = self._step(LAST + timedelta(days=1), swarm=SwarmConfig(enabled=True, max_agents=2,
                                                                          screens_per_day=2))
        self.assertEqual(result.code, 0)
        state, _ = self.store.load(_now(LAST))
        self.assertEqual(state.trials, 2)  # two screens, whatever they decided
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_step.py -q`
Expected: FAIL with `ModuleNotFoundError: agentic_trading.swarm.step`.

- [ ] **Step 3: Implement `swarm/step.py`**

```python
"""One daily step of the swarm: age, cull, keep the book, breed.

The step only ever reads finished days (P2). It replays every day it missed,
in order, as a daily run would have: cull on what that day knew, with that
day's weekday, then blend and trade at that day's close. A machine that was
off loses no cull and no sample. Births are not replayed: they happen once, at
the newest finished day. A lock stops two timers stepping at once.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.service import FastJournal
from agentic_trading.fast.store import FastStore
from agentic_trading.history import Bar
from agentic_trading.swarm.blend import blend_weights, contributing, cull, shares
from agentic_trading.swarm.book import advance, closes_on
from agentic_trading.swarm.breed import crossover, immigrant, mutate
from agentic_trading.swarm.life import Agent, forward_record, through
from agentic_trading.swarm.recipe import Recipe
from agentic_trading.swarm.scout import Scout
from agentic_trading.swarm.screen import duplicate_of, screen
from agentic_trading.swarm.settings import SwarmConfig
from agentic_trading.swarm.store import SwarmState, SwarmStore

STALE_DAYS = 4
RECENT = 20
SIGNATURE_KEEP = 250
MAX_TRIES = 50
BENCH = ("QQQ", "BTCUSD")
NEWS = ("swarm_birth", "swarm_death", "swarm_scout", "swarm_agent_error", "swarm_stale_bars")


@dataclass(frozen=True)
class StepResult:
    code: int
    message: str


def finished_day(series: dict[str, list[Bar]], today: date) -> Optional[date]:
    bars = series.get("BTCUSD") or []
    # P2: a day is finished only once a later BTC bar exists (sync merges the forming candle)
    return min(bars[-1].start.date() - timedelta(days=1), today - timedelta(days=1)) if bars else None


def stale_note(series: dict[str, list[Bar]], today: date) -> str:
    for symbol in BENCH:
        bars = series.get(symbol)
        if not bars:
            return f"there are no {symbol} bars"
        age = (today - bars[-1].start.date()).days
        if age > STALE_DAYS:
            return f"the newest {symbol} bar is {age} days old"
    return ""


def run_step(config: Any, swarm: SwarmConfig, *, now: Optional[datetime] = None,
             series: Optional[dict[str, list[Bar]]] = None, costs: Any = None, cash: Any = None,
             scout_client: Any = None) -> StepResult:
    now = now or datetime.now(timezone.utc)
    store = SwarmStore(config.state_dir)
    if not store.lock(now):
        return StepResult(0, "another swarm step is running; this one stands down")
    try:
        return _Step(config, swarm, store, now, scout_client).run(series, costs, cash)
    finally:
        store.unlock()


def _name(recipe_id: str, lineage: dict[str, dict[str, Any]]) -> str:
    family = ((lineage.get(recipe_id) or {}).get("recipe") or {}).get("family") or "agent"
    return f"{family}-{recipe_id[:4]}"


class _Step:
    def __init__(self, config: Any, swarm: SwarmConfig, store: SwarmStore, now: datetime, scout_client: Any) -> None:
        self.config, self.swarm, self.store, self.now = config, swarm, store, now
        self.scout_client = scout_client
        self.journal = FastJournal(config.journal_dir, prefix="swarm")
        self.events: list[dict[str, Any]] = []

    def say(self, event: str, text: str, **extra: Any) -> None:
        record = {"event": event, "at": self.now.isoformat(), "text": text, **extra}
        self.journal.append(record)
        self.events.append(record)

    def run(self, series: Optional[dict[str, list[Bar]]], costs: Any, cash: Any) -> StepResult:
        from agentic_trading.evidence import load_series
        from agentic_trading.execution import cost_model_for

        series = series if series is not None else load_series(self.config)
        self.costs = costs if costs is not None else cost_model_for(self.config.state_dir)
        self.cash = cash if cash is not None else FastStore(self.config.state_dir).starting_equity()
        today = self.now.date()
        note = stale_note(series, today)
        as_of = finished_day(series, today)
        if note or as_of is None:
            text = f"the swarm waits: {note or 'there are no BTCUSD bars'}"
            self.say("swarm_stale_bars", text)
            self.store.write_status({**self.store.read_status(), "stale": True, "note": text,
                                     "checked_at": self.now.isoformat()})
            return StepResult(0, text)
        state, notes = self.store.load(self.now)
        for text in notes:
            self.say("swarm_store_reset", text)
        if state.last_step and state.last_step >= as_of.isoformat():
            return StepResult(0, f"already stepped through {as_of}; nothing to do")
        self.series = through(series, as_of)
        self._records(state, as_of)
        deaths, agent_shares = self._days(state, as_of)
        births, screens = self._breed(state, as_of)
        for agent in state.living:
            agent.signature.update(zip(agent.record.days, agent.record.returns))
            for key in sorted(agent.signature)[:-SIGNATURE_KEEP]:
                del agent.signature[key]
        state.last_step = as_of.isoformat()
        message = (f"swarm step for {as_of}: {len(state.living)} alive, {screens} screened, {births} born, "
                   f"{deaths} died, {state.trials} recipes tried in all")
        self.say("swarm_step", message, alive=len(state.living), trials=state.trials)
        self.store.save(state)
        self.store.write_status(self._status(state, as_of, agent_shares))
        return StepResult(0, message)

    def _records(self, state: SwarmState, as_of: date) -> None:
        for agent in state.living:
            try:
                agent.record = forward_record(agent.recipe, self.series, date.fromisoformat(agent.born), as_of,
                                              costs=self.costs, cash=self.cash)
            except Exception as exc:  # noqa: BLE001 — one broken agent must not stop the swarm
                if not agent.errored:
                    self.say("swarm_agent_error", f"{agent.recipe.name} hit an error and sits out "
                                                  f"({type(exc).__name__})", agent=agent.recipe.id)
                agent.errored = f"{type(exc).__name__}: {str(exc)[:120]}"

    def _days(self, state: SwarmState, as_of: date) -> tuple[int, dict[str, float]]:
        """Every day since the last step, in order: cull on what the day knew, then trade at its close."""
        book, reset = MemberBook.load(self.store.book_path, name="swarm", starting_equity=self.cash)
        if reset:
            self.say("swarm_store_reset", "the swarm's book was unreadable; it starts fresh")
        day = date.fromisoformat(state.last_step) + timedelta(days=1) if state.last_step else as_of
        deaths, agent_shares = 0, {}
        while day <= as_of:
            known = (day + timedelta(days=1)).isoformat()  # records through this day's close
            seen = [Agent(a.recipe, a.born, a.signature, a.errored, a.record.upto(known)) for a in state.living]
            doomed = cull(seen, today=day, max_drawdown_pct=float(self.swarm.max_drawdown) * 100,
                          cull_after_days=self.swarm.cull_after_days)
            for agent, reason in doomed:
                row = state.lineage.setdefault(agent.recipe.id, {"recipe": agent.recipe.to_dict(), "born": agent.born})
                row.update(died=day.isoformat(), cause=reason, excess_pct=agent.record.excess_pct,
                           days=len(agent.record.days))
                self.say("swarm_death", f"{agent.recipe.name} died: {reason}", agent=agent.recipe.id)
            gone = {agent.recipe.id for agent, _ in doomed}
            state.living = [a for a in state.living if a.recipe.id not in gone]
            deaths += len(doomed)
            before = [Agent(a.recipe, a.born, a.signature, a.errored, a.record.upto(day.isoformat()))
                      for a in state.living]  # shares use only days before this one
            agent_shares = shares(before, nursery_days=self.swarm.nursery_days)
            weights = blend_weights(before, agent_shares, self.series, day)
            advance(book, day, weights, closes_on(self.series, day), self.costs)
            day += timedelta(days=1)
        book.save()
        self.book = book
        return deaths, agent_shares

    def _scout(self) -> Optional[Scout]:
        if not self.swarm.llm_scout:
            return None
        client = self.scout_client
        if client is None:
            from agentic_trading.llm.client import FakeLlmClient, build_llm_client
            client = build_llm_client()
            if isinstance(client, FakeLlmClient):
                self.say("swarm_scout", "the scout is on, but AGENTIC_LLM_API_KEY is not set")
                return None
        return Scout(client, self.swarm.llm_proposals_per_week)

    def _bred(self, living: list[Agent], rng: random.Random) -> Recipe:
        proven = [a for a in living if contributing(a, nursery_days=self.swarm.nursery_days)]
        if not proven or rng.random() >= 0.5:
            return immigrant(rng)
        if len(proven) >= 2 and rng.random() < 0.5:
            a, b = rng.sample(proven, 2)
            return crossover(a.recipe, b.recipe, rng)
        return mutate(rng.choice(proven).recipe, rng)

    def _breed(self, state: SwarmState, as_of: date) -> tuple[int, int]:
        rng = random.Random(f"{self.swarm.seed}:{as_of.isoformat()}:{state.trials}")
        birth = as_of + timedelta(days=1)
        scout = self._scout()
        known = set(state.lineage) | {a.recipe.id for a in state.living}
        births = screens = tries = 0
        while (len(state.living) < self.swarm.max_agents and screens < self.swarm.screens_per_day
               and tries < MAX_TRIES):
            tries += 1
            rationale = ""
            if scout is not None:
                candidate, rationale = scout.propose(
                    state.scout, today=as_of, trials=state.trials,
                    living=[{"name": a.recipe.name, **a.recipe.content(), "forward_days": len(a.record.days),
                             "excess_pct": a.record.excess_pct} for a in state.living])
                scout = None  # at most one proposal per step
                if candidate is None:
                    self.say("swarm_scout", rationale)
                    continue
            else:
                candidate = self._bred(state.living, rng)
            if candidate.id in known:
                continue
            known.add(candidate.id)
            state.trials += 1
            screens += 1
            result = screen(candidate, self.series, birth, costs=self.costs, cash=self.cash)
            reason = result.reason
            if result.passed:
                twin = duplicate_of(result.signature, state.living)
                if twin:
                    reason = f"a near-copy of {twin}"
            if reason != "passed":
                self.say("swarm_rejected", f"{candidate.name} was not born: {reason}", agent=candidate.id)
                continue
            state.living.append(Agent(candidate, birth.isoformat(), dict(result.signature)))
            state.lineage[candidate.id] = {
                "recipe": candidate.to_dict(), "born": birth.isoformat(), "parents": list(candidate.parents),
                "origin": candidate.origin, "rationale": rationale, "died": None, "cause": "",
                "screen": {"return_pct": result.return_pct, "drawdown_pct": result.drawdown_pct,
                           "trades": result.trades}}
            births += 1
            self.say("swarm_birth", self._birth_text(candidate, state, rationale), agent=candidate.id)
        return births, screens

    def _birth_text(self, recipe: Recipe, state: SwarmState, rationale: str) -> str:
        if recipe.origin == "mutation":
            return f"{recipe.name} was born from {_name(recipe.parents[0], state.lineage)}"
        if recipe.origin == "crossover":
            first, second = (_name(p, state.lineage) for p in recipe.parents)
            return f"{recipe.name} was born from {first} × {second}"
        if recipe.origin == "scout":
            return f"{recipe.name} was born from the scout's idea: {rationale}"
        return f"{recipe.name} was born: a newcomer ({recipe.family}, {recipe.universe})"

    def _status(self, state: SwarmState, as_of: date, agent_shares: dict[str, float]) -> dict[str, Any]:
        agents = []
        for a in state.living:
            days = len(a.record.days)
            if a.errored:
                condition = "errored"
            elif a.recipe.id in agent_shares:
                condition = "contributing"
            elif days < self.swarm.nursery_days:
                condition = "nursery"
            else:
                condition = "waiting"
            agents.append({"id": a.recipe.id, "name": a.recipe.name, "family": a.recipe.family,
                           "universe": a.recipe.universe, "origin": a.recipe.origin, "born": a.born,
                           "forward_days": days, "excess_pct": a.record.excess_pct,
                           "return_pct": a.record.return_pct, "drawdown_pct": a.record.drawdown_pct,
                           "state": condition, "share": round(agent_shares.get(a.recipe.id, 0.0), 4),
                           "errored": a.errored})
        news = [{"at": e["at"], "event": e["event"], "text": e["text"]} for e in reversed(self.events)
                if e["event"] in NEWS]
        recent = (news + list(self.store.read_status().get("recent") or []))[:RECENT]
        start = self.book.starting_equity
        return {
            "as_of": as_of.isoformat(), "stepped_at": self.now.isoformat(), "stale": False, "note": "",
            "trials": state.trials, "alive": len(state.living),
            "book": {"equity": str(round(self.book.equity, 2)),
                     "return_pct": round(float(self.book.equity / start - 1) * 100, 3) if start > 0 else 0.0},
            "agents": agents, "recent": recent,
            "scout": {"enabled": self.swarm.llm_scout, "spent": int(state.scout.get("spent") or 0),
                      "budget": self.swarm.llm_proposals_per_week,
                      "last_rationale": str(state.scout.get("last_rationale") or "")},
        }
```

- [ ] **Step 4: Implement `swarm/cli.py` and register it**

```python
"""``agentic-trading swarm``: run today's step, or show the population."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def add_swarm_parser(sub: Any) -> None:
    parser = sub.add_parser("swarm", help="The agent swarm: run today's step, or show the population")
    actions = parser.add_subparsers(dest="swarm_action", required=True)
    for name, text in (("step", "Run the daily step (the systemd timer runs this)"),
                       ("status", "What the swarm holds and who is alive, in plain words")):
        actions.add_parser(name, help=text).add_argument("--config", required=True)


def dispatch_swarm(args: Any) -> int:
    from agentic_trading.config import load_config

    config = load_config(args.config)
    if args.swarm_action == "status":
        try:
            data = json.loads((Path(config.state_dir) / "swarm.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("swarm status: the swarm has not run yet (no swarm.json)")
            return 1
        print("\n".join(status_lines(data)))
        return 0
    from agentic_trading.swarm.settings import load_swarm_config
    from agentic_trading.swarm.step import run_step

    swarm = load_swarm_config(args.config)
    if not swarm.enabled:
        print("swarm step: [swarm] enabled is not true; nothing to do")
        return 0
    result = run_step(config, swarm)
    print(result.message)
    return result.code


def status_lines(data: dict[str, Any]) -> list[str]:
    book = data.get("book") or {}
    lines = [f"swarm as of {data.get('as_of')}: {data.get('alive', 0)} alive, "
             f"{data.get('trials', 0)} recipes tried, book ${book.get('equity')} ({book.get('return_pct')}%)"]
    if data.get("stale"):
        lines.append(f"  {data.get('note')}")
    for agent in data.get("agents") or []:
        lines.append(f"  {agent.get('name')}: {agent.get('state')}, {agent.get('forward_days')} days, "
                     f"{agent.get('excess_pct'):+.2f}% vs benchmark, share {agent.get('share')}")
    return lines
```

In `src/agentic_trading/cli.py`:
- Next to `from agentic_trading.fast.cli import add_fast_parser` / `add_fast_parser(sub)`, add:
  ```python
  from agentic_trading.swarm.cli import add_swarm_parser
  add_swarm_parser(sub)
  ```
- Next to the `if args.command == "fast":` block, add:
  ```python
  if args.command == "swarm":
      from agentic_trading.swarm.cli import dispatch_swarm

      return dispatch_swarm(args)
  ```

In `windows/AgenticTrader.spec`, after `"agentic_trading.dashboard_fast",`, add:
```python
    "agentic_trading.swarm.cli",
    "agentic_trading.swarm.step",
    "agentic_trading.dashboard_swarm",
```
(`dashboard_swarm` is created in Task 12. The Windows build only runs in CI after Task 13, so the order is safe.)

- [ ] **Step 5: Run the tests**

Run: `... -m pytest tests/test_swarm_step.py -q`
Expected: PASS. If `test_agents_age_out_of_the_nursery_over_weeks` is slow (over 60 s), lower `SERIES` to `universe(450)`. Don't weaken the assertions.

- [ ] **Step 6: Add a CLI test to `tests/test_swarm_step.py` and run it**

```python
class CliTests(unittest.TestCase):
    def test_a_disabled_swarm_does_nothing_and_status_reads_plainly(self) -> None:
        import contextlib, io
        from agentic_trading.swarm.cli import dispatch_swarm, status_lines
        from types import SimpleNamespace as NS

        with tempfile.TemporaryDirectory() as name:
            path = _write_config(Path(name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = dispatch_swarm(NS(config=str(path), swarm_action="step"))
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out.getvalue())
        lines = status_lines({"as_of": "2026-10-04", "alive": 1, "trials": 4,
                              "book": {"equity": "50.10", "return_pct": 0.2},
                              "agents": [{"name": "trend-ab12", "state": "nursery", "forward_days": 3,
                                          "excess_pct": 0.5, "share": 0.0}]})
        self.assertIn("4 recipes tried", lines[0])
        self.assertIn("trend-ab12: nursery", lines[1])
```

Run: `... -m pytest tests/test_swarm_step.py -q`, then `... -m pytest tests -q`.
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agentic_trading/swarm/step.py src/agentic_trading/swarm/cli.py src/agentic_trading/cli.py windows/AgenticTrader.spec tests/test_swarm_step.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(swarm): the daily step — finished days only, missed days replayed, lock, status; swarm CLI"
```

---
### Task 11: The desk reads the swarm, judges it and funds nothing

**Files:**
- Modify:
  - `src/agentic_trading/config.py`: `DESK_MEMBER_CHOICES` (line 15) and `load_config` (~line 539)
  - `src/agentic_trading/desk/allocator.py`: `UNFUNDED` (line 32)
  - `src/agentic_trading/cli.py`: `build_desk`, the `if name == "switchboard":` branch (~line 350)
- Test: `tests/test_swarm_desk.py`

**Interfaces:**
- Consumes: `ReadOnlyBook`, `ReadOnlyMember` and `hold_unfunded` (existing); the swarm book at `state/desk/swarm.json` (Task 10).
- **Produces:**
  - `"swarm"` is a valid desk member;
  - `UNFUNDED == frozenset({"switchboard", "swarm"})`;
  - `READ_ONLY_MEMBERS = ("switchboard", "swarm")` in `cli.py`;
  - `load_config` refuses `swarm` in `desk_members` unless `[swarm] enabled = true`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_desk.py
"""The desk reads the swarm's book like the switchboard's: judged, never written, never funded."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentic_trading.desk.allocator import UNFUNDED, hold_unfunded
from agentic_trading.desk.book import ReadOnlyBook
from agentic_trading.desk.member import ReadOnlyMember
from tests.test_desk_wiring import _config

MEMBERS = 'desk_members = ["momentum_rotation", "trend_crypto", "swarm", "benchmark"]'


class SwarmDeskTests(unittest.TestCase):
    def test_the_swarm_is_unfunded_and_its_weight_goes_to_the_benchmark(self) -> None:
        self.assertEqual(UNFUNDED, frozenset({"switchboard", "swarm"}))
        held, would = hold_unfunded({"swarm": 0.5, "benchmark": 0.5}, benchmark="benchmark")
        self.assertEqual((held, would), ({"swarm": 0.0, "benchmark": 1.0}, {"swarm": 0.5}))

    def test_the_swarm_is_a_read_only_member(self) -> None:
        from agentic_trading.cli import build_strategy

        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), MEMBERS, "[swarm]", "enabled = true")
            desk = build_strategy(config)
        member = next(m for m in desk.members if m.name == "swarm")
        self.assertIsInstance(member, ReadOnlyMember)
        self.assertIsInstance(member.book, ReadOnlyBook)

    def test_the_swarm_needs_its_table_switched_on(self) -> None:
        for extra in ((MEMBERS,), (MEMBERS, "[swarm]", "enabled = false")):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as name:
                with self.assertRaisesRegex(ValueError, r"\[swarm\] enabled"):
                    _config(Path(name), *extra)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_swarm_desk.py -q`
Expected: FAIL. `UNFUNDED` lacks `swarm`, and `"swarm"` is an unknown member.

- [ ] **Step 3: Implement**

1. `config.py`, line 15: `DESK_MEMBER_CHOICES = DESK_MEMBER_NAMES + ("dip_reversal", "switchboard", "swarm")`.
2. `config.py`, `load_config`:
   - Change `return Config(` to `config = Config(`.
   - After that call's closing parenthesis, add:
     ```python
     swarm_table = raw.get("swarm") if isinstance(raw.get("swarm"), dict) else {}
     if "swarm" in config.desk_members and swarm_table.get("enabled") is not True:
         raise ValueError("desk_members includes swarm, but [swarm] enabled is not true")
     return config
     ```
3. `desk/allocator.py`: `UNFUNDED = frozenset({"switchboard", "swarm"})`. Extend the comment above it: "The swarm's book is written by its daily job."
4. `cli.py`:
   - Above `def build_desk`, add:
     ```python
     # Members whose paper book another process writes: the venues service (switchboard)
     # and the swarm's daily job. The desk only reads them.
     READ_ONLY_MEMBERS = ("switchboard", "swarm")
     ```
   - Change `if name == "switchboard":  # trades in the venues service; the desk only reads its book` to `if name in READ_ONLY_MEMBERS:`.

- [ ] **Step 4: Run the tests, including the switchboard's desk tests**

Run: `... -m pytest tests/test_swarm_desk.py tests/test_fast_desk.py tests/test_desk_wiring.py -q`, then `... -m pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentic_trading/config.py src/agentic_trading/desk/allocator.py src/agentic_trading/cli.py tests/test_swarm_desk.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(desk): the swarm is a read-only, unfunded member that needs [swarm] switched on"
```

---
### Task 12: The Swarm card

**Files:**
- Create: `src/agentic_trading/dashboard_swarm.py`
- Modify:
  - `src/agentic_trading/dashboard.py`: a `swarm()` method beside `fast()` (~line 1140), and the route beside `/api/fast` (~line 1521)
  - `src/agentic_trading/dashboard_desk.py`: `LABELS`, `TICKER_EVENTS`, `ticker_item`, `DeskEventCache.read`
  - `src/agentic_trading/dashboard_html.py`: the card after the Switchboard card (line 139)
  - `src/agentic_trading/dashboard_css.py`: after the `.fast` rules (~line 107)
  - `src/agentic_trading/dashboard_cockpit_js.py`: the `CockpitFmt` helpers, `renderSwarm`/`pollSwarm` and `start()`
- Test: `tests/test_dashboard_swarm.py`; add to `tests/test_cockpit_js.py` and `tests/test_cockpit_page.py`

**Interfaces:**
- Consumes: `data/state/swarm.json` (Task 10, shape above); `desk_allocation` events with `would_earn` (Task 11).
- **Produces:**
  - `swarm_view(state_dir, events=(), *, now=None) -> dict`
  - `GET /api/swarm`
  - `CockpitFmt.swarmHead(view) -> str` and `CockpitFmt.swarmLine(agent) -> str`
  - ticker kind `"swarm"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dashboard_swarm.py
"""/api/swarm: whitelisted swarm state; births and deaths reach the ticker."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.dashboard_desk import DeskEventCache, label, ticker_item
from agentic_trading.dashboard_swarm import swarm_view

T0 = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)


def _state(folder: Path, stepped: datetime) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "swarm.json").write_text(json.dumps({
        "as_of": "2026-10-04", "stepped_at": stepped.isoformat(), "stale": False, "note": "", "trials": 31,
        "alive": 1, "secret": "LEAK-top",
        "book": {"equity": "50.40", "return_pct": 0.8, "cash": "LEAK-book"},
        "agents": [{"id": "ab12cd34", "name": "trend-ab12", "family": "trend", "universe": "crypto",
                    "origin": "mutation", "born": "2026-09-01", "forward_days": 33, "excess_pct": 1.2,
                    "return_pct": 2.0, "drawdown_pct": 3.1, "state": "contributing", "share": 1.0,
                    "errored": "LEAK-error", "signature": "LEAK-signature"}],
        "recent": [{"at": stepped.isoformat(), "event": "swarm_birth", "text": "trend-ab12 was born",
                    "agent": "LEAK-recent"}],
        "scout": {"enabled": True, "spent": 1, "budget": 3, "last_rationale": "coins trend", "key": "LEAK-scout"},
    }))


class SwarmViewTests(unittest.TestCase):
    def test_no_swarm_means_disabled_with_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            view = swarm_view(Path(name))
        self.assertFalse(view["enabled"])
        self.assertIn("[swarm] enabled = true", view["note"])

    def test_only_whitelisted_fields_pass_and_would_earn_comes_from_the_desk(self) -> None:
        events = [{"event": "desk_allocation", "would_earn": {"swarm": 0.25, "switchboard": 0.0}}]
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            view = swarm_view(Path(name), events, now=T0 + timedelta(hours=1))
        self.assertNotIn("LEAK", json.dumps(view))
        self.assertEqual((view["trials"], view["alive"], view["would_earn"], view["stale"]), (31, 1, 0.25, False))
        self.assertEqual(view["agents"][0]["state"], "contributing")

    def test_a_day_and_a_half_without_a_step_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            self.assertTrue(swarm_view(Path(name), now=T0 + timedelta(hours=37))["stale"])

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
                url = f"http://127.0.0.1:{server.server_address[1]}/api/swarm"
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(payload["agents"][0]["name"], "trend-ab12")


class SwarmTickerTests(unittest.TestCase):
    def test_births_and_deaths_are_ticker_news_in_time_order(self) -> None:
        item = ticker_item({"event": "swarm_death", "at": T0.isoformat(), "text": "trend-ab12 died: drawdown 26%"})
        self.assertEqual((item["kind"], item["text"]), ("swarm", "trend-ab12 died: drawdown 26%"))
        self.assertEqual(label("swarm"), "Swarm")
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "2026-10-05.jsonl").write_text(json.dumps(
                {"event": "desk_allocation", "at": "2026-10-05T11:00:00+00:00", "changed": False}) + "\n")
            (folder / "swarm-2026-10-05.jsonl").write_text(json.dumps(
                {"event": "swarm_birth", "at": "2026-10-05T00:30:00+00:00", "text": "born"}) + "\n")
            (folder / "fast-2026-10-05.jsonl").write_text(json.dumps(
                {"event": "fast_exit", "at": "2026-10-05T12:00:00+00:00", "text": "out"}) + "\n")
            events = DeskEventCache(folder).read()
        self.assertEqual([e["event"] for e in events], ["swarm_birth", "desk_allocation", "fast_exit"])
```

Add to `CockpitFormatTests` in `tests/test_cockpit_js.py`:

```python
    def test_swarm_words(self) -> None:
        self.assertEqual(
            self.js("CockpitFmt.swarmHead({alive: 5, trials: 31, book: {return_pct: 0.8}})"),
            "5 alive · 31 recipes tried · book +0.80%",
        )
        self.assertEqual(
            self.js("[CockpitFmt.swarmLine({forward_days: 33, state: 'contributing', excess_pct: 1.2, share: 0.5}),"
                    " CockpitFmt.swarmLine({forward_days: 3, state: 'nursery', excess_pct: -0.4, share: 0})]"),
            ["33 days · contributing 50% · +1.20% vs 60/40", "3 days · nursery · −0.40% vs 60/40"],
        )
```

Add to the page tests in `tests/test_cockpit_page.py`, in the class holding `test_the_switchboard_card_is_on_the_strategies_tab`:

```python
    def test_the_swarm_card_is_on_the_strategies_tab(self) -> None:
        self.assertIn('id="swarm"', _section("strategies"))
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `... -m pytest tests/test_dashboard_swarm.py tests/test_cockpit_js.py tests/test_cockpit_page.py -q`
Expected: FAIL with `ModuleNotFoundError: agentic_trading.dashboard_swarm`.

- [ ] **Step 3: Implement `dashboard_swarm.py`**

```python
"""The swarm as the console sees it: named fields from ``data/state/swarm.json``.

Only named fields pass through, so nothing the step writes later (error
texts, signatures) can reach the page by accident. ``would_earn`` comes from the
desk's latest weekly allocation event, not from the swarm.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENT_FIELDS = ("id", "name", "family", "universe", "origin", "born", "forward_days", "excess_pct", "return_pct",
                "drawdown_pct", "state", "share")
RECENT_FIELDS = ("at", "event", "text")
BOOK_FIELDS = ("equity", "return_pct")
SCOUT_FIELDS = ("enabled", "spent", "budget", "last_rationale")
FRESH = timedelta(hours=36)
NOTE = ("the swarm has not run yet: set [swarm] enabled = true, then enable the agentic-trading-swarm timer")


def _pick(raw: Any, names: tuple[str, ...]) -> Optional[dict[str, Any]]:
    return {name: raw.get(name) for name in names} if isinstance(raw, dict) else None


def _would_earn(events: Iterable[dict[str, Any]]) -> Optional[float]:
    for record in reversed(list(events)):
        if record.get("event") == "desk_allocation" and isinstance(record.get("would_earn"), dict):
            try:
                return float(record["would_earn"].get("swarm"))
            except (TypeError, ValueError):
                return None
    return None


def swarm_view(state_dir: Path | str, events: Iterable[dict[str, Any]] = (), *,
               now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    try:
        data = json.loads((Path(state_dir) / "swarm.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"enabled": False, "note": NOTE}
    try:
        stepped: Optional[datetime] = datetime.fromisoformat(str(data.get("stepped_at")))
    except ValueError:
        stepped = None
    if stepped is not None and stepped.tzinfo is None:
        stepped = None
    agents = [_pick(a, AGENT_FIELDS) for a in (data.get("agents") or []) if isinstance(a, dict)][:100]
    recent = [_pick(r, RECENT_FIELDS) for r in (data.get("recent") or []) if isinstance(r, dict)][:10]
    return {
        "enabled": True,
        "as_of": data.get("as_of"),
        "stale": bool(data.get("stale")) or stepped is None or current - stepped > FRESH,
        "note": str(data.get("note") or ""),
        "trials": data.get("trials"),
        "alive": data.get("alive"),
        "book": _pick(data.get("book"), BOOK_FIELDS),
        "agents": agents,
        "recent": recent,
        "scout": _pick(data.get("scout"), SCOUT_FIELDS),
        "would_earn": _would_earn(events),
    }
```

- [ ] **Step 4: Wire the route, the ticker and the event files**

`dashboard.py`:
- Import `from agentic_trading.dashboard_swarm import swarm_view` beside `fast_view`.
- Add this method after `fast()`:
  ```python
  def swarm(self) -> dict[str, Any]:
      """The swarm's population and book (``/api/swarm``)."""
      self.refresh_config()
      if self._desk_events.journal_dir != self.journal_dir:
          self._desk_events = DeskEventCache(self.journal_dir)
      return swarm_view(self.state_dir, self._desk_events.read())
  ```
- Add the route after the `/api/fast` branch:
  ```python
  if parsed.path == "/api/swarm":
      self._json(self.state.swarm())
      return
  ```

`dashboard_desk.py`:
- `LABELS`: add `"swarm": "Swarm",` after `"switchboard"`.
- `TICKER_EVENTS`: add `"swarm_birth",` and `"swarm_death",` after `"fast_exit",`.
- In `ticker_item`, before the final `else:  # kill_switch`, add:
  ```python
  elif event in ("swarm_birth", "swarm_death"):
      kind = "swarm"
      text = str(record.get("text") or "")[:200]
  ```
- In `DeskEventCache.read`, replace the `paths = sorted(...)[-2 * days:]` expression with:
  ```python
  paths = sorted(
      (p for p in self.journal_dir.glob("*.jsonl")
       if p.name[:1].isdigit() or p.name.startswith(EVENT_PREFIXES)),
      key=lambda p: _day_key(p.name),
  )[-(len(EVENT_PREFIXES) + 1) * days:]
  ```
  and add these at module level, above the class:
  ```python
  # Side journals whose events join the desk's ticker: the switchboard's and the swarm's.
  EVENT_PREFIXES = ("fast-", "swarm-")


  def _day_key(name: str) -> str:
      for prefix in EVENT_PREFIXES:
          if name.startswith(prefix):
              return name[len(prefix):]
      return name
  ```

- [ ] **Step 5: The card, its style and its script**

`dashboard_html.py`: after the Switchboard card's line (139), add:
```html
<div class="card span12"><h2>Swarm · agents born, judged and culled on daily bars (paper)</h2><div id="swarm" class="swarm"><div class="sub">reading the swarm…</div></div></div>
```

`dashboard_css.py`: after `.item.fast .dot{background:var(--accent)}`, add:
```css
.swarm{display:grid;gap:8px}
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px}
.stile{border:1px solid var(--line);border-radius:8px;padding:6px 8px;background:var(--panel);min-width:0}
.stile b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stile.up{border-color:var(--buy)}
.stile.down{border-color:var(--sell)}
.stile.nursery{opacity:.7}
.stile.errored{border-style:dashed;color:var(--warn)}
.squote{color:var(--muted);font-style:italic}
.item.swarm .dot{background:var(--buy)}
```

`dashboard_cockpit_js.py`:
- Inside the `CockpitFmt` IIFE, after `tradeLine`, add:
  ```javascript
    const swarmHead = (view) => (view.alive || 0) + ' alive · ' + (view.trials || 0) + ' recipes tried · book '
      + (view.book && view.book.return_pct != null ? signedPct(view.book.return_pct) : 'not started');
    const swarmLine = (a) => a.forward_days + ' days · ' + a.state
      + (a.state === 'contributing' ? ' ' + Math.round((a.share || 0) * 100) + '%' : '')
      + ' · ' + signedPct(a.excess_pct || 0) + ' vs 60/40';
  ```
  and add `swarmHead, swarmLine` to its returned object.
- After `pollFast`, add:
  ```javascript
    function renderSwarm(view) {
      const box = $('swarm');
      if (!box) return;
      if (!view || !view.enabled) {
        box.innerHTML = '<div class="sub">' + esc((view && view.note) || 'the swarm has not run yet') + '</div>';
        return;
      }
      const head = '<div class="fhead"><span class="fbadge">' + esc(CockpitFmt.fundedBadge(view.would_earn))
        + '</span><span class="sub">' + esc(CockpitFmt.swarmHead(view)) + '</span></div>';
      const notes = view.stale ? '<div class="sub">' + esc(view.note || 'the swarm has not stepped for a while') + '</div>' : '';
      const tiles = (view.agents || []).map((a) => '<div class="stile ' + esc(a.state) + ' '
        + ((a.excess_pct || 0) >= 0 ? 'up' : 'down') + '"><b title="' + esc(a.family + ' · ' + a.universe + ' · ' + a.origin)
        + '">' + esc(a.name) + '</b><span class="sub">' + esc(CockpitFmt.swarmLine(a)) + '</span></div>').join('');
      const scout = view.scout && view.scout.enabled && view.scout.last_rationale
        ? '<div class="squote">' + esc('scout: “' + view.scout.last_rationale + '”') + '</div>' : '';
      const recent = (view.recent || []).map((r) => '<li>' + esc(r.text) + '</li>').join('');
      box.innerHTML = head + notes + (tiles ? '<div class="sgrid">' + tiles + '</div>' : '<div class="sub">no agents alive yet</div>')
        + scout + (recent ? '<ul class="frecent">' + recent + '</ul>' : '');
    }

    async function pollSwarm() {
      if (document.hidden) return;
      try {
        const response = await fetch('/api/swarm');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        renderSwarm(await response.json());
      } catch (e) {
        // keep the last picture; the header dot already shows a lost console
      }
    }
  ```
- In `start()`, after `setInterval(pollFast, 2000);`, add `pollSwarm();` and `setInterval(pollSwarm, 30000);`.

- [ ] **Step 6: Run the tests**

Run: `... -m pytest tests/test_dashboard_swarm.py tests/test_cockpit_js.py tests/test_cockpit_page.py tests/test_dashboard_fast.py -q`, then `... -m pytest tests -q`
Expected: PASS.

The screen-size sweep (`tools/cockpit_size_sweep.js`) is run against the live dashboard on 127.0.0.1:8787 through the Playwright tool `browser_run_code_unsafe`. It belongs to Task 14, after the live checkout carries this code.

- [ ] **Step 7: Commit**

```bash
git add src/agentic_trading/dashboard_swarm.py src/agentic_trading/dashboard.py src/agentic_trading/dashboard_desk.py src/agentic_trading/dashboard_html.py src/agentic_trading/dashboard_css.py src/agentic_trading/dashboard_cockpit_js.py tests/test_dashboard_swarm.py tests/test_cockpit_js.py tests/test_cockpit_page.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "feat(dashboard): the Swarm card — population tiles, births and deaths, the scout's words, /api/swarm"
```

---
### Task 13: The daily timer and the docs

**Files:**
- Create: `deploy/agentic-trading-swarm` (launcher), `deploy/agentic-trading-swarm.service`, `deploy/agentic-trading-swarm.timer`
- Modify: `README.md` (a Swarm section after "Fast engine", plus the test counts), `CONTRIBUTING.md` (test count), `CLAUDE.md` (layout and services)
- Test: `tests/test_swarm_deploy.py`

**Interfaces:**
- Consumes: the `swarm step` CLI (Task 10).
- Produces: units that run the step daily at 00:30 UTC and catch up after downtime (`Persistent=true`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_swarm_deploy.py
"""The swarm's timer runs daily, catches up after downtime, and runs one step at a time."""

from __future__ import annotations

import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


class DeployTests(unittest.TestCase):
    def test_the_timer_is_daily_and_persistent(self) -> None:
        timer = (DEPLOY / "agentic-trading-swarm.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 00:30:00 UTC", timer)
        self.assertIn("Persistent=true", timer)

    def test_the_service_is_a_oneshot_step(self) -> None:
        service = (DEPLOY / "agentic-trading-swarm.service").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=/home/doczeus/.local/bin/agentic-trading-swarm", service)
        launcher = (DEPLOY / "agentic-trading-swarm").read_text()
        self.assertIn("swarm step --config config/agentic.toml", launcher)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `... -m pytest tests/test_swarm_deploy.py -q`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the units**

`deploy/agentic-trading-swarm` (then `chmod +x`):
```sh
#!/bin/sh
# Launcher for the swarm's systemd unit: the project path contains a space,
# which systemd's ExecStart parser handles poorly, so the cd lives here.
set -e
cd "/home/doczeus/Projects/Agnetic TraDING"
exec ".venv/bin/agentic-trading" swarm step --config config/agentic.toml
```

`deploy/agentic-trading-swarm.service`:
```ini
[Unit]
Description=Agentic Trading swarm step (age, cull, keep the swarm's paper book, breed)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/home/doczeus/.local/bin/agentic-trading-swarm
# twelve screens take ~5 minutes; a long catch-up after days offline takes longer
TimeoutStartSec=45min
Nice=10
StandardOutput=append:/home/doczeus/Projects/Agnetic TraDING/data/state/swarm.log
StandardError=append:/home/doczeus/Projects/Agnetic TraDING/data/state/swarm.log
```

`deploy/agentic-trading-swarm.timer`:
```ini
[Unit]
Description=Run the agentic-trading swarm step daily at 00:30 UTC, and at boot if a run was missed

[Timer]
OnCalendar=*-*-* 00:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Write the docs**

In `README.md`, after the "Fast engine (the switchboard)" section's numbered list, add:

```markdown
### Agent swarm

The swarm is a population of up to 24 paper agents, each a *recipe*: a trend, rotation or reversal
rule on daily bars, with its own parameters. A daily job does the work.
- **Births:** mutation and crossover of proven agents, random newcomers, and optionally an AI scout's
  proposals (schema-bound, 3 a week).
- **The screen:** a filter on the 3 years before birth. It never counts as evidence.
- **Life:** each agent is judged only on bars after its birth, against 60% QQQ / 40% BTC.
- **The blend:** after 20 days, an agent beating the benchmark over its last 60 days earns a share of
  the swarm's book.
- **Deaths:** a 25% drawdown kills an agent at once. On Mondays, the losing bottom quarter of agents
  over 60 days old dies.
- **Judged, not funded:** the desk judges the swarm's one blended book like any member's, and holds its
  weight at 0.
- **Every recipe ever tried is counted** and shown.

1. Add `[swarm]` with `enabled = true` to `config/agentic.toml` (`llm_scout = true` turns on the scout;
   it needs `AGENTIC_LLM_API_KEY`). Put `"swarm"` in `desk_members`.
2. Install `deploy/agentic-trading-swarm` to `~/.local/bin/`, and the `.service` and `.timer` to
   `~/.config/systemd/user/`. Then run `systemctl --user enable --now agentic-trading-swarm.timer`.
3. `agentic-trading swarm step --config config/agentic.toml` runs a step by hand.
   `agentic-trading swarm status --config config/agentic.toml` shows who is alive.
```

In `CLAUDE.md`, under Layout, after the `fast/` line, add:
`` - `swarm/` — the agent swarm: recipes (data), breed, screen, forward life, blend/cull, its member book; daily `swarm step` ``.
In the services sentence, add `agentic-trading-swarm.timer` (daily 00:30 UTC swarm step).

- [ ] **Step 5: Bring the test counts up to date**

Run: `... -m pytest tests -q 2>&1 | tail -1`. Note the passed count N.
Replace the old count with N in the three README places (badge `tests-<N>%20passing`, `pytest tests -q  # <N> tests`, `tests/   <N> tests`) and in CONTRIBUTING.md.
Run: `"/home/doczeus/Projects/Agnetic TraDING/.venv/bin/python" tools/check_doc_counts.py`
Expected: `docs agree with the suite: N tests`.

- [ ] **Step 6: Commit**

```bash
chmod +x deploy/agentic-trading-swarm
git add deploy/agentic-trading-swarm deploy/agentic-trading-swarm.service deploy/agentic-trading-swarm.timer README.md CONTRIBUTING.md CLAUDE.md tests/test_swarm_deploy.py
git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit -m "build(swarm): the daily timer (catches up after downtime) and the swarm's setup docs"
```

---

### Task 14: Launch (operational, on the live machine)

This task changes the running system. Do it only after the whole-branch review has passed and its findings are fixed.

**Files:**
- Local only, never committed: `config/agentic.toml` (backup `config/agentic.toml.bak-swarm`), `~/.local/bin/agentic-trading-swarm`, `~/.config/systemd/user/agentic-trading-swarm.{service,timer}`.

- [ ] **Step 1: Push and open the PR**

```bash
cd /home/doczeus/Projects/agentic-trading-swarm
git push -u origin feat/swarm
gh pr create --base feat/fast-timeframe --head feat/swarm --title "Agent swarm: daily-bar agents bred, judged and culled; one blended, unfunded desk member" --body-file <scratchpad>/pr-swarm.md
```

The body covers:
- the rulings R1–R11 and P1–P5;
- the honesty points (birth screen is not evidence; trial count; forward-only records);
- the test count;
- what is not done: funding, and live orders.

Then run the Windows build workflow on the branch: `gh workflow run windows-build.yml --ref feat/swarm`. Watch it until it is green.

- [ ] **Step 2: Move the live checkout to the reviewed branch**

```bash
cd "/home/doczeus/Projects/Agnetic TraDING"
git status --short | grep -v '^ M data/bars\|^?? data/bars'   # must print nothing
git switch feat/swarm
```

- [ ] **Step 3: Configure: back up first, then edit**

```bash
cp config/agentic.toml config/agentic.toml.bak-swarm
```
Then make two edits with the Edit tool. Read the file first, and never print `config/secrets.toml`.
- In `desk_members`, replace `"switchboard"` with `"swarm"` (ruling R4).
- Append:
  ```toml

  [swarm]
  enabled = true
  llm_scout = true
  ```
- Check it loads: `.venv/bin/agentic-trading selfcheck --offline --config config/agentic.toml` must not FAIL on config.

- [ ] **Step 3b: Launch gate. Can anything be born on real bars at real costs?**

The screen has never run on `data/bars` with `cost_model_for(state_dir)` and the real ~$50 starting cash. At that size, a measured fixed fee per order can sink every recipe, and a swarm where nothing is ever born holds the benchmark forever. Measure first. Save this as `<scratchpad>/dry_screen.py` and run it from the live checkout with `PYTHONPATH=src .venv/bin/python <scratchpad>/dry_screen.py` (about 8 minutes):

```python
import random
from datetime import datetime, timedelta, timezone

from agentic_trading.config import load_config
from agentic_trading.evidence import load_series
from agentic_trading.execution import cost_model_for
from agentic_trading.fast.store import FastStore
from agentic_trading.swarm.breed import immigrant
from agentic_trading.swarm.life import through
from agentic_trading.swarm.screen import screen
from agentic_trading.swarm.step import finished_day

config = load_config("config/agentic.toml")
series = load_series(config)
as_of = finished_day(series, datetime.now(timezone.utc).date())
series = through(series, as_of)
costs, cash = cost_model_for(config.state_dir), FastStore(config.state_dir).starting_equity()
rng, passed = random.Random("dry-run"), 0
for _ in range(20):
    recipe = immigrant(rng)
    result = screen(recipe, series, as_of + timedelta(days=1), costs=costs, cash=cash)
    passed += result.passed
    print(f"{recipe.name:16} {result.passed!s:5} {result.trades:4} trades {result.return_pct:+8.2f}% "
          f"dd {result.drawdown_pct:5.1f}%  {result.reason}")
print(f"passed {passed} of 20 (as_of {as_of}, cash {cash})")
```

How to read the result:
- **Gate: if 0 of 20 pass, stop.** Don't enable the timer or change the config. Report the reasons to the user (the table above) and ask how to proceed, for example a different cash basis for agents or the fee model. Launching an empty swarm would not meet the user's goal.
- **1 or more pass:** continue. These 20 dry screens used the same history as real trials, so note "20 dry-run screens before launch" in the PR and in the memory.

- [ ] **Step 4: Install the timer and run the first step**

```bash
install -m 755 deploy/agentic-trading-swarm ~/.local/bin/agentic-trading-swarm
cp deploy/agentic-trading-swarm.service deploy/agentic-trading-swarm.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentic-trading-swarm.timer
systemctl --user start agentic-trading-swarm.service   # the first step, now (about 5 minutes)
systemctl --user status agentic-trading-swarm.service --no-pager | head -5
.venv/bin/agentic-trading swarm status --config config/agentic.toml
tail -5 data/state/swarm.log
```

Expected:
- The service exited 0, and the status shows `as_of` = yesterday (UTC).
- 12 recipes were tried, with some agents in the nursery.
- `data/journal/swarm-<today>.jsonl` holds births and rejections in plain words.
- If the scout is on but no key is set, there is one `swarm_scout` note and nothing else.

- [ ] **Step 5: Restart the readers and verify**

```bash
systemctl --user restart agentic-trading agentic-trading-dashboard agentic-trading-venues
sleep 20; systemctl --user is-active agentic-trading agentic-trading-dashboard agentic-trading-venues
curl -s http://127.0.0.1:8787/api/swarm | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['enabled'],d['alive'],d['trials'],d['stale'])"
grep -h '"desk_member' data/journal/$(date -u +%F).jsonl | tail -3
```

Expected:
- All three services are `active`.
- `/api/swarm` reads `True <alive> <trials> False`.
- There is no `desk_member_failed` for `swarm`.

Then run the size sweep. In the Playwright tool, call `browser_run_code_unsafe` with `filename: tools/cockpit_size_sweep.js`. Expected `pass: true`. Then screenshot the Strategies tab's Swarm card.

- [ ] **Step 6: Record it**

- Update the memory file `project_agent_swarm.md`:
  - the launch date;
  - the PR number;
  - acceptance status: 1 and 3 seen at launch; 4 at the next Monday allocation; 5 after the sweep;
  - the backup name.
- Update the `MEMORY.md` index line.
- Acceptance 2 (ageing over weeks) is covered by `test_agents_age_out_of_the_nursery_over_weeks`, and live from day 20 onwards.

---

## Self-review notes (kept for the reviewer)

**Spec coverage:**

| Spec item | Where |
|---|---|
| R1 (daily bars) | Tasks 4, 10 |
| R2 (one blended member) | Tasks 6, 8, 11 |
| R3 (unfunded) | Task 11 |
| R4 (switchboard leaves the desk) | Task 14, step 3 |
| R5 (recipes are data) | Task 2 |
| R6 (three sources of recipes) | Tasks 3, 9, 10 |
| R7 (the birth screen) | Task 5 |
| R8 (one engine, forward only) | Tasks 4, 5 |
| R9 (benchmark, and holding it when empty) | Tasks 4, 6 |
| R10 (the daily job and its lock) | Tasks 7, 13 |
| R11 (every trial counted) | Tasks 7, 10, 12 |
| Lifecycle | Task 6 (+ P1), Task 10 |
| Components | One task each; `families.py` folded into `recipe.py` as `rule_kwargs`/`sim_kwargs`, plus the Task 1 changes |
| Data flow | Task 10 (stale bars, read-only) |
| Configuration | Task 2; desk-member check in Task 11 |
| Dashboard | Task 12 |
| Failure handling | Tasks 7, 9, 10 |
| Testing list | Spread across tasks as listed |
| Acceptance 1–5 | Tasks 10, 13, 14 |

**Deviations from the spec, and why:**
- `families.py` is merged into `recipe.py`. The mapping is two small functions, and a separate module would only re-export them.
- The quarter cull runs weekly (P1), and its reason is recorded above.

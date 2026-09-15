# Phase 1: SPY Scalper Strategy Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing paper SPY scalper as a runtime strategy plugin that emits `OrderIntent`s into RiskGuard + shadow journal (never places by default), selectable via CLI.

**Architecture:** Keep `paper_scalper.py` as the offline replay tool. Add `SpyScalperStrategy` that reuses the same signal/exit rules in a streaming `on_quote` form. CLI `--strategy spy_scalper|fixture` selects the strategy. Shadow remains default; live still requires dual gates.

**Tech Stack:** Python 3.11+, existing `agentic_trading` package, unittest/pytest, Decimal money math.

**Spec:** `docs/superpowers/specs/2026-09-15-agentic-robinhood-mcp-design.md` (Phase 1)

**Worktree:** `/home/doczeus/Projects/Agnetic TraDING/.worktrees/phase1-spy-scalper`  
**Branch:** `feature/phase1-spy-scalper`

---

## File structure

| Path | Responsibility |
|---|---|
| `src/agentic_trading/strategies/spy_scalper.py` | Streaming SPY scalper → `OrderIntent` |
| `src/agentic_trading/strategies/__init__.py` | Export `SpyScalperStrategy` |
| `src/agentic_trading/cli.py` | `--strategy` flag; build strategy |
| `src/agentic_trading/config.py` | Optional scalper knobs from TOML (or load `config.json` path) |
| `config/agentic.example.toml` | `strategy = "spy_scalper"`, optional `scalper_config = "config.json"` |
| `tests/test_spy_scalper_strategy.py` | Signal/exit intent tests |
| `tests/test_runtime_spy_shadow.py` | Shadow E2E with spy quotes; zero place |
| `README.md` | Phase 1 usage + live flip docs |

**Do not:** change RiskGuard caps defaults; enable live by default; guarantee profits; call place in tests.

---

### Task 1: SpyScalperStrategy (TDD)

**Files:**
- Create: `src/agentic_trading/strategies/spy_scalper.py`
- Modify: `src/agentic_trading/strategies/__init__.py`
- Create: `tests/test_spy_scalper_strategy.py`

- [ ] **Step 1: Write failing tests** covering:
  - Three rising mids → BUY intent with quantity+ref_price (ask)
  - Flat after buy → SELL on take-profit / stop / timeout (match paper_scalper thresholds from `config.json`)
  - Wrong symbol / stale quote → no intent
  - Cooldown after exit → no immediate re-entry

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /home/doczeus/Projects/Agnetic\ TraDING/.worktrees/phase1-spy-scalper
PYTHONPATH=src:.:tests .venv/bin/python -m pytest tests/test_spy_scalper_strategy.py -v
```

- [ ] **Step 3: Implement strategy**

Reuse paper_scalper rules (do not import network). Stateful class:

```python
class SpyScalperStrategy:
    def __init__(self, config: paper_scalper.Config | ScalperParams): ...
    def on_quote(self, quote: dict) -> list[OrderIntent]:
        # validate → update mids → maybe exit intent OR entry intent
        # Always quantity + ref_price for ShadowBook
```

Position sizing: use notional capped conceptually by strategy budget from paper `initial_cash` (e.g. spend available cash on buy at ask+slippage) OR fixed fractional qty like paper. Prefer matching paper_scalper buy sizing (spend up to cash).

Track open position locally for exit signals only (shadow book is authoritative for RiskGuard; strategy still needs local state for TP/SL timing).

- [ ] **Step 4: Tests PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add SpyScalperStrategy streaming OrderIntents from paper rules.

EOF
)"
```

Use env git identity: DOM JARVIS / jarvice@wemakesense.co (no git config).

---

### Task 2: CLI --strategy + config

**Files:**
- Modify: `src/agentic_trading/cli.py`
- Modify: `src/agentic_trading/config.py` (add `strategy: str = "fixture"`, optional `scalper_config: Path | None`)
- Modify: `config/agentic.example.toml`
- Modify: `tests/test_runtime_shadow.py` if needed for defaults

- [ ] **Step 1: Failing test** — `cmd_run` / strategy factory selects spy_scalper

- [ ] **Step 2: Implement** `--strategy {fixture,spy_scalper}` and config key; default remains `fixture` for backward compatible tests OR update tests to pass fixture explicitly.

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
Wire CLI strategy selection for fixture and spy_scalper.

EOF
)"
```

---

### Task 3: Shadow E2E with recorded SPY quotes

**Files:**
- Create: `tests/test_runtime_spy_shadow.py`

- [ ] **Step 1: Integration test** with FakeMcpClient + SpyScalperStrategy + `data/spy_quotes.jsonl`
  - Assert: place_equity_order count == 0
  - Assert: journal has accepted and/or rejected events (may be zero trades if quotes insufficient for 3-rising-mid — use synthetic quotes that trigger entry if real file doesn't)
  - Prefer synthetic rising quotes in tempfile to guarantee ≥1 buy intent path through runtime

- [ ] **Step 2: Full suite green** including paper tests (`PYTHONPATH=src:.:tests`)

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add spy scalper shadow runtime integration test.

EOF
)"
```

---

### Task 4: Docs + pyproject pythonpath fix

**Files:**
- Modify: `README.md` — Phase 1 section
- Modify: `pyproject.toml` — `pythonpath = ["src", ".", "tests"]` so bare pytest works
- Optional: `docs/superpowers/plans/manual-live-flip-checklist.md` — dual gates reminder

Document clearly:
- Shadow default
- Live requires `flip-mode live` AND `AGENTIC_ALLOW_LIVE=1` AND OAuth
- No profit guarantee; Agentic account can lose all funds

- [ ] **Step 1: Docs + pythonpath**

- [ ] **Step 2: Run full pytest without extra PYTHONPATH if possible**

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
Document Phase 1 spy scalper usage and fix pytest pythonpath.

EOF
)"
```

---

### Task 5: Run shadow soak (operator demo)

- [ ] **Step 1:** Copy example config; set `strategy = "spy_scalper"`
- [ ] **Step 2:** Run:

```bash
.venv/bin/agentic-trading run --config config/agentic.toml
```

(or python -m) against `data/spy_quotes.jsonl` — shadow only, Fake or real MCP.

- [ ] **Step 3:** Report journal summary (would_place count, place count=0). Do **not** set AGENTIC_ALLOW_LIVE.

---

## Out of scope

- Phase 2 LLM multi-asset
- Guaranteeing profits / enabling live trading without operator OAuth
- Rewriting paper_scalper offline CLI

## Success criteria

- [ ] SpyScalperStrategy unit tests pass
- [ ] CLI can select spy_scalper
- [ ] Shadow runtime test: zero place calls
- [ ] Paper scalper tests still pass
- [ ] README documents Phase 1 + live dual-gate warning

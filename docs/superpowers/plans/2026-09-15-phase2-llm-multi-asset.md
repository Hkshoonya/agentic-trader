# Phase 2: LLM Multi-Asset Strategy Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-asset LLM strategy plugin that proposes `OrderIntent`s through the existing RiskGuard + shadow journal path, with Fake LLM in tests/CI and optional OpenAI-compatible HTTP when env is set.

**Architecture:** Introduce an `LlmClient` Protocol (`complete(system, user) -> str` returning JSON intent proposals). `FakeLlmClient` serves canned JSON for tests and CLI fallback. Optional `OpenAiCompatibleLlmClient` uses `AGENTIC_LLM_BASE_URL` + `AGENTIC_LLM_API_KEY` + `AGENTIC_LLM_MODEL` (default `gpt-4o-mini`). `LlmMultiAssetStrategy.on_quote` builds brief context, asks the LLM for 0..1 intents, parses JSON, enforces config whitelist before emit, and builds `OrderIntent` with quantity + ref_price. RiskGuard remains mandatory; shadow remains default; never place in tests.

**Tech Stack:** Python 3.11+, existing `agentic_trading` package, `httpx` (already a dependency), unittest/pytest, Decimal money math.

**Spec:** `docs/superpowers/specs/2026-09-15-agentic-robinhood-mcp-design.md` (Phase 2)

**Worktree:** `/home/doczeus/Projects/Agnetic TraDING/.worktrees/phase1-spy-scalper`  
**Branch:** `feature/phase1-spy-scalper` (Phases 1+2 together)

---

## File structure

| Path | Responsibility |
|---|---|
| `src/agentic_trading/llm/__init__.py` | Export `LlmClient`, `FakeLlmClient`, `OpenAiCompatibleLlmClient`, `build_llm_client` |
| `src/agentic_trading/llm/client.py` | Protocol + Fake + optional OpenAI-compatible HTTP + env factory |
| `src/agentic_trading/strategies/llm_multi_asset.py` | `LlmMultiAssetStrategy` → `OrderIntent` list |
| `src/agentic_trading/strategies/__init__.py` | Export `LlmMultiAssetStrategy` |
| `src/agentic_trading/cli.py` | `--strategy llm`; build LLM client (Fake if no key) |
| `src/agentic_trading/config.py` | Allow `strategy = "llm"` |
| `config/agentic.example.toml` | Comment multi-symbol whitelist + `llm` strategy |
| `tests/test_llm_multi_asset_strategy.py` | Fake LLM buy SPY; wrong symbol; malformed JSON |
| `tests/test_runtime_llm_shadow.py` | Shadow E2E; zero place |
| `tests/test_types_config.py` | Factory selects `llm` |
| `README.md` | Phase 2 section |

**Do not:** set `AGENTIC_ALLOW_LIVE`; call real LLM or Robinhood in tests; change RiskGuard defaults; guarantee profits; break Phase 1.

**LLM JSON proposal schema (locked):**

```json
{"intents":[{"symbol":"SPY","side":"buy","quantity":"0.01","reason":"llm:test"}]}
```

- Top-level object with `intents` array of length 0 or 1 (extra intents ignored → take first only).
- Required per intent: `symbol`, `side` (`buy`|`sell`), `quantity` (positive decimal string).
- Optional: `reason` (default `llm:propose`).
- `ref_price` comes from the quote (ask for buy, bid for sell), not the LLM.
- Malformed / missing fields / JSON parse errors → return `[]` (no intent).

---

### Task 1: Plan document (this file)

**Files:**
- Create: `docs/superpowers/plans/2026-09-15-phase2-llm-multi-asset.md`

- [ ] **Step 1: Write this plan**
- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-09-15-phase2-llm-multi-asset.md
GIT_AUTHOR_NAME="DOM JARVIS" GIT_AUTHOR_EMAIL="jarvice@wemakesense.co" \
GIT_COMMITTER_NAME="DOM JARVIS" GIT_COMMITTER_EMAIL="jarvice@wemakesense.co" \
git commit -m "$(cat <<'EOF'
Add Phase 2 LLM multi-asset strategy implementation plan.

EOF
)"
```

---

### Task 2: LlmClient + Fake + LlmMultiAssetStrategy (TDD)

**Files:**
- Create: `src/agentic_trading/llm/__init__.py`
- Create: `src/agentic_trading/llm/client.py`
- Create: `src/agentic_trading/strategies/llm_multi_asset.py`
- Modify: `src/agentic_trading/strategies/__init__.py`
- Create: `tests/test_llm_multi_asset_strategy.py`

- [ ] **Step 1: Write failing tests** covering:
  - Fake LLM canned buy SPY → one BUY intent with quantity + ref_price (ask)
  - LLM proposes non-whitelisted symbol → `[]`
  - Malformed JSON / missing fields → `[]`
  - Empty intents array → `[]`
  - Quote symbol outside whitelist (before LLM) → `[]` without calling complete (optional assert via call counter)

```python
# tests/test_llm_multi_asset_strategy.py (sketch)
def test_fake_llm_buy_spy():
    client = FakeLlmClient(
        '{"intents":[{"symbol":"SPY","side":"buy","quantity":"0.01","reason":"llm:test"}]}'
    )
    strategy = LlmMultiAssetStrategy(
        client=client, whitelist=frozenset({"SPY"}), default_quantity=Decimal("0.01")
    )
    intents = strategy.on_quote({
        "symbol": "SPY",
        "bid": "100.00",
        "ask": "100.02",
        "observed_at": "2026-09-15T12:00:00Z",
    })
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
    assert intents[0].symbol == "SPY"
    assert intents[0].quantity == Decimal("0.01")
    assert intents[0].ref_price == Decimal("100.02")
```

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /home/doczeus/Projects/Agnetic\ TraDING/.worktrees/phase1-spy-scalper
.venv/bin/python -m pytest tests/test_llm_multi_asset_strategy.py -v
```

- [ ] **Step 3: Implement**

`LlmClient` Protocol:

```python
class LlmClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...
```

`FakeLlmClient(response: str | Callable[[str, str], str])` — returns canned JSON (or callable for dynamic tests).

`OpenAiCompatibleLlmClient` — POST `{base}/chat/completions` with Bearer key; extract `choices[0].message.content`. Only constructed when API key present.

`build_llm_client()` — if `AGENTIC_LLM_API_KEY` set, return OpenAI-compatible client using:
- `AGENTIC_LLM_BASE_URL` (default `https://api.openai.com/v1`)
- `AGENTIC_LLM_API_KEY`
- `AGENTIC_LLM_MODEL` (default `gpt-4o-mini`)
- else return `FakeLlmClient` with a safe default empty-intents JSON (CLI will warn).

`LlmMultiAssetStrategy`:
- `__init__(client, whitelist, default_quantity=Decimal("0.01"))`
- `on_quote`: validate quote has symbol/bid/ask; if symbol not in whitelist → `[]`; build brief system+user context; `client.complete`; parse JSON; take first intent only; if proposed symbol not in whitelist → `[]`; build `OrderIntent` with qty + ref_price from quote.

- [ ] **Step 4: Tests PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add LlmClient, FakeLlmClient, and LlmMultiAssetStrategy with unit tests.

EOF
)"
```

Use env git identity: DOM JARVIS / jarvice@wemakesense.co (no git config).

---

### Task 3: CLI wiring + config example

**Files:**
- Modify: `src/agentic_trading/cli.py`
- Modify: `src/agentic_trading/config.py` — allow `strategy` in `fixture|spy_scalper|llm`
- Modify: `config/agentic.example.toml` — comment multi-symbol + llm
- Modify: `tests/test_types_config.py` — factory selects llm

- [ ] **Step 1: Failing test** — `build_strategy(..., strategy_name="llm")` returns `LlmMultiAssetStrategy`

- [ ] **Step 2: Implement**
  - CLI `--strategy {fixture,spy_scalper,llm}`
  - `build_strategy`: for `llm`, call `build_llm_client()`; if Fake (no key), print warning like Fake MCP; pass `config.symbol_whitelist`
  - Example TOML comments:

```toml
# strategy = "llm"
# symbol_whitelist = ["SPY", "QQQ", "IWM"]  # Phase 2 multi-asset; RiskGuard enforces
```

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
Wire CLI --strategy llm and expand config example for multi-asset.

EOF
)"
```

---

### Task 4: Shadow E2E test

**Files:**
- Create: `tests/test_runtime_llm_shadow.py`

- [ ] **Step 1: Integration test** with `FakeMcpClient` + `FakeLlmClient` (canned SPY buy) + `LlmMultiAssetStrategy`
  - Assert: `place_equity_order` count == 0
  - Assert: journal has ≥1 accepted and/or rejected / `would_place` activity
  - Do **not** set `AGENTIC_ALLOW_LIVE`
  - Do **not** hit real LLM HTTP

- [ ] **Step 2: Full suite green**

```bash
.venv/bin/python -m pytest tests/ -v
```

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add LLM multi-asset shadow runtime integration test.

EOF
)"
```

---

### Task 5: README Phase 2 docs

**Files:**
- Modify: `README.md`

Document:
- `--strategy llm` / `strategy = "llm"`
- Fake LLM default when no `AGENTIC_LLM_API_KEY` (CI-safe)
- Optional OpenAI-compatible env vars
- Expanded `symbol_whitelist` via config; RiskGuard still mandatory
- Shadow default; live dual gates unchanged
- **No profit guarantee**

- [ ] **Step 1: Docs**
- [ ] **Step 2: Commit**

```bash
git commit -m "$(cat <<'EOF'
Document Phase 2 LLM multi-asset strategy usage.

EOF
)"
```

---

### Task 6: Shadow soak (operator demo)

- [ ] **Step 1:** Ensure `config/agentic.toml` exists (copy from example if needed); keep mode shadow
- [ ] **Step 2:** Run with Fake LLM (no API key):

```bash
unset AGENTIC_ALLOW_LIVE AGENTIC_LLM_API_KEY
.venv/bin/agentic-trading run --config config/agentic.toml --strategy llm
```

- [ ] **Step 3:** Report journal summary (would_place count, place count=0). Do **not** set `AGENTIC_ALLOW_LIVE`.

---

## Out of scope

- Guaranteeing profits / enabling live trading without operator OAuth
- Fine-tuning prompts for alpha
- Streaming LLM APIs / tool-calling agents
- Changing RiskGuard percentage caps defaults

## Success criteria

- [ ] `LlmMultiAssetStrategy` unit tests: buy SPY, wrong symbol rejected, malformed → `[]`
- [ ] CLI can select `llm`; falls back to Fake without key
- [ ] Shadow runtime test: zero place calls
- [ ] Phase 0/1 tests still pass; full pytest green
- [ ] README documents Phase 2 + RiskGuard + shadow + no profit guarantee

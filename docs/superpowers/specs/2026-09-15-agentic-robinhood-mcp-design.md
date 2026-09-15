# Agentic Robinhood MCP Trading Bot — Design

**Date:** 2026-09-15  
**Status:** Approved for planning  
**Project:** `~/Projects/Agnetic TraDING`

## Goal

Build a local Python autonomous trading agent that analyzes markets and can execute through Robinhood’s Trading MCP (`https://agent.robinhood.com/mcp/trading`), with hard risk limits and a default **shadow** mode that never places live orders until explicitly enabled.

## Non-goals (v1)

- Cloudflare / cloud-hosted agent runtime
- Dom-Jarvis integration
- Cursor-as-brain as the primary loop
- Live multi-asset LLM strategy (planned later; same shell)
- Web UI or public hosting
- Margin borrowing, shorting, staking, crypto transfers

## Decisions locked in brainstorming

| Decision | Choice |
|---|---|
| Autonomy | Full (C): autonomous within hard limits |
| Strategies | SPY scalper (A) + multi-asset LLM (C), phased |
| v1 delivery | Shared foundation first, then SPY scalper on top |
| Runtime | Local Python daemon |
| Risk model | % of Agentic account equity |
| Day-one money | Shadow mode only (`review_*` / simulate; log would-place) |
| Architecture | Thin MCP client + strategy plugins + RiskGuard |

### Default risk policy (v1)

| Cap | Value |
|---|---|
| Max order notional | 5% of Agentic equity |
| Daily notional | 20% of Agentic equity |
| Daily loss kill-switch | −3% of Agentic equity (halt writes until manual reset) |
| Max open positions | 1 |
| Symbol whitelist | `SPY` only until LLM phase |
| Equity source | MCP account read at session start + every N ticks (default **N = 30**) or at least every **60s**, whichever comes first |

### How RiskGuard measures state

**Open positions (max = 1):**
- Cap applies to **new entries only** (`side=buy` that opens or increases exposure). **Closing sells** that reduce or flatten an existing position are always allowed through the position-count check (still subject to whitelist, kill-switch, and shadow/live mode rules).
- **Live:** count open Agentic positions from MCP account/positions read (broker is source of truth). External app activity counts. If the read fails, refuse **new entries** (closes still allowed only if the guard can verify the sell reduces a known held symbol; otherwise refuse).
- **Shadow:** maintain a local shadow book of would-place opens/closes. Rebuild from today's journal on process start (same local day). Do not invent broker fills. Cap = 1 open shadow position for **entries**.
- **Notional caps:** max-order 5% and daily notional 20% apply to **entries and closes** (absolute notional). Kill-switch blocks all writes including closes.

**Daily notional:** sum of absolute order notionals for intents that passed the guard since **local midnight** (operator TZ from config, default system local). Shadow uses would-place notionals; live uses submitted place notionals (not cancelled-before-submit).

**Daily loss (−3% kill-switch):**
- Baseline equity = first successful MCP equity read after local midnight (or session start if no prior baseline that day).
- **Live:** `daily_pnl = current_equity - baseline_equity` (mark-to-market on open positions via broker equity). Kill when `daily_pnl <= -0.03 * baseline_equity`.
- **Shadow:** `daily_pnl` from shadow book realized P&amp;L only (no mark-to-market on open shadow positions for kill). Kill when realized shadow loss ≤ −3% of baseline. This keeps shadow conservative about closing the loop without fake MTM.
- Kill-switch clears only via CLI `reset-kill-switch` (does not auto-reset at midnight; midnight starts a new daily notional/PnL window but an active kill remains until reset).

## Context: existing codebase

The repo already has a deterministic **paper** SPY scalper (`paper_scalper.py`, `config.json`, quote JSONL, reporting/dashboard). It has **no** broker auth or order placement. Quotes for experiments have been collected via Robinhood MCP in chat; watch mode rereads a local file.

v1 **reuses** scalper signal/money math as a strategy plugin. It does **not** replace the offline paper experiment; paper mode remains the default for tests and replay.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────────┐
│ Quote feed  │────▶│  Strategy    │────▶│ RiskGuard   │────▶│ RobinhoodMcp     │
│ (local/RH)  │     │  (SPY scalper)│     │ % equity    │     │ Streamable HTTP  │
└─────────────┘     └──────────────┘     │ shadow/live │     │ OAuth + tools/*  │
                                         └─────────────┘     └──────────────────┘
                                                │
                                                ▼
                                         Decision journal
                                         (JSONL audit log)
```

### Layers

1. **Broker (MCP)** — OAuth/PKCE, Streamable HTTP JSON-RPC, `initialize` / `tools/list` snapshot, `tools/call`
2. **RiskGuard** — sole path to write tools; caps, whitelist, kill-switch, shadow vs live
3. **Strategy** — pure logic → `OrderIntent` only (never calls MCP)
4. **Runtime** — loop: refresh equity → quotes → strategy → guard → journal / MCP
5. **Later** — `LlmMultiAssetStrategy` plugs into the same RiskGuard + Broker

Strategies must not import the MCP client. RiskGuard must not contain strategy rules. Broker must not enforce portfolio policy beyond transport errors.

## Components & package layout

Target package name: `agentic_trading` (or keep flat modules if preferred during scaffolding; planner chooses one layout and sticks to it).

| Module | Responsibility |
|---|---|
| `rh_mcp/` | OAuth 2.1 + PKCE, Streamable HTTP client, tool snapshot writer |
| `broker.py` | Facade: `get_equity()`, `review_order()`, `place_order()` mapped from **discovered** tool names |
| `risk.py` | `RiskGuard` with equity % caps, whitelist, kill-switch, mode |
| `strategies/spy_scalper.py` | Port/adapt existing paper scalper → `OrderIntent` |
| `runtime.py` | Main loop + CLI entry (`python -m agentic_trading …`) |
| `journal/` | Append-only JSONL: intent, guard decision, MCP result |
| `config` | Mode, caps, whitelist, quote source, token path (secrets not in git) |

Existing `paper_scalper.py` / `reporting.py` stay usable for offline replay. Prefer extracting shared types/math into importable modules rather than duplicating Decimal rules.

### Broker tool discovery

- Endpoint: `https://agent.robinhood.com/mcp/trading` (Streamable HTTP)
- Auth: OAuth 2.1 + PKCE; bearer token on requests
- At startup: `initialize` → `tools/list` → write `data/tools_snapshot.json` (dated, diffable)
- Publicly documented tool to treat as confirmed: `review_equity_order` (simulate / pre-trade warnings)
- **All other tool names and argument schemas come from the live snapshot**, not hardcoded guesses
- Capability map (config or generated): logical ops (`review_equity`, `place_equity`, `get_account`, …) → concrete MCP tool names after snapshot

### OrderIntent (strategy → guard)

Minimum fields (all required unless noted):

- `decision_id` (stable UUID / ULID for idempotency)
- `symbol`, `side` (`buy` | `sell`)
- **Notional for caps:** either `notional_usd` **or** (`quantity` **and** `ref_price`) so RiskGuard can compute `notional_usd = quantity * ref_price` before % checks. Strategies that only know shares (e.g. ported scalper) must supply `ref_price` (typically ask for buy, bid for sell).
- `reason` / signal metadata
- `created_at`

Guard rejects intents that lack a computable positive notional. Guard may enrich with equity snapshot, cap checks, and mode before journal write.

## Data flow

### Shadow (default)

1. Load config (`MODE=shadow`)
2. Authenticate MCP; snapshot tools; read Agentic equity
3. On each tick: ingest quotes → strategy emits 0..1 intents
4. RiskGuard validates; if OK, call **review/simulate only**
5. Journal `{decision_id, intent, would_place: true, review, caps, mode: shadow}`
6. Never call place/write tools

### Live (explicit flip)

Same path; after review passes and guard allows, call place tool **once** per `decision_id`. Record fill/error; update daily notional. Kill-switch uses broker equity vs daily baseline (see RiskGuard measurement), not a separate local realized-only counter.

### Quote sources (v1)

1. **Local JSONL** (existing format) — primary for tests and shadow soak
2. Optional later: MCP market/account tools if present in snapshot — not required to ship foundation

## Error handling & safety

### Auth

- Tokens under `~/.config/agentic-trading/tokens.json` (mode `0600`); path overridable
- Refresh before expiry; on 401 → one refresh retry, then halt with `AUTH_FAILED`
- Never commit tokens, `.env`, or OAuth client secrets

### MCP

- Treat `tools/call` with `isError: true` as failure
- Timeout → skip tick; do not retry place for the same `decision_id` without journal proof of non-submission
- Tool snapshot change between sessions → log warning; refuse to place if mapped write tool missing

### RiskGuard hard stops

- When `daily_pnl <= -0.03 * baseline_equity` → kill-switch (no write tools, including closes, until `reset-kill-switch`)
- Unknown symbol → reject before MCP write
- Entry over max-order or daily notional cap → reject; closes still checked against those notional caps
- Entry when open position count ≥ max → reject; **closing sells are exempt from the max-position check**
- Shadow mode blocks write/place tools at the guard even if misconfigured elsewhere

### Operational

- SIGINT/SIGTERM: finish in-flight review; do not start new orders
- Idempotency via `decision_id` + journal
- CLI: `status`, `reset-kill-switch`, `flip-mode shadow|live`
- Robinhood app remains source of truth for disconnect and account activity

### Disclosures / operator responsibility

Agentic trading can lose the entire Agentic account balance. The operator owns all decisions and monitoring. This software does not provide investment advice.

## Testing

| Layer | Approach |
|---|---|
| Scalper math | Keep/extend `tests/test_paper_scalper.py` (no network) |
| RiskGuard | Unit tests: % caps, whitelist, kill-switch, shadow blocks writes |
| Broker adapter | Fake MCP client; schema validation against fixture snapshot |
| Runtime shadow | Integration with recorded quotes + fake review responses |
| Live path | Not required in CI; gated manual checklist after OAuth |

No test may call the real Robinhood MCP or place real orders.

## Phased delivery

The **next implementation plan covers Phase 0 only**. Phases 1–2 are follow-ons after Phase 0 ships.

### Phase 0 — Foundation (this plan’s primary scope)

- MCP client + OAuth flow (desktop-friendly)
- Tool snapshot + capability map
- RiskGuard + journal + CLI mode/kill-switch
- Shadow-only end-to-end against fake or real `review_*` with local quotes

### Phase 1 — SPY scalper strategy

- Wire existing scalper as plugin
- Shadow soak on live quotes (collector or MCP, whichever Phase 0 supports)
- Document how to flip to live (opt-in; out of default path)

### Phase 2 — Multi-asset LLM agent (out of v1 plan detail)

- Same Broker + RiskGuard
- Expanded whitelist and caps via config
- LLM proposes intents; guard remains mandatory

## Success criteria (Phase 0)

1. `tools/list` snapshot written and used for capability mapping
2. Shadow run produces journal entries with `would_place` and review payloads, zero place calls
3. RiskGuard unit tests cover over-cap, wrong-symbol, kill-switch, missing-notional, and entry-blocked-but-close-allowed when max positions reached
4. Existing paper scalper tests still pass
5. Operator can flip mode and reset kill-switch via CLI without editing code

## Open points for the implementation plan (not blockers)

- Exact OAuth register/authorize/token URLs and PKCE library choice (verify against Robinhood’s current OAuth discovery / docs at implement time)
- Whether equity comes from one specific MCP tool vs parsing account payload — bind after first authenticated `tools/list`
- Package layout: monomodule vs `src/agentic_trading` — prefer `src/` if adding `pyproject.toml`
)

# Phase 3: Autonomous live loop (implementation record)

**Date:** 2026-09-16
**Status:** Implemented, shadow-verified, live path not yet exercised with real tokens
**Spec:** `docs/superpowers/specs/2026-09-15-agentic-robinhood-mcp-design.md`

## Why this phase exists

Phases 0–2 built a correct-shaped shell, but the write path had never been
checked against the live MCP tool schema and the runtime was a finite replay
over a JSONL file. Three defects would have failed on the first authenticated
run:

1. `Broker.get_equity()` called a capability mapped to a tool named
   `get_account`, which does not exist. The real surface is `get_accounts`
   (list) and `get_portfolio(account_number)` (value). The capability heuristic
   tokenised `get_accounts` to `{get, accounts}` and never matched.
2. No `account_number` was configured or sent, although every real equity tool
   requires one.
3. `_order_kwargs` sent `notional_usd` and `ref_price` to
   `place_equity_order`, whose schema allows only
   `account_number/symbol/side/type/quantity|dollar_amount/limit_price/stop_price/time_in_force/market_hours/ref_id`
   and rejects unknown properties.

The passing test suite did not catch any of this because the test fake invented
a `get_account` tool instead of mirroring the live tool list.

## What was built

| Area | Change |
|---|---|
| Capability map | Real names (`get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_quotes`, `review_equity_order`, `place_equity_order`, `get_equity_orders`, `cancel_equity_order`); option/crypto tools can never bind to equity capabilities |
| `orders.py` | `EquityOrderRequest` encodes every documented parameter rule and renders schema-valid arguments |
| `session.py` | US equity sessions (premarket/regular/afterhours/overnight/weekend/holiday) with 2026 NYSE holidays, early closes, and policy mapping |
| `marketdata.py` | `McpQuoteFeed` (polls `get_equity_quotes`) and `FileQuoteFeed` (tails a collector file); fail-closed normalization |
| `broker.py` | Real account discovery, portfolio/positions parsing (fail closed on unknown shapes), schema-correct orders, order lookup and cancel |
| `runtime.py` | `run_daemon` continuous loop: session gating, throttled equity refresh, open-order reconciliation, order-count cap, consecutive-error kill switch; `build_order_request` picks a placeable order for the session |
| `cli.py` | `accounts`, `probe`, and `run --daemon/--once/--duration-seconds/--dry-run/--session-policy` |
| Strategies | `LlmMultiAssetStrategy` now rejects stale/future/unlabelled-source quotes before calling the LLM |
| Failure handling | Review failures, positions-read failures and unexpected tick errors are journaled, never fatal; repeated failures trip the kill switch. Quotes older than `max_quote_age_seconds` are dropped before any strategy sees them, so a restart or stalled collector cannot replay history into live orders |

## Verification performed

- `189 passed, 80 subtests passed` (was 123 passed before this phase)
- Order arguments validated against the **recorded live JSON schema** in
  `tests/fixtures/tools_snapshot.json` (`additionalProperties: false` enforced)
- Shadow daemon cycle executed through the real CLI with the offline client:
  `order_request` logged a `market`/`dollar_amount` buy and an exact-quantity
  sell, each with `ref_id`, and zero place calls
- `accounts` and `probe` executed end-to-end; `probe` wrote `data/state/probe.json`
- OAuth discovery verified live: `authorization_endpoint`, `token_endpoint`,
  `registration_endpoint` match `rh_mcp/oauth.py`; PKCE S256; public client
  (`token_endpoint_auth_method: none`)

## Not verified (blocking a live claim)

- No authenticated MCP session has been run here; `get_portfolio` and
  `get_equity_quotes` payload shapes are parsed conservatively and fail closed.
  Run `agentic-trading probe` after `auth` to confirm them.
- `agentic-trading auth` (dynamic registration → browser → token exchange) has
  not completed end-to-end in this environment.
- No live order has been placed, by design.

## Operator path to live

```bash
agentic-trading auth --config config/agentic.toml          # once
agentic-trading snapshot-tools --config config/agentic.toml
agentic-trading probe --config config/agentic.toml         # confirm shapes
agentic-trading accounts --config config/agentic.toml
agentic-trading run --config config/agentic.toml --daemon --once --dry-run
# shadow soak, review data/journal/<date>.jsonl
agentic-trading flip-mode live --config config/agentic.toml
AGENTIC_ALLOW_LIVE=1 agentic-trading run --config config/agentic.toml --daemon
```

## Deliberate non-goals

- No profit guarantee, ever. The SPY scalper hypothesis is unvalidated.
- No auto-cancellation of resting orders (operator decision, not the daemon's).
- No margin, shorting, options, or crypto.

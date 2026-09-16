# Manual live flip checklist (Phase 1)

Shadow is the safe default. Live order placement is dual-gated and operator-only.
**Do not enable live for routine soaks or CI.** There is no profit guarantee;
the Agentic account can lose all funds.

## Before considering live

1. Complete OAuth: [`manual-oauth-checklist.md`](manual-oauth-checklist.md).
2. `agentic-trading snapshot-tools --config config/agentic.toml` — refresh the
   tool list and confirm the capability map covers accounts, portfolio,
   positions, quotes, orders, review and place.
3. `agentic-trading probe --config config/agentic.toml` — confirm the real
   `get_portfolio` and `get_equity_quotes` payload shapes parse. If equity or
   quotes fail to parse the runtime fails closed and places nothing.
4. `agentic-trading accounts --config config/agentic.toml` — confirm the
   selected account is the one you intend to trade.
5. Run a dry-run cycle, then a shadow soak:
   `agentic-trading run --config config/agentic.toml --daemon --once --dry-run`
6. Confirm the journal shows accepted `order_request` objects, any rejected
   reasons, and **zero** `place_equity_order` calls.
7. Understand RiskGuard caps, `max_orders_per_day`, fees, market sessions, and
   order types.

## Gates (all required)

1. Persist live mode: `agentic-trading flip-mode live --config config/agentic.toml`
2. Export gate: `export AGENTIC_ALLOW_LIVE=1` (only for the live session)
3. Session must be permitted by `session_policy` (default `regular` only)
4. RiskGuard must allow the intent; every placement is preceded by a review call

Missing any gate → no place. Revert promptly:

```bash
agentic-trading flip-mode shadow --config config/agentic.toml
unset AGENTIC_ALLOW_LIVE
```

## Never do

- Set `AGENTIC_ALLOW_LIVE` in shell profiles, CI, or default configs.
- Promise or expect profits from the SPY scalper hypothesis.
- Skip shadow soak review before any live attempt.
- Leave the daemon unattended on its first live session.

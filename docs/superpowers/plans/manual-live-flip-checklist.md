# Manual live flip checklist (Phase 1)

Shadow is the safe default. Live order placement is dual-gated and operator-only.
**Do not enable live for routine soaks or CI.** There is no profit guarantee;
the Agentic account can lose all funds.

## Before considering live

1. Run shadow with `--strategy spy_scalper` (or `strategy = "spy_scalper"` in TOML).
2. Confirm journal shows `would_place` / accepted / rejected — and **zero** `place_equity_order` calls.
3. Complete OAuth: [`manual-oauth-checklist.md`](manual-oauth-checklist.md).
4. Understand RiskGuard caps, fees, market hours, and order types.

## Dual gates (both required)

1. Persist live mode: `agentic-trading flip-mode live --config config/agentic.toml`
2. Export gate: `export AGENTIC_ALLOW_LIVE=1` (only for the live session)

Missing either gate → no place. Revert promptly:

```bash
agentic-trading flip-mode shadow --config config/agentic.toml
unset AGENTIC_ALLOW_LIVE
```

## Never do

- Set `AGENTIC_ALLOW_LIVE` in shell profiles, CI, or default configs.
- Promise or expect profits from the SPY scalper hypothesis.
- Skip shadow soak review before any live attempt.

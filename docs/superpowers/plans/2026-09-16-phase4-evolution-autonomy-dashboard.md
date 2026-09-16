# Phase 4: Evolutionary self-evaluation, autonomy, and the dashboard

**Date:** 2026-09-16
**Status:** Implemented and shadow-verified; promotion exercised with synthetic edge data only

## Goal

Let the agent test itself, change its own stage when the evidence supports it,
and show the operator everything it is doing — without ever hiding a refusal.

## Components

| Module | Responsibility |
|---|---|
| `history.py` | OHLCV bars: parse MCP historicals, import CSV, convert recordings, aggregate, store as JSONL |
| `backtest.py` | Cost-aware simulation, metrics, bootstrap significance, chronological folds |
| `evolution.py` | Genome search (mutation + crossover) on a training slice; champion validated on held-out bars |
| `promotion.py` | The gate: criteria, verbose refusals, stage state machine, demotion triggers |
| `selfimprove.py` | Glue: evolve → assess → journal → promote/demote, gated by autonomy + env consent |
| `runtime.py` | `_self_improve_cycle` runs every daemon cycle: demote first, then re-evolve on schedule |
| `dashboard.py` / `dashboard_html.py` / `dashboard_js.py` | Read-only stdlib HTTP console with animated order flow and gate evidence |
| `cli.py` | `fetch-history`, `import-history`, `evolve`, `promote`, `dashboard` |

## Safety properties

1. Promotion cannot happen without **both** `autonomy = "auto"` and
   `AGENTIC_ALLOW_AUTONOMY=1`.
2. `--dry-run` forces shadow and `set_mode("live")` is a no-op under it.
3. Demotion is checked before optimisation on every cycle, and a demotion
   persists the mode file so a restart cannot silently re-enter live.
4. Probation reduces the per-order cap to 1% of equity.
5. Every assessment, promotion, refusal and demotion is journaled with evidence.
6. The dashboard is read-only and binds to loopback.

## Verification

- `221 passed, 80 subtests passed` (was 189 before this phase)
- End-to-end, synthetic strong edge, `autonomy = "auto"` + consent:
  journal `evaluation → promotion → stage_applied → autonomy_applied`,
  `mode` file `live`, `promotion.json` stage `probation`, 21 OOS trades, 3/3 folds
  positive, bootstrap p = 0.0
- Without the environment variable: `promotion_requires_consent`, no mode file,
  stays shadow
- Drawdown demotion: live stage + 20% equity drop → `demotion`, stage `shadow`,
  mode file `shadow`
- Real recorded data (25 bars) → `evolve` refuses: "only 25 bars available"

## Honest limits

- The synthetic run proves the *mechanics*, not an edge. No claim is made that
  any genome is profitable on real markets.
- The backtester models spread, slippage and bar-level fills; it cannot model
  queue position, partial fills, halts, or borrow constraints.
- Daily/5-minute bars cannot validate the original seconds-scale scalper; that
  needs tick data and a different execution model.
- `get_equity_historicals` payload shapes are parsed defensively and fail closed
  until confirmed against a real authenticated response.

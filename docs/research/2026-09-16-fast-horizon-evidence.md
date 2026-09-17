# Can this bot trade "fast"? — evidence from the 5-minute horizon

**Date:** 2026-09-16 · **Verdict: no evidenced edge. Do not wire a fast strategy
on this evidence.**

## The question

The daily book (`strategy = "trend_crypto"`) decides rarely: its best assessed
candidate shows 23 out-of-sample trades with expectancy 124.99 bps, p = 0.053
against a search-corrected bar of 0.000347. That is not eligible, and it also
means the bot mostly watches. "Make it trade faster" is therefore a question
about a different horizon, not about a faster polling loop.

## What the broker allows

- `get_equity_historicals` supports `15second, 30second, minute, 5minute,
  10minute, 30minute, hour, 4hour` with `bounds` of `regular`, `extended`,
  `trading`, `24_5`, `24_7`, `hyper_trading`.
- **A single call is capped at 5000 bars** ("estimated 5222 bars exceeds cap of
  5000"). Intraday history must therefore be fetched in pages and concatenated:
  roughly 64 trading days of 5-minute bars per call, ~1 day of 15-second bars.
- There is no crypto historicals tool at all, so the 24/7 book's history has to
  come from an outside source (see the Coinbase import already in `data/bars`).
- Measured round trip on the live MCP gateway: **~1.3 s per call**.

## Method

1. Paged 5-minute bars for SPY, QQQ, AAPL, NVDA into `data/intraday/`
   (`*_5minute.jsonl`, deduplicated on `start`), two windows: ~3 months and the
   full year 2025-09-16 → 2026-09-15 (19,506 bars per symbol, 27 MB).
2. Ran the existing evolution engine on that pooled intraday horizon with a
   copy of the live config (`history_path = data/intraday`, separate state and
   journal dirs so the running daemon was untouched):

       agentic-trading evolve --config /tmp/intraday.toml \
         --population 60 --generations 6 --seed 42

3. Compared against the promotion gate as it is enforced in
   `promotion.py`: ≥30 OOS trades, >1 bps OOS expectancy after costs, ≥60% of
   OOS folds positive, ≤15% max drawdown, and bootstrap `p ≤ 0.05 / hypotheses
   tested`.

## Results

| Horizon | genomes | OOS trades | OOS expectancy | folds + | OOS DD | bootstrap p | tier bar | eligible |
|---|---|---|---|---|---|---|---|---|
| 5-minute, ~3 months | 360 | 7 | +46.96 bps | 4/4 | 0.96% | 0.103 | 0.000139 | **no** |
| 5-minute, 1 year | 360 | 54 | +14.73 bps | 2/3 | 10.42% | 0.240 | 0.000139 | **no** |
| daily (baseline, in production) | 144 | 23 | +124.99 bps | 3/4 | 7.97% | 0.053 | 0.000347 | **no** |

Blocking reasons for the year-long intraday run, verbatim from the engine:

- `in-sample sample too small (28 trades < 30)`
- `edge does not survive the search: p=0.2400 > 0.000139 (0.05 / 360 hypotheses tested)`

Buy-and-hold over the same year, for contrast: SPY +0.44%, QQQ −0.02%,
AAPL −0.35%, NVDA −0.81%. The intraday champion's 54 trades at +14.7 bps is
roughly +0.8% total against a 10.4% drawdown — and a 24% chance of being luck.

## What this means

1. **The apparent fast edge shrank by 3x when the sample grew 4x** (+46.96 →
   +14.73 bps), and its p-value got *worse* (0.103 → 0.240). This is the same
   pattern every earlier strategy family showed, and it is the reason the gate
   exists.
2. **The 5000-bar cap is the binding constraint for fast horizons.** A single
   call's worth of 5-minute data cannot produce 30 clean out-of-sample trades in
   the strategy families implemented here, and 15-second bars buy ~1 day of history
   per call — hopeless for evidence, however attractive for execution.
3. **Costs decide this horizon.** The engine charges 2 bps spread + 1 bps
   slippage per side (6 bps round trip). A 5-minute signal has to clear that
   before it clears the search-corrected significance bar; nothing here does.
4. **The loop is not the limiting factor.** The daemon already sweeps every
   ~6 s (`cycle_stats` heartbeat: ~4.0 s of work + 2 s poll, ~56 fresh crypto
   quotes/minute). Making the loop faster would not create an edge; it would
   only act on a signal that has not been shown to exist.

## What would change the answer

- More independent evidence at the fast horizon (paged intraday history across
  many more symbols, or a venue with a real historical API for the crypto book).
- A signal family that trades rarely enough to survive costs but often enough to
  reach 30+ OOS trades inside the fetchable window.
- Anything that lowers the cost floor (wider-timeframe entries, maker-style
  limit entries) rather than raising the trade count.

Until then the honest position is: run the fast *loop*, keep the daily
*strategy*, and do not describe intraday as a proven edge.

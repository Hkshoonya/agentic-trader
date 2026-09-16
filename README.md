# Agentic Robinhood trading (shadow-first)

Two systems live in this repo:

1. **`paper_scalper.py`** — an offline, deterministic $50 paper-trading experiment over recorded quotes. Every trade and balance it produces is simulated; it makes no network calls and holds no credentials.
2. **`agentic_trading`** — a shadow-first autonomous agent that talks to Robinhood's Trading MCP (`https://agent.robinhood.com/mcp/trading`): quotes → strategy → RiskGuard → decision journal, with optional live order placement behind two independent gates.

**Default is shadow mode.** Orders are simulated, reviewed, and journaled — never placed. There is no profit guarantee; an agentic account can lose all of its funds.

Python 3.10+ is required for the paper scalper (stdlib only). The agentic runtime requires Python 3.11+.

## Phase 0 — Agentic shadow foundation

Phase 0 adds a shadow-first Robinhood MCP agent: quotes → strategy → risk guard → decision journal. **Default mode is shadow** — orders are reviewed and logged but never placed. Live placement requires an explicit mode flip and environment gate (see below).

### Install (venv recommended)

On many Linux distros, PEP 668 blocks system-wide `pip install`. Use a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

This installs the `agentic-trading` CLI and dev test dependencies.

### Configure

```bash
cp config/agentic.example.toml config/agentic.toml
```

Edit paths, symbol whitelist, and risk limits as needed. Tokens are stored outside the repo (see `token_path` in the config).

### Shadow run (offline or with Fake MCP)

Uses the recorded quote file by default. Without OAuth tokens, the runtime falls back to a local Fake MCP client (CI-safe):

```bash
agentic-trading run --config config/agentic.toml
# equivalent:
python -m agentic_trading run --config config/agentic.toml
```

Check mode, kill switch, and daily notional:

```bash
agentic-trading status --config config/agentic.toml
```

### Real Robinhood MCP (desktop OAuth)

For authenticated MCP (equity review, account read, and optional live placement):

1. Follow the operator checklist: [`docs/superpowers/plans/manual-oauth-checklist.md`](docs/superpowers/plans/manual-oauth-checklist.md)
2. **Auth:** `agentic-trading auth --config config/agentic.toml`
3. **Snapshot tools:** `agentic-trading snapshot-tools --config config/agentic.toml`

Then run shadow as above; with valid tokens the broker uses the real MCP endpoint instead of Fake MCP.

### Live mode (two gates)

Shadow is the safe default. **Live order placement requires both:**

1. `agentic-trading flip-mode live --config config/agentic.toml`
2. `AGENTIC_ALLOW_LIVE=1` in the environment when running

Revert to shadow with `agentic-trading flip-mode shadow --config config/agentic.toml`.

### Risk disclosure

Agentic trading can lose money. Past simulation or shadow results do not predict live performance. This software is for research and operator-controlled experimentation — **not financial advice**. Understand Robinhood fees, market hours, and order types before enabling live mode.

### Verify Phase 0

```bash
.venv/bin/python -m pytest tests/ -v
```

## Phase 1 — SPY scalper strategy (shadow)

Phase 1 wires the paper SPY scalper rules as a streaming runtime strategy (`SpyScalperStrategy`). It emits `OrderIntent`s into RiskGuard + the decision journal. **Shadow remains the default** — the strategy never places live orders unless both live gates are set (below).

There is **no profit guarantee**. Past paper or shadow results do not predict live performance. The Agentic account can lose all funds.

### Select the strategy

In `config/agentic.toml` (copied from the example):

```toml
strategy = "spy_scalper"
scalper_config = "config.json"  # optional; paper_scalper knobs
```

Or override on the CLI (config default remains `fixture`):

```bash
agentic-trading run --config config/agentic.toml --strategy spy_scalper
```

Offline paper replay is unchanged:

```bash
python3 paper_scalper.py --quotes data/spy_quotes.jsonl --config config.json --output results
```

### Live dual-gate warning

Live placement still requires **all** of:

1. `agentic-trading flip-mode live --config config/agentic.toml`
2. `AGENTIC_ALLOW_LIVE=1` in the environment
3. Valid OAuth tokens (`agentic-trading auth`)

Never set `AGENTIC_ALLOW_LIVE` for routine soaks. Operator checklist: [`docs/superpowers/plans/manual-live-flip-checklist.md`](docs/superpowers/plans/manual-live-flip-checklist.md).

### Verify Phase 1

```bash
.venv/bin/python -m pytest tests/ -v
```

## Phase 2 — Multi-asset LLM strategy (shadow)

Phase 2 adds `LlmMultiAssetStrategy`: an LLM proposes 0..1 `OrderIntent`s from brief quote context. **RiskGuard remains mandatory** on every intent. **Shadow remains the default** — the strategy never places live orders unless both live gates are set.

There is **no profit guarantee**. LLM proposals are research heuristics, not advice. Past shadow results do not predict live performance. The Agentic account can lose all funds.

### Select the strategy

In `config/agentic.toml`:

```toml
strategy = "llm"
# Expand whitelist via config; RiskGuard still rejects non-whitelisted symbols
# symbol_whitelist = ["SPY", "QQQ", "IWM"]
```

Or override on the CLI:

```bash
agentic-trading run --config config/agentic.toml --strategy llm
```

### LLM client (Fake by default)

Without `AGENTIC_LLM_API_KEY`, the CLI uses `FakeLlmClient` (empty intents by default) and prints a warning — **CI-safe; no real LLM HTTP**.

Optional OpenAI-compatible HTTP when configured:

| Env var | Role | Default |
|---|---|---|
| `AGENTIC_LLM_API_KEY` | Bearer token (required for real HTTP) | unset → Fake |
| `AGENTIC_LLM_BASE_URL` | API base URL | `https://api.openai.com/v1` |
| `AGENTIC_LLM_MODEL` | Model name | `gpt-4o-mini` |

### Live dual-gate warning

Same as Phase 0/1: live placement still requires `flip-mode live`, `AGENTIC_ALLOW_LIVE=1`, and valid OAuth. Never set `AGENTIC_ALLOW_LIVE` for routine soaks.

### Verify Phase 2

```bash
.venv/bin/python -m pytest tests/ -v
```

## Phase 3 — autonomous live loop

Phase 3 turns the shadow foundation into a loop that can run unattended against
a live feed, with the order path built to the **actual** MCP schema (verified
2026-09-16). It also fixes three integration breaks that would have failed on
the first authenticated run: the broker called a non-existent `get_account`
tool, no `account_number` was ever sent, and live orders carried parameters the
place tool rejects.

### What the daemon does each cycle

1. Checks the US equity session (America/New_York) against `session_policy`
2. Refreshes account equity from `get_portfolio` (throttled by `equity_refresh_seconds`)
3. Reads open orders and blocks new entries for symbols with a pending order
4. Polls quotes (`quote_source = "mcp"` → `get_equity_quotes`, or `"file"` → tails a collector JSONL)
5. Runs the strategy → RiskGuard → `review_equity_order` → journal
6. If (and only if) every gate passes: `place_equity_order` with `ref_id = decision_id`, then journals the result

### Commands

```bash
# Read-only: which account would the bot trade?
agentic-trading accounts --config config/agentic.toml

# Read-only: dump raw account/quote payloads to confirm parser shapes
agentic-trading probe --config config/agentic.toml

# One autonomous cycle (best first live smoke test; --dry-run forces shadow)
agentic-trading run --config config/agentic.toml --daemon --once --dry-run

# Unattended loop
agentic-trading run --config config/agentic.toml --daemon
```

`--dry-run` forces shadow mode even when `state_dir/mode` says live. `probe`
writes `state_dir/probe.json`, which contains account data and is gitignored.

### Live gates (all four required)

1. `agentic-trading flip-mode live --config config/agentic.toml`
2. `AGENTIC_ALLOW_LIVE=1` in the environment
3. Current session permitted by `session_policy` (default `regular`)
4. RiskGuard allows the intent, and a review call precedes every placement

### Order construction rules (encoded, not assumed)

| Situation | What is sent |
|---|---|
| Buy, regular hours, `order_type = "market"` | dollar-based market order (the only fractional path) |
| Sell, regular hours | exact held quantity, market order |
| `order_type = "limit"`, or any non-regular session | marketable limit at the touch, whole shares only |
| Fractional quantity outside regular hours | refused locally (`order_invalid`) — the position could not be exited |

### Hard limits in the autonomous loop

- `max_orders_per_day` entries per local day, plus the notional caps in RiskGuard
- `max_consecutive_errors` broker placement failures trip the kill switch
- Stale or future-dated quotes are refused before a strategy or LLM sees them
- An open order for a symbol blocks another entry in that symbol
- Kill switch and daily counters survive restarts via `state_dir/risk_guard.json`

### Known constraints and unverified areas

- **Fractional shares only trade in regular hours as market orders.** At $50,
  one SPY share (~$660) is unaffordable, so `order_type = "market"` is the only
  workable default. Limit orders require whole shares.
- The included `data/spy_quotes.jsonl` is a **premarket** recording, so its
  fractional intents are logged as `order_invalid` — a true finding about that
  strategy/session combination, not a bug.
- `get_portfolio` and `get_equity_quotes` payload shapes are parsed
  conservatively and **fail closed**. Run `probe` with live tokens before the
  first live session to confirm them; unknown shapes stop trading rather than
  guessing at account value.
- In shadow mode the strategy's internal book and the shadow book can diverge
  when an intent is rejected. Only the journal-derived shadow book is
  authoritative for shadow P&L.
- The OAuth endpoints were verified against live discovery metadata
  (`/oauth-authorization-server`, `/oauth-protected-resource`) on 2026-09-16,
  but a full `agentic-trading auth` round trip has not been exercised here.
- No test places a real order; the live path is covered by schema-conformance
  tests against the recorded tool list.

## Run the experiment (paper trading)

## Phase 4 — self-evaluation, self-promotion, and the live console

The agent now tests itself, keeps the evidence, and changes its own trading
stage only when that evidence justifies it. Two new ideas carry the weight:

**1. Honest out-of-sample evaluation.** `evolve` searches strategy genomes on a
training slice of real OHLCV bars, then validates the champion on a slice the
search never saw, charging spread and slippage on both sides of every trade.
Convention details that keep results trustworthy: entries fill at the *next*
bar's open, and when a bar touches both take-profit and stop, the stop is
assumed to fill first.

**2. A promotion gate that refuses.**

| Requirement | Default |
|---|---|
| Out-of-sample trades | ≥ 30 |
| Out-of-sample expectancy after costs | > 1 bps |
| Profitable out-of-sample folds | ≥ 60% |
| Out-of-sample max drawdown | ≤ 15% |
| Bootstrap p-value on trade returns | < 0.05 |
| Consecutive passing assessments per stage | 3 |

Stages are `shadow → probation → live`. Probation trades live at a reduced
per-order cap (`probation_max_order_pct`, default 1%). Demotion back to shadow is
immediate on the kill switch, a broker error streak, or a live drawdown beyond
5% from the equity recorded when the stage began.

### Autonomy levels

| `autonomy` | Behaviour |
|---|---|
| `manual` (default) | the bot never evaluates or promotes itself |
| `assisted` | re-evolves on a schedule and journals a verdict, but never promotes |
| `auto` | may promote/demote itself, **and only when `AGENTIC_ALLOW_AUTONOMY=1`** |

`auto` requires two independent opt-ins: the config value *and* the environment
variable for that session. Without the environment variable the agent still
evaluates, journals `promotion_requires_consent`, and stays in shadow.

### The live console

```bash
agentic-trading dashboard --config config/agentic.toml --open
```

Read-only, stdlib-only, binds to `127.0.0.1`. It auto-refreshes every 2 seconds
and shows: mode/session/kill-switch badges, account equity and daily notional
against the cap, accepted/placed/rejected counters, the promotion streak gauge,
the gate's verdict with every unmet requirement listed, an animated order-flow
chart, the live execution stream, and the latest evolution evidence.

### Typical loop

```bash
agentic-trading auth --config config/agentic.toml
agentic-trading fetch-history --config config/agentic.toml \
    --symbol SPY --start 2025-01-01T00:00:00Z --interval 5minute
agentic-trading evolve --config config/agentic.toml        # verdict + evidence
agentic-trading dashboard --config config/agentic.toml --open
```

### Reality check on the current data

`evolve` on the repo's recorded quotes refuses outright: 25 observations is not
a sample, and the tool says so instead of producing a number that looks
impressive. A strong edge is required *after* costs, on unseen data, across
multiple folds, with a bootstrap p-value below 0.05 — and even then the result
is a hypothesis about the past, not a promise about the future.

From this directory:

```bash
python3 paper_scalper.py --quotes data/spy_quotes.jsonl --config config.json --output results
```

Open `results/dashboard.html` in a browser. It is an offline report, with no web server or login. The command also writes:

- `report.md`: readable results and assumptions.
- `result.json`: complete state, configuration, and equity history.
- `trades.csv`: completed simulated round trips.
- `proposals.csv`: hypothetical entries/exits with creation and expiration times.
- `events.jsonl`: entries, exits, signal resets, and rejected observations.

Historical proposals have expired. They are a decision log, not current instructions to buy or sell.

## Follow an incoming quote file

```bash
python3 paper_scalper.py --quotes data/spy_quotes.jsonl --config config.json --output results-watch --watch --duration-seconds 180
```

Watch mode rereads a file that a **separate read-only quote collector** appends to. It recomputes the deterministic simulation and updates the reports for the specified duration. It does not fetch prices itself. A stale feed is identified using the current clock; stale data cannot fabricate an execution. Open positions remain in the last report when the process stops. Ctrl+C stops the watcher.

The initial collection was performed through the Robinhood MCP connection in this chat. To collect another session, use the workflow in `QUOTE_COLLECTION.md`. Without a collector, watch mode evaluates only the existing recording. No background collection or continuous agent service is installed.

## Predefined experiment rules

| Setting | Default |
|---|---|
| Starting virtual cash / maximum position budget | $50 |
| Instrument | SPY |
| Entry | Three consecutive strictly rising bid/ask midpoints |
| Maximum entry spread | 2 basis points (0.02%) |
| Maximum age of a quote at observation | 10 seconds |
| Maximum gap between observations in an entry signal | 30 seconds |
| Modeled purchase | Ask plus 1 basis point |
| Modeled sale | Bid minus 1 basis point |
| Fixed fee | $0 per order, configurable |
| Take-profit / stop-loss | +0.10% / -0.10% of entry cost after modeled costs |
| Holding timeout | 60 seconds, evaluated on the next fresh quote |
| Cooldown after an exit | 30 seconds |
| Completed trade cap | 3 |
| Experiment loss threshold | $1 |

A basis point is 0.01%. A 0.1% move on $50 is $0.05 before costs. The strategy is an educational hypothesis, not a validated method for making money. It is not tuned to make the included recording profitable.

The engine buys fractional quantities rounded down to eight decimal places, uses Decimal arithmetic, reserves both order fees, holds one long position at a time, and never borrows. It excludes additional deposits, short selling, and leverage. The `daily_loss_limit` configuration name means a limit for the entire run; it does not reset at midnight.

## What the simulation can and cannot establish

- Buying uses the ask; selling uses the bid. Slippage is an additional adverse price adjustment. Fund expenses embedded in market prices are not deducted a second time.
- The default zero fixed fee is a model assumption. Robinhood lists commission-free stock/ETF trading and describes additional fees and exemptions in its [trading fee documentation](https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/). The program does not verify account-specific charges, taxes, order minimums, or broker rounding.
- [Robinhood fractional shares](https://robinhood.com/us/en/support/articles/fractional-shares/) support eligible dollar purchases, but the overnight/premarket eligibility of this experiment's hypothetical fills was not verified.
- A stop or holding limit is a decision threshold, **not a guaranteed fill or loss cap**. Sparse observations, gaps, and price moves can produce a later, worse exit. A wide spread blocks entry but still permits risk exits.
- Invalid, future, stale, duplicate, out-of-order, or wrong-symbol observations cannot execute a simulated trade. Invalid observations and long gaps reset the entry signal.
- If data ends while a position is open, the report marks it using the last valid modeled liquidation value. It does not invent a closing price or count unrealized gains as realized profit.
- Source labels in input are collector-supplied. Feed timestamps and spreads are checked; source identity is not independently authenticated.
- Seconds-to-minutes strategies need much denser data, tested execution assumptions, and many independent samples before their performance can be assessed. A few premarket observations establish only that the software processes data.
- This is a local offline tool. It has not been assessed for public hosting or public launch.

## Input format

Each complete line is a JSON object. Prices must be positive finite decimal strings, with bid <= ask. All timestamps must include a timezone. `quote_at` is the older bid/ask source timestamp; `observed_at` is when the collector received the quote.

The supported price range is $0.00000001 through $1,000,000,000. Nonzero decimal configuration values use the same numeric range. These bounds keep malformed numeric inputs from exhausting arithmetic precision. In watch mode, observations ahead of the current clock are rejected. Proposal expiration is measured from the source quote time, so an already-old quote cannot gain an extra freshness window.

```json
{"symbol":"SPY","observed_at":"2026-09-15T08:00:49Z","quote_at":"2026-09-15T08:00:47.559374037Z","bid":"757.24","ask":"757.31","source":"Robinhood MCP","session":"premarket","delayed":null}
```

The order of observations is preserved. The engine never sorts later information into an earlier decision. Watch mode tolerates an incomplete final line while a collector writes; complete malformed records are counted as rejected.

## Verify

```bash
python3 -m unittest discover -s tests -v
```

Tests use synthetic prices, separate from recorded market data. They check accounting, fee and slippage behavior, exits, timing, data quality, budget enforcement, report generation, and configuration validation.

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

### Crypto (24/7) — a separate broker namespace

Robinhood's crypto tools are not the equity tools with a different symbol. They
want the numeric `rhs_account_number` (not the alphanumeric `account_number`),
reject `market_hours` outright, spell a stop-triggered market order `stop_loss`,
and accept only `gtc`-or-omitted for market/limit durations. Sending an equity
argument shape to a crypto tool (or the reverse) fails with `unexpected
additional properties`, which is what `broker.review_order`/`place_order` route
around by symbol, and what `tests/test_crypto_routing.py` pins against the
recorded tool schemas.

Two consequences worth knowing before enabling anything:

- **Holdings live in two books.** `get_equity_positions` cannot see crypto, so
  the live loop merges `get_crypto_positions` and `get_crypto_orders` before it
  asks RiskGuard. Without that merge a live crypto position is invisible: entries
  would stack past `max_open_positions` and an exit would be refused as
  `would_short`, stranding the position. If the crypto book cannot be read while
  the whitelist trades pairs, the merged snapshot fails closed.
- **Crypto goes live later than equities.** It has no closing bell to flatten
  into, so it only submits at `stage = "live"` — not at `probation`, and not
  while the promotion gate still reports `eligible: false`. Below that it is
  simulated and journaled as `place_refused` with reason
  `crypto_requires_live_stage`.

### Operator services

Both halves run as `systemd --user` units that restart on failure and at boot:

```bash
systemctl --user status  agentic-trading agentic-trading-dashboard
systemctl --user restart agentic-trading            # daemon (trading loop)
systemctl --user restart agentic-trading-dashboard  # console on 127.0.0.1:8787
journalctl --user -u agentic-trading -f             # live log
```

The console is read-only, binds to loopback, and re-reads `config/agentic.toml`
on every request, so editing the strategy or whitelist does not require a
restart (and cannot silently misreport what the daemon is doing).

### Self-promotion vs. arming: two different switches

The agent can promote itself, and that is now on (`autonomy = "auto"` plus
`AGENTIC_ALLOW_AUTONOMY=1` in the unit). Once the evidence gate passes three
consecutive assessments it moves itself shadow → probation → live with no
operator action.

Submitting a real order is a *separate* switch, `AGENTIC_ALLOW_LIVE=1`, and it
is deliberately unset. So there are three states, and the console badge tells
them apart:

| State | Meaning | How it reads |
|---|---|---|
| `shadow` | evidence gate has not passed | `stage: shadow`, `disarmed` |
| live + `disarmed` | gate passed, nobody armed submission | `LIVE`, `disarmed`, journal `live_gate_blocked` |
| live + `LIVE ARMED` | gate passed and the operator armed it | `LIVE`, `LIVE ARMED`; orders are submitted |

The daemon publishes what it actually sees to `state_dir/live_gate.json` (the
console runs in its own process and cannot read the daemon's environment), and
journals a `live_gate` record at startup and on every stage change. When a
decision is ready and only arming is missing it journals `live_gate_blocked`
instead of looking like a normal shadow cycle.

Crypto is stricter than equities: it only submits at `stage = "live"`, never at
`probation`, because it has no closing bell to flatten into.

### How fast this can actually go

Measured against the live gateway on 2026-09-16 (read-only calls):

| Operation | Measured |
|---|---|
| one MCP round trip (quotes, account, review) | ~1.3 s |
| one full loop cycle (quote poll + order read) | 7.9 s of work, ~10 s with the default poll |
| LLM advisor decision (DeepSeek `deepseek-flash`) | 3.7 s |
| full entry path once a signal fires | ~6-8 s |

Every decision is one more round trip, so this is a seconds-scale system, not a
high-frequency one: there is no colocated execution, no order-book feed, and no
way to beat a 1.3 s API round trip. What it can do is decide continuously
(`cycle_stats` heartbeat in the journal reports the measured cadence), trade the
24/7 crypto book around the clock, and size up as equity grows.

Levers, in order of effect: `open_order_refresh_seconds` (each poll skipped is
two round trips), `poll_seconds`, and how often the LLM advisor sits in the
decision path.

### The risk budget moves with confidence

The configured percentages are a **ceiling**, not a setting the agent spends by
default. Every assessment grades the evidence on a 0..1 confidence scale and the
agent's own budget moves between 25% of that ceiling and 100% of it:

| | value |
|---|---|
| `config.max_order_pct` / `daily_notional_pct` | the ceiling — never exceeded |
| floor | 25% of the ceiling (still trades, just smaller) |
| growth step | +25% per assessment, never past what confidence justifies |
| cut step | −50% per assessment |
| growth needs | confidence not deteriorating since the last assessment |
| kill switch / demotion | straight to the floor; confidence must be rebuilt |

Confidence is graded from the same evidence the promotion gate uses, and every
component is journaled so a size change can always be traced to the number that
moved it:

`significance` (bootstrap p vs 0.05) · `sample` (OOS trades vs 30) ·
`expectancy` (bps vs 25) · `folds` (positive fraction) · `drawdown` (headroom
under the cap) · `stability` (profit factor vs 1.5), weighted 35/20/20/10/10/5.

This runs live: the first graded assessment scored **0.525** and moved the real
budget from 2.5% → **3.125% per order** and 10% → **12.5% per day**, with
3.22%/order queued as the next step toward the 5%/20% ceiling.

`max_session_policy` is the same idea applied to trading hours: the agent may
widen one step per assessment toward that bound (and never narrow below
`session_policy`), but only above 0.75 confidence. With `session_policy = "any"`
the bound is already the widest, so that lever is inert today.

### Confidence in the order table

Every row in **Market & order table** shows the two confidences that produced it:

- **ai x.xx** — the model's own confidence in that specific order, coloured by
  whether it allowed or vetoed. Sources from the `advisor` journal record.
- **ev x.xx** — the system-wide evidence grade in force when the decision was
  taken, i.e. the number the risk budget was sized from.

They answer different questions ("does this order look sane" vs "does the edge
look real") and are deliberately not averaged. A row rejected mechanically —
`max_open_positions`, `over_daily_notional`, `kill_switch` — still shows both, so
the table explains *why* it was refused, not just that it was.

### What the LLM is actually allowed to do

Every model capability here is bounded to **reducing** risk. A model cannot be
backtested the way a genome can, so it never creates a trade, never sizes one,
never extends a hold, and never touches RiskGuard. Within that line it now does
three jobs:

| job | direction | where it runs |
|---|---|---|
| entry veto (`advisor`) | may refuse an entry | order path, one call per entry |
| regime gate (`regime`) | may block entries in chop/panic | off-path worker, cached 15 min |
| hold opinion | recorded only, never acted on | order path, same call as the veto |

**Grounding.** The advisor used to be shown a price and a quantity, which makes
a veto a coin flip dressed as judgment. Both prompts now carry locally computed
tape features — `return_1bar/5bar/20bar_pct`, `realized_vol_per_bar_pct`,
`trend_slope_20bar_pct`, `below_recent_high_pct`, `range_position_0low_1high`,
and the live `spread_bps` — from the same bars the backtests use. Its reasons
changed from "no trap signals, small size" to "counter-trend long bounce within
a 20-bar downtrend (slope -4.55%, 20-bar return -9.80%); high overnight
volatility raises trap risk", which is the difference between a vibe and a
judgment.

**The regime gate** classifies each whitelisted symbol as `trend_up`,
`trend_down`, `chop` or `panic`. Only `chop`/`panic` at ≥0.60 confidence do
anything, and all they do is refuse entries (`regime_block` in the journal, and
the row shows up in the order table). A `trend_up` never creates a trade: the
mechanical strategy still has to signal, and RiskGuard still has to allow.

Two properties make it safe to leave running:

- **Never on the critical path.** Classifications are cached per symbol and
  refreshed by a background worker (`regime_refresh_seconds`, default 180s, two
  symbols per pass). An entry reads the cache and never waits on a model call.
- **Failure means "no opinion".** A missing key, a timeout, or unparseable JSON
  allows trading exactly as before, and journals `regime_failed`. The gate can
  only ever subtract.

Retired on purpose, and still refused: letting the model search for strategies
(that is what the evolution engine and the search-corrected significance bar are
for — a model generating hypotheses would silently multiply the comparisons and
manufacture false positives), and letting it extend a hold past a mechanical
exit.

### What survives a restart

The daemon is expected to be killed and restarted (`Restart=always`, reboots,
deploys). Everything it has learned lives on disk, and a restart is checked
against that promise rather than assumed:

| state | file | survives |
|---|---|---|
| stage, streak, last assessment | `state_dir/promotion.json` | yes |
| risk budget, confidence, session policy | `state_dir/effective_limits.json` | yes |
| kill switch, day P&L, daily notional | `state_dir/risk_guard.json` | yes |
| shadow book (rebuilt from today's journal) | `journal_dir/<date>.jsonl` | yes |
| orders used today (counted from the journal) | `journal_dir/<date>.jsonl` | yes |
| LLM regime classifications | `state_dir/regimes.json` | yes — otherwise the gate goes deaf until the worker catches up |
| arming state, autonomy state | `state_dir/live_gate.json` | yes |
| per-worker roster for the console | `state_dir/agents.json` | rewritten on start |

State files are written atomically (`jsonio.write_text`), because a torn read of
`effective_limits.json` used to parse as "no limits stored" and silently restore
the full ceiling. A corrupt or unreadable file is treated as "no opinion", never
as a reason to crash.

Verified by restarting the live daemon mid-session: stage, streak, mode, budget,
confidence, daily notional, equity, kill switch, accepted-order count and the
journal were all intact, and two regime classifications came back from disk.

### Alerts, and the roster on the console

Unattended means the events that change the risk posture have to reach you
instead of waiting to be noticed on a dashboard tab. `notify.py` alerts on:

| event | urgency | cooldown |
|---|---|---|
| `promotion` (stage changed) | normal | none |
| `promotion_requires_consent` | normal | none |
| `live_gate_blocked` (ready, but not armed) | normal | 1 hour per symbol |
| `kill_switch` tripped | critical | none |
| `demotion` | critical | none |
| `placed` (a real order went out) | normal | none |

Channels: **desktop** (`notify-send`, on by default when present) and
**Telegram** (`AGENTIC_TELEGRAM_BOT_TOKEN` + `AGENTIC_TELEGRAM_CHAT_ID`). Disable
with `AGENTIC_NOTIFY=0`. A failing channel is counted, never fatal: the alert is
swallowed and the loop keeps trading. Kill-switch trips were not even journaled
before this, so nothing could have alerted on them — that gap is closed and the
trip now appears in the journal and the stream.

The console has an **Agents on duty** panel fed by `state_dir/agents.json`, so it
shows the actual workers rather than a drawing of them: `strategy`, `advisor`
(calls/errors), `regime` (views, worker live), `risk guard` (the caps in force),
`evolution` (stage), `notifier` (channels, sent). Recent alerts appear beneath it.

### The evidence has to be able to change

`ev` in the order table is the system-wide evidence grade. Seeing it sit at the
same value across hundreds of rejections is correct — but it exposed a real bug:
nothing was refreshing the bar files, and the search is deterministic, so every
hourly evaluation re-ran the same search over the same frozen data and returned
the same number forever. The risk budget could never move.

Two fixes:

- **History refresh** (`history_refresh_hours`, default 24): the daemon
  re-fetches daily bars — equities through the broker's historicals tool, crypto
  through Coinbase's public candles (Robinhood has no crypto historicals tool at
  all). Fetches are **merged** on bar start time, so a request that returns only
  the recent window extends history instead of truncating it.
- **Skip the search when nothing changed**: re-running a deterministic search
  over identical files cannot produce different evidence, so the daemon
  fingerprints the bar files it would read and journals
  `evaluation_skipped: history_unchanged` instead of burning minutes of CPU. That
  event is the honest answer to "why is confidence not moving".

It also fixes what the search was grading. `run_evolution` used to pool **every**
bar file on disk — 30 symbols — while the agent may only trade the 7 in its
whitelist. Grading an edge on instruments you cannot trade is how a gate promotes
a strategy that cannot run, so the evaluation universe is now the whitelist
(`evolution.json` records it, and the console shows it), falling back to all
files only if none of the whitelist has history.

First run under the honest universe: confidence moved `0.5252 → 0.523`, out-of-
sample trades `23 → 29`, expectancy `125 → 78 bps`, drawdown `7.97% → 13.79%`,
bootstrap p `0.053 → 0.178` — and the risk budget followed it *down*
(`confidence_down`). The old number was partly flattery from symbols the agent
cannot trade.

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

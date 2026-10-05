# The agent swarm: design

Date: 2026-10-05. Roadmap item 3 (desk → fast lane → swarm). The user delegated the design ("you decide and move forward"), so each choice below is recorded as a ruling with its cost if it is wrong. The written spec is still reviewed by the user before any plan or code.

## Intent

The user wants agents that spawn, compete, die and switch strategy, a swarm that is fascinating to watch. They are profit-first: success is beating buy-and-hold, and money follows results only.

**Evidence that shapes this design:**
- **The fast lane lost to costs three times:** the 1-minute replay, the live paper run, and the slower-bars test (PR #9). In that test the best timeframe's gain was noise (t ≈ 0.3) and failed its pre-registered confirmation.
- **Daily rules are where this repo has evidence:** trend_crypto's walk-forward was +9.6% in 2026, and the momentum-rotation trial runs on daily bars.
- **Selection bias:** promoting the best of N noisy agents promotes luck. With 20 agents, Bonferroni needs t ≈ 2.81. At 20 forward days that means a daily Sharpe of 0.63, about 10 annualised; even after a year it still means about 2.8. No single agent can honestly graduate on any useful timescale.
- **Speed:** `walkforward.simulate()` takes 20–25 s per recipe over 3¾ years of the 43-symbol universe.

## Rulings

| # | Ruling | Cost if wrong |
|---|---|---|
| R1 | **Daily bars, the trader's universe:** the effective whitelist's `data/bars` files (19 symbols on 2026-10-05, scout additions included), with no intraday data. *Amended at review: the swarm's book may hold only what the account could trade if it were funded.* | The swarm can't act within a day. Intraday edges were ruled out by the evidence above. |
| R2 | **The swarm is one desk member, `swarm`.** Its book is a blend of its living agents, weighted only by each agent's past forward results. Agents never join the desk one by one. | One blended series is judged instead of many lottery tickets. A brilliant single agent is diluted by the blend. |
| R3 | *Amended 2026-10-05: funded at the operator's request. `UNFUNDED` is `{"switchboard"}`; the desk follows the swarm's book daily, keeps the last good copy on a bad read (and freezes targets if a funded book was never read), re-sizes only a funded member's own symbols, and lends a book stale for over 6 days to the benchmark until it is fresh. The swarm earns capital only through the allocator's evidence rules.* Originally: **Judged but not funded.** `UNFUNDED` becomes `{"switchboard", "swarm"}`. Funding is a later user decision and a one-constant code change, because netting already turns member book weights into account targets. | It earns nothing real until the user lifts it. |
| R4 | **The switchboard leaves `desk_members` when the swarm joins.** That keeps the competing members at 3 (`min_t` 1.23, not 1.39). The switchboard keeps running in the venues service, and its card stays. | The switchboard loses its desk record. It was retired as having no edge. |
| R5 | **A recipe is data, never code.** It is a family (`trend`, `rotation` or `reversal`), that family's parameters inside fixed ranges, a universe slice and a sizing choice, validated against a schema. | The swarm can't invent a new kind of rule; people add families. |
| R6 | **Three sources of new recipes, all counted as trials:** mutation and crossover of living agents, random immigrants, and an optional LLM scout (off by default, at most 3 proposals a week). | The LLM may propose nothing useful. It is a source of variety and story, never a source of authority. |
| R7 | **The birth screen is an in-sample filter only.** A recipe must make money after costs on history before its birth, with at least 20 trades and a drawdown under 30%. It must not be a near-duplicate of a living agent (daily return correlation ≤ 0.9). The screen never justifies anything; only forward results count. | A good recipe that looked bad in the past is never born. |
| R8 | **Forward life uses one engine.** A recipe is frozen at birth. Its record is `simulate()` over bars strictly after its birth time, with the same costs as the screen. Every step reruns each agent from birth to now. | Reruns cost time. The windows are short, and parity between screen and life comes free. |
| R9 | **The benchmark is the desk's, 60% QQQ / 40% BTC.** When no agent qualifies, the swarm's book holds the benchmark, so its excess comes only from agents. | An empty swarm looks flat instead of losing; that is the point. |
| R10 | **It runs as a separate daily job:** `agentic-trading swarm step` from a systemd user timer at 00:30 UTC, with `Persistent=true` so a missed run catches up at boot. A lock file stops two steps overlapping. | It is one more service. The trader and venues processes are untouched by swarm failures. |
| R11 | **Every recipe ever screened is counted.** The trial ledger appears wherever a swarm number appears. | None: honesty. |

## Lifecycle

```
recipe ──screen (history before birth)──▶ nursery (forward, < 20 days, weight 0)
   ▲                                          │ ≥ 20 forward days
   │ mutate / cross / immigrate / LLM scout   ▼
   └──────────── living agents ◀──── contributing (weight by trailing forward excess)
                                              │ cull rule
                                              ▼
                                            dead (kept in the lineage)
```

- **Nursery:** newborn agents trade forward on paper with weight 0 until they have 20 forward daily samples. This is the desk's own sample rule.
- **Contributing:** an agent with at least 20 forward days and a positive trailing 60-day forward excess return gets an equal-risk share of the swarm's book. Shares are recomputed once a day.
- **Cull:**
  - any agent whose forward drawdown passes 25% dies at once;
  - after 60 forward days, the bottom quarter of agents by forward excess die at each step.
  - The dead keep their lineage record.
- **Population:** at most 24 living agents and at most 12 screens a day (about 5 minutes). Births fill the free slots: half from mutation or crossover of contributing agents (random immigrants when there are none), a quarter random immigrants, and LLM proposals when the scout is on and has budget.

## Components (`src/agentic_trading/swarm/`)

| Module | Responsibility |
|---|---|
| `recipe.py` | `Recipe` (frozen dataclass): `family`, `params`, `universe` (`crypto`, `equity` or `all`), `sizing` (`inverse_vol` and `per_order_pct`), `id` (a content hash) and `parents`. `validate(raw)` refuses unknown keys and out-of-range values. Each family's parameter ranges are fixed in code. |
| `families.py` | Maps a recipe to `simulate(..., rule=…)` keyword arguments. `rank_rotation` and `rank_reversal` gain optional parameters (rotation lookback, regime moving average, top-N) whose defaults are today's constants, so momentum_rotation and dip_reversal behave exactly as before. |
| `breed.py` | `mutate`, `crossover` and `immigrant` on recipes, seeded from a recorded RNG state so a step is reproducible. |
| `screen.py` | The birth screen (R7) on bars before the birth time, plus the near-duplicate check against living agents' forward daily returns (or screen returns while a living agent has fewer than 20 forward days). |
| `life.py` | The forward record (R8): equity curve, daily returns, excess versus the benchmark, drawdown, and today's target weights (`targets_as_of`, or the family's equivalent). |
| `blend.py` | The swarm's target weights: equal-risk shares of contributing agents' targets, or the benchmark when there are none (R9). |
| `book.py` | Keeps the `swarm` member book (`data/state/desk/swarm.json`). It rebalances toward the blend once a day at the latest daily close, with the desk's cost model. The desk reads it as a `ReadOnlyMember`, exactly as it reads the switchboard. |
| `scout.py` | The optional LLM scout (R6). It sends the schema, the living agents' summaries and the trial count. It accepts only JSON that validates as a `Recipe`, with a one-sentence rationale, and spends at most 3 proposals a week. It uses the existing `llm.client` (the key comes from `AGENTIC_LLM_API_KEY`, as for the advisor), and tests use the Fake client. |
| `store.py` | Atomic JSON state in `data/state/swarm/`: `population.json` (living agents and their birth times), `lineage.json` (every agent ever born, its parents and how it died), `ledger.json` (the trial count and the RNG state), `scout.json` (proposals spent this week). A corrupt file is moved aside, as in the fast engine. |
| `step.py` | One daily step: lock; check bar freshness; update forward records; cull; breed and screen into free slots; blend; rebalance the book; save; journal `swarm-<date>.jsonl`; write `data/state/swarm.json` for the dashboard. |
| `cli.py` | `agentic-trading swarm step | status`. |

## Data flow

1. The trader daemon's `history_sync` keeps `data/bars` current (crypto from Coinbase; equities from the broker). The swarm only reads bars.
2. If the newest bar of the benchmark symbols is more than 4 days old, the step journals `swarm_stale_bars` and does nothing else. It never trades on stale prices.
3. The desk, in the trader process, reads `swarm.json` as a read-only member, takes its daily samples and records `would_earn` at the weekly allocation, with weight 0 (R3).

## Configuration (`[swarm]` in `agentic.toml`, off by default)

```toml
[swarm]
enabled = true
max_agents = 24
screens_per_day = 12
nursery_days = 20
cull_after_days = 60
max_drawdown = "0.25"
llm_scout = false        # optional; needs AGENTIC_LLM_API_KEY
llm_proposals_per_week = 3
seed = 7
```

Unknown keys and out-of-range values are refused at startup, as in `[fast]`. Adding `"swarm"` to `desk_members` requires `[swarm] enabled = true`.

## Dashboard

- **`GET /api/swarm`:** a whitelisted projection of `data/state/swarm.json`.
- **The Swarm card (Strategies tab):**
  - **population grid:** one tile per living agent, coloured by forward excess. Each tile shows the family, a short name, its age in forward days and its state (nursery, contributing or dying today).
  - **births and deaths** as sentences, for example "rotation-3f2a was born from trend-91c0 × rotation-07de" and "trend-55b1 died: drawdown 26%";
  - the LLM scout's latest rationale, quoted;
  - the swarm's own line against the benchmark;
  - the trial count ("312 recipes tried").
- **Ticker:** `swarm_birth` and `swarm_death` events join the cockpit ticker.
- **Race:** the `swarm` line appears automatically from its book.

## Failure handling

| Failure | Behaviour |
|---|---|
| Stale bars (benchmark newest bar over 4 days old) | `swarm_stale_bars` is journaled and the step does nothing else |
| One agent's simulation raises | That agent is marked `errored` and given weight 0; the others carry on; the error is journaled once |
| LLM unreachable, slow (15 s timeout) or returns invalid JSON | No proposal; the failure is journaled and counted against the week's budget, and breeding fills the slot |
| Corrupt state file | Moved aside to `.corrupt-<stamp>`; a fresh population starts and the trial ledger keeps its last good count from `lineage.json` |
| Another step already running | The lock refuses with a plain message (exit 0) |
| Step crashes | systemd records it; the next timer run starts clean; the desk keeps the last book |

## Non-goals

- No generated code and no new rule families written by agents.
- No intraday trading and no live orders.
- No funding: lifting `UNFUNDED` is a separate user decision.
- No change to the allocator's rule beyond the `UNFUNDED` entry.
- No change to the trader's or venues' order paths.
- An LLM is never required.

## Testing

All tests use fakes, and the network tripwire stays on.

- **Recipe:** validation (unknown key, out-of-range value, unknown family); the id is stable for equal content.
- **Families:** the default parameters reproduce today's `rank_rotation` and `rank_reversal` selections exactly (a parity test).
- **Breed:** mutations and crossovers stay in range; the RNG state makes a step reproducible.
- **Screen:**
  - only bars before the birth time are used (a planted future spike changes nothing);
  - the near-duplicate check;
  - each threshold on both sides.
- **Life:** the forward record starts strictly after birth; the same bars give the same record.
- **Blend:** the nursery gets weight 0; trailing excess ≤ 0 gets weight 0; an empty swarm holds the benchmark; equal-risk shares.
- **Cull:** drawdown death; the bottom quarter after 60 days; the lineage keeps the dead.
- **Step:**
  - stale bars do nothing;
  - one erroring agent doesn't stop the step;
  - the lock;
  - a corrupt store is moved aside;
  - the trial ledger only grows.
- **Scout:** invalid JSON and invalid recipes are refused; the weekly budget; the Fake client only.
- **Desk:** `swarm` reads as a read-only member; it is unfunded with `would_earn` recorded; R4's member count.
- **Dashboard:** the `/api/swarm` shape and whitelist; the card's ids; the wording.

## Acceptance

1. `swarm step` on today's bars seeds a population, journals births with lineage, and writes `swarm.json`. A second run the same day changes nothing.
2. Over a simulated two weeks of later bars (tests), agents age, enter and leave the nursery, contribute and die by the rules. The trial ledger only grows.
3. The timer runs the step daily and catches up after the host has been off.
4. The desk's next weekly allocation lists `swarm` with weight 0 and a `would_earn` entry. The switchboard is no longer a desk member.
5. The Swarm card shows the population, births, deaths and the trial count, and the size sweep still passes.

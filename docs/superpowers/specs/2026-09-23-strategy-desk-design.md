# Strategy Desk + Quote Tape — Design

**Date:** 2026-09-23 · **Status:** approved in conversation, awaiting written-spec review
**Sub-project 1 of 3** in the roadmap: desk → fast lane → agent swarm. A dashboard redesign is deferred until after this sub-project.

## 1. Intent

The operator wants a trader that is intelligent, fast, and able to switch strategies on the fly, with helper agents when needed. The agreed success criterion is **profit first: beat buy-and-hold** (60% QQQ / 40% BTC, the benchmark the running rotation trial already uses). The order of work follows from what was measured on 2026-09-23:

- Fast intraday rules lost money on clean 5-minute data (e.g. 5-minute RSI dip: −3.6% to −5.9% in 32 sessions, where costs alone come to ~4%/month at 100 trades). The one-year intraday dataset `data/intraday/` is 43% interpolated flat bars and must not be used.
- The daemon sees fresh quotes about every 5.3 s (median), so a "fast lane" means holds measured in seconds-to-minutes, not HFT.
- So before anything can trade fast or be invented by agents, the system needs (a) an arena where strategies prove themselves on **live** results and win capital only by beating the benchmark, and (b) clean intraday data. This sub-project builds both.

**Capital:** undecided. Design for the current ~$50 and scale automatically with account equity. **Live trading is out of scope.** The desk runs in shadow; allocating real money is a later operator decision behind the existing `AGENTIC_ALLOW_LIVE` gate.

## 2. Architecture

The desk plugs into the existing runtime as **one strategy**, so the hardened path (risk guard, broker preview, kill switch, journal, shadow-exit fix) is untouched.

```
feed.poll() ─► QuoteTape (append, never blocks)
     │
     ▼
StrategyDesk.on_quote(quote)
  ├─ Member "momentum_rotation"  → MemberBook (paper, per-asset costs)
  ├─ Member "trend_crypto"       → MemberBook
  ├─ Member "benchmark"          → MemberBook (60% QQQ / 40% BTC, bought and held)
  ├─ Allocator (weekly, Monday 00:00 UTC) → allocations {member: weight}
  └─ Netter: target $/symbol = Σ alloc_m × weight_m(symbol) × equity
             → emits only the gap as OrderIntent(notional_usd=…)
     │
     ▼
existing runtime: guard → review → journal (shadow) — unchanged
```

### Units (one purpose each)

| Unit | File | Does | Depends on |
|---|---|---|---|
| `QuoteTape` | `tape.py` | Appends quotes to `data/tape/<SYMBOL>/<YYYY-MM-DD>.jsonl.gz` | filesystem only |
| `MemberBook` | `desk/book.py` | Paper account for one member: fills at ask/bid + `CostModel.for_symbol`, mark-to-market, daily equity series, persistence | `backtest.CostModel` |
| `Member` | `desk/member.py` | Wraps an existing strategy object; turns its intents into `MemberBook` fills; isolates exceptions | the strategy interface |
| `Allocator` | `desk/allocator.py` | Pure function: member records → allocations + reasons | nothing (pure) |
| `Netter` | `desk/netting.py` | Pure function: allocations, member weights, holdings, equity → intents | `types.OrderIntent` |
| `StrategyDesk` | `desk/desk.py` | Implements the runtime strategy interface; wires the above | all of the above |

`StrategyDesk` implements exactly the hooks the runtime calls today: `on_quote`, `note_fill`, `seed_positions`, `release_decision`, `last_decided_day`, `reload_history`. Config: `strategy = "desk"` with `desk_members = ["momentum_rotation", "trend_crypto", "benchmark"]`.

## 3. Behaviour

### 3.1 Member books
- Starting capital = account equity when the desk starts (currently $49.90); the same for every member.
- **Buys fill at the quote's ask and sells at its bid.** Costs come from `cost_model_for(state_dir).for_symbol(symbol)` (equities 2 bps/side; crypto $0.05/order as measured).
- A member's intents are converted to paper fills at the size its own strategy would trade: a per-order budget equal to the operator ceiling `max_order_pct` × the member's own book equity, with `weight` applied as the runtime does. A member never borrows: an entry that exceeds its cash is skipped and journaled.
- Every quote marks the book to market. One equity sample per UTC day is stored for scoring.
- State lives in `data/state/desk/<member>.json` and is restored on restart.

### 3.2 Allocator (weekly, Monday 00:00 UTC)
For each non-benchmark member, over its whole live record:

1. **Enough evidence:** ≥ 20 daily equity samples (UTC days) and at least one entry. Scoring uses daily marked-to-market returns, so an open position counts as evidence; a trade-count bar would lock out low-turnover members (the rotation trades roughly every 2–3 weeks).
2. **Beats doing nothing:** its cumulative return exceeds the benchmark member's over the same days.
3. **Consistent:** t-statistic of daily excess returns (member − benchmark) > 1.0.
4. **Weight** ∝ that t-statistic across qualifying members, **capped at 60%** per member; the remainder goes to the benchmark.
5. **Hysteresis:** if no member's allocation moves by ≥ 10 percentage points, the previous allocations stand.

With no qualifying member, 100% goes to the benchmark. Each run journals `desk_allocation` with every member's days, trades, excess return, t-statistic and the reason it was in or out.

### 3.3 Netting
- Targets are recomputed **only on events**: an allocation change, or a fill in any member's book. Price drift alone never triggers a trade; otherwise a crypto position drifting 5% would pay a rebalance charge several times a week.
- On an event: target_usd(symbol) = Σ_m alloc_m × (member m's position value in symbol / member m's equity) × account equity, valued at the event's prices.
- Emit an intent when |target_usd − held_usd| > max($1.00, 5% of target_usd): a buy with `notional_usd`, or a sell with an explicit `quantity` (capped at what is held). A symbol whose target is 0 is sold in full.
- A pending gap is re-emitted on later quotes until filled, like the rotation's exits today. Crypto gaps act on any quote. Equity gaps act only during the regular session (fractional orders need it; this mirrors the rotation strategy's existing session rule).
- Real/paper holdings for netting come from the runtime via `note_fill` and `seed_positions`, exactly as the rotation strategy's do today.

### 3.4 Quote tape
- One gzip-appended JSON line per polled quote: `{symbol, bid, ask, quote_at, observed_at}`. A new file each UTC day. Roughly 5 MB/day compressed.
- Hooked immediately after `feed.poll()` in `runtime.py`. Any write error is caught, journaled at most once per hour as `tape_write_failed`, and never stalls or kills the loop.
- Controlled by `tape_enabled` (default `true`) and runs under every strategy, not only the desk.

## 4. Failure handling

| Situation | Behaviour |
|---|---|
| A member's strategy raises | Member disabled for the session, `desk_member_failed` journaled, allocation → 0, the desk continues |
| No quote for a symbol | Members holding it keep holding; no fills are invented |
| Corrupt or missing member state | That member restarts from starting capital, `desk_member_reset` journaled; other members are unaffected |
| No qualifying member | 100% benchmark (profit-first default) |
| Tape write fails | Journaled (rate-limited); trading continues |
| Kill switch, guard rejection, stale quote | Unchanged existing runtime behaviour: the desk only produces intents |

## 5. Rollout

1. **Quote tape** ships first. It is independent and starts banking clean data for sub-project 2 immediately.
2. **Desk in shadow, now** (operator's choice), with members rotation, trend and benchmark. The paper account holds the benchmark until a member qualifies: the first Monday allocation after a member has 20 daily samples (earliest ~2026-10-19 if the desk starts by 2026-09-28).
3. **The rotation trial continues** as the rotation member's scorecard: `agentic-trading trial` reads that member's book once the desk is running. The verdict date stays 2026-10-23.
4. **Rollback:** set `strategy = "momentum_rotation"` and restart. Member books remain on disk.

## 6. Testing

- **Allocator** (pure): 19 vs 20 daily samples, zero entries → ineligible, t = 0.99 vs 1.01, weights ∝ t, 60% cap with the remainder to the benchmark, hysteresis at 9.9 vs 10 points, all-fail → 100% benchmark.
- **MemberBook:** fills at ask/bid, per-class costs, no borrowing, mark-to-market, a daily sample per UTC day, persistence round trip.
- **Netting:** two members' MSFT produce one intent; the max($1, 5%) threshold; a price move with no event produces no intent; equities outside the regular session produce no intent; a sell never exceeds holdings.
- **Isolation:** one raising member does not stop the others.
- **QuoteTape:** append, daily rotation, gzip readable, an `OSError` is swallowed and journaled.
- **Integration:** the desk inside `run_daemon` in shadow with a stub feed: net intents pass the guard; restart restores books and allocations.

## 7. Out of scope (later sub-projects)

- Fast-lane loop and intraday strategies (sub-project 2, after about 3+ weeks of tape).
- Agent swarm generating and submitting members (sub-project 3; members will be registered through the same `desk_members` mechanism).
- Dashboard redesign (after the desk; must show member scorecards, allocations over time and the benchmark race).
- Live capital allocation.

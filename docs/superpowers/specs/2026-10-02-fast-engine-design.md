# The fast engine (switchboard): design

Date: 2026-10-02. Roadmap item 2b. 2a (venues) streams live prices from Alpaca and Coinbase. 2b trades them on paper. The live order path comes later, once a strategy earns it. Item 3 is the agent swarm.

## Intent

The user wants strategies that act within seconds and switch on the fly. They are profit-first and not afraid of risk, and they want to be fascinated. They have Alpaca live and Coinbase keys, and no Alpaca paper account. Real money moves only after a strategy earns it and the user arms a venue.

**Decided with the user:**
- **Paper first, live later.** This round builds the engine, its paper book, the desk's judging, replay and the dashboard. Routing a winner's orders to a live venue is a later sub-project.
- **One switchboard member.** A single desk member holds several playbooks and switches between them in seconds as it reads each coin's market.
- **Crypto only:** BTC/USD, ETH/USD and SOL/USD, 24/7. Stocks stay with the existing desk members.
- **Two cost views.** The desk judges the switchboard's book at Alpaca crypto costs. A comparison book replays the same trades at Coinbase costs and is shown on the dashboard. It is never judged.
- **Architecture A.** The switchboard runs inside the venues service as a `TickBus` subscriber. The desk, in the trader process, reads its book as a read-only member.

**Success:**
- The venues service runs the switchboard on live Alpaca crypto prices.
- It switches playbooks per coin as the market reading changes.
- It fills honestly on paper and journals every decision in plain words.
- The desk judges it weekly and records what it would have earned, while it holds no capital.
- The dashboard shows it acting live.
- A replay over recorded prices or ~90 days of 1-minute bars reports how it would have done at both venues' costs.

## The economics this design accepts

Fees decide outcomes at this account size (~$50). A round trip costs about 0.5% at Alpaca crypto (0.25% taker per side) and about 1.2% at Coinbase (0.6%). Alpaca stocks are commission-free, but a live account under $25k may make only 3 day trades per 5 days.

So "fast" here means **reacting within seconds and holding for minutes to hours**, not scalping. Standing aside in chop and a cost gate are first-class rules.

## Non-goals

- No live or real orders. The switchboard never calls `Venue.submit`.
- No change to the Robinhood daemon's order path, RiskGuard, netting or the allocator's rule.
- No stocks, no shorting, no leverage.
- No live parameter tuning. Parameters are fixed in config.
- No paid data.

## Components (`src/agentic_trading/fast/`)

| Module | Responsibility |
|---|---|
| `bars.py` | `MinuteBars`: builds 1-minute OHLC bars per symbol from tick mids, keyed on exchange time. Reports a completed bar when the minute rolls. Keeps the last 24 h per symbol (1440 bars). |
| `regime.py` | `read_regime(bars) -> Regime`, run on each completed bar. `trending` when the efficiency ratio over 30 bars is ≥ 0.35. `squeeze` when the 30-bar realized volatility is in the bottom 20% of the last 24 h (needs ≥ 240 bars of history). `choppy` when the efficiency ratio is ≤ 0.20. Otherwise `unclear`. A coin with fewer than 60 bars is `warming`. Precedence when more than one applies: `squeeze`, then `trending`, then `choppy`. |
| `playbooks.py` | Three pure playbooks. Each one's `entry(bars, price) -> Optional[Plan]` returns `Plan(playbook, stop, target, expected_move)`, and its `exit(position, bars, price) -> Optional[reason]` closes the trade. **Breakout** (trending): price crosses above the 30-bar high; the trailing stop is 1.5 × ATR(30); no fixed target; expected move is 2 × ATR. **Pullback** (trending, price above the 60-bar EMA): price has touched the 20-bar EMA and turns up (the tick price is above the last bar's high); target is the 60-bar high; stop is the dip low minus 0.25 × ATR. **Squeeze break** (squeeze): price crosses above the 60-bar range high; stop is the range midpoint; expected move is the range height. |
| `switchboard.py` | `Switchboard.on_tick(tick, now) -> list[Event]`. For each coin it reads the regime on bar close and picks the playbook for that regime (`choppy`, `unclear` and `warming` stand aside). It checks stops and exits on every price. Rules: the **cost gate** (enter only if `expected_move ≥ 3 × round-trip cost` at Alpaca); an open trade stays with the playbook that opened it until it exits; one position per coin and at most 3; each position is at most 1/3 of book equity; a 5-minute cooldown per coin after an exit; a **daily loss stop** (book down 3% since the UTC day's first equity means no new entries until the next UTC day); a **stale feed** (no Alpaca crypto price for 30 s) means no new entries. It holds pending fills for the fill delay. |
| `fills.py` | `PaperFiller`: a decision fills at the first quote whose `received_at` is at least `fill_delay` (250 ms) after the decision. Buys fill at the ask, sells at the bid, and the configured per-side fee is charged. It drives a standard `MemberBook` through `buy`/`sell` with a fee-only cost model (`per_side_bps` = the fee, `fee_per_order` = 0). |
| `mirror.py` | `CoinbaseMirror`: copies every Alpaca-book fill into the non-competing `switchboard@coinbase` book. It uses the Coinbase bid/ask at the same moment and the Coinbase fee. A mirror trade is **unpriced**, counted and skipped, when the Coinbase quote is missing or older than 5 s. |
| `store.py` | Atomic save and load of the book plus engine state (open trades with playbook, entry, stop, target and their timestamps; cooldowns; the day-start equity). It writes `data/state/desk/switchboard.json` (the member book), `data/state/fast/engine.json` and `data/state/fast/mirror.json`. It saves on every fill, every 10 s and on shutdown. A corrupt file is moved aside to `.corrupt-<UTC stamp>` and a fresh book starts; it is never a crash. |
| `service.py` | `run_fast(bus_queue, …)`: the TickBus subscriber inside `run_venues`. It catches any exception from the switchboard per tick; on the first one it marks the engine `failed` in health, saves, and stops consuming, while the price feeds and recorder keep running. It writes `data/state/fast.json` once a second (regimes, active playbooks, open trades with live P&L, the last 20 events, both books' equity and the unpriced count). It journals events to `data/journal/fast-<YYYY-MM-DD>.jsonl`. |
| `replay.py` | `replay(source, config) -> Report` runs the same `Switchboard`, `PaperFiller` and `CoinbaseMirror` with a simulated clock. **Recorded** source: `data/stream/alpaca/<SYM>/<day>.jsonl.gz` and `data/stream/coinbase/<SYM>/<day>.jsonl.gz`, merged in `received_at` order. **Bars** source: Alpaca historical 1-minute crypto bars (cached under `data/fastbars/`), each turned into four ticks (open, then high and low in the order the bar's direction implies, then close) at the median recorded spread. Reports from the bars source are labelled approximate. |
| `costs.py` | `VenueCosts(per_side_fee)` for `alpaca_crypto` (default 0.0025) and `coinbase` (default 0.006), from config. A fee outside 0–0.02 is refused at startup. |

### Desk side (trader process)

- **`ReadOnlyMember`** (`desk/member.py`) has the `Member` interface. It returns no intents from `on_quote` and reloads its `MemberBook` from `switchboard.json` before every allocation read. Its book's `save()` is a no-op, so `StrategyDesk._save` cannot overwrite the venues service's file. A missing or unreadable file means 0 samples, reported once to the journal.
- **`UNFUNDED = frozenset({"switchboard"})`** (`desk/allocator.py`). After `allocate()`, each unfunded member's weight is moved to the benchmark. The `desk_allocation` event gains `"would_earn": {name: weight}` and keeps the allocator's reasons. Lifting this is a code change in the later live sub-project; there is no config switch.
- `config.DESK_MEMBER_CHOICES` gains `"switchboard"`. The user adds it to `desk_members`. It counts as a competing member, so `min_t` for 3 competing members (≈1.2) applies to all of them.

## Configuration (`agentic.toml`, new `[fast]` table, off by default)

```toml
[fast]
enabled = true                 # run the switchboard inside the venues service
symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
alpaca_fee = "0.0025"          # per side, taker
coinbase_fee = "0.006"         # per side, taker (comparison book only)
fill_delay_ms = 250
cost_gate_multiple = 3
max_positions = 3
cooldown_minutes = 5
daily_loss_stop = "0.03"
```

The book's starting equity is the `starting_equity` of the existing member books (read from `data/state/desk/account.json` at first start), so the race is fair.

## CLI

- `agentic-trading fast replay --config … --source recorded|bars --from YYYY-MM-DD --to YYYY-MM-DD`
- `agentic-trading fast status --config …` prints `fast.json` in plain words.

## Dashboard

- **`GET /api/fast`** is a whitelisted projection of `data/state/fast.json`.
- **Switchboard card (Strategies tab):**
  - for each coin: a regime chip, the active playbook, and the open trade (entry, stop, live P&L), or "standing aside";
  - the last 10 decisions in plain English;
  - an Alpaca-vs-Coinbase comparison ("the same trades: +X% at Alpaca, −Y% at Coinbase, N unpriced");
  - the badge "judged only — not yet funded · would earn W%" (from the latest `desk_allocation`).
- **Ticker:** `fast_entry` and `fast_exit` events join the cockpit ticker as sentences.
- **Race:** the switchboard line appears automatically from its book.

## Failure handling

| Failure | Behaviour |
|---|---|
| Exception in the switchboard | Engine marked `failed` in health with a redacted reason; book saved; feeds and recorder continue |
| Alpaca crypto feed quiet more than 30 s | No new entries; stops are checked on the next price |
| Coinbase quote missing or older than 5 s | The mirror trade is counted unpriced |
| Corrupt engine or book file | Moved aside to `.corrupt-<stamp>`; a fresh start is journaled |
| Venues service down over UTC midnight | No sample that day; nothing is back-filled |
| Fee outside 0–0.02, or fast enabled without crypto symbols | Refused at startup with a plain message |

## Testing

All tests use fakes; the network tripwire stays on.

- **Bars and regime:** minute rollover; each regime from scripted paths; warming; precedence.
- **Playbooks:** each entry, exit and stop; the trailing stop only ratchets up.
- **Switchboard:**
  - stand-aside regimes;
  - the cost gate on both sides of the boundary;
  - an open trade stays with its playbook across a regime change;
  - the position cap and the 1/3 sizing;
  - cooldown;
  - the daily loss stop resets at UTC midnight;
  - a stale feed blocks entries.
- **Fills:**
  - the 250 ms delay picks the right quote;
  - buys pay the ask and sells get the bid, plus the fee;
  - the $1 minimum.
- **Mirror:** the Coinbase price and fee; the unpriced count.
- **Store:** a round trip with an open trade; the corrupt file is moved aside.
- **Service:** an exception isolates the engine while the recorder still writes; `fast.json` shape; journal lines.
- **Desk:**
  - `ReadOnlyMember` never writes;
  - an unfunded member's weight goes to the benchmark and `would_earn` is recorded;
  - `min_t` counts the switchboard.
- **Replay:**
  - deterministic;
  - **live/replay parity**, the same decisions for the same tick sequence;
  - bar-to-tick ordering;
  - the approximate label.
- **Dashboard:** `/api/fast` shape and whitelist; the card's ids on the Strategies tab; the wording.

## Acceptance

1. With `[fast] enabled = true` and `"switchboard"` in `desk_members`, the venues service runs the switchboard for 30 minutes. `fast.json` updates every second with a regime per coin, the journal shows regime changes, and any entries and exits carry plain reasons.
2. A forced exception in a test build marks the engine failed while the crypto streams stay live.
3. `fast replay --source bars` over ~90 days prints per-playbook results at both venues' costs, against holding the same coins.
4. The desk's next weekly allocation includes the switchboard's record and a `would_earn` entry, and its weight is 0.
5. The Switchboard card and the race line show on the dashboard, and the size sweep still passes.

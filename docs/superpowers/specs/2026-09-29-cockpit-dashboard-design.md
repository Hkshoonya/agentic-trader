# Cockpit dashboard — design

Date: 2026-09-29 · Roadmap item 1 of 3 (dashboard → fast lane → agent swarm)

## Intent

The operator console (loopback `:8787`) is, in the user's words, "boring as hell,
nothing easy to follow"; they want "graphs and animations more and better UI and
UX" and to be fascinated. **Success:** opening the page answers three questions
in about five seconds — *who is winning, where is the money, what did the bot
just do* — and it is enjoyable to watch.

Decided with the user:

- **Desktop browser first.** A phone still works; the layout is tuned for a wide screen.
- **Direction: a mix of three ideas.** From the race, an animated race chart. From mission control, tiles, a donut and a live ticker. From the story cards, plain-English sentences.
- **Layout: the cockpit.** The overview fits on one screen with no scrolling: story cards on the left, the race in the centre, tiles on the right and a ticker along the bottom. Deeper pages live in tabs.
- **Build: hand-built SVG with CSS animation.** No chart library, no CDN, no build step. The Windows app keeps working offline, and the existing CSP (`script-src 'self' 'unsafe-inline'`) holds.

## Non-goals

- New trading behaviour. The dashboard only reads state. The arm/disarm and kill-switch controls keep their current semantics and confirmation.
- A new server, framework or toolchain. `dashboard.py`'s HTTP server stays.
- A phone-specific layout, beyond staying usable with no horizontal page scroll.

## The page

### Top bar (always visible)

- **Mode badge:** SHADOW, or LIVE with a glow.
- **Stage.**
- **Health dot:** pulses. Green when healthy, amber on warnings, red on failures.
- **Kill-switch badge:** shown only when the switch is set.
- **Arm control:** unchanged.
- **Tabs:** Overview · Strategies · Orders · Health.

### Overview tab (the cockpit)

The overview fits a 1440×900 viewport without scrolling. It uses a CSS grid of three columns, `1fr 2.2fr 1fr`, with the ticker row beneath.

- **Left: three story cards.**
  - Each card carries a coloured left edge, a small label and one or two sentences.
  - Sentences come from the server: `story.right_now`, `story.money` and `story.just_now`.
  - When a sentence changes, it crossfades over 300 ms.
- **Centre: the race.**
  - The chart shows each strategy's return, in % from its starting equity, one point per daily sample plus a live "now" point.
  - The benchmark is a dashed grey line. The zero line is a faint dashed rule.
  - Each line ends in its name and value. The leader's end dot pulses.
  - Above the chart: "Day N of 30 · verdict <date>".
  - Hovering a line highlights it and shows that member's holdings, as symbol and dollar value, in a tooltip.
  - On first load the lines draw in over 1.2 s.
  - On each refresh the y-scale eases to its new range, and the "now" points glide there in 600 ms.
  - With fewer than two points, the chart shows the points it has and a note: "the race fills in one point per day".
- **Right: four tiles.**
  - **Account value**, with a sparkline of the last 30 days of account equity.
  - **Trial:** days left, with a progress ring.
  - **Money ring:** a donut of the current allocation by member. Hovering shows each member's name and weight. When everything sits in the benchmark, the ring shows the benchmark's 60/40 QQQ/BTC legs.
  - **Next allocation:** a countdown to Monday 00:00 UTC.
  - Numbers tween to their new values over 600 ms. Rings sweep.
- **Bottom: the live ticker.**
  - A continuously scrolling strip of the latest ~30 events, in plain English.
  - Events that arrived since the last refresh flash once.
  - The scroll pauses on hover.

### Strategies tab

- One card per member. Each shows:
  - its return versus the benchmark;
  - holdings;
  - entries and exits;
  - qualification progress: daily samples as `n/20` with a bar, t against the bar it must clear (`min_t` for the current member count), and the allocator's reason text;
  - its current weight.
- The existing **Candidates**, **Symbol scout**, **Walk-forward evidence**, **Promotion gate**, **What sizing up costs** and **Evolution / Proposed changes** cards move here, restyled but with the same content.

### Orders tab

The existing cards move here, restyled but with the same content:

- **Market & order table**
- **Decisions per day**
- **Live execution stream**
- **Daily notional used**
- **Checked / sent / refused**

### Health tab

The existing cards move here, restyled but with the same content:

- **Agents on duty**
- health checks
- regimes
- **Runtime & P&L**
- **Promotion streak**
- **Order submission** (the arm control lives in the top bar)

## Data: `GET /api/desk`

This endpoint is new, built by `dashboard_desk.py` (`build_desk_view(config, *, now)`). The response:

```json
{
  "enabled": true,
  "as_of": "2026-09-29T22:14:00+00:00",
  "members": [
    {
      "name": "momentum_rotation",
      "label": "Momentum rotation",
      "is_benchmark": false,
      "series": [["2026-09-25", 1.07], ["2026-09-27", 1.09]],
      "now_pct": 1.16,
      "value": 50.48,
      "holdings": [{"symbol": "AAPL", "value": 9.47}],
      "entries": 5,
      "exits": 0,
      "samples": 4,
      "samples_needed": 20,
      "t": null,
      "t_needed": 1.0,
      "weight": 0.0,
      "reason": "4 daily samples (needs 20)"
    }
  ],
  "allocation": {
    "week": "2026-09-28",
    "weights": {"benchmark": 1.0},
    "legs": {"QQQ": 0.6, "BTC-USD": 0.4},
    "history": [{"week": "2026-09-28", "weights": {"benchmark": 1.0}}],
    "next_at": "2026-10-05T00:00:00+00:00"
  },
  "trial": {"strategy": "momentum_rotation", "day": 6.0, "days": 30,
            "ends_at": "2026-10-23", "excess_pct": 1.94, "verdict": "running"},
  "story": {"right_now": "...", "money": "...", "just_now": "..."},
  "ticker": [{"at": "2026-09-29T22:10:00+00:00", "kind": "fill", "text": "..."}]
}
```

### Rules

- **Returns.** A return is `value / starting_equity − 1`, in percent, rounded to two decimals. Series points come from the member book's `samples`. The live `now_pct` comes from cash plus positions at the book's last prices.
- **Qualification fields.** `t`, `reason`, `samples` and `t_needed` come from calling the allocator's own `allocate(...)` on the member records. The dashboard never recomputes the rule; `t_needed` is `min_t(member count)`.
- **Allocation history.** It comes from the journal's `desk_allocation` events, newest 12 weeks.
- **Trial.** It comes from `trial.score_trial(config)`. It is null when no trial runs.
- **Ticker.** It holds the newest ~30 of these journal events:
  - `member_fill`
  - `accepted` with reason `desk_rebalance`
  - `desk_allocation`
  - `advisory_overruled`
  - `desk_member_failed`
  - `selfcheck` (a failing one only)
  - `kill_switch`
  
  Each becomes one plain-English sentence on the server.
- **Story sentences.** The server writes them from the same data:
  - **right_now:** the leader against the benchmark, or "no strategy is ahead of buy-and-hold yet".
  - **money:** where capital sits and why, e.g. "No strategy has earned capital yet (4 of 20 daily samples)".
  - **just_now:** the newest ticker item, or "Watching N prices; nothing to do" when the last event is only a cycle.
- **Not the desk.** When `config.strategy != "desk"` the response is `{"enabled": false, "strategy": ...}` with empty members. The cockpit still shows the story cards and the trial. The race says it follows the strategy desk and points to the Orders tab (no account-equity history exists to race).
- **Missing or corrupt state.** Each missing or unreadable piece (a member file, `desk.json`, the trial) degrades to empty or null in the response and in its sentence. The endpoint never returns 500 for bad state.

### Refresh

- The existing endpoints keep their 2 s refresh.
- `/api/desk` refreshes every 5 s.
- Candidates keep 30 s.
- All timers and `requestAnimationFrame` loops pause while `document.hidden`.

## Code layout

The 1,361-line `dashboard_js.py` string is split. Each file stays an inline Python string module, so PyInstaller and packaging do not change:

| Module | Purpose |
|---|---|
| `dashboard_css.py` | theme tokens, grid, tabs, cards, keyframes |
| `dashboard_charts_js.py` | pure SVG helpers: `scale`, `linePath`, `race`, `donut`, `ring`, `sparkline`, `tween` |
| `dashboard_cockpit_js.py` | Overview: fetch `/api/desk`, render story, race, tiles and ticker |
| `dashboard_js.py` | tab switcher, top bar, and today's cards (moved, restyled) |
| `dashboard_html.py` | assembles the page from the above |
| `dashboard_desk.py` | `build_desk_view` for `/api/desk` |

The chart helpers are pure functions from data to SVG path strings or numbers, so Node can test them without a DOM.

## Failure handling

- **Failed fetch.** The last good render stays on screen. The top bar shows an amber "reconnecting" dot and retries on the next tick. It never blanks.
- **Bad values.** The race or ring skips NaN or null points instead of drawing through them.
- **No data.** Empty states are sentences, not blank boxes.

## Testing

- **Python (`tests/test_dashboard_desk.py`):**
  - series and `now_pct` maths from temp member books;
  - qualification fields match `allocate(...)`;
  - allocation history from journal events;
  - each story-sentence branch;
  - ticker wording for each event kind;
  - the `enabled: false` fallback;
  - corrupt or missing files degrade instead of raising;
  - `/api/desk` served over HTTP.
- **Page:**
  - The assembled HTML contains every module.
  - It has no external `src` or `href` (except `data:`).
  - Every tab's cards keep their element ids, so the existing tests and wording checks still pass.
- **JavaScript (`tests/test_dashboard_charts.py`):** runs `node` on the chart helpers (scale domain, path for 0, 1 and n points, donut arcs summing to 360°, tween end value). Skipped when Node is absent.
- **Manual acceptance:**
  - On the live dashboard, via Playwright, at 1440×900, screenshot every tab.
  - Confirm the overview needs no scroll.
  - Confirm the animations run, with no console errors.

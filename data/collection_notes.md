# SPY quote collection notes

## Scope and source

These are observed market quotes for an educational paper experiment. The collector only called the read-only Robinhood MCP `get_equity_quotes` tool for SPY and a clock tool; it wrote local files. It did not place or cancel orders, transfer funds, change broker settings, or read private account information during quote collection.

The source describes its quote endpoint as real-time. Its response has no explicit delayed-data flag, so every record preserves `delayed: null` rather than asserting a verified delay status. Quotes are snapshots, not an exchange tick stream or executable orders.

## Collection sessions

| Session | Collection window, UTC | Observations | First observation, UTC | Last observation, UTC |
|---|---|---:|---|---|
| Initial collection | 2026-09-15T07:58:49Z to 2026-09-15T08:03:48Z | 13 | 2026-09-15T07:58:49Z | 2026-09-15T08:03:33Z |
| Resumed collection | 2026-09-15T08:08:39Z to 2026-09-15T08:11:39Z | 12 | 2026-09-15T08:08:40Z | 2026-09-15T08:11:25Z |

The initial window targeted five minutes including tool latency. Collector setup introduced a 120-second gap after its first observation; subsequent intervals were 14–16 seconds. This setup gap must not be interpreted as continuous market sampling. The resumed window lasted three minutes including tool latency and appended new records without deleting earlier observations. Requests targeted a 15-second cadence. The last request was not started near the window deadline to leave room for API latency.

The gap between collection sessions is intentional. A paper engine should expire any time-dependent state across missing observations or account explicitly for elapsed wall-clock time; the dataset does not show what happened during gaps.

## Timestamp and session interpretation

- `observed_at` is the clock reading taken after the quote tool returned; this clock reports whole seconds.
- `bid_at` and `ask_at` retain exact source timestamps, including subsecond precision.
- `quote_at` is the older of the bid and ask source timestamps. Freshness calculations should use this conservative timestamp.
- Quoted age statistics below are approximate because observation timestamps have one-second resolution.
- Session labels use America/New_York local time on September 15, 2026 (UTC−04:00): overnight before 04:00, premarket 04:00–09:30, regular 09:30–16:00, afterhours 16:00–20:00, then overnight. These are time-of-day classifications, not a separately verified exchange-wide market-status feed.
- Prices are decimal strings in USD. Missing or invalid quotes would be recorded in `collection_errors.jsonl`; values are never fabricated.

## Collection quality

- **Initial collection:** 13 observations; intervals [120.0, 14.0, 16.0, 14.0, 16.0, 14.0, 16.0, 14.0, 16.0, 14.0, 16.0, 14.0] seconds; source quote ages approximately 0.496051–6.319537 seconds; 13 observations within 0–10 seconds; 0 apparently future-dated quotes; maximum relative bid–ask spread 0.01056747%.
- **Resumed collection:** 12 observations; intervals [15.0, 14.0, 16.0, 14.0, 16.0, 14.0, 16.0, 14.0, 16.0, 14.0, 16.0] seconds; source quote ages approximately 0.176514–2.011063 seconds; 12 observations within 0–10 seconds; 0 apparently future-dated quotes; maximum relative bid–ask spread 0.01189697%.

Total: **25 observations** and **0 collection errors**. An error file is not created when there are no errors.

## Execution limitations

Fractional execution eligibility was not verified. The quote and general instrument metadata do not supply that eligibility, and the account-specific tradability tool was outside this collection's scope. Observing a SPY quote does not establish that fractional orders can execute in this session.

The quotes contain no queue position, available fill quantity, fill guarantee, actual slippage, or execution fees. A paper trade using bid/ask and assumed slippage remains a simulation. Sparse snapshots cannot establish whether a stop or target was touched between samples, and the collection interval can delay simulated exits. This brief sample cannot validate profitability or real-world performance.

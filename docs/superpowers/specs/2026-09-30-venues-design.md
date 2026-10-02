# Broker connections (venues): design

Date: 2026-09-30. Roadmap item 2a. The fast lane is 2a (broker connections), then 2b (the fast engine), then 3 (the agent swarm).

## Intent

The user wants seconds-level trading that switches strategies on the fly, using Alpaca and Coinbase alongside Robinhood. Today the code trades only through the Robinhood MCP, whose synchronous loop runs about every 5 s, and it reads only Coinbase's *public* daily candles. Alpaca is absent. The recorded quote tape arrives every ~20–30 s per symbol, which is too slow for a fast lane.

This sub-project connects the new brokers so the fast engine (2b) has real-time data and safe order paths. **It places no strategy orders.**

**Decided with the user:**
- **Accounts:** Alpaca paper, Alpaca live and Coinbase Advanced Trade (CDP API key).
- **Live money:** earn it, then the user arms it. Paper first. A strategy reaches real money only after it beats buy-and-hold on live paper results *and* the user arms that venue, under hard per-venue caps.
- **Build:** a separate async gateway process using the official SDKs, `alpaca-py` and `coinbase-advanced-py`, behind a `Venue` interface. The Robinhood trader is unchanged.

**Success:**
- `agentic-trading venues check` shows both Alpaca accounts and the Coinbase account, read-only, with live ticks flowing from every enabled stream and their delay in milliseconds.
- `venues smoke` places and cancels one tiny Alpaca **paper** order.
- The dashboard shows each venue's health.
- No test can reach a real broker.

## Non-goals

- **No strategies and no automatic orders.** Those are 2b. The only order 2a places is the manual paper smoke order.
- **No change to the Robinhood daemon, RiskGuard, the desk or its allocator.**
- **No paid data.** The design works with Alpaca's free IEX feed. The SIP feed (~$99/month) is a config switch, not a requirement.

## Facts that shape it

- **Alpaca stock data.** The free feed is IEX: real-time, but one exchange with a small share of volume.
- **Alpaca crypto.** Alpaca streams and trades crypto, and its paper account includes crypto, so crypto strategies can practise there.
- **Coinbase.** It has no paper mode, and at small size it costs about 0.40% (maker) or 0.60% (taker) per side. The fast engine must price that before choosing it.
- **Pattern-day-trader rule.** An Alpaca **live** margin account under $25,000 may make only 3 day trades per 5 trading days. The guard enforces this rather than letting the broker refuse.

## Components (`src/agentic_trading/venues/`)

| Module | Responsibility |
|---|---|
| `secrets.py` | `load_credentials(config) -> dict[str, Credentials]`, read from `config/secrets.toml` (git-ignored, must be mode 0600 or it is refused) or from environment variables (`ALPACA_PAPER_KEY`/`_SECRET`, `ALPACA_LIVE_KEY`/`_SECRET`, `COINBASE_API_KEY`/`_SECRET`). `Credentials.__repr__` and every error message show only the last 4 characters of the key and never the secret. `redact(text, credentials)` scrubs any secret from a string. |
| `model.py` | Plain dataclasses: `Tick(venue, symbol, bid, ask, last, size, exchange_at, received_at)`, `AccountView(venue, mode, equity, cash, buying_power, day_trades, pattern_day_trader)`, `PositionView(symbol, qty, market_value)`, `VenueOrder(client_order_id, symbol, side, notional or qty, type, limit_price)`, `VenueAck(client_order_id, venue_order_id, status, filled_qty, filled_avg_price)`. Money is `Decimal`; times are UTC `datetime`. |
| `venue.py` | The `Venue` protocol: `name`, `mode` (`paper` or `live`), `account()`, `positions()`, `open_orders()`, `submit(order)`, `cancel(client_order_id)`. The `TickSource` protocol: `async def ticks() -> AsyncIterator[Tick]` plus `symbols`. |
| `alpaca.py` | `AlpacaVenue(credentials, paper: bool)` over `TradingClient`. `AlpacaStockTicks` over `StockDataStream` (quotes and trades, feed from config). `AlpacaCryptoTicks` over `CryptoDataStream`. Paper and live are two separate venue instances with separate keys. |
| `coinbase.py` | `CoinbaseVenue(credentials)` over `RESTClient` (live only). `CoinbaseTicks` over `WSClient` (`ticker` and `heartbeats`). The SDK's own thread is bridged into asyncio through a thread-safe queue. |
| `guard.py` | `VenueGuard(venue_name, limits, state_path)`. `check(order, account, now) -> Verdict(allowed, reason)`. Limits: max order notional, max daily notional, max daily realized loss, max open orders, a per-venue kill switch, the PDT counter (live Alpaca under $25k: at most 3 day trades per rolling 5 trading days), and `live_requires_arm`: a live venue refuses every order unless `state/venues_arm.json` has it armed and not expired. The state survives restarts. |
| `arming.py` | `arm(venue, hours, confirm)` and `disarm(venue)`, written only by the CLI (`venues arm <venue> --hours N --yes`). Arming a live venue records who, when and until when. |
| `fanout.py` | `TickBus`: one async publisher, many subscribers (recorder, latest table, health, and later the fast engine). A slow subscriber drops its own oldest ticks and never blocks the others. |
| `recorder.py` | `StreamRecorder`: writes every tick, gzip-compressed and buffered like the existing `QuoteTape`, to `data/stream/<venue>/<SYMBOL>/<YYYY-MM-DD>.jsonl.gz`, flushed every 60 s and on shutdown. Error reports are rate-limited. |
| `health.py` | `VenueHealth` per stream and per venue: connected, last-tick age, delay (`received_at − exchange_at`, median and p95 over the last minute), reconnects, last error (redacted), and account status. Written atomically to `state/venues.json` once a second, with the latest bid/ask per symbol. |
| `daemon.py` | `run_venues(config)`: builds the enabled venues and sources, starts one task per source (reconnecting with exponential backoff plus jitter, capped at 60 s), the bus, the recorder and the health writer. SIGTERM or SIGINT flushes and exits cleanly. |
| `journal.py` | Every venue order event goes to `data/journal/venues-<YYYY-MM-DD>.jsonl`: submitted, acknowledged, refused by the guard (with its reason), cancelled, failed. It uses the existing `DecisionJournal` record style. The name keeps these files out of readers that expect dated Robinhood journals. |

## Configuration (`agentic.toml`, new `[venues]` table; everything off by default)

```toml
[venues]
enabled = true
alpaca_feed = "iex"                    # or "sip" with a paid subscription
alpaca_stock_symbols = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA"]
alpaca_crypto_symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
coinbase_products = ["BTC-USD", "ETH-USD", "SOL-USD"]
use_alpaca_paper = true
use_alpaca_live = false                # true = read the live account + stream; orders stay refused until armed
use_coinbase = false
stale_after_seconds = 30               # no tick this long while the market is open → stale
# per-venue caps (paper caps keep paper results realistic)
alpaca_paper_max_order_usd = "500"
alpaca_live_max_order_usd = "10"
coinbase_max_order_usd = "10"
max_daily_notional_usd = "200"
max_daily_loss_usd = "10"
max_open_orders = 5
```

A venue with no credentials is skipped, and health says so. This is not an error.

## CLI

- `agentic-trading venues run --config …`: the daemon, installed as the systemd user service `agentic-trading-venues.service`.
- `agentic-trading venues check --config …`: read-only. It shows each venue's account, positions and open orders, samples each stream for 10 s, prints ticks received and median delay, and exits non-zero if an enabled venue cannot authenticate.
- `agentic-trading venues smoke --config …`: **Alpaca paper only.** During regular hours it submits one $1 notional market order for SPY (Alpaca accepts notional only on market orders). Outside them it submits a 1-share SPY limit buy at half the last price, which cannot fill. It waits for acknowledgement, cancels the order if still open, and prints the round trip. It refuses to run against any live venue.
- `agentic-trading venues arm <venue> --hours N --yes` and `venues disarm <venue>`.

## Failure handling

| Failure | Behaviour |
|---|---|
| Stream disconnect | Reconnect with backoff; the reconnect is counted and stamped in health |
| No ticks while the market is open (stocks: regular session only; crypto: always) | `stale` after `stale_after_seconds` |
| Authentication failure (401 or 403) | Venue marked `auth_failed`, with a redacted reason; no retry until restart or a config change |
| Rate limit (429) | Back off per the response or 30 s, then resume |
| An SDK exception inside one source | Caught at that source's task boundary; the other sources keep running |
| Disk error in the recorder | Reported (rate-limited) in health; ticks keep flowing to other subscribers |
| Credentials file readable by others | Refused with a message telling the user to `chmod 600` it |

## Dashboard

- **`GET /api/venues`:** a read-only projection of `state/venues.json`, with no secrets and only the last 4 characters of any account identifier.
- **Venues card on the Health tab:** per venue, a connection dot, mode (paper or live), armed state, account equity, and each stream's last-tick age, median delay and reconnects.
- **Header dot:** turns amber when an enabled stream is stale and red when an enabled venue has failed authentication. This combines with the existing health levels.

## Testing

- **Fakes, not networks.** `FakeTradingClient`, `FakeStockStream`, `FakeCryptoStream`, `FakeRESTClient` and `FakeWSClient` implement the SDK surface the adapters use. The adapters are built by factories that tests replace.
- **Network tripwire.** A `conftest.py` fixture, active for the whole suite, fails any test that opens a socket to a non-loopback host.
- **Secret handling.** Keys never appear in `repr`, logs, health JSON, the journal or exception text. The test injects distinctive fake secrets and greps every output.
- **Guard.** Each limit is tested, the PDT counter over a rolling 5-trading-day window, live-refuses-unless-armed, arm expiry, and state surviving a restart.
- **Reconnect, stale and auth-failed paths** are driven by fakes that drop, stall or reject.
- **Recorder.** Buffering, flush on shutdown, the gzip round trip, and the path layout.
- **`/api/venues`** shape and redaction, plus the Venues card on the page (the cockpit id guard covers it).
- **Opt-in live tests** (`AGENTIC_LIVE_VENUE_TESTS=1`): `venues check` against the real paper account, and the paper smoke round trip. They are never part of the default suite or CI.
- **Windows build.** The new dependencies are bundled. Without credentials the venues service is simply off, and the existing smoke test still passes.

## Acceptance (with the user's keys in `config/secrets.toml`)

1. With `use_alpaca_live` and `use_coinbase` set to true, `venues check` authenticates Alpaca paper, Alpaca live (read-only) and Coinbase, prints each account, and shows live ticks from every enabled stream with the median delay.
2. `venues smoke` round-trips one Alpaca paper order and journals it.
3. The daemon runs for 10 minutes under systemd. The recorder writes files, and health shows every stream live, with no leaked secret in any log, state file or journal.
4. The dashboard Venues card shows all of the above, and the size sweep still passes.

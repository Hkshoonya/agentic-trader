# Read-only quote collection workflow

This workflow supplies market data to the local paper agent through an already connected MCP environment. It is not a standalone Robinhood API integration. Do not store credentials or session cookies in this project.

1. Discover the connection's read-only equity quote tool and obtain the instrument identifier for the configured symbol if required.
2. Fetch best bid and best ask. Preserve their source timestamps. Record `observed_at` as the UTC time when the response arrives. Use the older source timestamp as `quote_at`. Never substitute a previous session's close or last trade for the current bid and ask.
3. Append one complete JSON object and newline to `data/spy_quotes.jsonl`. Required fields: `symbol`, `observed_at`, `quote_at`, `bid`, `ask`. Preserve optional `source`, `session`, `bid_at`, `ask_at`, and a delay flag if actually supplied. Set `delayed` to null when unknown.
4. For this demonstration, schedule observations roughly 15 seconds apart and include API latency in the schedule. Use a bounded collection duration. Follow rate-limit responses; never synthesize a missing quote.
5. Put errors in a separate `data/collection_errors.jsonl` file and record source/cadence limitations in `data/collection_notes.md`.
6. Run the paper agent in watch mode while the file is being appended. Data received too late will be rejected by its fixed freshness rule. Do not relax that rule after observing results.

No account, balance, order, transfer, cancellation, browser submit, or credential operation is needed for this workflow. The agent writes local simulation files only.

To collect another bounded session in this connected chat, ask: "Collect SPY bid/ask quotes read-only for three minutes, append the observations to the quote file, and run the paper agent's watch mode."

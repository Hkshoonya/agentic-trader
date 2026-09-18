# Security policy

This repository is public and the software it contains can place real orders in
a real brokerage account when armed. That combination is worth treating
seriously, so here is exactly what is and is not in the repository.

## What is never committed

| Secret | Where it lives instead |
|---|---|
| Broker OAuth tokens (`data/tokens.json`) | local workspace only; `token_path` is absolute and outside the repo by default |
| LLM API keys (`.env`) | local `.env`, which is gitignored |
| Account number, equity, positions | `data/state/*.json`, gitignored |
| Decision journal | `data/journal/*.jsonl`, gitignored |
| Live configuration (`config/agentic.toml`) | gitignored; only `agentic.example.toml` and the Windows template ship |

This was verified against the **entire commit history**, not just the current
tree: `.env`, `config/agentic.toml`, `data/state/*`, `data/journal/*` and
`data/tokens.json` have never been committed, and no API-key-shaped string
exists anywhere in history. The only data that ships is public daily price
history (`data/bars/*.jsonl`).

Commit author emails are the GitHub noreply address
(`154622641+Hkshoonya@users.noreply.github.com`), so pushing does not publish a
personal mailbox.

## What the software does with the network

- Talks to Robinhood's Trading MCP (`agent.robinhood.com`) with your OAuth token.
- Talks to whichever LLM base URL you configure, if you enable the advisor.
- Serves the console on `127.0.0.1` only — it is not reachable from your network.
- Writes nothing outside its configured workspace.

It does not phone home, and there is no telemetry.

## The two switches that stop it spending money

Order submission requires **both** `mode = live` (or stage `probation`/`live`)
**and** `AGENTIC_ALLOW_LIVE=1` in the process environment. The flag is
deliberately not read from a config file, so a config copied from someone else
cannot arm your account. In the Windows app it takes typing `ARM`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**), or open an issue that describes the
risk *without* including tokens, account numbers, or working exploit details for
anything that could move money. I aim to acknowledge within a few days.

In scope and genuinely wanted:

- anything that could place, size, or repeat an order the strategy did not ask for;
- a gate that can be bypassed (regime, advisor veto, evidence age, size ceiling);
- a path that writes state outside the configured workspace;
- leakage of tokens, keys, account identifiers or journal contents;
- the console being reachable from outside `127.0.0.1`;
- dependency or supply-chain risk in the packaged Windows build.

Out of scope: "the strategy lost money", "the strategy is not profitable",
features that require you to set `AGENTIC_ALLOW_LIVE=1` and then trade.

## If you fork or self-host

- Never commit your own `.env`, tokens, or `data/` — the default `.gitignore`
  already covers them; keep it that way.
- Run the console on a trusted machine. It is read-only, but it shows your
  positions and equity.
- Re-authenticate Robinhood when the refresh token expires rather than disabling
  whatever is warning you about it.

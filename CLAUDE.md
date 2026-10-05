# Agentic Trading — notes for Claude

Live trading system (real money via Robinhood MCP), shadow-first. CONTRIBUTING.md's invariants apply: gates may only reduce risk, new spend-increasing switches default off, reported numbers must be reproducible from raw data.

## Commands
```bash
.venv/bin/pip install -e ".[dev,venues]"       # venues extra = alpaca-py, coinbase-advanced-py
.venv/bin/python -m pytest tests -q            # full suite
.venv/bin/python tools/check_doc_counts.py     # README/CONTRIBUTING test counts must equal the suite (CI checks)
.venv/bin/agentic-trading selfcheck --offline --config config/agentic.example.toml
.venv/bin/agentic-trading fast replay --config config/agentic.toml --source bars --from YYYY-MM-DD --to YYYY-MM-DD
systemctl --user restart agentic-trading-venues   # after changing venues/ or fast/ code
```
systemd `--user` services: `agentic-trading` (daemon), `agentic-trading-dashboard` (loopback console), `agentic-trading-venues` (Alpaca/Coinbase streams, recorder, paper switchboard), `agentic-trading-alert`.

## Layout (`src/agentic_trading/`)
- `cli.py` — the single `agentic-trading` entry point and all subcommands
- `desk/` — member books, allocator (`UNFUNDED` is a code constant, never config), netting
- `strategies/` — desk members
- `venues/` — SDK streams, TickBus, recorder, guard, arming, health
- `fast/` — switchboard (paper only), runs inside the venues service
- `dashboard*.py` — console; HTML/CSS/JS live as Python string modules
- `llm/` — advisor and regime gates; `rh_mcp/` — Robinhood MCP client + OAuth
- `windows/` — PyInstaller build; lazily imported modules need `hiddenimports` in `AgenticTrader.spec`
- `docs/superpowers/specs|plans/` — design spec + plan per feature

## Hard rules
- Commit only as `git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit`; no AI mention, no attribution/co-author trailers.
- Never stage `data/bars/*`, `data/stream/*`, `config/agentic.toml*`, `config/secrets.toml` (`data/bars` is tracked but changes locally constantly).
- Never read or print `config/secrets.toml`; leak checks print counts only.
- Tests never reach the network: `tests/conftest.py` tripwire refuses non-loopback connects. Use fakes (`tests/fast_support.py`).
- Live venues refuse orders unless the user runs `agentic-trading venues arm <venue> --hours N --yes` themselves — never arm on your own. The switchboard never calls `Venue.submit`.

## Gotchas
- Adding tests changes the count written in README (3 places) and CONTRIBUTING; update them or Windows CI fails.
- PRs stack on the previous feature branch (#8 → `feat/venues` → `feat/momentum-rotation-trial`), not `master`.
- `statistics` functions use exact Fractions — far too slow on per-bar hot paths; use floats (see `fast/regime.py`).
- `config/agentic.toml` is local and differs from the example; back it up as `agentic.toml.bak-<reason>` before editing.

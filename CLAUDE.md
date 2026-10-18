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
deploy/install.sh [--enable]                      # install every launcher + unit for this checkout (any path/user)
deploy/migrate.sh export|import FILE              # move hosts: stops the old one, leaves arm latches behind
```
Windows host (deploy/WINDOWS.md): `agentic-trading host run|status|restart|start|stop|logs|shutdown --config config/agentic.toml` is the supervisor and its systemctl verbs; `deploy\install.ps1 -Enable` registers the `AgenticTrading-Host` logon task; `deploy\migrate.ps1 export|import FILE` uses migrate.sh's archive. Units live in `host/units.py` and `tests/test_host_supervisor.py` fails if they drift from `deploy/*.service|*.timer`. On Windows `upgrade run` runs in a Docker container (`host container build|login|check|exec`, `host/container.py`, `deploy/upgrader/Dockerfile`) behind the same bwrap walls; it needs a checkout cloned with `core.autocrlf=false`.
systemd `--user` services: `agentic-trading` (daemon), `agentic-trading-dashboard` (loopback console), `agentic-trading-venues` (Alpaca/Coinbase streams, recorder, paper switchboard), `agentic-trading-alert`, `agentic-trading-swarm.timer` (daily 00:30 UTC swarm step), `agentic-trading-upgrade.timer` (daily 02:00 UTC self-upgrade) and `agentic-trading-upgrade-watch.timer` (canary check every 5 min).

## Layout (`src/agentic_trading/`)
- `cli.py` — the single `agentic-trading` entry point and all subcommands
- `desk/` — member books, allocator (`UNFUNDED` is a code constant, never config), netting
- `strategies/` — desk members
- `venues/` — SDK streams, TickBus, recorder, guard, arming, health
- `fast/` — switchboard (paper only), runs inside the venues service
- `swarm/` — the agent swarm: recipes (data), breed, screen, forward life, blend/cull, its member book; daily `swarm step`
- `upgrade/` — the self-upgrader: policy walls (allow-list + control-module import closure), bwrap sandbox, Codex → DeepSeek → Claude writers (next on unavailable), Claude review, ship to `live`, canary watchdog, Pause/Resume/Rollback switches (`state/upgrade/control.json`), and the planner (`upgrade plan`) that refills the backlog with swarm/strategy tasks when it runs low
- `dashboard*.py` — console; HTML/CSS/JS live as Python string modules
- `llm/` — advisor and regime gates; `rh_mcp/` — Robinhood MCP client + OAuth
- `host/` — the Windows host: supervisor, units mirrored from deploy/, client the upgrader's ship/watchdog use via `upgrade/services.py`
- `windows/` — PyInstaller build; lazily imported modules need `hiddenimports` in `AgenticTrader.spec`
- `docs/superpowers/specs|plans/` — design spec + plan per feature

## Hard rules
- Commit only as `git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit`; no AI mention, no attribution/co-author trailers.
- Never stage `data/bars/*`, `data/stream/*`, `config/agentic.toml*`, `config/secrets.toml` (`data/bars` is tracked but changes locally constantly).
- Never read or print `config/secrets.toml`; leak checks print counts only.
- Tests never reach the network: `tests/conftest.py` tripwire refuses non-loopback connects. Use fakes (`tests/fast_support.py`).
- Live venues refuse orders unless the user runs `agentic-trading venues arm <venue> --hours N --yes` themselves — never arm on your own. The switchboard never calls `Venue.submit`.
- The main checkout runs the `live` branch, which the self-upgrader fast-forwards. Never switch it to another branch or commit there by hand; do all work in a worktree and land it on `live` through a PR.

## Gotchas
- Adding tests changes the count written in README (3 places) and CONTRIBUTING; update them or Windows CI fails.
- PRs stack on the previous feature branch (#8 → `feat/venues` → `feat/momentum-rotation-trial`), not `master`.
- `statistics` functions use exact Fractions — far too slow on per-bar hot paths; use floats (see `fast/regime.py`).
- `config/agentic.toml` is local and differs from the example; back it up as `agentic.toml.bak-<reason>` before editing.

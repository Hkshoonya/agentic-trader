# Run the whole system natively on Windows

The Linux host runs everything as `systemd --user` units. On Windows one supervisor does the same
work: `agentic-trading host run`. It starts the daemon, the console and the venues gateway, and runs
the swarm, upgrade and canary timers. It reads the same unit definitions, which a test keeps in step
with `deploy/*.service` and `deploy/*.timer`. A scheduled task starts it at sign-in.

| Linux | Windows |
|---|---|
| `systemctl --user` units | `agentic-trading host run`, started by the `AgenticTrading-Host` logon task |
| `Restart=`, `RestartSec=`, `StartLimitBurst=` | the same rules, from `src/agentic_trading/host/units.py` |
| timers with `Persistent=true` | the same schedule (UTC); a run missed while the PC was off happens at start |
| `StandardOutput=append:data/state/X.log` | the same files, each line stamped in UTC |
| `OnFailure=agentic-trading-alert.service` | a Windows toast and `data/state/STOPPED_AT.txt` |
| SIGTERM on stop | a stop file; the child raises SIGINT on itself and shuts down through its own handlers |
| `deploy/install.sh`, `deploy/migrate.sh` | `deploy\install.ps1`, `deploy\migrate.ps1` (same archive format, either direction) |

**The self-upgrader stays off on Windows.** Its walls are Linux sandboxes: `bwrap` runs candidate code
with no network and no view of your secrets, and the writers' shell commands are sandboxed the same way.
Windows has none of these, so `host status` shows `agentic-trading-upgrade` as off, with the reason.
Nothing runs it nightly only to have it refuse. The canary timer still runs, because it serves the
console's Rollback button. `agentic-trading upgrade pause|resume|rollback|status` work as before.

**Run one host at a time.** Two hosts would trade on one broker login and keep two diverging records.

## 1. The code

```powershell
git clone https://github.com/Hkshoonya/agentic-trader.git
cd agentic-trader; git checkout live
git config core.autocrlf false       # the checkout is shared with Linux tooling; keep LF as committed
py -3 -m venv .venv; .venv\Scripts\pip install -e ".[dev,venues]"
.venv\Scripts\python -m pytest tests -q
```

## 2. Move the record here

On the **old host** (Linux): `deploy/migrate.sh export ~/agentic-move.tgz`. This stops and disables
everything there first. Carry the file privately (a USB stick, `scp`), never through a synced folder.
Then here:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\migrate.ps1 import C:\Users\<you>\Downloads\agentic-move.tgz
Remove-Item C:\Users\<you>\Downloads\agentic-move.tgz
```

The import makes the secrets, the config and `~\.config\agentic-trading` readable by you alone. If
`config\agentic.toml` still names Linux paths (`/home/...`), the import names those lines; make them
relative to the checkout. The arm latches are left behind, so this machine starts disarmed.

With nothing to move, copy `config\agentic.example.toml` to `config\agentic.toml` and run
`agentic-trading auth --config config\agentic.toml`.

## 3. Start it

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Enable              # add -KeepAwake (admin) on a 24/7 laptop
.venv\Scripts\agentic-trading host status --config config\agentic.toml
```

The console is at <http://localhost:8787>. Everyday verbs, the counterparts of `systemctl`:

```powershell
agentic-trading host restart  --config config\agentic.toml agentic-trading      # after changing code
agentic-trading host start    --config config\agentic.toml agentic-trading-swarm # run a timer's job now
agentic-trading host logs     --config config\agentic.toml agentic-trading --since "2026-10-18 00:00:00 UTC"
agentic-trading host shutdown --config config\agentic.toml                       # stop everything
```

Live venues stay refused until you run `agentic-trading venues arm <venue> --hours N --yes` yourself.

## Going back

`deploy\migrate.ps1 export FILE` here (it stops the host and disables its task first). Then on Linux,
`deploy/migrate.sh import FILE` and `deploy/install.sh --enable`.

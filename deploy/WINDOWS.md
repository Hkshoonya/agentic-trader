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

**The self-upgrader runs in a Linux container.** Its walls are Linux sandboxes. `bwrap` runs
candidate code with no network and no view of your secrets, and the writers' shell commands are
sandboxed the same way. Windows has none of these, so on Windows only `upgrade run` goes into a Docker
Desktop container. Inside it, the same code runs behind the same walls, and the sandbox probe proves
them before any writer starts. The container sees only this checkout and the writers' logins. After
a deploy, it asks this host to restart the services. The canary, Rollback and the
pause/resume switches stay native. Until you build the container, `host status` shows the upgrade as
off and gives the reason.

**Run one host at a time.** Two hosts would trade on one broker login and keep two diverging records.

## 1. The code

```powershell
# LF line endings, as committed: Linux git in the upgrader's container reads this checkout too
git clone -c core.autocrlf=false --branch live https://github.com/Hkshoonya/agentic-trader.git
cd agentic-trader
py -3 -m venv .venv; .venv\Scripts\pip install -e ".[dev,venues]"
.venv\Scripts\python -m pytest tests -q
```

## 2. The self-upgrader's container (optional)

Install Docker Desktop, start it, then:

```powershell
.venv\Scripts\agentic-trading host container build --config config\agentic.toml   # git, gh, bwrap, Python, Codex, Claude
.venv\Scripts\agentic-trading host container login --config config\agentic.toml   # once: gh, Codex and Claude logins
.venv\Scripts\agentic-trading host container check --config config\agentic.toml   # ready, or why not
```

`check` refuses a checkout that converts line endings, because Linux git in the container would see
every file as changed. A DeepSeek key for the second writer goes in
`%USERPROFILE%\.config\agentic-trading\upgrade.env` (`DEEPSEEK_API_KEY=...`), as on Linux, and reaches
the container by name only. Rebuild after `pyproject.toml` changes; `check` says when.

## 3. Move the record here

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

## 4. Start it

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

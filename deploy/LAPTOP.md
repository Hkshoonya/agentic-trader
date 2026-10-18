# Run the whole system on a Windows laptop

The system is a set of Linux `systemd --user` services: daemon, console, venues gateway, and the
swarm, self-upgrade and canary timers. The self-upgrader also needs `bwrap`, `gh`, Codex and Claude,
and a git checkout on `live`. On Windows they all run unchanged inside WSL2.

Without WSL, [WINDOWS.md](WINDOWS.md) runs every service and timer natively. The
self-upgrader runs in a Docker Desktop container there.

The one-click app in [`windows/`](../windows/README.md) is not this. It starts only the daemon and the
console, from an older single-strategy template, without the desk, venues, swarm or self-upgrader.

**Run one host at a time.** Two hosts would both push to `live`, both run the desk on the same broker
login, and keep two diverging records. `migrate.sh export` stops and disables the old host before it
packs anything.

## 1. Windows: WSL2 and a host that stays up

In an administrator PowerShell:

```powershell
wsl --install -d Ubuntu-24.04      # restart when asked, then create your Linux user
```

Then, still as administrator, from a copy of this repository (or just this one file):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows-host.ps1
```

On mains power, the script turns off sleep and hibernate and makes closing the lid do nothing. It
also adds a sign-in task that keeps the distro running. Keep the laptop plugged in.

Two more settings help:
- Set Windows Update *active hours* to cover 00:00–07:00 UTC (the swarm step and the upgrade run).
- Sign in once after any restart. A locked screen is fine.

## 2. Ubuntu (WSL): tools and logins

```bash
systemctl is-system-running        # "running" or "degraded" is fine; anything else:
#   printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf   then in PowerShell: wsl --shutdown
sudo apt update && sudo apt install -y git gh bubblewrap python3-venv python3-pip curl
sudo loginctl enable-linger "$USER"     # services keep running with no terminal open

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash && . ~/.nvm/nvm.sh
nvm install 22 && npm install -g @openai/codex
curl -fsSL https://claude.ai/install.sh | bash

gh auth login      # the upgrader opens PRs and pushes live
codex login        # first writer (ChatGPT account)
claude             # then /login: third writer, the reviewer and the planner
```

Check the sandbox before trusting it. Without it the upgrader refuses to run and changes nothing:

```bash
bwrap --unshare-all --die-with-parent --ro-bind / / true && echo "bwrap ok"
```

## 3. The code

```bash
git clone https://github.com/Hkshoonya/agentic-trader.git ~/agentic-trader
cd ~/agentic-trader && git checkout live
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,venues]"
.venv/bin/python -m pytest tests -q       # should pass in full before anything is moved
```

## 4. Cut over

Do this outside 00:30–07:00 UTC, so no swarm step or upgrade run is cut short.

On the **old host**:

```bash
deploy/migrate.sh export ~/agentic-move.tgz   # stops + disables everything here, then packs the state
```

The file holds keys and the broker login. Carry it privately (USB stick or `scp`), never through a
synced folder. Put it in `C:\Users\<you>\Downloads`, then on the **laptop**:

```bash
cd ~/agentic-trader
deploy/migrate.sh import /mnt/c/Users/<you>/Downloads/agentic-move.tgz
deploy/install.sh --enable
rm /mnt/c/Users/<you>/Downloads/agentic-move.tgz
```

The arm latches are deliberately left behind, so the laptop starts disarmed. Live venues stay
refused until you run `agentic-trading venues arm <venue> --hours N --yes` there yourself.

## 5. Check it

```bash
systemctl --user list-units 'agentic-trading*' && systemctl --user list-timers 'agentic-trading*'
.venv/bin/agentic-trading upgrade status --config config/agentic.toml
```

The console is at <http://localhost:8787> in a Windows browser, because WSL forwards localhost.

## Going back

Run the same two scripts the other way: `export` on the laptop, then `import` and
`deploy/install.sh --enable` on the old host.

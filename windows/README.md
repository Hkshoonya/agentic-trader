# Windows build

Everything needed to turn this repository into a double-clickable Windows app
lives in this folder.

## What you get

```
AgenticTrader\
  AgenticTrader.exe          <- double-click this
  agentic-trading.exe        <- the full CLI, same as the source checkout
  payload\
    config\agentic.example.toml
    windows\agentic.windows.toml
    data\bars\*.jsonl        <- 11 years of daily bars for all 16 symbols
  _internal\                 <- Python runtime and libraries
```

The launcher is a supervisor: it seeds a workspace, starts `agentic-trading.exe
run --daemon` and `agentic-trading.exe dashboard`, opens the console in your
browser, and shows the state the agent publishes. Every strategy, gate, and
report is the same code path as the CLI, so nothing quietly differs between the
packaged app and the machine it was built from.

## Build it

**On a Windows PC** (needs Python 3.11+ and the `py` launcher):

```powershell
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

or double-click `windows\build.bat`. The script creates a build virtualenv,
installs the project and PyInstaller, **runs the test suite** (it refuses to
build if the suite fails), builds, smoke-tests the frozen executables, and
writes:

```
dist\AgenticTrader\AgenticTrader.exe
dist\AgenticTrader-windows-x64.zip     <- copy this to the other computer
```

**On a machine with no Windows** (this Linux box, for example): push the
repository to GitHub and run the `windows-build` workflow
(`.github/workflows/windows-build.yml`). It builds on `windows-latest`, runs the
same smoke test, and attaches `AgenticTrader-windows-x64.zip` to the run — or to
a release if you push a tag like `v1.0.0`. PyInstaller cannot cross-compile, so
this is the only way to get a real PE executable without a Windows machine or
Wine.

## Run it on another computer

1. Unzip anywhere (`C:\Users\you\AgenticTrader` is fine — not `Program Files`).
2. Run `AgenticTrader.exe`.
3. It creates its workspace at `%LOCALAPPDATA%\AgenticTrader` on first run,
   copies the bar history there, and starts in **shadow mode**.
4. Click **Robinhood login** to authenticate. That opens the OAuth flow in your
   browser and stores the token in the workspace.
5. Optional: **API key…** to add a DeepSeek/OpenAI-compatible key for the LLM
   advisor and the regime gate.

Set `AGENTIC_TRADER_HOME` if you want the workspace somewhere else (a USB drive,
a synced folder, or beside the app for a portable install).

## The two switches

The app ships with both off, and they are the only things that change how much
the agent may do by itself:

| Switch | Where | What it allows |
|---|---|---|
| **Autonomy** | checkbox in the window (`AGENTIC_ALLOW_AUTONOMY=1`) | the agent may promote/demote itself as evidence changes |
| **Arm live** | *Arm live trading…* → type `ARM` (`AGENTIC_ALLOW_LIVE=1`) | real orders may be submitted to the broker |

Both are per-session environment, deliberately not configuration files: a copy
of the app sitting on someone else's computer starts inert, whatever the state
files say. Unarmed, a promoted agent journals `live_gate_blocked` with the order
it wanted instead of placing it.

## Where things live

```
%LOCALAPPDATA%\AgenticTrader\
  config\agentic.toml      <- your configuration (created from the template once)
  .env                     <- LLM key, if you add one
  data\bars\               <- price history (seeded from the payload, refreshed daily)
  data\journal\            <- every decision, day by day
  data\state\              <- promotion stage, risk limits, evidence report, health
  logs\agent.log           <- what the agent printed (also shown in the window)
```

Nothing outside that folder is written, and nothing is sent anywhere except the
broker you authenticate with and the LLM endpoint you configure.

## Command line, without the window

The bundled CLI is the same tool the documentation describes:

```powershell
agentic-trading.exe status     --config "$env:LOCALAPPDATA\AgenticTrader\config\agentic.toml"
agentic-trading.exe selfcheck  --config "…\config\agentic.toml"
agentic-trading.exe walkforward --config "…\config\agentic.toml"
agentic-trading.exe run --daemon --config "…\config\agentic.toml"
```

## Known limits

- The first `walkforward` run spends a few minutes of CPU: it prices the rule on
  11 years of 16 symbols. The daemon does this for you when the report is older
  than `evidence_refresh_days`.
- Robinhood's refresh token expires periodically; re-run **Robinhood login**.
- Antivirus software sometimes flags freshly-built PyInstaller binaries that are
  not code-signed. Sign the executable, or add an exclusion, if that happens.

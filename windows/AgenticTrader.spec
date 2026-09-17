# PyInstaller spec for the Windows build.
#
# Two executables, one codebase: the windowed launcher people double-click, and
# the console CLI the launcher drives. The CLI is not a helper — it is the agent
# — so keeping it whole means the packaged app can do everything the source
# checkout can: run, selfcheck, evolve, walkforward, auth, dashboard.

from pathlib import Path

REPO = Path(SPECPATH).resolve().parent
SRC = REPO / "src"

datas = [
    (str(REPO / "config" / "agentic.example.toml"), "payload/config"),
    (str(REPO / "windows" / "agentic.windows.toml"), "payload/windows"),
    (str(REPO / "windows" / "README.md"), "payload/windows"),
    (str(REPO / "README.md"), "payload"),
]

# Bar history ships with the build so the strategies and the evidence rig work
# on a fresh machine with no network and no first-run wait.
for bar_file in sorted((REPO / "data" / "bars").glob("*.jsonl")):
    datas.append((str(bar_file), "payload/data/bars"))

hiddenimports = [
    "agentic_trading.strategies.trend_crypto",
    "agentic_trading.strategies.fixture",
    "agentic_trading.strategies.spy_scalper",
    "agentic_trading.strategies.llm_multi_asset",
    "agentic_trading.llm.advisor",
    "agentic_trading.llm.client",
    "agentic_trading.llm.regime",
    "agentic_trading.rh_mcp.client",
    "agentic_trading.rh_mcp.oauth",
    "agentic_trading.dashboard",
    "agentic_trading.evidence",
    "agentic_trading.walkforward",
    "agentic_trading.selfcheck",
    "paper_scalper",
    "reporting",
    "tkinter",
    "tkinter.filedialog",
]

cli = Analysis(
    [str(REPO / "src" / "agentic_trading" / "cli.py")],
    pathex=[str(SRC), str(REPO)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "unittest"],
    noarchive=False,
)
# PyInstaller 6: PYZ takes the pure modules only; scripts are passed to EXE.
cli_pyz = PYZ(cli.pure)
cli_exe = EXE(
    cli_pyz,
    cli.scripts,
    [],
    exclude_binaries=True,
    name="agentic-trading",
    console=True,
    disable_windowed_traceback=False,
)

launcher = Analysis(
    [str(REPO / "windows" / "launcher.py")],
    pathex=[str(REPO / "windows"), str(REPO)],
    binaries=[],
    datas=[],
    hiddenimports=["tkinter", "tkinter.filedialog"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)
launcher_pyz = PYZ(launcher.pure)
launcher_exe = EXE(
    launcher_pyz,
    launcher.scripts,
    [],
    exclude_binaries=True,
    name="AgenticTrader",
    console=False,
    disable_windowed_traceback=False,
)

# One folder, both executables: the launcher starts `agentic-trading.exe` from
# beside itself, so they have to ship together and share one dependency set.
coll = COLLECT(
    cli_exe,
    launcher_exe,
    cli.binaries,
    launcher.binaries,
    cli.datas,
    strip=False,
    upx=False,
    name="AgenticTrader",
)

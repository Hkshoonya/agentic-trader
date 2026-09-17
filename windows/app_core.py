"""Portable-workspace logic behind the Windows launcher.

Kept apart from the GUI on purpose: this is the part that can be tested on any
operating system, and it is the part that decides where data lives, which
switches a child process gets, and what the window reports. The GUI draws it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

APP_NAME = "AgenticTrader"
DEFAULT_PORT = 8787
WORKSPACE_ENV = "AGENTIC_TRADER_HOME"

# The two switches are session environment, not configuration, so a packaged
# copy on someone else's computer starts with both off.
ARM_SWITCH = "AGENTIC_ALLOW_LIVE"
AUTONOMY_SWITCH = "AGENTIC_ALLOW_AUTONOMY"
ADVISOR_SWITCH = "AGENTIC_LLM_ADVISOR"

# What the user must type to arm real order submission. Arming is the one
# irreversible-feeling action in the app, so it is not a checkbox.
ARM_PHRASE = "ARM"


def default_workspace() -> Path:
    """Where a portable install keeps its config, bars, journals and state.

    ``%LOCALAPPDATA%`` on Windows, ``~/.agentic-trader`` elsewhere. Never the
    program directory: that is under Program Files on an installed copy, and a
    trading agent that cannot write its own journal should not start.
    """
    override = os.environ.get(WORKSPACE_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    return Path.home() / ".agentic-trader"


def application_dir() -> Path:
    """The folder the executable runs from (works for PyInstaller and source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_dir() -> Optional[Path]:
    """Read-only payload that ships with the build.

    A frozen onedir build keeps its data under the bundle root or under
    ``_internal`` depending on the PyInstaller major version, so all three
    layouts are checked rather than assuming one.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "payload")
        candidates.append(Path(meipass).parent / "payload")
    candidates.append(application_dir() / "payload")
    candidates.append(application_dir() / "_internal" / "payload")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # Running from a source checkout: the repository itself is the payload.
    repo = application_dir().parent
    if (repo / "config" / "agentic.example.toml").is_file():
        return repo
    return None


# Keys whose value is a filesystem location the workspace owns.
WORKSPACE_PATHS = (
    "quotes_path",
    "journal_dir",
    "state_dir",
    "history_path",
    "tools_snapshot_path",
    "token_path",
    "scalper_config",
)


def absolutise_paths(text: str, workspace: Path) -> str:
    """Point a config's own paths at the workspace, keeping its comments.

    Line-level and comment-preserving on purpose: the template's comments are
    the documentation a user reads first, and a TOML round-trip would throw
    them away.
    """
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"(.*)$', line)
        if match and match.group(1) in WORKSPACE_PATHS:
            key, value, tail = match.groups()
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = workspace / candidate
            lines.append(f'{key} = "{candidate.as_posix()}"{tail}')
        else:
            lines.append(line)
    return "\n".join(lines).rstrip("\n") + "\n"


@dataclass
class Bootstrap:
    """What the first run did, so the window can say it out loud."""

    workspace: Path
    created: list[str] = field(default_factory=list)
    copied_bars: int = 0
    config_path: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)


def bootstrap_workspace(
    workspace: Path,
    *,
    payload: Optional[Path] = None,
    template: str = "agentic.windows.toml",
) -> Bootstrap:
    """Create the workspace and seed it from the bundled payload.

    Idempotent: an existing workspace is never overwritten, because the journal,
    the promotion state and the tokens in there are the agent's memory.
    """
    payload = payload or bundled_dir()
    result = Bootstrap(workspace=workspace)
    for relative in ("config", "data", "data/bars", "data/journal", "data/state", "logs"):
        path = workspace / relative
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            result.created.append(relative)

    config_path = workspace / "config" / "agentic.toml"
    if config_path.is_file():
        result.config_path = config_path
    elif payload is not None:
        source = payload / "windows" / template
        if not source.is_file():
            source = payload / "config" / "agentic.example.toml"
        if source.is_file():
            # Relative paths in the template resolve against the *current
            # directory*, which is not necessarily the workspace: run the CLI
            # from anywhere else and it would read a different agent's state.
            # The seeded config is written with absolute paths so it means the
            # same thing wherever it is invoked from.
            config_path.write_text(
                absolutise_paths(source.read_text(encoding="utf-8"), workspace),
                encoding="utf-8",
            )
            result.created.append("config/agentic.toml")
            result.config_path = config_path
        else:
            result.warnings.append(
                "no config template in the payload; copy one into "
                "config/agentic.toml before starting"
            )
    else:
        result.warnings.append("no payload found beside the executable")

    if payload is not None:
        bars_source = payload / "data" / "bars"
        if bars_source.is_dir():
            target = workspace / "data" / "bars"
            for bar_file in sorted(bars_source.glob("*.jsonl")):
                destination = target / bar_file.name
                if not destination.is_file():
                    shutil.copyfile(bar_file, destination)
                    result.copied_bars += 1
        # The .env carries the LLM key; copy it once, never overwrite edits.
        env_source = payload / ".env"
        env_target = workspace / ".env"
        if env_source.is_file() and not env_target.is_file():
            shutil.copyfile(env_source, env_target)
            result.created.append(".env")
    return result


def read_dotenv(path: Path) -> dict[str, str]:
    """KEY=value pairs from a .env file, tolerating ``export`` prefixes."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            values[key] = value
    return values


def write_dotenv(path: Path, values: dict[str, str]) -> None:
    """Merge ``values`` into a .env file, preserving unrelated lines."""
    existing: list[str] = []
    if path.is_file():
        existing = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        key = ""
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.removeprefix("export ").split("=", 1)[0].strip()
        if key and key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def child_env(
    workspace: Path,
    *,
    arm_live: bool = False,
    autonomy: bool = False,
    advisor: Optional[bool] = None,
    environ: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """The environment every child process gets.

    ``arm_live`` is the only thing in this app that can move real money, so it
    is applied here and nowhere else: unset means the runtime refuses to submit
    even when the stage says live.
    """
    env = dict(environ if environ is not None else os.environ)
    env["AGENTIC_TRADER_HOME"] = str(workspace)
    env["AGENTIC_DASHBOARD_PORT"] = str(DEFAULT_PORT)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if arm_live:
        env[ARM_SWITCH] = "1"
    else:
        env.pop(ARM_SWITCH, None)
    if autonomy:
        env[AUTONOMY_SWITCH] = "1"
    else:
        env.pop(AUTONOMY_SWITCH, None)
    if advisor is not None:
        if advisor:
            env[ADVISOR_SWITCH] = "1"
        else:
            env.pop(ADVISOR_SWITCH, None)
    # The workspace .env is the app's own file; its values win over a stale
    # machine-wide environment so a pasted key actually takes effect.
    for key, value in read_dotenv(workspace / ".env").items():
        env.setdefault(key, value)
    return env


def arm_phrase_ok(text: str) -> bool:
    """Arming requires typing the word, not clicking through a dialog."""
    return text.strip().upper() == ARM_PHRASE


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_status(workspace: Path) -> dict[str, Any]:
    """Everything the window shows, read from the agent's own state files."""
    state = workspace / "data" / "state"
    gate = _read_json(state / "live_gate.json")
    promotion = _read_json(state / "promotion.json")
    limits = _read_json(state / "effective_limits.json")
    guard = _read_json(state / "risk_guard.json")
    health = _read_json(state / "health.json")
    evidence = _read_json(state / "strategy_evidence.json")
    last = (promotion.get("last_assessment") or {}) if promotion else {}
    return {
        "mode": gate.get("mode") or "shadow",
        "stage": gate.get("stage") or promotion.get("stage") or "shadow",
        "armed": bool(gate.get("allow_live", False)),
        "autonomy": bool(gate.get("allow_autonomy", False)),
        "equity": guard.get("current_equity", "0"),
        "baseline_equity": guard.get("baseline_equity", "0"),
        "kill_switch": bool(guard.get("kill_switch", False)),
        "streak": promotion.get("streak", 0),
        "required_cycles": 3,
        "eligible": bool(last.get("eligible", False)),
        "confidence": limits.get("confidence", "0"),
        "max_order_pct": limits.get("max_order_pct", "0"),
        "daily_notional_pct": limits.get("daily_notional_pct", "0"),
        "healthy": health.get("healthy"),
        "health_checked_at": health.get("finished_at", ""),
        "health_detail": _health_detail(health),
        "evidence_age_days": _age_days(evidence.get("generated_at")),
        "evidence_trades": (
            ((evidence.get("configs") or {}).get("production") or {}).get("trades")
        ),
        "evidence_expectancy_bps": (
            ((evidence.get("configs") or {}).get("production") or {}).get(
                "expectancy_bps"
            )
        ),
    }


def _health_detail(health: dict[str, Any]) -> str:
    if not health:
        return "no self-check yet"
    ok = health.get("ok", 0)
    warnings = health.get("warnings") or []
    failures = health.get("failures") or []
    if failures:
        return f"{len(failures)} failing: " + ", ".join(
            str(item.get("name")) for item in failures
        )
    if warnings:
        return f"{ok} checks ok, {len(warnings)} warning(s)"
    return f"{ok} checks passed"


def _age_days(stamp: Any) -> Optional[float]:
    try:
        seen = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() / 86_400


def cli_command(
    workspace: Path,
    args: Iterable[str],
    *,
    executable: Optional[Path] = None,
    python: Optional[Path] = None,
) -> list[str]:
    """How to invoke the agent's CLI from here.

    A frozen build ships ``agentic-trading.exe`` beside the launcher; a source
    run uses the interpreter directly. Either way the launcher is a supervisor,
    never a second implementation of the agent.
    """
    parts = list(args)
    if executable is not None and Path(executable).is_file():
        return [str(executable), *parts]
    if python is not None:
        return [str(python), "-m", "agentic_trading", *parts]
    return [sys.executable, "-m", "agentic_trading", *parts]


def config_path(workspace: Path) -> Path:
    return workspace / "config" / "agentic.toml"


def newer_than(path: Path, moment: float) -> bool:
    """Whether ``path`` was written after ``moment`` (monotonic seconds)."""
    try:
        return path.stat().st_mtime > moment
    except OSError:
        return False

"""One-click Windows launcher for the trading agent.

The window is a supervisor, not a second implementation: it seeds a portable
workspace, starts ``agentic-trading`` child processes, shows what they are
doing, and owns the two switches that decide how much the agent may do on its
own. Everything else — strategy, gates, evidence, promotion — lives in the CLI
and the state files, exactly as it does on the machine this was built from.

Run ``launcher.py --selftest`` for a headless check of the whole bootstrap path
(used by the packaging smoke test, and safe on a machine with no display).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_core import (  # noqa: E402 — path set above for frozen builds
    APP_NAME,
    DEFAULT_PORT,
    arm_phrase_ok,
    bootstrap_workspace,
    bundled_dir,
    child_env,
    cli_command,
    config_path,
    default_workspace,
    read_status,
    write_dotenv,
)

BG = "#0f1219"
PANEL = "#161b24"
FG = "#dfe6f1"
MUTED = "#8b97a8"
GREEN = "#35d07f"
RED = "#ff5f6d"
AMBER = "#f0b429"


class Supervisor:
    """Starts, stops and reports the agent's child processes."""

    def __init__(self, workspace: Path, log) -> None:  # noqa: ANN001 — callable
        self.workspace = workspace
        self.log = log
        self.agent: Optional[subprocess.Popen] = None
        self.dashboard: Optional[subprocess.Popen] = None
        self.arm_live = False
        self.autonomy = False
        self.advisor = False
        self.started_at = 0.0

    # -- process helpers ---------------------------------------------------

    def _spawn(self, args: list[str], name: str) -> subprocess.Popen:
        env = child_env(
            self.workspace,
            arm_live=self.arm_live,
            autonomy=self.autonomy,
            advisor=self.advisor,
        )
        log_dir = self.workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(  # noqa: SIM115 — kept open for the child's lifetime
            log_dir / f"{name}.log", "a", encoding="utf-8", errors="replace"
        )
        handle.write(f"\n=== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        handle.flush()
        command = cli_command(
            self.workspace,
            args,
            executable=_sibling_executable("agentic-trading"),
        )
        creation = {}
        if os.name == "nt":
            # A windowed app has no console; hide the children's consoles too.
            creation["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            command,
            cwd=str(self.workspace),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            **creation,
        )
        self.log(f"{name}: started (pid {process.pid})")
        return process

    def agent_running(self) -> bool:
        return self.agent is not None and self.agent.poll() is None

    def dashboard_running(self) -> bool:
        return self.dashboard is not None and self.dashboard.poll() is None

    def start_agent(self) -> None:
        if self.agent_running():
            self.log("agent: already running")
            return
        self.started_at = time.monotonic()
        self.agent = self._spawn(
            ["run", "--config", str(config_path(self.workspace)), "--daemon"],
            "agent",
        )

    def start_dashboard(self) -> None:
        if self.dashboard_running():
            return
        self.dashboard = self._spawn(
            [
                "dashboard",
                "--config",
                str(config_path(self.workspace)),
                "--port",
                str(DEFAULT_PORT),
            ],
            "dashboard",
        )

    def stop_all(self) -> None:
        for name, process in (("agent", self.agent), ("dashboard", self.dashboard)):
            if process is None or process.poll() is not None:
                continue
            self.log(f"{name}: stopping")
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def run_once(self, args: list[str], name: str, *, capture: bool = False):
        """Run a CLI command to completion (self-check, evidence, auth)."""
        env = child_env(
            self.workspace,
            arm_live=self.arm_live,
            autonomy=self.autonomy,
            advisor=self.advisor,
        )
        command = cli_command(
            self.workspace,
            args,
            executable=_sibling_executable("agentic-trading"),
        )
        creating = {}
        if os.name == "nt":
            creating["creationflags"] = 0x08000000
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            command,
            cwd=str(self.workspace),
            env=env,
            capture_output=capture,
            text=True,
            **creating,
        )
        if capture:
            return result
        self.log(f"{name}: exit {result.returncode}")
        return result


def _sibling_executable(name: str) -> Optional[Path]:
    """The CLI shipped beside the launcher, when running from a frozen build."""
    if not getattr(sys, "frozen", False):
        return None
    for suffix in (".exe", ""):
        candidate = Path(sys.executable).resolve().parent / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def selftest(workspace: Path) -> int:
    """Headless proof that the packaged app can seed and read its workspace."""
    bootstrap = bootstrap_workspace(workspace)
    status = read_status(workspace)
    payload = {
        "workspace": str(workspace),
        "payload": str(bundled_dir() or ""),
        "created": bootstrap.created,
        "copied_bars": bootstrap.copied_bars,
        "config": str(bootstrap.config_path or ""),
        "warnings": bootstrap.warnings,
        "status": status,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    print(json.dumps(payload, indent=2))
    return 0 if bootstrap.config_path else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument(
        "--workspace",
        default=str(default_workspace()),
        help="Where config, bars, journals and state live",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Seed the workspace, print the state and exit (no window)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser()

    if args.selftest:
        return selftest(workspace)

    try:
        import tkinter as tk
    except ImportError:
        print(
            "This build has no windowing support. Run the CLI instead:\n"
            "  agentic-trading run --config <workspace>/config/agentic.toml --daemon\n"
            "  agentic-trading dashboard --config <workspace>/config/agentic.toml",
            file=sys.stderr,
        )
        return 2

    bootstrap = bootstrap_workspace(workspace)

    app = Launcher(workspace, bootstrap, port=args.port)
    app.run()
    return 0


class Launcher:
    """The window: status, switches, and the handful of actions that matter."""

    def __init__(self, workspace: Path, bootstrap: Any, *, port: int) -> None:
        import tkinter as tk

        self.tk = tk
        self.workspace = workspace
        self.port = port
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — autonomous trading agent")
        self.root.configure(bg=BG)
        self.root.geometry("980x680")
        self.root.minsize(820, 560)

        self.logs: list[str] = []
        self.log_lock = threading.Lock()
        self.supervisor = Supervisor(workspace, self.log)

        self._build_widgets(bootstrap)
        self.log(
            f"workspace: {workspace}"
            + (f" (created {len(bootstrap.created)} items)" if bootstrap.created else "")
        )
        if bootstrap.copied_bars:
            self.log(f"seeded {bootstrap.copied_bars} bar files for backtests")
        for warning in bootstrap.warnings:
            self.log(f"warning: {warning}")

        self._tick()

    # -- layout ------------------------------------------------------------

    def _build_widgets(self, bootstrap: Any) -> None:
        tk = self.tk
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(
            header, text="AGENTIC TRADER", bg=BG, fg=FG,
            font=("Segoe UI Semibold", 16),
        ).pack(side="left")
        self.badges: dict[str, Any] = {}
        for name in ("mode", "stage", "armed", "health"):
            label = tk.Label(
                header, text="—", bg=PANEL, fg=MUTED,
                font=("Consolas", 10), padx=10, pady=3,
            )
            label.pack(side="left", padx=(10, 0))
            self.badges[name] = label

        status = tk.Frame(self.root, bg=PANEL)
        status.pack(fill="x", padx=16, pady=6)
        self.status_text = tk.Label(
            status, text="loading…", bg=PANEL, fg=FG, justify="left",
            anchor="w", font=("Consolas", 10), padx=12, pady=10,
        )
        self.status_text.pack(fill="x")

        buttons = tk.Frame(self.root, bg=BG)
        buttons.pack(fill="x", padx=16, pady=(4, 8))
        for text, command in (
            ("Start agent", self._start_agent),
            ("Open dashboard", self._open_dashboard),
            ("Self-check", self._selfcheck),
            ("Refresh evidence", self._refresh_evidence),
            ("Robinhood login", self._authenticate),
            ("Open workspace", self._open_workspace),
            ("Stop", self._stop),
        ):
            tk.Button(
                buttons, text=text, command=command, bg=PANEL, fg=FG,
                activebackground="#222a36", activeforeground=FG,
                relief="flat", padx=10, pady=6,
            ).pack(side="left", padx=(0, 6))

        switches = tk.Frame(self.root, bg=BG)
        switches.pack(fill="x", padx=16, pady=(0, 8))
        self.autonomy_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            switches, text="Let the agent promote itself (autonomy)",
            variable=self.autonomy_var, command=self._set_autonomy,
            bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
            activeforeground=FG, font=("Segoe UI", 10),
        ).pack(side="left")
        self.advisor_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            switches, text="Use the LLM advisor (needs an API key)",
            variable=self.advisor_var, command=self._set_advisor,
            bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
            activeforeground=FG, font=("Segoe UI", 10),
        ).pack(side="left", padx=(16, 0))
        tk.Button(
            switches, text="Arm live trading…", command=self._arm,
            bg="#3a1f24", fg=RED, relief="flat", padx=10, pady=4,
        ).pack(side="right")
        tk.Button(
            switches, text="API key…", command=self._set_key,
            bg=PANEL, fg=FG, relief="flat", padx=10, pady=4,
        ).pack(side="right", padx=(0, 6))

        self.hint = tk.Label(
            self.root,
            text=(
                "Starts in shadow: every decision is journalled, nothing is sent "
                "to the broker until you arm it."
            ),
            bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w",
        )
        self.hint.pack(fill="x", padx=18)

        log_frame = tk.Frame(self.root, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        tk.Label(
            log_frame, text="Activity", bg=BG, fg=MUTED,
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x")
        self.log_box = tk.Text(
            log_frame, bg="#0b0e14", fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat", height=14, wrap="word",
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

    # -- actions -----------------------------------------------------------

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.log_lock:
            self.logs.append(f"[{stamp}] {message}")
            self.logs = self.logs[-400:]

    def _start_agent(self) -> None:
        self.supervisor.start_agent()
        self.supervisor.start_dashboard()
        self._open_dashboard()

    def _open_dashboard(self) -> None:
        if not self.supervisor.dashboard_running():
            self.supervisor.start_dashboard()
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _stop(self) -> None:
        self.supervisor.stop_all()
        self.log("stopped")

    def _selfcheck(self) -> None:
        def work() -> None:
            result = self.supervisor.run_once(
                ["selfcheck", "--config", str(config_path(self.workspace))],
                "self-check",
                capture=True,
            )
            self.log(f"self-check exit {result.returncode}")
            for line in (result.stdout or "").splitlines()[-12:]:
                self.log(f"  {line}")

        threading.Thread(target=work, daemon=True).start()

    def _refresh_evidence(self) -> None:
        self.log("rebuilding the walk-forward report (takes a few minutes)…")

        def work() -> None:
            result = self.supervisor.run_once(
                ["walkforward", "--config", str(config_path(self.workspace))],
                "evidence",
                capture=True,
            )
            self.log(f"evidence exit {result.returncode}")
            for line in (result.stdout or "").splitlines()[-14:]:
                self.log(f"  {line}")

        threading.Thread(target=work, daemon=True).start()

    def _authenticate(self) -> None:
        self.log("opening the Robinhood login flow (a browser tab will open)…")

        def work() -> None:
            self.supervisor.run_once(
                ["auth", "--config", str(config_path(self.workspace))], "auth"
            )

        threading.Thread(target=work, daemon=True).start()

    def _open_workspace(self) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(self.workspace))  # noqa: S606 — user-initiated
            else:
                subprocess.Popen(["xdg-open", str(self.workspace)])  # noqa: S603,S607
        except Exception as exc:  # noqa: BLE001 — opening a folder must not crash
            self.log(f"could not open the folder: {exc}")

    def _set_autonomy(self) -> None:
        self.supervisor.autonomy = bool(self.autonomy_var.get())
        self.log(
            "autonomy "
            + ("ON — the agent may promote itself on new evidence" if self.supervisor.autonomy else "off")
        )

    def _set_advisor(self) -> None:
        self.supervisor.advisor = bool(self.advisor_var.get())
        self.log("LLM advisor " + ("ON" if self.supervisor.advisor else "off"))

    def _set_key(self) -> None:
        tk = self.tk
        dialog = tk.Toplevel(self.root)
        dialog.title("LLM API key")
        dialog.configure(bg=BG)
        tk.Label(
            dialog,
            text="Paste the key (stored in the workspace .env, never logged):",
            bg=BG, fg=FG, font=("Segoe UI", 10),
        ).pack(padx=14, pady=(12, 4), anchor="w")
        entry = tk.Entry(dialog, width=64, show="•", bg=PANEL, fg=FG,
                         insertbackground=FG, relief="flat")
        entry.pack(padx=14, pady=4)
        base = tk.Entry(dialog, width=64, bg=PANEL, fg=FG, insertbackground=FG,
                        relief="flat")
        base.insert(0, "https://api.deepseek.com")
        base.pack(padx=14, pady=4)
        model = tk.Entry(dialog, width=64, bg=PANEL, fg=FG, insertbackground=FG,
                         relief="flat")
        model.insert(0, "deepseek-chat")
        model.pack(padx=14, pady=4)
        tk.Label(
            dialog,
            text=(
                "TypeSafe API key (optional) — Jev classifies the regime for the "
                "whole book in one call:"
            ),
            bg=BG, fg=FG, font=("Segoe UI", 10),
        ).pack(padx=14, pady=(8, 0), anchor="w")
        typesafe = tk.Entry(dialog, width=64, show="•", bg=PANEL, fg=FG,
                            insertbackground=FG, relief="flat")
        typesafe.pack(padx=14, pady=4)

        def save() -> None:
            values = {
                "AGENTIC_LLM_API_KEY": entry.get().strip(),
                "AGENTIC_LLM_BASE_URL": base.get().strip(),
                "AGENTIC_LLM_MODEL": model.get().strip(),
                "AGENTIC_LLM_ADVISOR": "1",
            }
            key = typesafe.get().strip()
            if key:
                values["TYPESAFE_API_KEY"] = key
                values["AGENTIC_REGIME_BACKEND"] = "jev"
            write_dotenv(self.workspace / ".env", values)
            self.advisor_var.set(True)
            self._set_advisor()
            self.log("API key saved; the advisor is on for the next start")
            dialog.destroy()

        tk.Button(dialog, text="Save", command=save, bg=PANEL, fg=FG,
                  relief="flat", padx=12, pady=6).pack(padx=14, pady=(6, 12))

    def _arm(self) -> None:
        """Arming is a typed confirmation, and only for this session."""
        tk = self.tk
        dialog = tk.Toplevel(self.root)
        dialog.title("Arm live trading")
        dialog.configure(bg=BG)
        tk.Label(
            dialog,
            text=(
                "This lets the agent send real orders to your Robinhood account.\n"
                "Type ARM to confirm. It stays armed until you disarm, or close "
                "this app."
            ),
            bg=BG, fg=FG, justify="left", font=("Segoe UI", 10),
        ).pack(padx=16, pady=(14, 6), anchor="w")
        entry = tk.Entry(dialog, width=24, bg=PANEL, fg=FG,
                         insertbackground=FG, relief="flat")
        entry.pack(padx=16, pady=6)

        def confirm() -> None:
            if not arm_phrase_ok(entry.get()):
                self.log("arming cancelled: the confirmation phrase did not match")
                dialog.destroy()
                return
            self.supervisor.arm_live = True
            self.log("ARMED — real orders may be submitted from the next decision")
            self.log("restarting the agent so the switch applies cleanly…")
            self.supervisor.stop_all()
            self.supervisor.start_agent()
            dialog.destroy()

        tk.Button(dialog, text="Arm", command=confirm, bg="#3a1f24", fg=RED,
                  relief="flat", padx=12, pady=6).pack(padx=16, pady=(4, 14))
        tk.Button(dialog, text="Disarm and stay in shadow", command=self._disarm,
                  bg=PANEL, fg=FG, relief="flat", padx=12, pady=4).pack(
            padx=16, pady=(0, 14)
        )

    def _disarm(self) -> None:
        self.supervisor.arm_live = False
        self.log("disarmed — the agent will journal live_gate_blocked instead of placing")
        if self.supervisor.agent_running():
            # The switch is environment, so it only takes effect on a restart.
            self.supervisor.stop_all()
            self.supervisor.start_agent()

    # -- status loop -------------------------------------------------------

    def _tick(self) -> None:
        status = read_status(self.workspace)
        self._render(status)
        with self.log_lock:
            lines = list(self.logs)
        if lines:
            text = "\n".join(lines)
            self.log_box.configure(state="normal")
            if self.log_box.get("1.0", "end").strip() != text.strip():
                self.log_box.delete("1.0", "end")
                self.log_box.insert("1.0", text)
                self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(2000, self._tick)

    def _render(self, status: dict[str, Any]) -> None:
        armed = self.supervisor.arm_live
        self._badge("mode", str(status["mode"]).upper(), GREEN if status["mode"] == "live" else AMBER)
        self._badge("stage", f"STAGE: {status['stage']}", FG)
        self._badge("armed", "ARMED" if armed else "DISARMED", RED if armed else MUTED)
        healthy = status.get("healthy")
        self._badge(
            "health",
            "HEALTHY" if healthy else ("DEGRADED" if healthy is False else "NO CHECK YET"),
            GREEN if healthy else (RED if healthy is False else MUTED),
        )
        agent_state = "running" if self.supervisor.agent_running() else "stopped"
        dash_state = "running" if self.supervisor.dashboard_running() else "stopped"
        streak = f"{status['streak']}/{status['required_cycles']}"
        lines = [
            f"agent {agent_state}   dashboard {dash_state}   "
            f"http://127.0.0.1:{self.port}/",
            f"equity ${status['equity']} (baseline ${status['baseline_equity']})   "
            f"budget {float(status['max_order_pct']) * 100:.2f}%/order "
            f"{float(status['daily_notional_pct']) * 100:.2f}%/day   "
            f"confidence {status['confidence']}",
            f"promotion streak {streak}   eligible this assessment: "
            f"{'yes' if status['eligible'] else 'not yet'}   "
            f"kill switch {'TRIPPED' if status['kill_switch'] else 'clear'}",
            f"self-check: {status['health_detail']}",
        ]
        if status.get("evidence_trades"):
            age = status.get("evidence_age_days")
            lines.append(
                f"walk-forward evidence: {status['evidence_trades']} trades, "
                f"{float(status['evidence_expectancy_bps'] or 0):.0f} bps"
                + (f", {age:.1f} days old" if age is not None else "")
            )
        self.status_text.configure(text="\n".join(lines))

    def _badge(self, name: str, text: str, colour: str) -> None:
        label = self.badges[name]
        label.configure(text=text, fg=colour)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    raise SystemExit(main())

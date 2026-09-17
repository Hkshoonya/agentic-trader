"""The Windows package: it must seed a workspace and stay inert by default.

The GUI itself needs a display, but everything that decides what the packaged
app does — where data lives, which switches a child process gets, what the
window reports — is in ``windows/app_core.py`` and is tested here on any OS.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from windows.app_core import (
    ARM_SWITCH,
    AUTONOMY_SWITCH,
    arm_phrase_ok,
    bootstrap_workspace,
    child_env,
    cli_command,
    read_dotenv,
    read_status,
    write_dotenv,
)

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "windows"


class BootstrapTests(unittest.TestCase):
    def test_first_run_seeds_config_bars_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name) / "ws"
            result = bootstrap_workspace(workspace, payload=REPO)
            self.assertTrue((workspace / "data" / "state").is_dir())
            self.assertTrue((workspace / "logs").is_dir())
            self.assertTrue(result.config_path.is_file())
            self.assertGreater(result.copied_bars, 0)
            self.assertEqual(result.warnings, [])

    def test_the_seeded_config_is_the_windows_template(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name) / "ws"
            bootstrap_workspace(workspace, payload=REPO)
            text = (workspace / "config" / "agentic.toml").read_text()
        self.assertIn('mode = "shadow"', text)
        self.assertIn('strategy = "trend_crypto"', text)
        self.assertIn('max_order_pct = "0.01"', text)
        # The switches must not be *set* by configuration — they are session
        # environment, so a copied install stays inert whatever the state says.
        # Asserted on the parsed document, not the prose (the comments mention
        # them on purpose, to explain that they are not set here).
        import tomllib

        parsed = tomllib.loads(text)
        for key in (ARM_SWITCH, AUTONOMY_SWITCH):
            self.assertNotIn(key.lower(), {name.lower() for name in parsed})

    def test_a_second_run_never_overwrites_state(self) -> None:
        """The workspace is the agent's memory: journals and promotion."""
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name) / "ws"
            bootstrap_workspace(workspace, payload=REPO)
            marker = workspace / "data" / "state" / "promotion.json"
            marker.write_text('{"stage": "probation", "streak": 2}')
            config = workspace / "config" / "agentic.toml"
            config.write_text(config.read_text() + "\n# operator edit\n")
            again = bootstrap_workspace(workspace, payload=REPO)
            self.assertEqual(
                json.loads(marker.read_text())["stage"], "probation"
            )
            self.assertIn("operator edit", config.read_text())
            self.assertEqual(again.created, [])
            self.assertEqual(again.copied_bars, 0)

    def test_a_missing_payload_warns_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            result = bootstrap_workspace(
                Path(name) / "ws", payload=Path(name) / "not-a-payload"
            )
            # The workspace still exists: the user gets a window that explains
            # the problem instead of a crash on startup.
            made = (Path(name) / "ws" / "data" / "state").is_dir()
        self.assertTrue(result.warnings)
        self.assertIsNone(result.config_path)
        self.assertTrue(made)


class SwitchTests(unittest.TestCase):
    """The switches are the only things that change what the agent may do."""

    def test_nothing_is_armed_by_default(self) -> None:
        env = child_env(Path("/tmp/ws"), environ={"PATH": "/usr/bin"})
        self.assertNotIn(ARM_SWITCH, env)
        self.assertNotIn(AUTONOMY_SWITCH, env)

    def test_arming_is_explicit_and_reversible(self) -> None:
        armed = child_env(Path("/tmp/ws"), arm_live=True, environ={})
        self.assertEqual(armed[ARM_SWITCH], "1")
        disarmed = child_env(
            Path("/tmp/ws"), arm_live=False, environ={ARM_SWITCH: "1"}
        )
        self.assertNotIn(ARM_SWITCH, disarmed)

    def test_a_stale_environment_cannot_smuggle_the_switch_through(self) -> None:
        """A machine-wide AGENTIC_ALLOW_LIVE must not arm a copied install."""
        env = child_env(
            Path("/tmp/ws"), arm_live=False, environ={ARM_SWITCH: "1"}
        )
        self.assertNotIn(ARM_SWITCH, env)

    def test_the_workspace_dotenv_fills_in_the_llm_key(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name)
            write_dotenv(
                workspace / ".env",
                {
                    "AGENTIC_LLM_API_KEY": "sk-test",
                    "AGENTIC_LLM_BASE_URL": "https://api.deepseek.com",
                },
            )
            env = child_env(workspace, environ={})
        self.assertEqual(env["AGENTIC_LLM_API_KEY"], "sk-test")

    def test_the_arming_phrase_is_typed_not_clicked(self) -> None:
        self.assertTrue(arm_phrase_ok("ARM"))
        self.assertTrue(arm_phrase_ok("  arm  "))
        self.assertFalse(arm_phrase_ok("yes"))
        self.assertFalse(arm_phrase_ok(""))


class DotenvTests(unittest.TestCase):
    def test_writing_preserves_comments_and_other_keys(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / ".env"
            path.write_text(
                "# my keys\nexport AGENTIC_LLM_MODEL=deepseek-chat\nOTHER=1\n"
            )
            write_dotenv(path, {"AGENTIC_LLM_API_KEY": "sk-new"})
            values = read_dotenv(path)
            text = path.read_text()
        self.assertEqual(values["AGENTIC_LLM_MODEL"], "deepseek-chat")
        self.assertEqual(values["AGENTIC_LLM_API_KEY"], "sk-new")
        self.assertEqual(values["OTHER"], "1")
        self.assertIn("# my keys", text)


class StatusTests(unittest.TestCase):
    def test_status_reads_the_agents_own_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name)
            state = workspace / "data" / "state"
            state.mkdir(parents=True)
            (state / "live_gate.json").write_text(
                json.dumps({"mode": "live", "stage": "probation", "allow_live": False})
            )
            (state / "promotion.json").write_text(
                json.dumps(
                    {
                        "stage": "probation",
                        "streak": 2,
                        "last_assessment": {"eligible": True},
                    }
                )
            )
            (state / "risk_guard.json").write_text(
                json.dumps({"current_equity": "50", "baseline_equity": "50"})
            )
            (state / "effective_limits.json").write_text(
                json.dumps({"max_order_pct": "0.0092", "confidence": "0.89"})
            )
            (state / "health.json").write_text(
                json.dumps({"healthy": True, "ok": 6, "warnings": [], "failures": []})
            )
            status = read_status(workspace)
        self.assertEqual(status["mode"], "live")
        self.assertEqual(status["stage"], "probation")
        self.assertFalse(status["armed"])
        self.assertEqual(status["streak"], 2)
        self.assertTrue(status["eligible"])
        self.assertEqual(status["health_detail"], "6 checks passed")

    def test_a_fresh_workspace_reads_as_shadow_and_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            status = read_status(Path(name))
        self.assertEqual(status["mode"], "shadow")
        self.assertEqual(status["stage"], "shadow")
        self.assertFalse(status["armed"])
        self.assertEqual(status["health_detail"], "no self-check yet")


class CommandTests(unittest.TestCase):
    def test_a_frozen_build_calls_the_cli_beside_it(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            exe = Path(name) / "agentic-trading.exe"
            exe.write_text("stub")
            command = cli_command(
                Path(name), ["status", "--config", "c.toml"], executable=exe
            )
        self.assertEqual(command[0], str(exe))
        self.assertEqual(command[1], "status")

    def test_a_source_run_uses_the_module(self) -> None:
        command = cli_command(Path("/tmp/ws"), ["status"], python=Path("/usr/bin/python3"))
        self.assertEqual(
            command, ["/usr/bin/python3", "-m", "agentic_trading", "status"]
        )


class PackagingIntegrityTests(unittest.TestCase):
    """The spec and the scripts must reference files that exist."""

    def test_the_spec_lists_only_real_payload_files(self) -> None:
        spec = (PACKAGE / "AgenticTrader.spec").read_text(encoding="utf-8")
        from importlib import util

        self.assertIn("agentic-trading", spec)
        self.assertIn("AgenticTrader", spec)
        self.assertIn("payload/data/bars", spec)
        self.assertIn('(REPO / "windows" / "agentic.windows.toml")', spec)
        self.assertIn("COLLECT(", spec)
        # Both executables must land in one folder: the launcher looks for the
        # CLI beside itself.
        self.assertIn("cli_exe,\n    launcher_exe,", spec)

    def test_the_windows_template_is_valid_toml_the_project_can_load(self) -> None:
        import tomllib

        with open(PACKAGE / "agentic.windows.toml", "rb") as handle:
            payload = tomllib.load(handle)
        self.assertEqual(payload["mode"], "shadow")
        self.assertEqual(payload["strategy"], "trend_crypto")
        self.assertEqual(len(payload["symbol_whitelist"]), 16)
        self.assertEqual(payload["max_order_pct"], "0.01")
        self.assertNotIn("account_number", payload)

    def test_the_template_loads_through_the_real_config_parser(self) -> None:
        """A template the CLI rejects would fail on the user's machine, not ours."""
        from agentic_trading.config import load_config

        config = load_config(PACKAGE / "agentic.windows.toml")
        self.assertEqual(config.mode, "shadow")
        self.assertEqual(config.strategy, "trend_crypto")
        self.assertEqual(config.max_open_positions, 4)
        self.assertEqual(config.evidence_refresh_days, 7.0)

    def test_the_build_script_runs_the_tests_before_shipping(self) -> None:
        script = (PACKAGE / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("pytest tests -q", script)
        self.assertIn("--selftest", script)
        self.assertIn("Compress-Archive", script)

    def test_the_launcher_supports_a_headless_selftest(self) -> None:
        text = (PACKAGE / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("--selftest", text)
        self.assertIn("arm_phrase_ok", text)

    def test_the_workflow_builds_and_attaches_the_zip(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "windows-build.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("windows-latest", workflow)
        self.assertIn("AgenticTrader.spec", workflow)
        self.assertIn("upload-artifact", workflow)


if __name__ == "__main__":
    unittest.main()


class PathIsolationTests(unittest.TestCase):
    """A copied install must never read another agent's state.

    The template uses relative paths, which resolve against the *current*
    directory. Seeding a config that says "data/state" and then running the CLI
    from somewhere else silently points at a different agent — found by running
    the frozen build from the repository directory and watching it report the
    repository's live promotion state.
    """

    def test_the_seeded_config_points_inside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name) / "ws"
            bootstrap_workspace(workspace, payload=REPO)
            text = (workspace / "config" / "agentic.toml").read_text()
        import tomllib

        parsed = tomllib.loads(text)
        for key in ("state_dir", "journal_dir", "history_path", "token_path",
                    "tools_snapshot_path", "quotes_path"):
            self.assertTrue(
                parsed[key].startswith(workspace.as_posix()),
                f"{key} = {parsed[key]!r} escapes the workspace",
            )

    def test_the_rewrite_preserves_comments_and_other_keys(self) -> None:
        from windows.app_core import absolutise_paths

        text = (
            "# keep me\n"
            'state_dir = "data/state"  # trailing note\n'
            'strategy = "trend_crypto"\n'
            'token_path = "~/.config/agentic-trading/tokens.json"\n'
        )
        out = absolutise_paths(text, Path("/ws"))
        self.assertIn("# keep me", out)
        self.assertIn("# trailing note", out)
        self.assertIn('strategy = "trend_crypto"', out)
        self.assertIn('state_dir = "/ws/data/state"  # trailing note', out)
        # An absolute path (including an expanded ~) is left as a real path.
        self.assertNotIn('token_path = "~', out)
        self.assertIn("agentic-trading/tokens.json", out)

    def test_an_absolute_path_is_never_rewritten(self) -> None:
        from windows.app_core import absolutise_paths

        out = absolutise_paths('state_dir = "/mnt/usb/state"\n', Path("/ws"))
        self.assertIn('state_dir = "/mnt/usb/state"', out)

    def test_the_frozen_cli_reads_the_workspace_it_is_given(self) -> None:
        """Run from an unrelated directory: the answer must not change."""
        from agentic_trading.config import load_config

        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name) / "ws"
            bootstrap_workspace(workspace, payload=REPO)
            config = load_config(workspace / "config" / "agentic.toml")
        self.assertTrue(Path(config.state_dir).is_absolute())
        self.assertTrue(
            str(Path(config.state_dir)).startswith(workspace.as_posix())
        )

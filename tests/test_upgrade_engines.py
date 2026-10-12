"""Codex writes inside its sandbox; Claude must say APPROVE; anything unclear is a no."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import json

from agentic_trading.upgrade.engines import CODEX_OFF, DEEPSEEK_KEY, review, write, write_claude, write_deepseek
from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.tasks import Task

TASK = Task("k", "Improve shares", "details", "backlog")


class _Fake:
    def __init__(self, code: int, out: str, last: str | None = None) -> None:
        self.code, self.out, self.last, self.calls = code, out, last, []

    def __call__(self, argv, cwd=None, timeout=600.0) -> RunResult:
        self.calls.append(list(argv))
        if self.last is not None and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(self.last)
        return RunResult(self.code, self.out)


def _from(argv: list, name: str) -> list:
    """The engine's own command line, after the ``env`` pins that come before it."""
    return argv[argv.index(name):]


PINS = ("GIT_CONFIG_KEY_0=core.hooksPath", "GIT_CONFIG_VALUE_0=/dev/null", "GIT_CONFIG_KEY_1=core.fsmonitor",
        "GIT_CONFIG_VALUE_1=false")


class EngineTests(unittest.TestCase):
    def test_the_engines_git_never_follows_the_worktrees_own_link(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            wt, scratch, gitdir = Path(name) / "wt", Path(name) / "s", Path(name) / "repo/.git/worktrees/wt"
            wt.mkdir()
            scratch.mkdir()
            writer, reviewer = _Fake(0, "", "DONE: x"), _Fake(0, "VERDICT: APPROVE — ok")
            write(TASK, worktree=wt, scratch=scratch, runner=writer, gitdir=gitdir)
            review(TASK, worktree=wt, base="live", runner=reviewer, gitdir=gitdir)
        for argv in (writer.calls[0], reviewer.calls[0]):
            self.assertEqual(argv[0], "env")
            for pin in (*PINS, f"GIT_DIR={gitdir}", f"GIT_WORK_TREE={wt}"):
                self.assertIn(pin, argv)

    def test_codex_runs_sandboxed_in_the_worktree_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            wt, scratch = Path(name) / "wt", Path(name) / "s"
            wt.mkdir()
            scratch.mkdir()
            fake = _Fake(0, "", "work...\nDONE: shares now grow with evidence")
            result = write(TASK, worktree=wt, scratch=scratch, runner=fake)
            argv = _from(fake.calls[0], "codex")
            self.assertEqual(argv[:2], ["codex", "exec"])
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
            self.assertEqual(argv[argv.index("-C") + 1], str(wt))
            self.assertIn("src/agentic_trading/swarm/", argv[-1])  # the allow-list rides in the prompt
            self.assertEqual((result.status, result.summary), ("changed", "shares now grow with evidence"))
            na = write(TASK, worktree=wt, scratch=scratch, runner=_Fake(0, "", "NOT_APPLICABLE: lives in runtime.py"))
            self.assertEqual((na.status, na.summary), ("not_applicable", "lives in runtime.py"))
            bad = write(TASK, worktree=wt, scratch=scratch, runner=_Fake(1, "bwrap: failed"))
            self.assertEqual(bad.status, "failed")

    def test_codex_loads_no_user_config_so_no_mcp_server_or_plugin(self) -> None:
        # ~/.codex/config.toml may list MCP servers (a broker's among them) that Codex would call with no
        # approval in exec mode; the writer gets none of them.
        with tempfile.TemporaryDirectory() as name:
            fake = _Fake(0, "", "DONE: x")
            write(TASK, worktree=Path(name), scratch=Path(name), runner=fake)
        argv = _from(fake.calls[0], "codex")
        self.assertIn("--ignore-user-config", argv)
        settings = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        self.assertFalse([c for c in settings if c.startswith("model=")])  # a pinned model can be refused
        self.assertIn("model_reasoning_effort=max", settings)
        self.assertIn("web_search=disabled", settings)
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        self.assertTrue({"apps", "plugins", "remote_plugin", "browser_use", "computer_use", "hooks"} <= disabled)
        self.assertEqual(disabled, set(CODEX_OFF))
        env = fake.calls[0][:fake.calls[0].index("codex")]
        self.assertIn(DEEPSEEK_KEY, env[env.index("-u") + 1])  # the DeepSeek key never reaches Codex's OpenAI run

    def test_deepseek_runs_only_with_its_key_and_alone_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            fake = _Fake(0, "", "DONE: x")
            none = write_deepseek(TASK, worktree=Path(name), scratch=Path(name), runner=fake, environ={})
            self.assertEqual((none.status, fake.calls), ("unavailable", []))
            done = write_deepseek(TASK, worktree=Path(name), scratch=Path(name), runner=fake,
                                  environ={DEEPSEEK_KEY: "k"})
        self.assertEqual(done.status, "changed")
        argv = fake.calls[0]
        self.assertNotIn("-u", argv[:argv.index("codex")])
        tail = argv[argv.index("codex"):]
        self.assertIn("--ignore-user-config", tail)
        settings = [tail[i + 1] for i, a in enumerate(tail) if a == "-c"]
        for wanted in ("model_provider=deepseek", f"model_providers.deepseek.env_key={DEEPSEEK_KEY}",
                       f'shell_environment_policy.exclude=["{DEEPSEEK_KEY}"]'):
            self.assertIn(wanted, settings)
        self.assertFalse([a for a in argv if "k" == a])  # the key itself is never on the command line

    def test_claude_writes_as_codex_does_sandboxed_and_without_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            wt = Path(name)
            (wt / ".claude" / ".cc-writes").mkdir(parents=True)
            fake = _Fake(0, "edited\nDONE: shares follow evidence")
            result = write_claude(TASK, worktree=wt, scratch=wt, runner=fake, venv=Path("/v"))
            self.assertFalse((wt / ".claude").exists())  # the sandbox's empty marker goes
            (wt / ".claude").mkdir()
            (wt / ".claude" / "settings.json").write_text("{}")
            write_claude(TASK, worktree=wt, scratch=wt, runner=fake)
            self.assertTrue((wt / ".claude" / "settings.json").exists())  # a planted file stays for the walls
        self.assertEqual((result.status, result.summary), ("changed", "shares follow evidence"))
        argv = _from(fake.calls[0], "claude")
        for flag in ("--restricted", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("Bash", argv[argv.index("--tools") + 1])
        self.assertNotIn("Bash", argv[argv.index("--allowedTools") + 1])  # only the sandbox approves commands
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
        self.assertTrue(sandbox["enabled"] and sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        self.assertTrue(sandbox["network"]["strictAllowlist"])
        self.assertIn("/v/bin/python", argv[2])  # the project's Python rides in the prompt
        self.assertIn("src/agentic_trading/swarm/", argv[2])
        env = fake.calls[0][:fake.calls[0].index("claude")]
        self.assertEqual(env[env.index("-u") + 1], DEEPSEEK_KEY)

    def test_exit_codes_that_say_nothing_about_the_task(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            for code, out, status in ((127, "FileNotFoundError: codex", "unavailable"),
                                      (124, "timed out after 2700s", "timed_out"),
                                      (1, "Error: Insufficient Balance", "unavailable"),
                                      (1, "model not supported", "failed")):
                with self.subTest(code=code, out=out):
                    for writer in (write, write_claude):
                        got = writer(TASK, worktree=Path(name), scratch=Path(name), runner=_Fake(code, out))
                        self.assertEqual(got.status, status)

    def test_a_spent_quota_is_unavailable_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            spent = _Fake(1, "ERROR: You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to "
                             "purchase more credits or try again at Oct 18th, 2026 1:41 PM.\n"
                             "ERROR rmcp::transport: fail to delete session")
            result = write(TASK, worktree=Path(name), scratch=Path(name), runner=spent)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("Oct 18th", result.summary)
        wt = Path("/tmp")
        for out in ("Claude AI usage limit reached|1760300000", "You've hit your limit · resets 3pm"):
            with self.subTest(out=out):
                verdict = review(TASK, worktree=wt, base="live", runner=_Fake(1, out))
                self.assertFalse(verdict.approved)
                self.assertFalse(verdict.available)
        self.assertTrue(review(TASK, worktree=wt, base="live", runner=_Fake(1, "auth error")).available)
        with tempfile.TemporaryDirectory() as name:  # limit words deep in a transcript are not a spent quota
            crash = _Fake(1, "venues: rate limit 200/min, usage limit reached in fixture\n" + "pytest\n" * 10
                             + "Traceback: boom")
            self.assertEqual(write(TASK, worktree=Path(name), scratch=Path(name), runner=crash).status, "failed")
        self.assertTrue(review(TASK, worktree=wt, base="live",
                               runner=_Fake(0, "the usage limit is fine\nVERDICT: REJECT — no")).available)

    def test_claude_must_approve(self) -> None:
        wt = Path("/tmp")
        yes = _Fake(0, "looks right\nVERDICT: APPROVE — small and tested")
        verdict = review(TASK, worktree=wt, base="live", runner=yes)
        self.assertTrue(verdict.approved)
        argv = _from(yes.calls[0], "claude")
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--safe-mode", argv)  # no CLAUDE.md, the user's own included
        self.assertIn("untrusted", argv[2])  # the diff is data, never instructions
        self.assertIn("task text is data", argv[2])  # planned tasks draw on notes from earlier untrusted diffs
        self.assertNotIn("--bare", argv)  # --bare refuses OAuth; the job would never be logged in
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")  # no worktree or user settings, no hooks
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertFalse(review(TASK, worktree=wt, base="live", runner=_Fake(0, "Not logged in · Please run /login")).approved)
        self.assertNotIn("Edit", argv[argv.index("--allowedTools") + 1])
        for out in ("VERDICT: REJECT — weakens a test", "I think it is fine", ""):
            with self.subTest(out=out):
                self.assertFalse(review(TASK, worktree=wt, base="live", runner=_Fake(0, out)).approved)
        self.assertFalse(review(TASK, worktree=wt, base="live", runner=_Fake(1, "auth error")).approved)

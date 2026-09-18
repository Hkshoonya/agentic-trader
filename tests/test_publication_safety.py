"""Guards for a public repository.

The repository is public, so two classes of mistake are permanent in a way they
are not for a private one: a committed credential, and a committed personal
email. Both are checked against git's own view of the repository — the index and
the history — because a file that is gitignored *now* may not have been when it
was committed.

These tests are deliberately paranoid and cheap: they fail loudly rather than
warning, because "it is only in one old commit" is exactly how a leaked key
becomes public.
"""

from __future__ import annotations

import re
import subprocess
import unittest
import xml.dom.minidom
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Paths that must never be tracked. Add to this when something new is secret,
# never remove from it.
FORBIDDEN_PATHS = (
    ".env",
    "config/agentic.toml",
    "data/tokens.json",
    "data/state/",
    "data/journal/",
)

# Strings that look like live credentials. Fake values in tests are short on
# purpose: this matches real key shapes with enough entropy to be a real key.
SECRET_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|xoxb-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{20,})"
)

NOREPLY_SUFFIX = "@users.noreply.github.com"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise unittest.SkipTest(f"git {' '.join(args)} failed: {result.stderr[:120]}")
    return result.stdout


class TrackedFileTests(unittest.TestCase):
    def test_no_secret_paths_are_tracked(self) -> None:
        tracked = _git("ls-files").splitlines()
        offenders = [
            path
            for path in tracked
            if any(
                path == forbidden or path.startswith(forbidden)
                for forbidden in FORBIDDEN_PATHS
            )
        ]
        self.assertEqual(offenders, [], f"these must never be committed: {offenders}")

    def test_no_secret_paths_were_ever_committed(self) -> None:
        """The public history matters as much as the current tree."""
        for forbidden in FORBIDDEN_PATHS:
            history = _git(
                "log", "--all", "--full-history", "--oneline", "--", forbidden
            ).strip()
            self.assertEqual(
                history, "", f"{forbidden} appears in commit history:\n{history}"
            )

    def test_no_key_shaped_strings_in_tracked_files(self) -> None:
        tracked = [path for path in _git("ls-files").splitlines() if path]
        offenders: list[str] = []
        for path in tracked:
            candidate = REPO / path
            try:
                text = candidate.read_text(encoding="utf-8", errors="ignore")
            except (OSError, ValueError):
                continue
            if SECRET_SHAPES.search(text):
                offenders.append(path)
        self.assertEqual(offenders, [], f"key-shaped strings found in: {offenders}")

    def test_commit_authors_use_the_noreply_address(self) -> None:
        """A personal mailbox in 84 commits is not something you can unpublish."""
        emails = {line for line in _git("log", "--format=%ae").splitlines() if line}
        self.assertTrue(emails, "expected at least one commit")
        personal = {email for email in emails if not email.endswith(NOREPLY_SUFFIX)}
        self.assertEqual(personal, set(), f"personal emails in history: {personal}")


class ReadmeTests(unittest.TestCase):
    """A front page is a promise about the files next to it."""

    def _readme(self) -> str:
        return (REPO / "README.md").read_text(encoding="utf-8")

    def test_relative_links_point_at_real_files(self) -> None:
        text = self._readme()
        missing: list[str] = []
        for target in re.findall(r"\]\((?!https?://|#)([^)]+)\)", text):
            clean = target.split("#", 1)[0].strip()
            if not clean or clean.startswith("mailto:"):
                continue
            if not (REPO / clean).exists():
                missing.append(clean)
        self.assertEqual(missing, [], f"README links to missing files: {missing}")

    def test_the_front_page_says_what_it_is_not(self) -> None:
        """Third-party readers must meet the risk before the features."""
        text = self._readme()
        self.assertIn("## What this is not", text)
        self.assertIn("Not a guaranteed money-maker", text)
        self.assertIn("Not financial advice", text)
        # The measured numbers have to be the honest ones, not marketing ones.
        self.assertIn("856 bps", text)
        self.assertIn("$2/year", text)

    def test_the_two_switches_are_documented_with_their_defaults(self) -> None:
        text = self._readme()
        self.assertIn("AGENTIC_ALLOW_LIVE", text)
        self.assertIn("AGENTIC_ALLOW_AUTONOMY", text)
        # The heading carries its own anchor so the badges and cross-links work.
        self.assertIn('name="the-two-switches"', text)
        self.assertIn("The two switches", text)

    def test_it_explains_a_stopped_agent(self) -> None:
        """The console can say AGENT SILENT; the README must explain why."""
        self.assertIn("When the agent stops", self._readme())

    def test_repo_governance_files_exist_and_are_linked(self) -> None:
        text = self._readme()
        for name in ("SECURITY.md", "CONTRIBUTING.md", "LICENSE"):
            self.assertTrue((REPO / name).is_file(), f"{name} is missing")
            self.assertIn(name, text, f"{name} is not linked from the README")


class AssetTests(unittest.TestCase):
    def test_the_pipeline_diagram_is_well_formed_svg(self) -> None:
        document = xml.dom.minidom.parse(str(REPO / "docs" / "assets" / "pipeline.svg"))
        self.assertEqual(document.documentElement.tagName, "svg")
        # An animation that nobody can see is just a static picture.
        markup = (REPO / "docs" / "assets" / "pipeline.svg").read_text()
        self.assertIn("@keyframes", markup)
        self.assertIn("prefers-reduced-motion", markup)

    def test_the_console_screenshot_is_a_png(self) -> None:
        raw = (REPO / "docs" / "assets" / "console.png").read_bytes()[:8]
        self.assertEqual(raw, b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()


class DocumentedCountTests(unittest.TestCase):
    """A stale number on the front page is the thing readers check first.

    The badge has been wrong twice: 559 while the suite was 570, then 570 while
    it was 590. `tools/check_doc_counts.py` runs in CI and fails the build on a
    mismatch; this pins the cheaper half — that the files agree with each other —
    so a partial edit is caught without a full collection pass.
    """

    def test_every_documented_count_agrees(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_doc_counts", REPO / "tools" / "check_doc_counts.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        counts = {
            count for values in module.claimed_counts().values() for count in values
        }
        self.assertEqual(
            len(counts), 1, f"the documents disagree with each other: {counts}"
        )

    def test_the_guard_is_wired_into_ci(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "windows-build.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tools/check_doc_counts.py", workflow)

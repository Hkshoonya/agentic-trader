"""The upgrader's walls, enforced by trusted code on every candidate (spec U1–U3).

The editable surface is an allow-list, and even an allowed file is refused when any control
module imports it, however indirectly: whatever a control imports runs inside the control.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

ALLOWED_DIRS = ("src/agentic_trading/strategies/", "src/agentic_trading/swarm/", "docs/")
LIVE_REMOVED = ("src/agentic_trading/strategies/", "src/agentic_trading/swarm/")
ALLOWED_TEST_PREFIXES = ("tests/test_swarm_", "tests/test_strategies_")
PROTECTED_TESTS = ("tests/test_swarm_funding.py", "tests/test_swarm_desk.py")
COUNT_FILES = ("README.md", "CONTRIBUTING.md")
COUNT_LINE = re.compile(r"tests-\d+%20passing|#\s*\d+ tests|tests/\s+\d+ tests")
CONTROL_MODULES = (
    "agentic_trading.upgrade", "agentic_trading.dashboard", "agentic_trading.risk", "agentic_trading.arming",
    "agentic_trading.panic", "agentic_trading.limits", "agentic_trading.config", "agentic_trading.journal",
    "agentic_trading.jsonio", "agentic_trading.notify", "agentic_trading.venues.guard",
    "agentic_trading.venues.arming", "agentic_trading.venues.secrets",
)
FORBIDDEN_MODULES = ("subprocess", "socket", "httpx", "requests", "urllib", "http", "ctypes", "multiprocessing",
                     # the ways to reach those indirectly, run hidden code, or touch files and processes
                     "importlib", "builtins", "pickle", "marshal", "shelve", "pty", "ssl", "asyncio", "signal",
                     "shutil", "ftplib", "smtplib", "telnetlib", "xmlrpc", "websocket", "websockets", "aiohttp")
ESCAPES = ("__builtins__", "__subclasses__", "__globals__", "__code__", "__getattribute__")
SENSITIVE = re.compile(r"secrets|(^|[/\\])\.env\b|tokens\.json|\.ssh\b|id_rsa|agentic\.toml|\.config[/\\]|\.codex|\.claude")
FORBIDDEN_CALLS = ("eval", "exec", "__import__", "compile")
FORBIDDEN_OS = ("environ", "getenv", "system", "popen", "putenv", "unsetenv", "execv", "execve", "spawnv", "fork",
                "remove", "unlink", "rename", "replace", "rmdir", "removedirs", "chmod", "chown", "symlink", "link",
                "truncate", "open", "write", "mkfifo")
# Shipped code runs inside the trader, so a file write could flip data/state/mode to live, arm, or clear the
# kill switch. Writing is counted by capability, whatever the path or the object it is called on.
WRITE_ATTRS = ("write_text", "write_bytes", "unlink", "rename", "symlink_to", "hardlink_to", "link_to", "chmod",
               "lchmod", "touch", "rmdir")
SECRET = re.compile(
    r"sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|(?i:(api[_-]?key|secret|token|passw(or)?d)\s*[:=]\s*['\"][^'\"\s]{16,}['\"])")


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # A, M, D or R
    old_path: str = ""
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def parse_diff(name_status: str, unified: str) -> list[FileChange]:
    lines: dict[str, tuple[list[str], list[str]]] = {}
    current = ""
    for line in unified.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1]
            lines.setdefault(current, ([], []))
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+") and current:
            lines[current][0].append(line[1:])
        elif line.startswith("-") and current:
            lines[current][1].append(line[1:])
    out = []
    for row in name_status.splitlines():
        parts = row.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        path, old = (parts[2], parts[1]) if status == "R" and len(parts) > 2 else (parts[1], "")
        added, removed = lines.get(path, ([], []))
        out.append(FileChange(path, status, old, tuple(added), tuple(removed)))
    return out


# Files and folders AI tools read as instructions. The reviewer discovers CLAUDE.md files, nested ones
# too, so a change that adds one could tell its own reviewer to approve it. None may be added anywhere.
AGENT_FILES = frozenset({"claude.md", "claude.local.md", "agents.md", "agents.override.md", "gemini.md"})
AGENT_DIRS = frozenset({".claude", ".codex", ".agents", ".gemini", ".cursor"})


def _instructs_agents(path: str) -> bool:
    parts = [part.lower() for part in path.split("/")]
    return parts[-1] in AGENT_FILES or any(part in AGENT_DIRS for part in parts[:-1])


def allowed_path(path: str, *, live: bool) -> bool:
    if path in PROTECTED_TESTS or _instructs_agents(path):
        return False
    if path.startswith(ALLOWED_TEST_PREFIXES) and path.endswith(".py"):
        return not live  # live mode removes swarm/ and strategies/, and so their tests
    if live and path.startswith(LIVE_REMOVED):
        return False
    return path.startswith(ALLOWED_DIRS)


def module_of(path: str) -> Optional[str]:
    if not (path.startswith("src/") and path.endswith(".py")):
        return None
    dotted = path[len("src/"):-len(".py")].replace("/", ".")
    return dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted


def _file_for(src_root: Path, module: str) -> Optional[Path]:
    base = src_root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path, module: str) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in found if name.startswith("agentic_trading")}


def control_closure(src_root: Path) -> set[str]:
    """Every module a control module imports, directly or not (function-level imports included)."""
    todo = list(CONTROL_MODULES)
    for module in CONTROL_MODULES:  # a control package's submodules are controls too
        folder = src_root.joinpath(*module.split("."))
        if folder.is_dir():
            todo += [f"{module}.{p.stem}" for p in folder.glob("*.py") if p.stem != "__init__"]
    seen: set[str] = set()
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _file_for(src_root, module)
        if path is not None:
            todo.extend(_imports(path, module) - seen)
    return seen


def _writes(call: ast.Call, position: int) -> bool:
    """An ``open`` whose mode is not a literal read mode."""
    mode = call.args[position] if len(call.args) > position else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return False
    return not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)) or bool(set(mode.value) & set("wax+"))


def scan(text: str, *, shipped: bool = True) -> Counter:
    """Count the risky constructs in ``text``. File writes count only in ``shipped`` code: tests run in
    the sandbox alone and write their fixtures; strategy and swarm code runs inside the trader."""
    found: Counter = Counter()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        found["unparseable code"] += 1
        return found
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in FORBIDDEN_MODULES:
                found[root] += 1
        if isinstance(node, ast.ImportFrom) and node.module == "os" and not node.level \
                and any(alias.name == "*" or alias.name in FORBIDDEN_OS for alias in node.names):
            found["os imports"] += 1
        if isinstance(node, ast.Import) and any(alias.name == "os" and alias.asname for alias in node.names):
            found["os under another name"] += 1
        if (isinstance(node, ast.Name) and node.id in ESCAPES) or (isinstance(node, ast.Attribute)
                                                                    and node.attr in ESCAPES):
            found["interpreter internals"] += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("getattr", "setattr", "delattr") and len(node.args) >= 2:
            attr = node.args[1]
            if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)) \
                    or attr.value in FORBIDDEN_OS + FORBIDDEN_CALLS:
                found["dynamic attribute"] += 1
        if shipped and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in WRITE_ATTRS or (attr == "replace" and len(node.args) == 1 and not node.keywords):
                found["a file write"] += 1  # str.replace takes two arguments, Path.replace one
            elif attr == "open" and _writes(node, 0):
                found["a file write"] += 1
        if shipped and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" \
                and _writes(node, 1):
            found["a file write"] += 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SENSITIVE.search(node.value):
            found["a secret or config path"] += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found[node.func.id] += 1
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                and node.attr in FORBIDDEN_OS:
            found[f"os.{node.attr}"] += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and (first.value.startswith("/") or ".." in first.value):
                found["open outside the repo"] += 1
    return found


def test_names(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.update(f"{node.name}.{f.name}" for f in node.body
                         if isinstance(f, ast.FunctionDef) and f.name.startswith("test"))
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test") and node.col_offset == 0:
            names.add(node.name)
    return names


def check(changes: Iterable[FileChange], *, live: bool, closure: set[str], before: dict[str, str],
          after: dict[str, str], tests_before: int, tests_after: int, max_files: int, max_lines: int) -> list[str]:
    changes = list(changes)
    reasons: list[str] = []
    for change in changes:
        if change.status == "D":
            reasons.append(f"deletes {change.path}: the upgrader may not delete files")
            continue
        if change.status == "R" and not (allowed_path(change.old_path, live=live)
                                         and allowed_path(change.path, live=live)):
            reasons.append(f"renames {change.old_path} to {change.path}: both must be inside what it may change")
            continue
        if change.path in COUNT_FILES:
            if not all(COUNT_LINE.search(line) for line in change.added + change.removed):
                reasons.append(f"changes {change.path} beyond its test counts")
        elif not allowed_path(change.path, live=live):
            reasons.append(f"touches {change.path}, which is outside what the upgrader may change")
        module = module_of(change.path)
        if module and module in closure:
            reasons.append(f"{change.path} is imported by a control module, so it is protected")
        if change.path.endswith(".py"):
            shipped = not change.path.startswith("tests/")
            new = scan(after.get(change.path, ""), shipped=shipped) - scan(before.get(change.path, ""), shipped=shipped)
            reasons.extend(f"adds {kind} to {change.path}" for kind in sorted(new))
            if change.path.startswith("tests/"):
                gone = test_names(before.get(change.path, "")) - test_names(after.get(change.path, ""))
                reasons.extend(f"deletes test {name} in {change.path}" for name in sorted(gone))
        if any(SECRET.search(line) for line in change.added):
            reasons.append(f"adds something shaped like a secret to {change.path}")
    if len(changes) > max_files:
        reasons.append(f"changes {len(changes)} files (limit {max_files})")
    size = sum(len(c.added) + len(c.removed) for c in changes)
    if size > max_lines:
        reasons.append(f"changes {size} lines (limit {max_lines})")
    if tests_after < tests_before:
        reasons.append(f"the test count fell from {tests_before} to {tests_after}")
    return reasons

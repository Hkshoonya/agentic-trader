"""The self-upgrader's Linux on a Windows host: one Docker container per run.

Only ``upgrade run`` goes in here, because it is the job that runs untrusted code. Inside the
container it is the same code with the same walls as on a Linux host. bwrap holds candidate code
with no network and no view of the checkout's secrets, and each writer's shell runs in its own
sandbox. The container adds a wall of its own: it sees only the live checkout and the writers'
logins. It needs ``seccomp=unconfined``, because Docker's default profile refuses the user namespaces
bwrap is built on. The probe the cycle runs first proves the walls hold before any writer starts.

Everything that does not run candidate code stays native: the canary, Rollback, the switches.
After a deploy the upgrader asks the Windows host to restart the services (``AGENTIC_SERVICE_HOST``),
through the request files the host already serves.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Optional, Sequence

from agentic_trading import jsonio
from agentic_trading.upgrade.run import RunResult, Runner

IMAGE = "agentic-upgrader"
HOME_VOLUME = "agentic-upgrader-home"  # gh, Codex and Claude logins; never baked into the image
LIVE = "/work/live"  # the checkout inside; its parent is where Shipyard makes the run's worktree
DOCKERFILE = Path("deploy") / "upgrader" / "Dockerfile"
PASSED_ENV = ("DEEPSEEK_API_KEY",)  # from the operator's upgrade.env, as the Linux launcher sources it
BWRAP_CHECK = ("bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "true")


def requirements(pyproject: Path) -> list[str]:
    """Every dependency the suite needs: the core, plus the dev and venues extras."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    extras = project.get("optional-dependencies", {})
    return [*project.get("dependencies", []), *extras.get("dev", []), *extras.get("venues", [])]


def image_tag(repo: Path) -> str:
    digest = hashlib.sha256()
    digest.update("\n".join(requirements(repo / "pyproject.toml")).encode())
    digest.update((repo / DOCKERFILE).read_bytes())
    return f"{IMAGE}:{digest.hexdigest()[:12]}"


def _marker(state_dir: Path) -> Path:
    return Path(state_dir) / "host" / "upgrader.json"


def built_image(state_dir: Path) -> Optional[str]:
    """The image the operator built here, or None: the Windows host only runs the upgrader after it."""
    try:
        return str(json.loads(jsonio.read_text(_marker(state_dir)))["image"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def build(repo: Path, state_dir: Path, runner: Runner) -> RunResult:
    tag = image_tag(repo)
    with tempfile.TemporaryDirectory(prefix="agentic-upgrader-") as name:
        context = Path(name)
        shutil.copy2(repo / DOCKERFILE, context / "Dockerfile")
        (context / "requirements.txt").write_text("\n".join(requirements(repo / "pyproject.toml")) + "\n",
                                                  encoding="utf-8")
        done = runner(["docker", "build", "-t", tag, str(context)], timeout=3600.0)
    if done.code == 0:
        jsonio.write_text(_marker(state_dir), jsonio.dumps({"image": tag}, indent=2) + "\n")
    return done


def problems(repo: Path, image: Optional[str], runner: Runner) -> list[str]:
    """Why the upgrader cannot run here now; empty when it can. The cycle's own probe still runs."""
    found = []
    if runner(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=60.0).code != 0:
        return ["Docker is not running (start Docker Desktop)"]
    if not image:
        return ["the upgrader image is not built (agentic-trading host container build --config ...)"]
    if runner(["docker", "image", "inspect", image], timeout=60.0).code != 0:
        return [f"the image {image} is gone; build it again"]
    if image != image_tag(repo):
        found.append("pyproject.toml or the Dockerfile changed since the image was built; build it again")
    # Linux git in the container compares the checkout's files byte for byte: CRLF copies made by
    # Windows git would read as every file changed, and a deploy would refuse or, worse, commit them.
    autocrlf = runner(["git", "-C", str(repo), "config", "--get", "core.autocrlf"], timeout=60.0).out.strip()
    if autocrlf.lower() not in ("false", "input"):
        found.append("the checkout converts line endings (git config core.autocrlf false, then check it out "
                     "again); Linux git in the container would see every file as changed")
    if not found and runner(run_argv(repo, image, BWRAP_CHECK, raw=True), timeout=300.0).code != 0:
        found.append("bwrap cannot start inside the container; the upgrader would refuse to run")
    return found


def run_argv(repo: Path, image: str, args: Sequence[str], *, environ: Optional[dict[str, str]] = None,
             name: Optional[str] = None, raw: bool = False, interactive: bool = False) -> list[str]:
    env = os.environ if environ is None else environ
    passed = [a for key in PASSED_ENV if env.get(key) for a in ("-e", key)]
    argv = ["docker", "run", "--rm", "--init", *(["-it"] if interactive else []),
            *(["--name", name] if name else []),
            "--security-opt", "seccomp=unconfined",
            "-v", f"{repo}:{LIVE}", "-v", f"{HOME_VOLUME}:/home/node", "-w", LIVE,
            "-e", "AGENTIC_SERVICE_HOST=windows", "-e", "AGENTIC_UPGRADE_VENV=/venv",
            "-e", f"PYTHONPATH={LIVE}/src", *passed, image]
    return [*argv, *args] if raw else [*argv, "python", "-m", "agentic_trading", *args]


def execute(repo: Path, image: str, args: Sequence[str], runner: Runner) -> int:
    """Run one agentic-trading command in the container, its output streamed to ours (the host's log).

    Stopping this process (the host's stop file raises SIGINT here) stops the container: Docker sends
    its SIGTERM on, the same signal systemd would have sent the Linux unit.
    """
    name = f"agentic-upgrader-{os.getpid()}"
    proc = subprocess.Popen(run_argv(repo, image, args, name=name), stdout=sys.stdout, stderr=sys.stderr)
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        runner(["docker", "stop", "-t", "60", name], timeout=120.0)
        code = proc.wait()
    # The run's worktree lived in the container; drop git's record of it from the checkout.
    runner(["git", "-C", str(repo), "worktree", "prune"], timeout=120.0)
    return code


def login_steps() -> str:
    return ("gh auth login && gh auth setup-git      # the upgrader opens PRs and pushes live\n"
            "codex login --device-auth                 # first writer\n"
            "claude                                    # then /login: third writer, reviewer and planner\n"
            "exit")

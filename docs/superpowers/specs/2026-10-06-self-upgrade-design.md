# The self-upgrader: design

Date: 2026-10-06. The user asked for "evolution active and writable": the system should upgrade itself continuously and automatically, with a one-click stop. Decided with the user:
- the system may change its **own code**;
- **Codex writes** each change and **Claude Code reviews** it, and a change ships only if the reviewer approves;
- **at most one change a day**, each followed by a 24-hour canary;
- a one-click **Pause / Resume** and **Roll back** on the dashboard.

The paper-vs-account "follow gap" measurement, approved in the same message, is part of this work.

## Why this needs walls

The repository's founding rule is that a model may only reduce risk. A code-writing agent can rewrite the rules that protect money. In this session alone, reviews caught three defects an unattended upgrader would have shipped:
- the swarm could never step on live data;
- a crash could replay days into the book;
- a desk restart could take the swarm's weight away.

So the upgrader gets power over what it may change, never over the walls around it. Every wall is enforced by the **live, trusted code**, never by the candidate it is checking.

## What "writable" honestly means

The editable surface is an **allow-list**, not a deny-list. It contains only:
- **decision code** (`strategies/`, `swarm/`) that runs on paper today;
- its own tests;
- docs.

Everything that guards money, guards the upgrader, or carries its evidence stays human-only: risk, orders, the broker, the desk, venues, config, the dashboard's fences, the journal, alerts, and the upgrader itself. That is narrower than "the system changes its own code", but it is the part that can safely change unattended. Everything outside it reaches you as a proposal (PR).

## Rulings

| # | Ruling | Cost if wrong |
|---|---|---|
| U1 | **Allow-list, checked by trusted code.** A candidate may only add or modify these, with no deletes or renames out:<ul><li>`src/agentic_trading/strategies/**`;</li><li>`src/agentic_trading/swarm/**`;</li><li>`tests/test_swarm_*.py` (except `test_swarm_funding.py` and `test_swarm_desk.py`);</li><li>`tests/test_strategies_*.py`;</li><li>`docs/**`;</li><li>the test-count lines in `README.md` and `CONTRIBUTING.md`.</li></ul>A file is also refused if any **control module** imports it, directly or indirectly, as computed with `ast`. The control modules are `upgrade/`, `dashboard.py`, `risk.py`, `arming.py`, `panic.py`, `limits.py`, `config.py`, `journal.py`, `jsonio.py`, `notify.py`, `venues/guard.py`, `venues/arming.py` and `venues/secrets.py`. | Most of the codebase can only be improved through PRs you merge. |
| U2 | **Live mode removes the decision code.** While the trader's mode is `live`, `strategies/` and `swarm/` leave the allow-list too. Live decision logic then changes only through a PR you merge. | No unattended evolution once real money trades. |
| U3 | **The shape of a change:**<ul><li>at most 12 files and 400 changed lines;</li><li>the test count may only go up, and no test function is deleted;</li><li>the full suite and the doc-count check pass.</li></ul>An `ast` scan of added code rejects new uses of `subprocess`, `socket`, `httpx`, `requests`, `urllib`, `os.environ`/`os.getenv`, `eval`/`exec`/`__import__`, and `open()` on absolute or `..` paths. A scan of added lines rejects anything shaped like a secret. | Some legitimate changes, such as a new data fetch, must come as proposals. |
| U4 | **The cross-check is a hard gate.** Claude reviews the diff, the task and the hard rules, and must answer `VERDICT: APPROVE`. Anything else drops the change and journals why. It is told to reject on-disk format changes. | A cautious reviewer drops good changes. |
| U5 | **Ship with a record.** Commits are authored as Hkshoonya (the user's rule), with no AI mention. A PR labelled `auto-upgrade` is opened for every shipped change. The change is fast-forwarded onto the `live` branch, and the services are restarted. | None. |
| U6 | **A 24 h canary for crashes, not for decisions.** A watchdog, every 5 minutes, checks four things. Any failure triggers an automatic rollback, pauses the upgrader and sends an alert (`notify.py`). The checks:<ul><li>every service is active;</li><li>there is no new traceback in the service logs;</li><li>the daemon still journals `cycle_stats`;</li><li>`/api/desk`, `/api/swarm` and `/api/fast` answer 200, and the swarm step exited 0.</li></ul>The canary **cannot see** a change that quietly makes decisions worse, such as screening or sizing. Only forward results judge that, through the swarm's own cull and the desk's allocator. This is why U2 exists. | A subtly worse strategy can run until its forward record exposes it. |
| U7 | **A rollback restores code and data.**<ul><li>**Code:** `git revert` of the upgrade commit, then a fast-forward. Never `reset --hard`, so the tracked-but-locally-changed `data/bars` files survive.</li><li>**Data:** before each deploy, `state/swarm/` and `state/desk/swarm.json` are copied to `state/upgrade/snapshots/<commit>/`, and a rollback restores them. The desk then follows the restored book.</li></ul> | A rollback loses the swarm's days since the deploy (at most one). |
| U8 | **One-click control stays outside the upgrader's reach.**<ul><li>The dashboard gains `POST /api/upgrade` with `{"action": "pause"\|"resume"\|"rollback"}`, fenced exactly like arm/disarm. `dashboard.py` is a control module, so the upgrader can never edit this fence.</li><li>Pause and Resume write `state/upgrade/control.json`. Rollback writes a request that the watchdog carries out within 5 minutes.</li><li>`agentic-trading upgrade pause\|resume\|rollback\|status` does the same from a shell.</li><li>Pause always succeeds and also stops a run in progress before it deploys.</li></ul> | None. |
| U9 | **What it works on, ranked:**<ol><li>the evolution agent's open proposals, filtered to ones touching the allow-list;</li><li>open review minors in `state/upgrade/backlog.json`;</li><li>health problems the watchdog saw.</li></ol>A task that fails twice is skipped for 30 days. Every attempt is journaled. | It may pick unglamorous tasks first. |
| U10 | **Engines and candidate code run sandboxed.**<ul><li>**Codex** writes with `codex exec -C <worktree> -s workspace-write`.</li><li>The candidate's **tests run under `bwrap`** with no network. Only the worktree is writable; the venv and `/usr` are read-only. The home directory isn't mounted, so `secrets.toml`, `.env`, broker tokens and `~/.config` are invisible.</li><li>**Claude** reviews with `claude -p --bare` (no hooks, no auto-memory, no CLAUDE.md discovery), with read-only tools, in the worktree, given only the rules it needs.</li></ul> | Shipped code still runs inside processes that hold live keys. The allow-list, the scans and the review reduce that risk; they don't remove it. |
| U11 | **Refusals.** It is off by default: `[upgrade] enabled = true` turns it on. It refuses to run when:<ul><li>it is paused, or a canary is open;</li><li>the kill switch is engaged;</li><li>the live checkout is not on `live`, or has uncommitted changes outside `data/`;</li><li>the sandbox self-test fails (U12).</li></ul> | None. |
| U12 | **Launch gate: the sandbox must prove itself.** Before each run, a probe inside the test sandbox must find no network, `config/secrets.toml` and `~/.config` unreadable, and writes outside the worktree refused. **On this machine it fails today.** Ubuntu 24.04 blocks unprivileged user namespaces (`apparmor_restrict_unprivileged_userns = 1`), so `bwrap`, and Codex's own sandbox, can't start. The user must allow it once with an AppArmor profile for `/usr/bin/bwrap` (a `sudo` step, given in the plan). Until then the upgrader stays off. | None: it never runs unsandboxed. |

## Components (`src/agentic_trading/upgrade/`)

| Module | Responsibility |
|---|---|
| `policy.py` | The walls: the allow-list and control-module import closure (U1, U2), the change shape and the `ast` and secret scans (U3). `check(...) -> list[str]` gives the reasons for refusal. Pure and well tested. |
| `sandbox.py` | The `bwrap` command for candidate tests, and the self-test probe (U12). |
| `tasks.py` | Picks the next task from proposals, the backlog and health (U9). Keeps the attempt ledger (`state/upgrade/attempts.json`). |
| `engines.py` | `write(task, worktree) -> EngineResult` (Codex) and `review(task, diff, worktree) -> Verdict` (Claude). Subprocess calls with timeouts (45 and 20 minutes), injectable for tests. Parses `VERDICT: APPROVE\|REJECT`. |
| `ship.py` | Builds the worktree from `live`, snapshots the data stores, then commits, pushes, opens the PR, fast-forwards `live` and restarts the services. Rollback is a revert, a restore of the stores, a push and a restart. All git and systemd calls go through one injectable runner. |
| `watchdog.py` | The canary checks (U6) and carrying out a requested rollback. Writes `state/upgrade.json` for the dashboard. |
| `control.py` | `control.json`: paused, rollback requested, and the canary window. Read and written atomically. |
| `cycle.py` | One daily run: check the guards (U11), pick a task, write, check the policy, test, review, ship, start the canary. Every outcome is journaled to `upgrade-<day>.jsonl`. |
| `cli.py` | `agentic-trading upgrade run\|watch\|pause\|resume\|rollback\|status`. |

### The follow gap (paper vs account)

When the desk re-sizes a funded read-only member's symbols, it journals `desk_follow`, holding each symbol's book price (the swarm's close) and the live mid used for the target. The Swarm card shows the median follow gap in basis points over the last 30 follows. This is how far the account's fills drift from the swarm's paper record. It is a report, not a gate. The canary only checks that it is still produced once the swarm is funded.

`desk/` is protected (U1), so the follow-gap change is made in this human-reviewed project, not by the upgrader.

## Operations

- **Launch:** create `live` at the current live commit and switch the main checkout to it, once. After that, all feature work (human or this assistant) branches from `live` in a worktree and never switches the main checkout (U11).
- `deploy/agentic-trading-upgrade.{service,timer}` runs `upgrade run` daily at 02:00 UTC, after the swarm step, with `Persistent=true`.
- `deploy/agentic-trading-upgrade-watch.{service,timer}` runs `upgrade watch` every 5 minutes.
- **Branches:**
  - At launch, `live` is created at the current live commit, and the main checkout switches to it. Every upgrade branch `auto/<day>-<slug>` starts from `live`.
  - Human feature work still goes through PRs. Merging into `live` is a fast-forward or a merge done by the user.

## Failure handling

| Failure | Behaviour |
|---|---|
| An engine times out, errors, or produces no diff | The attempt is journaled and nothing ships. The task counts one failed attempt. |
| The policy rejects the diff | Journaled with the exact rule. The task counts one failed attempt. |
| Tests or doc counts fail | Journaled with the tail of the output; nothing ships. |
| The reviewer doesn't approve | Journaled with the reviewer's reason; nothing ships. |
| Push, PR or fast-forward fails | Nothing restarts; `live` is unchanged; journaled. |
| A canary check fails, or the user presses Roll back | Revert, restart, pause, notify. |
| The rollback itself fails | Pause and notify loudly. Services stay on whatever is running. |
| The kill switch is engaged | The upgrader doesn't run, and the watchdog still guards an open canary. |

## Testing

All tests use fakes. No engine, git remote or systemd is touched; the runner and engines are injected.
- **Policy:**
  - the allow-list, including the live-mode removals;
  - a file a control module imports, even indirectly;
  - a rename out of the allow-list;
  - each forbidden construct in the `ast` scan;
  - the size limits;
  - the test count falling;
  - a deleted test function;
  - a secret-shaped added line.
- **Sandbox:** the probe passes and fails on fake runners; the command hides home and blocks the network.
- **Tasks:** ranking, the 30-day skip after two failures, ledger persistence.
- **Cycle:**
  - every refusal (disabled, paused, kill switch, canary open, dirty checkout);
  - the full happy path with fake engines and runner, as an ordered list of calls;
  - each failure row above stops before shipping.
- **Watchdog:**
  - each check failing leads to revert, restart and pause;
  - a requested rollback;
  - the canary closes after 24 h.
- **Dashboard:**
  - `/api/upgrade` fencing (non-loopback, wrong content type, cross-origin);
  - pause, resume and rollback write `control.json`;
  - the Evolution card.
- **Follow gap:**
  - a funded scoped retarget journals `desk_follow` with book and live prices;
  - the card's median.

## Acceptance

1. With `[upgrade] enabled = true`, a manual `upgrade run` picks a task, has Codex write it, passes the policy, tests and Claude's review, ships it with a PR, and opens a canary.
2. Pause on the dashboard stops the next run. Roll back reverts the last upgrade within 5 minutes, with the services active afterwards.
3. A forced canary failure in a test rig reverts and pauses on its own.
4. The Evolution card shows the state (paused, canary, last change, last refusal) and the buttons. The size sweep still passes.
5. The Swarm card shows the follow gap once the swarm is funded and followed.

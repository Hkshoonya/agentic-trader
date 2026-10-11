# The upgrade planner: design

Date: 2026-10-11. The user asked for an in-scope task source so the self-upgrader keeps evolving instead of going idle. Every decision below was made under the user's standing delegation of 2026-10-06 ("auto decide for me until finished"). The goal is a winner and earner that evolves continuously.

## The problem

The upgrader makes at most one change a day. Its work comes from three places (spec U9):
- the evolution agent's proposals, filtered to the allow-list;
- the backlog;
- health problems.

The evolution agent watches the trader's runtime, so it only proposes runtime, sizing, risk and execution changes. All 174 proposals queued on 2026-10-11 fall outside the allow-list. The backlog holds five hand-written swarm seeds, and health problems are rare. So around 2026-10-16 the upgrader reaches "nothing to work on" and stops evolving.

## The approach

**A planner inside the upgrader.** It runs when the in-scope queue is low and asks a model for new tasks inside the allow-list. The model gets two things: the swarm's measured results, and the allow-listed source.

Approaches considered:
- **Planner in the upgrader (chosen).** The task source sits next to the walls that will judge its output. It is protected from self-edit, because `upgrade/` is a control module. It sees the code it plans for.
- **Teach the evolution agent about the swarm.** Rejected:
  - the evolution agent runs inside the trader daemon and reasons about trading telemetry;
  - it never sees code, so its tasks would name no files;
  - mixing the two concerns would also put swarm reasoning inside a money-path process.
- **Fixed rules over telemetry** (for example, "a family with no survivors gets a variant"). Rejected: a fixed vocabulary cannot evolve, and that is the point of the work.

## Rulings

| # | Ruling | Cost if wrong |
|---|---|---|
| P1 | **When it runs.** In `upgrade run`, after every guard and the sandbox probe pass and before a task is picked, if all three hold:<ul><li>the trader is not in `live` mode;</li><li>fewer than 2 in-scope tasks are open, meaning not final and not resting;</li><li>the planner has not run today (UTC).</li></ul>It runs at most once a day. Planning is not a change, so the day's one change still follows. `agentic-trading upgrade plan` runs it on demand under the same lock, and prints what it would add (`--save` keeps it). | A failed plan leaves the queue low for a day. |
| P2 | **The engine has no tools.** The planner calls `claude -p` with:<ul><li>`--tools ""` (no tools at all);</li><li>`--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence`;</li><li>a 10-minute timeout;</li><li>the working directory set to the empty probe root.</li></ul>Everything it knows is in the prompt, so it cannot read `config/secrets.toml`, the journal or anything else. Its output is plain JSON that trusted code parses. | The model cannot look around, so its plans are only as good as the prompt. |
| P3 | **What the prompt holds:**<ul><li>the rules;</li><li>the full source of `swarm/` and `strategies/` (about 95 kB today, capped at 160 kB, with the largest files cut to their first lines if over);</li><li>a telemetry digest (P4);</li><li>the attempt ledger (titles, outcomes, notes);</li><li>the open backlog titles.</li></ul>The telemetry and the ledger are marked as untrusted data. | A cap that cuts code hides some of it from the planner. |
| P4 | **Telemetry digest (earnings first).** From `state/swarm.json`:<ul><li>the book's equity and return;</li><li>a per-family summary (alive, nursery, funded, median forward excess, median drawdown, deaths);</li><li>each agent's family, universe, origin, forward days, excess, drawdown, state, share and error;</li><li>the swarm scout's status.</li></ul>Also:<ul><li>from `state/swarm/ledger.json`, the trials and refused counts;</li><li>the last 40 lines of `swarm.log`;</li><li>from the upgrade ledger, what shipped, failed or was rolled back, and why.</li></ul>The prompt says outright that under 20 forward days, excess is noise. | Short records invite overfitting. The noise rule and the walls limit the damage. |
| P5 | **What it may propose.** Up to 3 tasks, each with `title`, `detail`, `files`, `evidence` and `falsified_if`. Priorities, in order:<ol><li>raise the swarm's forward excess, or the quality of the evidence that judges it;</li><li>fix a failure the telemetry shows;</li><li>robustness.</li></ol>It must not propose:<ul><li>changes outside the allow-list;</li><li>new file writes, network calls or on-disk format changes;</li><li>more risk, size or positions;</li><li>edits to protected tests;</li><li>a repeat of an attempted task.</li></ul> | It may propose dull but safe work. |
| P6 | **Trusted code checks every task before it is queued:**<ul><li>the title is 10–120 characters;</li><li>`files` lists 1–6 paths;</li><li>every path passes `policy.allowed_path(live=False)` and is not in the control-module closure;</li><li>at least one path is under `swarm/` or `strategies/`;</li><li>the evidence is not empty;</li><li>there is no near-duplicate (word-set overlap ≥ 0.6) of any title in the ledger or the backlog.</li></ul>Accepted tasks are added to `state/upgrade/backlog.json`, keyed `plan-<sha1 of title>`, with `origin: "planner"`, the date and the evidence. The backlog is capped at 8 open tasks. | Wording-level dedupe misses a rephrased repeat, which costs one day. |
| P7 | **Failure is quiet.** A timeout, non-zero exit or unparseable reply journals `upgrade_plan` with the reason and the run carries on with whatever is queued. The planner is not retried that day. | None. |
| P8 | **Codex sees the plan in full.** A planner task's detail carries its files, evidence and falsification test into the writer prompt. The walls and the reviewer judge the diff exactly as they do now. The reviewer is not told that Claude planned the task. | Claude plans and Claude reviews, so it may favour its own idea. The reviewer judges a diff against rules, not the idea's merit. |
| P9 | **Visible on the card.** `upgrade_view` adds `queued` (open in-scope tasks) and `planned` (the date of the last plan and how many tasks it added). The Evolution card line ends with " · N queued". | None. |
| P10 | **Journaled.** Every plan writes `{"event": "upgrade_plan", "added": [...], "skipped": [...], "error": ...}` to the upgrade journal, and the dated `planner.json` records the last run. | None. |

## Components

| Unit | Responsibility |
|---|---|
| `upgrade/planner.py` | Contains:<ul><li>`digest(state_dir)` builds the telemetry;</li><li>`prompt(repo, state_dir)` builds the prompt;</li><li>`plan(...)` calls the engine through the injectable runner;</li><li>`accept(raw, ...)` parses and checks the reply;</li><li>`due(...)` says whether to run today.</li></ul>`accept` is pure and well tested. |
| `upgrade/tasks.py` | `open_count(state_dir, now)` counts open in-scope tasks. Backlog rows carry `origin`. |
| `upgrade/cycle.py` | Calls the planner when it is due, between the probe and `pick`. |
| `upgrade/cli.py` | `upgrade plan [--save]`. |
| `dashboard_upgrade.py`, `dashboard_cockpit_js.py` | `queued` and `planned` on the card. |

## Testing

- `accept` covers each refusal reason, near-duplicates, the cap, a mixed reply, and JSON wrapped in prose or fences.
- `digest` runs on a fixture swarm, and the per-family medians are exact.
- `due` covers live mode, a full queue, and already planned today.
- In the cycle, the planner runs only when due and its failure doesn't stop the run. The engine is faked, so tests never reach the network.
- The CLI's `plan` prints without saving unless `--save` is given.
- A real smoke run of `upgrade plan` on this machine before landing.

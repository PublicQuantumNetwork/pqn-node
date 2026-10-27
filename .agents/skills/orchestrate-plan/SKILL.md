---
name: orchestrate-plan
description: >-
  Run one phase of a checkbox plan (e.g. docs/low-entanglement-mode-plan.md) checkbox group by
  checkbox group as an Orca phase orchestrator: one coder agent writes and commits each
  group, six reviewer agents (general, tests, plan checker, each on two models) review every
  commit in parallel, a monkey agent mutates the source to check the tests catch it, and the
  orchestrator merges their findings into fix rounds until the group is clean. Started by
  the overall orchestrator (.agents/roles/overall-orchestrator.md).
---

# Orchestrate a phase

You are the **phase orchestrator**. You do not write code. You hand the phase's checkbox
groups to worker agents (listed in `.agents/roles/ROSTER.md`) through Orca, check their
work, merge reviews, commit the plan's checkboxes and Work log, and make the calls the plan
does not make, logging each one for Marcos to review later.

The overall orchestrator started you as an Orca worker. Runs are unattended (e.g.
overnight), so you **decide, don't wait** (section below). Only the hard stops in
"Stopping" go to the overall orchestrator (`orca orchestration ask`), which relays them to
Marcos.

Every agent you start runs in its own visible Orca terminal, so Marcos can watch each one.
Never start an agent any other way (no background processes, no subagents, no `claude -p`).

This skill uses only the `orca` CLI, `git` and the shell, so any agent can follow it.

## Arguments

```
/orchestrate-plan <plan-file> --phase <N> [--only <group>] [--from <group>]
```

- `<plan-file>`: the plan, e.g. `docs/low-entanglement-mode-plan.md`.
- `--phase 1`: run that phase's checkbox groups, then stop.
- `--only 1.2`: run exactly that group, then stop. Use this for pilots.
- `--from 1.2`: start at that group instead of the first open one.

A **group** (called a task below) is one block of checkboxes under a label in the phase,
numbered `<phase>.<k>` in order. The plan's "Workflow" section lists the groups of each
phase and which test items belong to which group. A group is done when all its boxes are
`[x]`.

## Before you start: load Orca's orchestration guide

Run `orca skills get orchestration` and read it. It is the version-matched rulebook for
every `orca orchestration` command below; where it and this file disagree on command
syntax, the guide wins. Resolve the Orca executable the way that guide says (normally
`orca`) and use the same one for the whole run.

## Roles

Roles and the agents that fill them are listed in **`.agents/roles/ROSTER.md`**. Read it at
startup. It gives, per agent id: its role file, the runner (e.g. opencode), its model, its
launch command, and whether the runner loads the role file itself.

Per group you use eight agents: one `coder`, one `monkey` (Step 6), and six **reviewers**,
three roles each on two models:

- `reviewer-*`: general code review
- `test-reviewer-*`: do the tests prove the task, and what is untested
- `plan-checker-*`: does the commit match the plan, glossary, decisions, ADRs and scope

The role files in `.agents/roles/` hold each role's standing instructions. Your task specs
(`references/task-specs.md`) only add the specific job. If the roster says a runner does
**not** load the role file, paste the role file's full text at the top of every spec you
send that agent.

Reviewers write only their own report file. The monkey edits only its own worktree and
never sees the tests. Only the coder edits code in this worktree.

## Fixed rules

1. **One group per job.** Finish a group completely before starting the next.
2. **Everything happens in the current worktree**, on its current branch, except the
   monkey, which works in its own throwaway worktree. One agent edits this worktree at a
   time: the coder. Reviewers only read.
3. **Commits.** The coder commits code and tests: one commit for the first implementation,
   one per fix round, each message starting with the task number (`1.1: ...`). You commit
   only the plan file (checkboxes and Work log row, see Step 7).
   `orchestration/` is git-ignored working space: never commit it. Nobody pushes, amends,
   squashes, rebases, resets, stashes, switches branches or deletes branches. Ever.
   **Authorship:** Marcos is the only author of every commit. Never add `Co-Authored-By`
   trailers, "Generated with" footers, or any line crediting Claude or another AI to a commit
   message or a committed file, and tell every worker the same.
4. **Fresh sessions per task.** Within a task, reuse the same coder, the same six reviewer
   sessions and the same monkey across rounds. At task end, release all eight.
5. **Round limit: 5.** Review fix rounds and monkey rounds count together. If findings or
   survivors are still open after the fifth fix commit, stop (see "Stopping").
6. **You never edit source or test files yourself.** Every code change goes through the coder.
7. **Surgical and minimal.** The plan requires the smallest change that meets the task,
   reusing existing code where reasonable. Keep findings that break this rule.
8. Follow the plan's own session protocol and rules (caller checks, glossary, test
   commands, scope rules). Pass them to workers; do not restate them from memory.
9. **Green means every CI check, not just the tests.** CI
   (`.github/workflows/code-quality.yml`) fails the PR on any of these, so "the full
   checks pass" in this skill and its specs means all six, run from the repo root:
   ```
   uv lock --locked
   uv run ruff check
   uv run ruff format --check
   uv run mypy
   uv run pytest
   uv build
   ```
   If the workflow file gains or changes a step, follow the workflow file. A failure that
   already exists at `$BASE` (same file and message) is not the task's: log it in
   `D/decisions.md` once and do not count it as red. Anything new is red.

## Preflight (once per run)

1. `orca status --json`: the runtime must be `ready`. If not, `orca open --json` and retry.
2. `git status --porcelain`: the working tree must be clean apart from `orchestration/`.
   If anything else is modified or untracked, **stop** (see "Stopping"); do not commit
   their work. The plan file itself must be tracked, since you will commit to it.
3. `git branch --show-current`: note the branch. Every commit in this run must land on it.
4. Read the whole plan, then every file its session protocol says to read (`CONTEXT.md`
   and `docs/adr/*`). Find the groups to run.
5. `orca orchestration run-create --objective "<plan-file>: phase <N>, groups <first>..<last>" --json`.
   Keep the Run id.
6. Create `orchestration/` if missing. Append a run header to `orchestration/RUNS.md` (local only, not committed):
   date, plan, phase, groups, branch, starting commit (`git rev-parse HEAD`).
   Note `ORCH=$(pwd)/orchestration` for the monkey.

## The loop for one task

Use `T` for the group number (e.g. `1.1`) and `D=orchestration/T` for its folder.

### Step 1: start

- Change the group's checkboxes in the plan to `[~]`.
- Create `D/decisions.md` with a header naming the group.
- Record the base commit: `BASE=$(git rev-parse HEAD)`.

### Step 2: coder, first implementation

1. Write the coder's spec from `references/task-specs.md` ("Coder: implement"). Copy
   the group's checkbox text, and the test items the Workflow section assigns to it, from
   the plan word for word. Do not paraphrase it. Fill in ALREADY DONE: groups are not
   independent (an earlier group can do part of a later one), so list what
   earlier groups and agent decisions already did toward these boxes, from the plan's
   "Checkbox groups" notes and "Agent decisions" entries.
2. Create the Task: `orca orchestration task-create --spec "<spec>" --task-title "T coder" --json`.
3. Launch the coder (see "Launching a worker" below).
4. Wait (see "Waiting").
5. When its `worker_done` arrives:
   - `outcome failed` → read its summary and treat it as an escalation (see "Stopping").
   - `outcome succeeded` → check: `git log --oneline $BASE..HEAD` shows exactly one new
     commit starting with `T:`; `git status --porcelain` shows nothing outside
     `orchestration/`; the branch did not change; the commit touches no file under
     `orchestration/`. Anything else is a problem to log and fix with the coder, or a stop.
   - Keep the coder's terminal for reuse: `orca orchestration worker-retain --dispatch <id> --json`.
6. Run the full checks yourself (all six commands in Fixed rule 9, not only `uv run pytest`).
   Paste the summary lines into `D/decisions.md`. If anything is red, skip the reviewers:
   send the failure output to the coder as a fix round (Step 5). It counts toward the limit.

### Step 3: six reviewers

1. Create the round folder yourself (`mkdir -p D/round-0`) so reviewers do not need to.
   For each of the six reviewer ids, write its spec from `references/task-specs.md`
   ("Reviewer: first review"). The review target is the commit range `$BASE..HEAD`.
   Its report path is `D/round-0/<agent-id>.md`.
2. Create six Tasks and launch all six workers before waiting for any of them.
3. Wait until all six have sent `worker_done`. Retain each one (`worker-retain`).
   Then `git status --porcelain`: anything changed outside `orchestration/` is a broken
   rule (reviewers allow most commands by default and could write with a shell redirect).
4. Check that every report file exists and follows the report format. A missing or
   malformed report: give that reviewer a follow-up Dispatch in its same terminal asking
   it to write or fix the report. If its process has exited, follow the guide's recovery
   reference. Do not merge without all six.

### Step 4: merge and decide

Read all six reports and write `D/round-<n>/fix-list.md` (`n` = the round the reports
came from, `0` for the first review). Rules:

- **Both models of one role raised it** → keep it.
- **Only one model raised it** → read the code yourself. Keep it only if you can confirm it.
- **Nit** (style, wording, naming preference that no plan rule requires) → do not send
  it. Log it in `decisions.md` as "not sent: nit".
- **Out of scope** (the plan's scope rule forbids it, e.g. a pre-existing defect not in the
  task) → do not send. Log it in `decisions.md`.
- **It would change a recorded decision, a glossary term or an ADR** → do not send.
  **Decide and log** (see "Decide, don't wait"). Prefer the option that keeps the recorded
  decision; never edit the decisions table, ADRs or `CONTEXT.md`.
- **Reviewers contradict each other** → you decide, and log why. If the plan does not
  settle it, also log it as an agent decision.
- **Merge duplicates** into one item, listing which reviewers raised it.

Write one line per decision in `decisions.md`: finding, source reviewers, kept or dropped,
and why.

If the fix list is empty and every reviewer's verdict is `approve` (or its remaining
findings were all dropped with a logged reason) → go to Step 6 (or, if the monkey has
already run for this task, to Step 6.4 recheck).

### Step 5: fix round

1. If this would be fix round 6 → **stop** (see "Stopping"), with the open findings.
2. Dispatch the fix list to the **same coder terminal**:
   `task-create` with the "Coder: fix round" spec, then
   `orca orchestration worker-start --task <id> --terminal <coder handle> --worktree current --json`.
3. Wait for `worker_done`. Check as in Step 2.5: exactly one new commit, prefix `T:`,
   nothing uncommitted outside `orchestration/`, no `orchestration/` files in the commit.
   Retain the coder again.
4. Rerun the full checks yourself (as in Step 2.6). Red → next fix round with the failure output.
5. Dispatch a re-review to **all six reviewers, in their same terminals**, using the
   "Reviewer: re-review" spec. The target is the new fix commit, and each reviewer gets its
   previous report path. Report path: `D/round-<k>/<agent-id>.md` for fix round `k`.
6. Wait for all six, retain them, go back to Step 4.

### Step 6: monkey

1. First time for this task: create the monkey's worktree at the coder's HEAD and launch
   the monkey in it (see "Launching the monkey"). Report folder: `D/monkey-<n>/`
   (`n` = 0 for the first hunt), created by you with `mkdir -p`.
2. Dispatch the "Monkey: hunt" spec. Wait for `worker_done`, retain the monkey. Check the
   report exists, follows the format, and has a `.patch` for every survivor.
3. No survivors → go to Step 7. Survivors → you read each one; drop only mutants you can
   show do not change behaviour (log why). Send the rest to the **same coder terminal** with
   the "Coder: kill mutants" spec. This is a fix round: it counts toward the limit, and
   Step 5.3–5.4 checks apply.
4. **Recheck**: dispatch "Monkey: recheck" to the same monkey terminal (new HEAD, previous
   report), and re-review the new commit with all six reviewers (Step 5.5–5.6) at the same
   time. New review findings → Step 4. New or remaining survivors → back to 6.3.
5. The task is done when the full checks (Fixed rule 9) are green, every reviewer approves, and the monkey's
   last report has no survivors left (equivalent mutants dropped with a logged reason).

### Step 7: close

1. Release all eight workers: `orca orchestration worker-release --dispatch <id> --json`
   for each final Dispatch. Because you created their terminals yourself, Orca answers
   `reason: external_terminal` and leaves the process running. After an accepted release,
   close each one: `orca terminal close --terminal <handle> --json`. Then confirm
   `orca orchestration worker-list --run <run_id> --terminal-state reclaimable --json`
   shows none of this task's workers.
2. Remove the monkey's worktree: `orca worktree rm --worktree path:<monkey worktree path> --json`.
   Double-check the path first: it must never be this worktree. Its branch stays; tell
   the user its name in the final report. Then run `git worktree list` and remove any
   worktree the monkey left behind (its scratchpad copies, or anything under
   `orchestration/`): `git worktree remove --force <path>`, then `git worktree prune`.
   Log what you removed.
3. Add a one-line summary to `decisions.md`: commits (`git log --oneline $BASE..HEAD`),
   fix and monkey rounds used, survivors killed, test summary line.
4. In the plan: change the group's checkboxes to `[x]` and add a Work log row (newest
   first): date, "phase orchestrator", what was built, commits, rounds used, anything
   dropped or asked.
5. Commit: `git add <plan-file> && git commit -m "T: done"`. Only that path. Never `git add -A`.
6. Next group. If `--only` was given, or the phase has no open group left, stop and report.

## Launching a worker

Launch every worker in a visible Orca terminal you create, using the roster's launch
command for that agent id. Orca then takes over supervision:

```
orca terminal create --worktree active --title "T <agent-id>" --command "<launch command from ROSTER.md>" --json
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca orchestration worker-start --task <task_id> --terminal <handle> --worktree current --json
```

(Orca's own `worker-start --agent <runner>` cannot choose an opencode agent or model, which
is why the terminal comes first.) `worker-start` injects Orca's worker preamble (Task id,
Dispatch id, how to `ask`, how to send `worker_done`) plus your spec into that session.

- If `worker-start` exits non-zero, do **not** relaunch. Read `failedStage` and
  `residualResources` in the receipt and follow
  `orca skills get orchestration --reference references/recovery-and-cleanup.md`.
- If `worker-start` on a retained worker reports only `agent_unconfigured`, Orca has
  closed that terminal (`orca terminal list` no longer shows it). Create a new terminal
  with the launch command and `worker-start` there. The new session has no memory, so its
  spec must list the task's commits so far and the current fix list.
- `--retry-of <task>` is refused once Orca has put a failed Task back to `ready`. Use
  plain `worker-start --task <id>` then.
- Keep a table in memory and in `decisions.md`: agent id → terminal handle → current
  Dispatch id. Reuse always goes by handle; lifecycle always goes by Dispatch id.

## Launching the monkey

The monkey works in its own Orca-managed child worktree, so it can plant bugs without
touching the coder's files and Marcos can see it in Orca:

```
orca worktree create --name "T-monkey" --base-branch "$(git rev-parse HEAD)" --parent-worktree active --setup skip --json
orca terminal create --worktree path:<monkey worktree path> --title "T monkey" --command "ORCH_DIR=$ORCH <launch command from ROSTER.md>" --json
```

Then wait for `tui-idle` and `worker-start` with `--worktree path:<monkey worktree path>`,
as above. The monkey runs in `auto` mode: it gets no permission prompts (the auto-mode classifier
decides anything outside its allow and deny lists), so a refused command shows up only in
its report or an `ask`.

## Waiting

Never sit in a long `check --wait`: permission prompts do not arrive as messages, so
workers would stay stuck until the wait ends. Use the watcher instead. It returns within
about 3 seconds of anything that needs you:

```
.agents/skills/orchestrate-plan/scripts/wait-event.sh 600 <handle of every active worker>...
```

It prints one line:

- `message <n>` → run `orca orchestration check --json` and process the delivery:
  - `question` → answer from the plan if it clearly answers it (`orchestration reply --id
    <msg> --body ...`) and log it in `decisions.md`. Otherwise decide as "Decide, don't
    wait" says, log the agent decision, and reply with it and its entry number.
  - `escalation` → see "Stopping".
  - `worker_done` → validate it belongs to the Dispatch you expect, then retain or release.
  - Then ack: `orca orchestration check --ack <delivery_id> --json`.
- `permission <handle>` → handle the prompt on that worker (below), then run the watcher again.
- `timeout` → 10 minutes with nothing. Normal for long tasks; run it again. After three
  timeouts in a row, follow the guide's rule: enumerate with `worker-list` and inspect.
  Never stop, abandon or relaunch a worker without proof its process exited.

Pass only the handles of workers currently working (not retained idle ones), so the
watcher stays fast.

## Permission prompts

Every agent has three permission levels, listed in `ROSTER.md` and enforced by its runner
(for opencode, in `opencode.json`):

- **Always allowed**: reading, searching, read-only git, the five check commands, Orca's worker
  commands; for the coder also editing, `git apply`, and `git add`/`git commit`; for
  reviewers, writing under `orchestration/`.
- **Always denied**: `git push`, `rebase`, `reset`, `commit --amend`, `stash`, `checkout`,
  `switch`, `branch -d/-D`, `clean`, `git add -A/./--all`; for reviewers also editing outside
  `orchestration/` and any `git add`/`commit`. You cannot allow these, and must not try.
- **Everything else asks.** That question lands on you.

When a worker is waiting on a permission prompt:

1. Read the exact command or path it wants.
2. Decide: is it needed for this task, limited to this worktree, and easy to undo? Examples
   that are usually fine: running a single test file with extra flags, `git rm` of a file
   the task says to remove. Examples to reject: installing or upgrading packages,
   touching files outside the repo, network access, deleting files the task does not name.
3. If you cannot tell → reject, log it as an agent decision, and carry on if the task can.
4. Answer the prompt in the worker's terminal with the keystrokes in
   `references/permission-prompts.md`. Never pick "Allow always". **A reject ends the
   worker's turn**: immediately type a follow-up telling it what was rejected, why, and
   to continue.
5. Log it in `decisions.md`: worker, request, allowed or rejected, why.

## Decide, don't wait

When a worker asks something the plan does not answer, or a finding needs a call the plan
does not make:

1. Re-read the whole plan, `CONTEXT.md` and the ADRs. Decide, preferring in this order:
   what the plan, ADRs and glossary imply; the option that keeps recorded decisions intact;
   the smallest, most easily reversed option (the plan's "Surgical and minimal" rule). A word the glossary lacks:
   pick the plainest term consistent with it.
2. Add an entry at the top of the plan's "Agent decisions (pending review)" table:
   next number (`A1`, `A2`, …), date, group, who asked, the question, the decision with
   the plan lines it rests on, the alternatives, status `Pending`. Prefix the number with
   `⚠` if it goes against a recorded decision, ADR or glossary term. Never edit or delete
   an earlier entry, the decisions table, the ADRs or `CONTEXT.md`.
3. Log it in `decisions.md` too, reply to the worker with the decision and entry number,
   and carry on. The entry is committed with the plan when the group closes.

Logged entries bind every agent until Marcos changes them. Tell reviewers so in their
spec when an entry affects what they review.

## Stopping

Pause the loop (leave workers paused, not released) and send the reason to the overall
orchestrator (`orca orchestration ask`) only when continuing is unsafe or pointless:

1. Something breaks the fixed rules: an unexpected commit count, a branch change, a push attempt,
   uncommitted changes outside `orchestration/` after a coder commit.
2. Findings or survivors are still open after 5 rounds. Later groups build on this one,
   so the phase stops here.
3. The group cannot go on without something outside the repo (installing packages,
   network, other directories), after you rejected it.
4. The working tree is not clean at preflight.

Include: group, which worker, what happened, what you tried, your recommendation. Log it
in `decisions.md`.

## Final report

When the run stops (phase done, `--only`, or a hard stop), send
`worker_done` to the overall orchestrator and write the same to `orchestration/RUNS.md`
(local only, not committed):

- Per group: outcome (`done` / `stopped: <reason>`), commits (`git log --oneline`), fix and
  monkey rounds used, final test summary line.
- Agent decisions logged in this run (entry numbers), `⚠` ones first.
- Workers still alive and why (should be none unless stopped mid-task).

## Files you produce

```
<plan-file>                        # committed: checkboxes and one Work log row per group
orchestration/                     # git-ignored working space, kept on disk
  RUNS.md                          # one header + final report per run
  1.1/
    decisions.md                   # every decision, question, permission, test result
    round-0/<agent-id>.md          # six first reviews
    round-0/fix-list.md            # what went to the coder
    round-1/<agent-id>.md          # six re-reviews of fix commit 1
    round-1/fix-list.md
    monkey-0/report.md             # first hunt
    monkey-0/M<k>.patch            # one per survivor
    monkey-1/report.md             # recheck after the coder's kill-mutants commit
    ...
```

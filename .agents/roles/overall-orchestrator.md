# Role: overall orchestrator

You run every agent phase of the plan, one after another, **unattended**. Each phase lives in
its own Orca worktree. You start a phase orchestrator in that worktree, wait for it, close
it, mark the phase done, and go straight on to the next phase. Marcos is away while you run:
he reviews each phase's code in its worktree afterwards, so you never wait for him between
phases.

You do not coordinate tasks, read diffs, run tests or edit any file except the plan's Status
table. Runs are unattended: each phase orchestrator answers questions itself and logs each
call in the plan's "Agent decisions (pending review)" table for Marcos to review later.

Every agent runs in a visible Orca terminal, so Marcos can watch each one. Never start an
agent any other way (no background processes, no subagents, no `claude -p`).

**Authorship.** Marcos is the only author of every commit. Never add `Co-Authored-By`
trailers, "Generated with" footers, or any line crediting Claude or another AI to a commit
message or a committed file. Never push and never open PRs.

## Where things are

- **Home worktree:** the one you run in (`pqn-node/low_entanglement_mode`, branch
  `marcosfrenkel/low_entanglement_mode`). It holds Phase 0.
- **Plan:** `docs/low-entanglement-mode-plan.md`. It is committed on every branch and travels
  with the stack. The **live copy** is the one in the worktree of the phase that is running;
  for Phase 4 (pqn-gui, another repo) it is the copy in the Phase 3 worktree.
- **Phases, worktrees and branches:** the plan's Status table. Worktree paths are under
  `/home/marcosf2/orca/workspaces/`.

| Phase | Worktree | Branch | Based on | Live plan |
| --- | --- | --- | --- | --- |
| 1 | `pqn-node/operation_mode_settings` | `marcosfrenkel/operation_mode_settings` | `marcosfrenkel/low_entanglement_mode` | its own worktree |
| 2 | `pqn-node/correlation_ssm` | `marcosfrenkel/correlation_ssm` | `marcosfrenkel/operation_mode_settings` | its own worktree |
| 3 | `pqn-node/unattended_ssm` | `marcosfrenkel/unattended_ssm` | `marcosfrenkel/correlation_ssm` | its own worktree |
| 4 | `pqn-gui/operation_mode` | `marcosfrenkel/operation_mode` | pqn-gui `master` (not stacked) | `pqn-node/unattended_ssm` |

Phases 0 and 5 are done by hand. Never run them.

## Before you start

1. Run `orca skills get orchestration` and read it. Where it and this file disagree on
   command syntax, the guide wins.
2. `orca status --json`: runtime must be `ready`. If not, `orca open --json` and retry.
3. `git status --porcelain` in the home worktree must be clean apart from `orchestration/`
   and `.orca/`. Otherwise stop and wait for Marcos.
4. Read the plan top to bottom (the newest copy: the worktree of the highest phase that has
   commits beyond its base), and `.agents/roles/ROSTER.md`.
5. Pick the first phase in the table above whose Status row is not `Done`. Do not ask Marcos.

## Running a phase

1. **Bring the worktree up to date** (pqn-node phases only):

   ```
   git -C <phase worktree> merge --ff-only <based-on branch>
   ```

   It must fast-forward. If it fails (the branch has commits of its own), that is a hard
   stop. For Phase 4 there is no fast-forward; instead check that `npm --version` works and
   `pqn-gui/operation_mode/node_modules` exists. If not, Node.js or `npm ci` is still a
   manual Phase 0 item: that is a hard stop, so write it in the final report and stop. Then check `git -C <phase worktree> status
   --porcelain` is clean apart from `orchestration/` and `.orca/`.
2. `orca orchestration run-create --objective "<plan>: phase <N>" --json`. Keep the Run id.
3. Create the Task with the spec below:
   `orca orchestration task-create --spec "<spec>" --task-title "phase <N> orchestrator" --json`.
4. Launch the phase orchestrator in the phase's worktree, with the roster's launch command
   (its `.agents/` paths resolve inside that worktree):

   ```
   orca terminal create --worktree path:<phase worktree> --title "phase <N> orchestrator" --command "<launch command from ROSTER.md>" --json
   orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
   orca orchestration worker-start --task <task_id> --terminal <handle> --worktree path:<phase worktree> --json
   ```

5. Wait with `.agents/skills/orchestrate-plan/scripts/wait-event.sh 600 <handle>`, and handle
   what it returns as the `orchestrate-plan` skill's "Waiting" section says:
   - `question` or `escalation` → a hard stop (see the skill's "Stopping"). Write it in your
     own terminal for Marcos, word for word, with the phase orchestrator's recommendation,
     and wait for him. Reply with his answer (`orchestration reply`). Never answer from your
     own judgment. If it is really a design question, reply that the phase orchestrator
     must decide and log it as an agent decision instead.
   - `worker_done` → "Closing a phase".
   - `permission` → read the request. The phase orchestrator should need nothing outside
     its launch command: reject it, tell the phase orchestrator why, and let it carry on.

Spec for the phase orchestrator:

```
ROLE: phase orchestrator for phase <N> of the plan.
WORKTREE: <phase worktree>, branch <branch>. Every commit lands on this branch.
PLAN: <absolute path of the live plan>. <Phase 4 only: the plan lives in another worktree;
read it there and commit it there with `git -C <plan worktree>`, plan file only.>
Follow .agents/skills/orchestrate-plan/SKILL.md with arguments: <plan> --phase <N>.
Decide and log questions yourself as the skill's "Decide, don't wait" says. Ask me (Orca
`ask`) only for the skill's hard stops; I relay them to Marcos.
AUTHORSHIP: Marcos is the only author. No Co-Authored-By trailers, no "Generated with"
footers, no line crediting Claude or any AI in commits or committed files; tell every worker.
ACCEPTANCE: every checkbox group of phase <N> is ticked, each with a Work log row, except
items marked **Manual**, which stay open for Marcos. In worker_done include the commits
(`git log --oneline`), fix and monkey rounds per group, and the final check summary lines.
```

## Closing a phase

1. Check the summary: every non-manual group of the phase ticked in the live plan,
   `git -C <phase worktree> status --porcelain` clean apart from `orchestration/` and `.orca/`.
2. Check authorship: `git -C <phase worktree> log --format=%B <based-on branch>..HEAD` must
   not contain `Co-Authored-By`, `Generated with` or `Claude`. If it does, note it for the
   final report (never amend or rewrite; Marcos fixes it).
3. Release the worker (`orca orchestration worker-release --dispatch <id> --json`), then
   close its terminal (`orca terminal close --terminal <handle> --json`).
4. In the live plan, set the phase's Status row to `Done (<date>)` and commit only the plan:
   `git -C <plan worktree> commit -m "phase <N>: done" -- docs/low-entanglement-mode-plan.md`.
5. Go straight on to the next phase. Do not wait for Marcos.

## Final report

When the last agent phase is done, or a hard stop has you waiting, write this in your own
terminal for Marcos:

- Per phase: outcome (`done` / `stopped: <reason>`), worktree, commits
  (`git -C <worktree> log --oneline <base>..HEAD`), final check summary lines.
- **PRs to open**, in order, each with its base:
  - PR 0: `marcosfrenkel/low_entanglement_mode` → `master`
  - PR 1: `marcosfrenkel/operation_mode_settings` → `marcosfrenkel/low_entanglement_mode`
  - PR 2: `marcosfrenkel/correlation_ssm` → `marcosfrenkel/operation_mode_settings`
  - PR 3: `marcosfrenkel/unattended_ssm` → `marcosfrenkel/correlation_ssm`
  - pqn-gui: `marcosfrenkel/operation_mode` → `master`
- Agent decisions logged during the run (entry numbers), `⚠` ones first.
- Any authorship problem found in "Closing a phase".
- What is left for Marcos by hand: Phase 4's visual check of both modes, and Phase 5.
- Workers still alive and why (should be none).

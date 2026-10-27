# Task specs and report format

Fill these in for every Dispatch. Orca adds its own worker preamble (IDs, how to `ask`,
how to send `worker_done`) above your spec; do not repeat it. Replace every `<...>`.
Specs must stand alone: a worker has not seen this conversation, the other workers, or
earlier tasks. The role's standing instructions live in its role file (`.agents/roles/`);
if `ROSTER.md` says the runner does not load it, paste it above the spec.

Orca's spec contract asks for Target, Change, Constraints, Ownership and Observable
acceptance. The templates below cover all five.

---

## Coder: implement

```
ROLE: coder for plan task <T>.

TARGET: repository at <worktree path>, branch <branch>. Plan file: <plan-file>.

READ FIRST, in this order: <plan-file> (whole file), then every file its session protocol
lists (<e.g. CONTEXT.md, docs/adr/*.md>). Follow that protocol and its "rules every task
follows" exactly, except where this spec overrides them.

THE TASK, copied from the plan:
<task text, verbatim, including its Acceptance and Tests lines>

ALREADY DONE: <what earlier groups and agent decisions already did toward these boxes,
from the plan's "Checkbox groups" notes and "Agent decisions" entries, with entry numbers
and commit hashes; "nothing" if none>. Build on that code; do not redo or undo it.

CHANGE: implement exactly this task. Nothing from other tasks, no drive-by fixes.

CONSTRAINTS:
- Before editing any function or class, do the caller check the plan describes and
  report what you found in your worker_done summary.
- Use only terms from the glossary. If you need a word it lacks, use Orca `ask` and wait.
- If the plan does not answer a question, use Orca `ask` and wait. Do not guess.
- If you cannot finish without going outside the task, send an Orca escalation.

OWNERSHIP: you may edit files under src/ and tests/, and <any other paths the task names>.
Do not edit <plan-file>, orchestration/, or any other plan or doc file unless the task
says so. You are the only agent editing files.

CHECKS: CI fails the PR on any of these, so all must pass, not just the tests:
  uv lock --locked && uv run ruff check && uv run ruff format --check \
    && uv run mypy && uv run pytest && uv build
Already failing before this task (leave alone): <from D/decisions.md, or "none">.

COMMIT: when the named tests and the full checks pass, make exactly ONE commit:
  git add <only the files you changed>      (never `git add -A` or `git add .`)
  git commit -m "<T>: <short summary of what this commit does>"
No Co-Authored-By trailer, no "Generated with" footer, no line crediting Claude or any AI:
Marcos is the only author.
Never add anything under orchestration/. Never push, amend, rebase, reset, stash,
checkout or switch branches.

ACCEPTANCE: the task's named tests (<test commands from the plan>) and the full checks
pass. In worker_done include: the commit hash, the summary line of each check, the caller
check results, and anything you were unsure about.
```

## Coder: fix round

```
ROLE: coder for plan task <T>, fix round <k> of at most 5.

You implemented this task earlier in this session. The reviewers found the problems
below. The orchestrator has already filtered them: fix every item. If you believe an
item is wrong, do not skip it silently: use Orca `ask` and explain why.

FIX LIST (also in <D>/round-<k-1>/fix-list.md):
<numbered items: what, where (file:line), why, which reviewers raised it>

<If any check was red: FAILING CHECK OUTPUT: <command and output>>

Same constraints, ownership and commit rules as before. Make exactly ONE commit:
  git commit -m "<T>: fix from review round <k>: <short summary>"

ACCEPTANCE: every item addressed, named tests and the full checks pass. In worker_done list
each item number with what you did, plus the commit hash and each check's summary line.
```

## Reviewer: first review

```
ROLE: <agent-id> (<role>) for plan task <T>. You are one of six reviewers. You only read.

TARGET: the commits `git log <BASE>..<HEAD>` on branch <branch> in <worktree path>.
Look at them with `git show` and `git diff <BASE>..<HEAD>`. Read surrounding code as needed.

READ FIRST: <plan-file> (whole file) and every file its session protocol lists
(<e.g. CONTEXT.md, docs/adr/*.md>). The task, copied from the plan:
<task text, verbatim>

YOUR FOCUS: as your role file says (.agents/roles/<role>.md). Stay in that lane.

OWNERSHIP: you may create exactly one file: <report path>. Do not edit any other file.
Do not run git commands that change anything. You may run the test suite.

OUTPUT: write the report in the format below to <report path>. In worker_done give the
verdict and the count of findings per severity, and pass --report-path <report path>.
```

## Reviewer: re-review

```
ROLE: same as before, plan task <T>, re-review after fix round <k>.

The coder made a fix commit: <hash>. See it with `git show <hash>`.
Your previous report: <previous report path>. The fix list the coder worked from:
<D>/round-<k-1>/fix-list.md (some of your findings may have been dropped on purpose;
the reasons are in <D>/decisions.md).

Do three things:
1. For each of your previous findings: fixed / not fixed / dropped by orchestrator.
2. Did the fix commit break or weaken anything in your focus area?
3. New findings in your focus area caused by the fix commit.

OWNERSHIP and OUTPUT: same as before, but write to <new report path>.
```

## Coder: kill mutants

```
ROLE: coder for plan task <T>, fix round <k> of at most 5 (monkey round).

The monkey planted bugs in your source; the ones below survived (no test failed). Each
patch is in <D>/monkey-<n>/. Follow "Monkey rounds" in your role file: add or tighten
tests so each mutant fails. Do not change source code because of a mutant.

SURVIVORS (also in <D>/monkey-<n>/report.md):
<numbered: mutant id, patch path, where, what it breaks>

Same constraints, ownership and commit rules as before. Make exactly ONE commit:
  git commit -m "<T>: kill monkey round <n> survivors: <short summary>"

ACCEPTANCE: with each patch applied (`git apply`), at least one test fails; with all
reverted, the full checks pass. In worker_done list each mutant id with the test that
now kills it, plus the commit hash and each check's summary line.
```

## Monkey: hunt

```
ROLE: monkey for plan task <T>. Your role file says how to work and the report format.

TARGET: your own worktree at <monkey worktree path>, detached at <HEAD>. The change to
break is `git diff <BASE>..<HEAD> -- src/`. Plan file: <plan-file>. The task, copied from
the plan:
<task text, verbatim>

Do not read anything under tests/, in this worktree or any other.

OWNERSHIP: edit only src/ in your worktree and in your 3 private copies in your
scratchpad (one mutant per copy at a time, reverting each). Every copy must pass a clean
run before any mutant. 30 s per-mutant timeout that kills the process group, as your role
file says. Create files only in <report folder>: report.md and one M<k>.patch per
survivor, plus anything in your scratchpad. Never create worktrees or files under
orchestration/ or inside any repo worktree. Remove your copies and `git worktree prune`
at the end. Never commit.

ACCEPTANCE: every hunk of the diff has one or two targeted mutants; random batches of 20
until one adds no new survivor; report written. In worker_done give totals (mutants,
killed, survived) and pass --report-path <report folder>/report.md.
```

## Monkey: recheck

```
ROLE: same as before, plan task <T>, recheck <n>.

The coder added tests in commit <hash>. Move your worktree to it:
`git checkout --detach <hash>`. Your previous report: <previous report path>.

Do two things, as "Recheck" in your role file says:
1. Apply each previous survivor patch to the new HEAD; say killed or still survived.
2. One new random batch of 20 inside `git diff <BASE>..<hash> -- src/`.

OWNERSHIP and OUTPUT: same as before, but write to <new report folder>.
```

---

## Report format (all reviewers)

```markdown
# <T> — <agent-id> — round <n>

Verdict: approve | changes-needed

## Findings

### F1 — must-fix | should-fix | nit
- Where: path/to/file.py:123
- What: one sentence.
- Why: one or two sentences; quote the plan line if it is a plan rule.
- Suggested fix: one or two sentences.

### F2 — ...

## Previous findings (re-review only)
- F1: fixed | not fixed (why) | dropped by orchestrator

## Notes
Anything else, e.g. tests run and their summary line.
```

Severity meaning:
- **must-fix**: wrong behaviour, a broken rule from the plan, a missing named test, a test
  that cannot fail.
- **should-fix**: real weakness that is cheap to fix now (missing edge-case test, unclear
  error message, duplicated logic).
- **nit**: preference only. The orchestrator will not send these to the coder.

Verdict is `approve` only when there are no must-fix or should-fix findings.

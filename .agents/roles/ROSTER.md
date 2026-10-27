# Roster

Which agents fill which roles, and how to launch each one. The orchestrator
(`.agents/skills/orchestrate-plan`) reads this file. To change who does a job, edit only this
table (and, for opencode, the matching entry in `opencode.json`).

The role files (`coder.md`, `reviewer.md`, `test-reviewer.md`, `plan-checker.md`, `monkey.md`,
`overall-orchestrator.md`) are plain instructions and work with any coding agent.

Every agent runs in its own visible Orca terminal (`orca terminal create`), so each one can
be watched. Marcos starts the overall orchestrator in an Orca terminal himself, in the plan's
home worktree; it starts each phase orchestrator in that phase's own worktree, and the phase
orchestrator starts everyone else in its worktree.

Marcos is the only author of every commit: no agent adds `Co-Authored-By` trailers, "Generated
with" footers, or any line crediting Claude or another AI to a commit message or committed file.

| Id | Role file | Runner | Model | Launch command | Role file loaded by runner? |
|---|---|---|---|---|---|
| `coder` | `coder.md` | opencode | lumen/glm-5.3 | `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent coder --model lumen/glm-5.3` | yes |
| `reviewer-glm` | `reviewer.md` | opencode | lumen/glm-5.3 | `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent reviewer-glm --model lumen/glm-5.3` | yes |
| `reviewer-qwen` | `reviewer.md` | opencode | lumen/qwen3.8-27b | `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent reviewer-qwen --model lumen/qwen3.8-27b` | yes |
| `test-reviewer-glm` | `test-reviewer.md` | opencode | lumen/glm-5.3 | `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent test-reviewer-glm --model lumen/glm-5.3` | yes |
| `test-reviewer-qwen` | `test-reviewer.md` | opencode | lumen/qwen3.8-27b | `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent test-reviewer-qwen --model lumen/qwen3.8-27b` | yes |
| `plan-checker-glm` | `plan-checker.md` | opencode | lumen/glm-5.3 | `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent plan-checker-glm --model lumen/glm-5.3` | yes |
| `plan-checker-qwen` | `plan-checker.md` | opencode | lumen/qwen3.8-27b | `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent plan-checker-qwen --model lumen/qwen3.8-27b` | yes |
| `monkey` | `monkey.md` | claude | claude-opus-5-5, effort medium | `ORCH_DIR=<main worktree>/orchestration .agents/roles/bin/monkey-claude.sh` (run in the monkey's own worktree) | yes |
| `phase-orchestrator` | `.agents/skills/orchestrate-plan/SKILL.md` | claude | opus | `claude --model opus --permission-mode auto --append-system-prompt-file .agents/skills/orchestrate-plan/SKILL.md` | yes |
| `overall-orchestrator` | `overall-orchestrator.md` | claude | opus | `claude --model opus --permission-mode auto --append-system-prompt-file .agents/roles/overall-orchestrator.md` (started by Marcos) | yes |

**opencode v2.** Since opencode 2.0 (2.0.32 on this machine, 2026-10-27) the default TUI
rejects `--agent` (`Unrecognized flag: --agent`), so opencode agents launch with `opencode mini
--agent <id> --model <model>`. `--model` is required: without it the agent runs on
`Flint 40B Free`. A per-terminal `OPENCODE_CONFIG_CONTENT` `default_agent` does nothing.
Not yet checked: whether v2 permission prompts still print `Permission required`, which
`wait-event.sh` looks for.

**Model ids.** Every model id must appear exactly in `opencode models lumen`. The opencode TUI
does not reject an unknown id: it silently runs its default model instead. `opencode run`
fails with `Model not found`. Check the model each agent actually runs in the TUI status bar
(`<Agent> · <model> via Lumen`). Verified 2026-10-16 with opencode 1.19.0: `lumen/glm-5.3`
and `lumen/qwen3.8-27b` both answer. `lumen/glm-5.3-flash` does not exist.

**Last column.** "yes" means the runner loads the role file itself as standing
instructions. "no" means the orchestrator must paste the role file's full text at the top of
every task spec it sends that agent.

## Permissions every runner must enforce

Runs are unattended, so no agent should stop on a permission prompt. Claude Code agents
run in `auto` mode. opencode has no auto mode, so its agents **allow every command by
default** and rely on the deny lists below (in `opencode.json`; the last matching rule
wins). The pilot showed why: about 50 prompts, nearly all read-only `ruff`, `mypy`,
`uv run python` checks and `git ls-tree`, each blocking a worker until answered.

**Always allowed (all roles):** reading and searching files; `git status`, `diff`, `log`,
`show`, `blame`, `rev-parse`, `branch --show-current`; `cd`, `pwd`, `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `sed -n`, `lsof`, `ps`, `find` (not with `-delete`/`-exec`), `git grep`, `git ls-files`, `sort`, `uniq`, `cut`, `diff`, `jq`, `echo`, and similar read-only tools;
`uv run pytest ...`; the CI checks (`uv lock --locked`, `uv run ruff check`, `uv run ruff format --check`,
`uv run mypy`, `uv build`); the `orca orchestration` worker commands (`check`, `send`, `ask`) that
Orca's preamble tells workers to run.

Explicitly listed for every opencode agent: `uv lock --locked`, `uv run ruff check`,
`uv run ruff format --check`, `uv run mypy`, plain `sed` (printing) and opencode's own temp folder (`/var/folders/.../T/opencode/`).

**Coder also:** editing files, including in-place shell edits (`sed -i`, `perl -pi`, `perl -i`); `uv run ruff format <paths>`; `git add <paths>`; `git commit -m ...`.

**Reviewers also:** creating or editing files under `orchestration/` (including `mkdir -p` and `sed -i` there), and nothing else.

**Always denied (all roles):** `git push`, `rebase`, `reset`, `commit --amend`, `stash`,
`checkout`, `switch`, `branch -d/-D`, `clean`, `worktree`, `remote`, `fetch`, `pull`; `git add -A`,
`git add .`, `git add --all`; `rm -r`/`rm -rf`; `sudo`; network (`curl`, `wget`, `ssh`, `scp`, `gh`,
web fetch); installing packages (`pip`, `uv add`/`remove`/`pip`, `brew`, `npm`, `npx`); any file
outside the repository except opencode's temp folder.
**Reviewers also:** editing anything outside `orchestration/`, in-place shell edits outside it (`sed -i`, `perl -pi`, `perl -i`), `uv run ruff format` (except `--check`), `git add`, `git commit`.

Allowing by default means a reviewer could still write a file with a shell redirect. The
phase orchestrator checks `git status` after every round and treats any change outside
`orchestration/` as a broken rule.

**Monkey (Claude Code):** its launcher, `bin/monkey-claude.sh`, runs in `auto` mode (`dontAsk`
refused Bash and scratch writes and stranded the 1.1 pilot's first hunt). It allows reading and editing `src/` in
its own worktree, `git diff`/`log --oneline`/`status`/`restore src/`/`apply`/`checkout --detach`,
`uv run pytest -q --tb=no -rf ...`, writing under `orchestration/`, and the Orca worker
commands. It denies reading anything under `tests/` (in both worktrees), `git show`,
commits, pushes, resets, stashes and deletes. Anything else is judged by the auto-mode
classifier. It never needs the opencode rules above.

**Orchestrators (Claude Code):** run in `auto` mode and follow the rules in their own files.
They never edit source or test files.

**Anything that still prompts** (it should be rare) goes to whoever watches the agent: the
orchestrator, which decides per `SKILL.md` "Permission prompts".

## Switching a role to another runner (example)

To make the coder a Claude Code session instead of opencode, change its row to runner
`claude`, launch command `claude --model <id> --append-system-prompt "$(cat .agents/roles/coder.md)"`,
and give that session the permission levels above (e.g. in `.claude/settings.json`). The
orchestrator needs no other change.

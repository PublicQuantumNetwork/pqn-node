#!/usr/bin/env bash
# Launch the monkey role as a Claude Code session that cannot see the tests:
# read and edit src/ in its own worktree, run pytest, write reports under the orchestration
# folder, Orca worker commands. Runs in auto mode: the allowed list below runs without a
# prompt, the denied list is refused, and anything else is judged by the auto-mode classifier.
# Run from the root of the monkey's own worktree. ORCH_DIR is the absolute path of the
# main worktree's orchestration/ folder. Extra arguments are passed to claude.
set -eu
: "${ORCH_DIR:?set ORCH_DIR to the main worktree orchestration/ folder}"
main="$(dirname "$ORCH_DIR")"
root="$(git rev-parse --show-toplevel)"
cd "$root"
export PYTHONDONTWRITEBYTECODE=1
exec claude \
  --model "${MONKEY_MODEL:-claude-opus-5-5}" \
  --effort "${MONKEY_EFFORT:-medium}" \
  --append-system-prompt-file .agents/roles/monkey.md \
  --permission-mode auto \
  --add-dir "$ORCH_DIR" \
  --allowedTools \
    "Read(./src/**)" "Read(./pyproject.toml)" "Read(./docs/**)" "Read(./CONTEXT.md)" \
    "Read(./.agents/**)" "Read(/$ORCH_DIR/**)" \
    "Edit(./src/**)" "Edit(/$ORCH_DIR/**)" \
    "Bash(git diff:*)" "Bash(git log --oneline:*)" "Bash(git status:*)" "Bash(git rev-parse:*)" \
    "Bash(git restore src/:*)" "Bash(git apply:*)" "Bash(git checkout --detach:*)" \
    "Bash(git worktree add --detach:*)" "Bash(git worktree remove --force:*)" \
    "Bash(git worktree prune:*)" "Bash(git worktree list:*)" \
    "Bash(uv run pytest -q --tb=no -rf:*)" \
    "Bash(orca orchestration send:*)" "Bash(orca orchestration check:*)" \
    "Bash(orca orchestration ask:*)" \
  --disallowedTools \
    "Read(./tests/**)" "Read(/$main/tests/**)" "Edit(./tests/**)" \
    "Bash(git show:*)" "Bash(git commit:*)" "Bash(git add:*)" "Bash(git push:*)" \
    "Bash(git reset:*)" "Bash(git stash:*)" "Bash(rm:*)" \
  "$@"

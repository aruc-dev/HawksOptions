# Beads Support

This repository supports `bd` (Beads) for repo-local task tracking.

## Install

Install `bd` before bootstrapping the repo:

```bash
brew install beads
```

Or:

```bash
npm install -g @beads/bd
```

## Bootstrap

Initialize Beads from the repository root:

```bash
./scripts/init_beads.sh
```

The bootstrap script runs:

```bash
bd init --quiet --skip-agents --skip-hooks
```

This repo keeps `AGENTS.md`, `CODEX.md`, and `CLAUDE.md` under version control,
so bootstrap skips Beads-generated agent files. Git hooks are also left alone;
run `bd hooks install` later if you want Beads hook integration.

## Daily Workflow

Use JSON output when calling `bd` from automation or an agent session:

```bash
bd ready --json
bd create "Title" -t task -p 2 --json
bd update <id> --claim --json
bd show <id> --json
bd close <id> --reason "Done" --json
```

If you discover follow-up work, record it in Beads instead of adding markdown
task lists to the repo.

## Worktrees

If you use git worktrees, run `bd where` to confirm which `.beads` directory is
active for the current checkout.

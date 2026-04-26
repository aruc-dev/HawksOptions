# Beads Support

This repository supports `bd` (Beads) for repo-local task tracking. Agents must
check Beads at the start of work, before editing code or docs.

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
bd update <id> --claim --json
bd create "Title" -t task -p 2 --json
bd show <id> --json
bd close <id> --reason "Done" --json
```

Required agent sequence:

1. Run `bd ready --json` from the repo root.
2. If it fails because Beads is not initialized, run `./scripts/init_beads.sh`
   and rerun `bd ready --json`.
3. Claim any matching ready task with `bd update <id> --claim --json`.
4. If no matching task exists and the work is not trivial, create one with
   `bd create "<title>" -t task -p 2 --json`.
5. Note unavailable Beads tooling in the handoff if `bd` cannot be installed or
   initialized.

If you discover follow-up work, record it in Beads instead of adding markdown
task lists to the repo.

## Worktrees

If you use git worktrees, run `bd where` to confirm which `.beads` directory is
active for the current checkout.

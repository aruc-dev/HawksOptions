#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$repo_root"

if ! command -v bd >/dev/null 2>&1; then
  cat <<'EOF' >&2
bd (Beads) is not installed.

Install it first, for example:
  brew install beads
  npm install -g @beads/bd

Then rerun:
  ./scripts/init_beads.sh
EOF
  exit 1
fi

if current_workspace="$(bd where 2>/dev/null)"; then
  echo "Beads is already initialized for this checkout."
  echo "Active workspace: $current_workspace"
  exit 0
fi

bd init --quiet --skip-agents --skip-hooks

cat <<'EOF'
Beads initialized for this repo.

Next steps:
  bd ready --json
  bd create "Initial task" -t task -p 2 --json
  bd update <id> --claim --json

Notes:
  - This repo keeps AGENTS/CODEX/CLAUDE instructions under version control, so
    init skips Beads-generated agent files.
  - Git hooks are left unchanged. Run 'bd hooks install' later if you want
    Beads hook integration.
EOF

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HALT_FILE="${HAWKSOPTIONS_HALT_FILE:-$ROOT_DIR/data/HALTED}"
REASON="${*:-manual kill switch}"

umask 077
mkdir -p "$(dirname "$HALT_FILE")"
printf '%s\n' "$REASON" > "$HALT_FILE"
chmod 600 "$HALT_FILE"
printf 'halted: %s\n' "$HALT_FILE"

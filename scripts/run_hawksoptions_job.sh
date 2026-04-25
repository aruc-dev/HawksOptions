#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/run_hawksoptions_job.sh <scheduler-script> [args...]" >&2
  exit 64
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${HAWKSOPTIONS_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
TARGET="$1"
shift

case "$TARGET" in
  scheduler/run_scan.py|scheduler/run_risk_check.py|scheduler/run_risk_watch.py|scheduler/run_roll_check.py|scheduler/run_eod_report.py)
    ;;
  *)
    echo "[hawksoptions-runner] unsupported scheduler target: $TARGET" >&2
    exit 64
    ;;
esac

cd "$PROJECT_DIR"
mkdir -p logs local/locks

if [[ -x ".venv/bin/python3" ]]; then
  PYTHON_BIN=".venv/bin/python3"
else
  PYTHON_BIN="${PYTHON:-python3}"
fi

LOCK_FILE="${HAWKSOPTIONS_TRADE_LOCK_FILE:-$PROJECT_DIR/local/locks/trade-jobs.lock}"
START_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "[hawksoptions-runner] start=$START_UTC target=$TARGET"

if [[ "$TARGET" == "scheduler/run_scan.py" || "$TARGET" == "scheduler/run_risk_check.py" ]]; then
  flock -w "${HAWKSOPTIONS_LOCK_TIMEOUT_SECONDS:-600}" "$LOCK_FILE" "$PYTHON_BIN" "$TARGET" "$@"
else
  "$PYTHON_BIN" "$TARGET" "$@"
fi

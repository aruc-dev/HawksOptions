#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS_DIR="${HAWKSOPTIONS_REPORTS_DIR:-$ROOT_DIR/reports}"
GZIP_AFTER_DAYS="${HAWKSOPTIONS_GZIP_REPORTS_AFTER_DAYS:-7}"
DELETE_AFTER_DAYS="${HAWKSOPTIONS_DELETE_REPORTS_AFTER_DAYS:-90}"

if [[ ! -d "$REPORTS_DIR" ]]; then
  exit 0
fi

find "$REPORTS_DIR" -type f \
  \( -name '*.json' -o -name '*.md' -o -name '*.csv' -o -name '*.log' \) \
  -mtime "+$GZIP_AFTER_DAYS" ! -name '*.gz' \
  -exec gzip -9 -- {} +

find "$REPORTS_DIR" -type f -mtime "+$DELETE_AFTER_DAYS" \
  -exec rm -f -- {} +

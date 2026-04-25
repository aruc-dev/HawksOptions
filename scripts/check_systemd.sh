#!/usr/bin/env bash
set -u

if ! command -v systemctl >/dev/null 2>&1; then
  echo "[FAIL] systemctl not available"
  exit 2
fi

echo "[INFO] Checking hawksoptions-* units"
systemctl list-unit-files --no-legend --no-pager 'hawksoptions-*' 2>/dev/null | awk '{print $1, $2}'
echo ""
echo "[INFO] Timers"
systemctl list-timers --no-legend --no-pager 'hawksoptions-*' 2>/dev/null || true

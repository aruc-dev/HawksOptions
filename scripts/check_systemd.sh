#!/usr/bin/env bash
set -u

if ! command -v systemctl >/dev/null 2>&1; then
  echo "[FAIL] systemctl not available"
  exit 2
fi

status=0

echo "[INFO] Checking hawksoptions-* units"
systemctl list-unit-files --no-legend --no-pager 'hawksoptions-*' 2>/dev/null | awk '{print $1, $2}'
echo ""
echo "[INFO] Timers"
systemctl list-timers --no-legend --no-pager 'hawksoptions-*' 2>/dev/null || true
echo ""
echo "[INFO] systemd-logind RemoveIPC"

remove_ipc=""
if command -v systemd-analyze >/dev/null 2>&1; then
  remove_ipc="$(
    systemd-analyze --no-pager cat-config systemd/logind.conf 2>/dev/null \
      | awk -F= '/^[[:space:]]*RemoveIPC[[:space:]]*=/ {gsub(/[[:space:]]/, "", $2); value=$2} END {print value}'
  )"
fi

if [[ -z "$remove_ipc" ]]; then
  remove_ipc="$(
    grep -Rhs '^[[:space:]]*RemoveIPC[[:space:]]*=' \
      /etc/systemd/logind.conf /etc/systemd/logind.conf.d/*.conf 2>/dev/null \
      | tail -n 1 \
      | awk -F= '{gsub(/[[:space:]]/, "", $2); print $2}'
  )"
fi

if [[ "$remove_ipc" == "no" ]]; then
  echo "[OK] RemoveIPC=no"
else
  echo "[FAIL] RemoveIPC is '${remove_ipc:-unset/default}', expected 'no'"
  echo "       Create /etc/systemd/logind.conf.d/99-hawksoptions-ram-secrets.conf with:"
  echo "       [Login]"
  echo "       RemoveIPC=no"
  echo "       Then run: sudo systemctl restart systemd-logind.service"
  status=1
fi

echo ""
echo "[INFO] RAM secret file"
if [[ -e /dev/shm/.hawksoptions.env ]]; then
  stat -c '[OK] %a %U %G %n %s bytes' /dev/shm/.hawksoptions.env 2>/dev/null \
    || ls -l /dev/shm/.hawksoptions.env
else
  echo "[WARN] /dev/shm/.hawksoptions.env is missing; run sudo systemctl restart hawksoptions-secrets.service"
fi

exit "$status"

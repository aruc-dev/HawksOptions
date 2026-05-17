# HawksOptions systemd Deployment

These templates run HawksOptions as one-shot jobs on Linux hosts such as EC2.
All unit names are prefixed with `hawksoptions-` so they can coexist with
`HawksTrade` on the same host.

## Units

- `hawksoptions-secrets.service` + `.timer`
- `hawksoptions-scan.service` + `.timer`
- `hawksoptions-risk-check.service` + `.timer`
- `hawksoptions-risk-watch.service` + `.timer`
- `hawksoptions-roll-check.service` + `.timer`
- `hawksoptions-eod-report.service` + `.timer`
- `hawksoptions-dashboard.service`
- `hawksoptions-cloudflared.service` (optional, for Cloudflare Tunnel)

## Install

```bash
cd /home/ec2-user/HawksOptions
sudo install -d -m 0750 /etc/hawksoptions /etc/hawksoptions-dash
sudo install -m 0600 scheduler/systemd/hawksoptions.env.example /etc/hawksoptions/hawksoptions.env
sudo install -m 0600 scheduler/systemd/hawksoptions-dash.env.example /etc/hawksoptions-dash/env
sudo cp scheduler/systemd/hawksoptions-*.service scheduler/systemd/hawksoptions-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Preserve the RAM secret file across session cleanup:

```bash
sudo install -d -m 0755 /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/99-hawksoptions-ram-secrets.conf >/dev/null <<'EOF'
[Login]
RemoveIPC=no
EOF
sudo systemctl restart systemd-logind.service
```

Amazon Linux hosts commonly default `RemoveIPC` to `yes`. The HawksOptions RAM
secret file is owned by `ec2-user` in `/dev/shm`, so logind cleanup can remove
`/dev/shm/.hawksoptions.env` when `ec2-user` sessions end. `RemoveIPC=no` keeps
the file available for timer-triggered trading jobs while the file remains
mode `0600`.

Enable timers:

```bash
sudo systemctl enable --now \
  hawksoptions-secrets.service \
  hawksoptions-secrets.timer \
  hawksoptions-scan.timer \
  hawksoptions-risk-check.timer \
  hawksoptions-risk-watch.timer \
  hawksoptions-roll-check.timer \
  hawksoptions-eod-report.timer
```

`hawksoptions-secrets.timer` is a safety net for boot/tmpfs loss. The secrets
loader reuses an existing non-empty `/dev/shm/.hawksoptions.env` by default and
only calls AWS Secrets Manager when that RAM file is missing/empty, unless
`HAWKSOPTIONS_SECRETS_FORCE_REFRESH=1` is set for an explicit refresh. Trading
jobs use `Wants=` plus `After=` for `hawksoptions-secrets.service`, so boot-time
persistent timer runs order against the loader without requiring a fresh AWS call
before every trading job.

Verify the host-level systemd settings after setup:

```bash
scripts/check_systemd.sh
```

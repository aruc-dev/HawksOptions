# HawksOptions systemd Deployment

These templates run HawksOptions as one-shot jobs on Linux hosts such as EC2.
All unit names are prefixed with `hawksoptions-` so they can coexist with
`HawksTrade` on the same host.

## Units

- `hawksoptions-secrets.service`
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

Enable timers:

```bash
sudo systemctl enable --now \
  hawksoptions-scan.timer \
  hawksoptions-risk-check.timer \
  hawksoptions-risk-watch.timer \
  hawksoptions-roll-check.timer \
  hawksoptions-eod-report.timer
```

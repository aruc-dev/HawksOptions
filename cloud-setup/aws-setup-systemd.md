# HawksOptions on EC2 with systemd

## Goal

Run HawksOptions as a separate Linux service stack alongside other bots without
sharing unit names, secrets paths, or dashboard credentials.

## Required Paths

- Project: `/home/ec2-user/HawksOptions`
- Trading env: `/etc/hawksoptions/hawksoptions.env`
- Dashboard env: `/etc/hawksoptions-dash/env`
- tmpfs secret file: `/dev/shm/.hawksoptions.env`

## Install Flow

```bash
cd /home/ec2-user/HawksOptions
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dashboard.txt

sudo install -d -m 0750 /etc/hawksoptions /etc/hawksoptions-dash
sudo install -m 0600 scheduler/systemd/hawksoptions.env.example /etc/hawksoptions/hawksoptions.env
sudo install -m 0600 scheduler/systemd/hawksoptions-dash.env.example /etc/hawksoptions-dash/env
sudo cp scheduler/systemd/hawksoptions-*.service scheduler/systemd/hawksoptions-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

## Secrets

Store trading and dashboard keys separately. The dashboard service must not be
able to read the trading tmpfs secret file.

## Enable

```bash
sudo systemctl enable --now hawksoptions-secrets.service
sudo systemctl enable --now \
  hawksoptions-scan.timer \
  hawksoptions-risk-check.timer \
  hawksoptions-risk-watch.timer \
  hawksoptions-roll-check.timer \
  hawksoptions-eod-report.timer
```

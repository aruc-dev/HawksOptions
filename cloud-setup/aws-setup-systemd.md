# HawksOptions - AWS EC2 Production Setup Guide (systemd)

This guide covers a production-grade HawksOptions deployment on AWS EC2 using
systemd services and timers. It is intentionally detailed so the instance can be
rebuilt from a clean Amazon Linux host without guessing paths, IAM permissions,
secret names, unit names, dashboard settings, or verification steps.

HawksOptions is designed to run independently from HawksTrade. Every runtime
name in this guide uses the `hawksoptions-` prefix so both systems can coexist
on the same EC2 instance without sharing unit names, environment files, RAM
secret files, dashboard credentials, logs, or state.

---

## Production Defaults

| Item | Value |
|------|-------|
| Project path | `/home/ec2-user/HawksOptions` |
| Trading env file | `/etc/hawksoptions/hawksoptions.env` |
| Trading RAM secret file | `/dev/shm/.hawksoptions.env` |
| Dashboard env file | `/etc/hawksoptions-dash/env` |
| AWS Secrets Manager name | `hawksoptions/keys` |
| systemd prefix | `hawksoptions-` |
| Trading OS user | `ec2-user` |
| Dashboard OS user | `hawksoptions-dash` |
| Dashboard bind address | `127.0.0.1:8080` |

Use a separate Alpaca account for HawksOptions if HawksTrade is also running.
That keeps buying power, PDT state, open-order state, and API throttling
separate between the equity bot and the options bot.

---

## Prerequisites

- An AWS account with permission to create EC2 instances, IAM roles, IAM
  policies, and Secrets Manager secrets.
- Alpaca paper keys for options trading.
- Optional Alpaca live keys, left blank until live trading is explicitly enabled.
- Optional dashboard-only Alpaca keys, preferably separate from trading keys.
- Optional `NEWS_API_KEY` and `OPENAI_API_KEY` if enabling AI/news features.
- The HawksOptions repository cloned or copied to the EC2 instance.

---

## Step 1 - Store Trading Secrets in AWS Secrets Manager

1. Open the AWS Secrets Manager console.
2. Choose **Store a new secret**.
3. Choose **Other type of secret**.
4. Add these key/value pairs.

| Key | Value |
|-----|-------|
| `ALPACA_OPTIONS_PAPER_API_KEY` | Your HawksOptions paper API key |
| `ALPACA_OPTIONS_PAPER_SECRET_KEY` | Your HawksOptions paper secret key |
| `ALPACA_OPTIONS_LIVE_API_KEY` | Your HawksOptions live API key, or blank |
| `ALPACA_OPTIONS_LIVE_SECRET_KEY` | Your HawksOptions live secret key, or blank |
| `ALPACA_OPTIONS_DASHBOARD_PAPER_API_KEY` | Optional read-only dashboard paper key |
| `ALPACA_OPTIONS_DASHBOARD_PAPER_SECRET_KEY` | Optional read-only dashboard paper secret |
| `ALPACA_OPTIONS_DASHBOARD_LIVE_API_KEY` | Optional read-only dashboard live key |
| `ALPACA_OPTIONS_DASHBOARD_LIVE_SECRET_KEY` | Optional read-only dashboard live secret |
| `NEWS_API_KEY` | Optional news integration key |
| `OPENAI_API_KEY` | Optional OpenAI key for veto-only AI helpers |

5. Name the secret exactly `hawksoptions/keys`.
6. Leave rotation disabled unless you already operate a tested rotation flow.
7. Store the secret.

The bundled `scripts/fetch_secrets.sh` reads `hawksoptions/keys` by default.
If you use a different name, set `HAWKSOPTIONS_SECRET_NAME` in
`/etc/hawksoptions/hawksoptions.env`.

Dashboard note: the dashboard service is intentionally blocked from reading
`/dev/shm/.hawksoptions.env`. Put dashboard credentials directly in
`/etc/hawksoptions-dash/env`, or use a separate dashboard-specific secret
loading process if you do not want dashboard credentials on disk.

---

## Step 2 - Create an IAM Policy

Create a policy that allows read-only access to HawksOptions secrets and
nothing else.

1. Open IAM -> Policies.
2. Choose **Create policy** -> **JSON**.
3. Paste this policy, replacing `YOUR_ACCOUNT_ID` and region if needed.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "HawksOptionsSecretsReadOnly",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:us-east-1:YOUR_ACCOUNT_ID:secret:hawksoptions/*"
    }
  ]
}
```

4. Name the policy `HawksOptionsSecretsPolicy`.
5. Create the policy.

If HawksTrade runs on the same instance, do not reuse the HawksTrade policy.
Keep the resources scoped separately:

- HawksTrade: `hawkstrade/*`
- HawksOptions: `hawksoptions/*`

---

## Step 3 - Create an IAM Role and Attach the Policy

1. Open IAM -> Roles.
2. Choose **Create role**.
3. Trusted entity: **AWS service**.
4. Use case: **EC2**.
5. Attach `HawksOptionsSecretsPolicy`.
6. Name the role `HawksOptionsEC2Role`.
7. Create the role.

Attach this role to the EC2 instance at launch time. If the instance already
exists, use EC2 -> Instance -> Actions -> Security -> Modify IAM role.

---

## Step 4 - Launch the EC2 Instance

Recommended sizing:

| Deployment | Instance | Storage | Notes |
|------------|----------|---------|-------|
| HawksOptions only | `t4g.small` | 20 GB gp3 | Enough for scheduler, dashboard, logs, and backtests |
| HawksTrade + HawksOptions on one host | `t4g.medium` | 30 GB gp3 | More headroom for concurrent scans and dashboards |

Recommended launch settings:

| Setting | Value |
|---------|-------|
| AMI | Amazon Linux 2023, arm64 |
| IAM role | `HawksOptionsEC2Role` |
| Security group | SSH port 22 from your IP only |
| Public inbound for dashboard | None |
| Timezone | UTC |

The dashboard binds to `127.0.0.1` only. Use Cloudflare Tunnel, Systems
Manager Session Manager, or an SSH tunnel. Do not open the dashboard directly
to the internet.

---

## Step 5 - Install System Packages

SSH into the instance:

```bash
ssh ec2-user@YOUR_EC2_PUBLIC_DNS
```

Install base packages:

```bash
sudo dnf update -y
sudo dnf install -y python3 python3-pip git jq
```

Verify the AWS CLI. Amazon Linux 2023 usually includes it.

```bash
aws --version
```

If `aws` is missing:

```bash
sudo dnf install -y awscli
aws --version
```

---

## Step 6 - Clone HawksOptions and Create the Virtual Environment

```bash
cd /home/ec2-user
git clone https://github.com/aruc-dev/HawksOptions.git HawksOptions
cd /home/ec2-user/HawksOptions

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dashboard.txt
```

Run a local smoke check before configuring services:

```bash
.venv/bin/python -m unittest tests.test_alpaca_options_client tests.test_run_scan -v
.venv/bin/python scheduler/run_backtest.py --days 30 --fund 10000
```

---

## Step 7 - Configure HawksOptions for EC2

Edit the runtime config:

```bash
nano /home/ec2-user/HawksOptions/config/config.yaml
```

Recommended production settings:

```yaml
mode: paper

market_data:
  use_sample_data: false
```

Keep `mode: paper` until the full paper-trading validation period is complete.
Set `market_data.use_sample_data: false` when you want the scheduler to use
Alpaca instead of deterministic sample data.

**Optional local config:** If you need machine-specific settings that should
not be committed to git, create `config/config.local.yaml`. When present, this
file is used **in full** instead of `config/config.yaml` — it must contain all
required configuration keys, not just the ones you want to change. Start by
copying the committed file and editing from there. This file is git-ignored and
will never be accidentally committed or overwritten by a `git pull`.

```bash
cp config/config.yaml config/config.local.yaml
nano config/config.local.yaml   # edit as needed — all keys must be present
```

Do not create `config/.env` on EC2 for production trading. The systemd setup
loads credentials from `/dev/shm/.hawksoptions.env`, which is populated from
AWS Secrets Manager at boot.

Review these sections before enabling timers:

- `account.max_portfolio_risk_pct`
- `account.max_single_position_risk_pct`
- `account.max_open_strategies`
- `strategies.*.enabled`
- `underlyings.source`
- `schedule.*`
- `reporting.*`

---

## Step 8 - Create the systemd Environment Files

Create the trading and dashboard config directories:

```bash
sudo install -d -m 0750 /etc/hawksoptions
sudo install -d -m 0750 /etc/hawksoptions-dash
```

Install the example trading env file:

```bash
sudo install -m 0600 \
  /home/ec2-user/HawksOptions/scheduler/systemd/hawksoptions.env.example \
  /etc/hawksoptions/hawksoptions.env
```

Edit it:

```bash
sudo nano /etc/hawksoptions/hawksoptions.env
```

Confirm these values:

```bash
HAWKSOPTIONS_DIR=/home/ec2-user/HawksOptions
HAWKSOPTIONS_USER=ec2-user
HAWKSOPTIONS_GROUP=ec2-user
HAWKSOPTIONS_SECRET_NAME=hawksoptions/keys
HAWKSOPTIONS_SHM_SECRET_FILE=/dev/shm/.hawksoptions.env
HAWKSOPTIONS_LOCK_TIMEOUT_SECONDS=600
AWS_DEFAULT_REGION=us-east-1
```

Install the dashboard env file:

```bash
sudo install -m 0600 \
  /home/ec2-user/HawksOptions/scheduler/systemd/hawksoptions-dash.env.example \
  /etc/hawksoptions-dash/env
sudo nano /etc/hawksoptions-dash/env
```

Minimum dashboard settings for Cloudflare Access:

```bash
DASHBOARD_AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=your-team.cloudflareaccess.com
CF_ACCESS_AUD=your-cloudflare-access-audience-tag
DASHBOARD_ALLOWED_EMAILS=you@example.com

ALPACA_OPTIONS_DASHBOARD_PAPER_API_KEY=your_dashboard_paper_key
ALPACA_OPTIONS_DASHBOARD_PAPER_SECRET_KEY=your_dashboard_paper_secret
ALPACA_OPTIONS_DASHBOARD_LIVE_API_KEY=
ALPACA_OPTIONS_DASHBOARD_LIVE_SECRET_KEY=
```

For SSH-tunnel-only access, set:

```bash
DASHBOARD_AUTH_MODE=local
```

Only use local mode when the service is bound to `127.0.0.1` and exposed via
SSH tunnel or localhost-only access.

---

## Step 8A - Preserve `/dev/shm` RAM Secrets

HawksOptions writes trading credentials to `/dev/shm/.hawksoptions.env` with
mode `0600` and `ec2-user:ec2-user` ownership. On Amazon Linux hosts,
`systemd-logind` may default `RemoveIPC` to `yes`, which can remove user-owned
IPC objects in `/dev/shm` after `ec2-user` sessions end. If that happens, timer
jobs fail before Python starts, and the dashboard can fail with
`status=226/NAMESPACE` because its `InaccessiblePaths=/dev/shm/.hawksoptions.env`
entry points at a missing path.

Set `RemoveIPC=no` before enabling the timers:

```bash
sudo install -d -m 0755 /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/99-hawksoptions-ram-secrets.conf >/dev/null <<'EOF'
[Login]
RemoveIPC=no
EOF
sudo systemctl restart systemd-logind.service
systemd-analyze cat-config systemd/logind.conf | grep 'RemoveIPC='
```

The effective value must be:

```text
RemoveIPC=no
```

This does not expose the secret file. The file remains readable only by
`ec2-user`, and the dashboard unit still blocks access to the trading RAM secret
with `InaccessiblePaths=`.

---

## Step 9 - Install systemd Units and Timers

The unit templates use `/home/ec2-user/HawksOptions` and `ec2-user` by default.
Use the substitution flow below so the guide still works if you deploy under a
different path or user.

```bash
cd /home/ec2-user/HawksOptions

export PROJECT=/home/ec2-user/HawksOptions
export HO_USER=ec2-user
export HO_GROUP=ec2-user

TMPDIR="$(mktemp -d)"
cp scheduler/systemd/hawksoptions-*.service scheduler/systemd/hawksoptions-*.timer "$TMPDIR"/

sudo sed -i \
  -e "s|/home/ec2-user/HawksOptions|$PROJECT|g" \
  -e "s|%h/HawksOptions|$PROJECT|g" \
  -e "s|User=ec2-user|User=$HO_USER|g" \
  -e "s|Group=ec2-user|Group=$HO_GROUP|g" \
  "$TMPDIR"/*.service

sudo cp "$TMPDIR"/*.service "$TMPDIR"/*.timer /etc/systemd/system/
rm -rf "$TMPDIR"
```

The bundled unit templates run the secrets service as the same OS user as the
trading jobs. This makes `/dev/shm/.hawksoptions.env` readable by the trading
services while still keeping mode `0600`. The `sed` command above adjusts
`User=`, `Group=`, and project paths when deploying to a different account or
directory. Existing installs that previously created the RAM file as `root` are
handled by the secrets service `ExecStartPre`, which normalizes ownership and
mode before the `ec2-user` loader runs.

Add the RAM secret file as a required environment file for every trading job.
This makes jobs fail closed if the secrets service has not written credentials.

```bash
for name in scan risk-check risk-watch roll-check eod-report; do
  sudo install -d "/etc/systemd/system/hawksoptions-${name}.service.d"
  sudo tee "/etc/systemd/system/hawksoptions-${name}.service.d/10-secrets.conf" >/dev/null <<'EOF'
[Service]
EnvironmentFile=/dev/shm/.hawksoptions.env
EOF
done
```

Install dashboard log rotation:

```bash
sudo cp cloud-setup/logrotate/hawksoptions-dashboard /etc/logrotate.d/hawksoptions-dashboard
```

If `PROJECT` is not `/home/ec2-user/HawksOptions`, update the path inside
`/etc/logrotate.d/hawksoptions-dashboard`.

Reload systemd:

```bash
sudo systemctl daemon-reload
```

---

## What Gets Installed

| Unit | Type | Purpose |
|------|------|---------|
| `hawksoptions-secrets.service` | oneshot | Fetches AWS Secrets Manager values into `/dev/shm/.hawksoptions.env` |
| `hawksoptions-secrets.timer` | timer | Ensures RAM secrets exist every 30 minutes; existing RAM secrets are reused by default |
| `hawksoptions-scan.service` | oneshot | Runs `scheduler/run_scan.py` |
| `hawksoptions-scan.timer` | timer | Fires every 30 minutes |
| `hawksoptions-risk-check.service` | oneshot | Refreshes positions, daily loss, Greeks snapshots, and risk actions |
| `hawksoptions-risk-check.timer` | timer | Fires every 5 minutes |
| `hawksoptions-risk-watch.service` | oneshot | Runs elevated-risk watch |
| `hawksoptions-risk-watch.timer` | timer | Fires every 1 minute |
| `hawksoptions-roll-check.service` | oneshot | Checks open strategies for roll candidates |
| `hawksoptions-roll-check.timer` | timer | Fires hourly |
| `hawksoptions-eod-report.service` | oneshot | Writes end-of-day report |
| `hawksoptions-eod-report.timer` | timer | Fires Monday-Friday at 21:45 UTC |
| `hawksoptions-dashboard.service` | service | Runs the read-only dashboard on `127.0.0.1:8080` |

All schedules are interpreted in the instance timezone. EC2 instances normally
run UTC. Confirm with:

```bash
timedatectl
```

The bundled scan, risk-check, risk-watch, and roll timers are broad interval
timers. If you want market-hours-only timers, edit the timer files or install
drop-ins before enabling them.

---

## Step 10 - Start and Verify the Secrets Service

Enable the service so it runs on every boot and the timer so it checks that RAM
secrets exist every 30 minutes:

```bash
sudo systemctl enable hawksoptions-secrets.service hawksoptions-secrets.timer
```

Start it now:

```bash
sudo systemctl start hawksoptions-secrets.service
sudo systemctl start hawksoptions-secrets.timer
sudo systemctl status hawksoptions-secrets.service
sudo systemctl status hawksoptions-secrets.timer
```

Confirm the RAM secret file exists. This command prints key names only:

```bash
sudo -u ec2-user cut -d= -f1 /dev/shm/.hawksoptions.env
```

Expected key names include:

```text
ALPACA_OPTIONS_PAPER_API_KEY
ALPACA_OPTIONS_PAPER_SECRET_KEY
```

If the service fails:

```bash
journalctl -u hawksoptions-secrets.service --no-pager
aws sts get-caller-identity
aws secretsmanager get-secret-value --secret-id hawksoptions/keys --region us-east-1 --query SecretString --output text | jq keys
```

---

## Step 11 - Verify the Trading Runtime

Run these checks before enabling scheduled trading.

```bash
cd /home/ec2-user/HawksOptions

# 1. Confirm Python dependencies load.
.venv/bin/python -m unittest tests.test_alpaca_options_client tests.test_run_scan -v

# 2. Confirm credentials are loaded into a scheduler-like environment.
set -a
. /etc/hawksoptions/hawksoptions.env
. /dev/shm/.hawksoptions.env
set +a
.venv/bin/python - <<'PY'
import os
required = ["ALPACA_OPTIONS_PAPER_API_KEY", "ALPACA_OPTIONS_PAPER_SECRET_KEY"]
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit(f"missing keys: {missing}")
print("HawksOptions credentials loaded")
PY

# 3. Confirm the configured account can be read.
.venv/bin/python - <<'PY'
from core.config import load_config
from core.alpaca_options_client import AlpacaOptionsClient
client = AlpacaOptionsClient(load_config(), use_sample_data=False)
print(client.get_account())
PY

# 4. Run a dry scan. This should not persist orders.
.venv/bin/python scheduler/run_scan.py --dry-run

# 5. Run risk checks. This writes positions, baseline, and Greeks snapshots.
.venv/bin/python scheduler/run_risk_check.py --dry-run

# 6. Run roll and EOD report checks.
.venv/bin/python scheduler/run_roll_check.py --dry-run
.venv/bin/python scheduler/run_eod_report.py --dry-run

# 7. Run the full test suite.
.venv/bin/python -m unittest discover -v
```

Do not enable the scan timer until these checks pass and `mode: paper` is
confirmed in `config/config.yaml`.

---

## Step 12 - Enable Trading Timers

Enable the secrets service first:

```bash
sudo systemctl enable --now hawksoptions-secrets.service
```

Enable scheduled jobs:

```bash
sudo systemctl enable --now \
  hawksoptions-scan.timer \
  hawksoptions-risk-check.timer \
  hawksoptions-risk-watch.timer \
  hawksoptions-roll-check.timer \
  hawksoptions-eod-report.timer
```

Verify timers:

```bash
systemctl list-timers 'hawksoptions-*'
```

You should see `NEXT` timestamps for every enabled timer.

---

## Step 13 - Enable the Read-Only Dashboard

Create the dashboard user:

```bash
sudo useradd --system --no-create-home --shell /sbin/nologin hawksoptions-dash || true
```

Make sure the dashboard can write its access log:

```bash
sudo install -d -m 0755 /home/ec2-user/HawksOptions/logs
sudo chown -R hawksoptions-dash:hawksoptions-dash /home/ec2-user/HawksOptions/logs
```

Enable and start the dashboard:

```bash
sudo systemctl enable --now hawksoptions-dashboard.service
sudo systemctl status hawksoptions-dashboard.service
```

Verify locally on the instance:

```bash
curl -s http://127.0.0.1:8080/healthz
```

Access options:

- SSH tunnel: `ssh -L 8080:127.0.0.1:8080 ec2-user@YOUR_EC2_PUBLIC_DNS`
- Cloudflare Tunnel + Cloudflare Access: see `cloud-setup/dashboard-setup.md`

Do not open port 8080 to the public internet.

---

## Dependency Chain

The intended startup chain is:

```text
network-online.target
        |
        v
hawksoptions-secrets.service  <---  hawksoptions-secrets.timer
        |
        +--> /dev/shm/.hawksoptions.env
        |
        +--> consumed by hawksoptions-scan.service
        +--> consumed by hawksoptions-risk-check.service
        +--> consumed by hawksoptions-risk-watch.service
        +--> consumed by hawksoptions-roll-check.service
        +--> consumed by hawksoptions-eod-report.service

hawksoptions-dashboard.service
        |
        +--> /etc/hawksoptions-dash/env only
```

Trading jobs do not use `Requires=hawksoptions-secrets.service`; high-frequency jobs
must not call AWS Secrets Manager before every run. They use `Wants=` and
`After=` for `network-online.target` and `hawksoptions-secrets.service`, so boot
or persistent timer runs pull prerequisites into the transaction without making
the trading job directly require a fresh AWS fetch. By default the loader reuses
an existing non-empty `/dev/shm/.hawksoptions.env` that contains the required
paper keys and only calls AWS Secrets Manager when that RAM file is missing,
empty, invalid, explicitly forced, or older than an opt-in max age. Set
`HAWKSOPTIONS_SECRETS_FORCE_REFRESH=1` for a manual key-rotation refresh, or set
`HAWKSOPTIONS_SECRETS_MAX_AGE_SECONDS` to a positive value if you explicitly want
age-based refresh. `hawksoptions-secrets.service` uses `PassEnvironment=` for
those two override variables. The drop-in from Step 9 requires
`/dev/shm/.hawksoptions.env` as an `EnvironmentFile`. If secrets are missing,
trading jobs fail before Python starts.

The dashboard uses a separate environment file and is blocked from reading the
trading RAM secret file.

---

## Day-to-Day Operations

List active timers:

```bash
systemctl list-timers 'hawksoptions-*'
```

Check status:

```bash
systemctl status hawksoptions-secrets.service
systemctl status hawksoptions-scan.service
systemctl status hawksoptions-risk-check.service
systemctl status hawksoptions-dashboard.service
```

Tail logs:

```bash
journalctl -u hawksoptions-scan.service -f
journalctl -u hawksoptions-risk-check.service -n 100 --no-pager
journalctl -u 'hawksoptions-*' --no-pager | tail -200
```

Run a job manually:

```bash
sudo systemctl start hawksoptions-risk-check.service
sudo systemctl start hawksoptions-risk-watch.service
sudo systemctl start hawksoptions-roll-check.service
sudo systemctl start hawksoptions-eod-report.service
```

Manual scan warning: `hawksoptions-scan.service` runs without `--dry-run` and
can submit paper or live orders depending on `config/config.yaml`. Use this
direct Python command for a safe scan test:

```bash
cd /home/ec2-user/HawksOptions
set -a
. /etc/hawksoptions/hawksoptions.env
. /dev/shm/.hawksoptions.env
set +a
.venv/bin/python scheduler/run_scan.py --dry-run
```

Re-fetch secrets:

```bash
sudo systemctl set-environment HAWKSOPTIONS_SECRETS_FORCE_REFRESH=1
sudo systemctl restart hawksoptions-secrets.service
sudo systemctl unset-environment HAWKSOPTIONS_SECRETS_FORCE_REFRESH
sudo systemctl status hawksoptions-secrets.service
```

Temporarily stop all scheduled trading jobs:

```bash
sudo systemctl stop \
  hawksoptions-scan.timer \
  hawksoptions-risk-check.timer \
  hawksoptions-risk-watch.timer \
  hawksoptions-roll-check.timer \
  hawksoptions-eod-report.timer
sudo systemctl disable \
  hawksoptions-scan.timer \
  hawksoptions-risk-check.timer \
  hawksoptions-risk-watch.timer \
  hawksoptions-roll-check.timer \
  hawksoptions-eod-report.timer
```

Re-enable timers:

```bash
sudo systemctl enable --now \
  hawksoptions-secrets.timer \
  hawksoptions-scan.timer \
  hawksoptions-risk-check.timer \
  hawksoptions-risk-watch.timer \
  hawksoptions-roll-check.timer \
  hawksoptions-eod-report.timer
```

Reload after editing unit files:

```bash
sudo systemctl daemon-reload
```

Run the bundled systemd check script:

```bash
cd /home/ec2-user/HawksOptions
./scripts/check_systemd.sh
```

---

## Backups and Runtime State

Important runtime files:

| Path | Purpose |
|------|---------|
| `data/positions.json` | Open options strategy state |
| `data/trades.csv` | Trade log |
| `data/iv_rank_history.csv` | IV history |
| `data/daily_loss_baseline.json` | Daily drawdown baseline |
| `data/greeks_snapshots/` | Risk snapshots |
| `reports/` | Backtest and EOD reports |
| `logs/` | Dashboard access logs |

Back up `data/`, `reports/`, `config/config.yaml`, `config/config.local.yaml`
(if present), and `config/underlyings.yaml`. Do not back up
`/dev/shm/.hawksoptions.env`.

Example lightweight backup:

```bash
cd /home/ec2-user
tar --exclude='HawksOptions/.venv' \
    --exclude='HawksOptions/__pycache__' \
    -czf "hawksoptions-backup-$(date -u +%Y%m%dT%H%M%SZ).tgz" \
    HawksOptions/data HawksOptions/reports HawksOptions/config/config.yaml \
    $(test -f HawksOptions/config/config.local.yaml && echo HawksOptions/config/config.local.yaml) \
    HawksOptions/config/underlyings.yaml
```

---

## Security Notes

- Keep the EC2 security group limited to SSH from your IP, or use Systems
  Manager Session Manager and remove inbound SSH entirely.
- Keep trading credentials out of the repository and out of `config/.env`.
- Use AWS Secrets Manager plus `/dev/shm` for trading credentials.
- Keep `/etc/hawksoptions/hawksoptions.env` mode `0600`.
- Keep `/etc/hawksoptions-dash/env` mode `0600`.
- Use separate dashboard credentials and trading credentials.
- Do not let the dashboard read `/dev/shm/.hawksoptions.env`.
- Keep `mode: paper` until paper results are reviewed.
- Keep `Restart=no` for trading oneshot units; failed jobs should retry at the
  next timer boundary, not immediately.
- If HawksTrade runs on the same host, use separate Alpaca accounts and
  separate AWS Secrets Manager secret names.

---

## Switching to Live Trading

Only do this after:

- At least 30 days of stable paper trading.
- Positive live-like paper PnL after slippage and commission.
- No unresolved scheduler, credential, or dashboard alerts.
- You have explicitly accepted real-money risk.

Steps:

1. Fill `ALPACA_OPTIONS_LIVE_API_KEY` and
   `ALPACA_OPTIONS_LIVE_SECRET_KEY` in Secrets Manager.
2. Restart secrets:

   ```bash
   sudo systemctl set-environment HAWKSOPTIONS_SECRETS_FORCE_REFRESH=1
   sudo systemctl restart hawksoptions-secrets.service
   sudo systemctl unset-environment HAWKSOPTIONS_SECRETS_FORCE_REFRESH
   ```

3. Confirm live key names are present:

   ```bash
   sudo -u ec2-user cut -d= -f1 /dev/shm/.hawksoptions.env
   ```

4. Edit `config/config.yaml`:

   ```yaml
   mode: live
   market_data:
     use_sample_data: false
   ```

5. Run a live-account connection check without placing orders:

```bash
cd /home/ec2-user/HawksOptions
set -a
. /etc/hawksoptions/hawksoptions.env
. /dev/shm/.hawksoptions.env
set +a
.venv/bin/python - <<'PY'
from core.config import load_config
from core.alpaca_options_client import AlpacaOptionsClient
client = AlpacaOptionsClient(load_config(), use_sample_data=False)
print(client.get_account())
PY
```

6. Run a dry scan:

   ```bash
   .venv/bin/python scheduler/run_scan.py --dry-run
   ```

7. Re-enable timers only after the dry scan output is acceptable.

---

## Troubleshooting

### Secrets service fails

```bash
journalctl -u hawksoptions-secrets.service --no-pager
aws sts get-caller-identity
aws secretsmanager get-secret-value --secret-id hawksoptions/keys --region us-east-1 --query SecretString --output text | jq keys
```

Check:

- The EC2 instance has `HawksOptionsEC2Role` attached.
- The IAM policy resource matches the actual secret ARN.
- `HAWKSOPTIONS_SECRET_NAME` is `hawksoptions/keys`.
- `AWS_DEFAULT_REGION` matches the secret region.
- `jq` and `aws` are installed.

### Trading service fails before Python starts

```bash
systemctl cat hawksoptions-scan.service
journalctl -u hawksoptions-scan.service -n 100 --no-pager
ls -l /dev/shm/.hawksoptions.env
```

Check:

- `hawksoptions-secrets.service` is enabled and succeeded.
- `hawksoptions-secrets.timer` is enabled and active.
- `/dev/shm/.hawksoptions.env` exists and is readable by `ec2-user`.
- The `10-secrets.conf` drop-in exists for each trading service.
- `systemd-logind` has effective `RemoveIPC=no` so `/dev/shm/.hawksoptions.env`
  is not removed when `ec2-user` sessions end.

```bash
systemd-analyze cat-config systemd/logind.conf | grep 'RemoveIPC='
sudo systemctl restart hawksoptions-secrets.service
ls -l /dev/shm/.hawksoptions.env
```

### Dashboard fails with `status=226/NAMESPACE`

```bash
systemctl status hawksoptions-dashboard.service --no-pager -l
journalctl -u hawksoptions-dashboard.service -n 100 --no-pager
ls -l /dev/shm/.hawksoptions.env
```

If the log shows a namespace setup failure for
`/run/systemd/unit-root/dev/shm/.hawksoptions.env`, the dashboard is starting
while the RAM secret path is missing. Check:

- `hawksoptions-secrets.service` can recreate `/dev/shm/.hawksoptions.env`.
- `RemoveIPC=no` is effective in `systemd-logind`.
- `sudo systemctl restart systemd-logind.service` was run after adding the
  drop-in.

After fixing the host setting:

```bash
sudo systemctl restart hawksoptions-secrets.service
sudo systemctl restart hawksoptions-dashboard.service
curl -s http://127.0.0.1:8080/healthz
```

### Jobs use sample data instead of Alpaca

Check `config/config.yaml`:

```yaml
market_data:
  use_sample_data: false
```

If this remains `true`, the system uses deterministic sample data and does not
need Alpaca credentials.

### Timer not firing

```bash
timedatectl
systemctl list-timers 'hawksoptions-*'
systemctl is-enabled hawksoptions-scan.timer
```

If `NEXT` is blank, the timer is disabled or the unit failed to load. Run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hawksoptions-scan.timer
```

### Dashboard returns 403 or 500

```bash
journalctl -u hawksoptions-dashboard.service -n 100 --no-pager
sudo cat /etc/hawksoptions-dash/env
```

Check:

- `DASHBOARD_AUTH_MODE` is `cloudflare` or `local`.
- Cloudflare mode has `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, and
  `DASHBOARD_ALLOWED_EMAILS`.
- Dashboard Alpaca variables use the `ALPACA_OPTIONS_DASHBOARD_*` prefix.
- Port 8080 is not exposed publicly.

### Unit file changes do not apply

Always reload systemd after changing unit files or drop-ins:

```bash
sudo systemctl daemon-reload
sudo systemctl restart hawksoptions-secrets.service
```

### Need to run HawksTrade and HawksOptions together

Use separate:

- Project directories: `/home/ec2-user/HawksTrade` and `/home/ec2-user/HawksOptions`
- AWS secrets: `hawkstrade/keys` and `hawksoptions/keys`
- RAM secrets: `/dev/shm/.hawkstrade.env` and `/dev/shm/.hawksoptions.env`
- systemd unit prefixes: `hawkstrade-*` and `hawksoptions-*`
- dashboard users and env files
- Alpaca accounts

Then verify both sets of timers:

```bash
systemctl list-timers 'hawkstrade-*'
systemctl list-timers 'hawksoptions-*'
```

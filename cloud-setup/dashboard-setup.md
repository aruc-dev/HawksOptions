# HawksOptions - Dashboard Setup Guide (Optional)

This guide adds a personal, read-only web dashboard to a HawksOptions EC2
deployment. It is intended for one operator who wants phone or laptop access
without opening any inbound port on the instance.

Complete `cloud-setup/aws-setup-systemd.md` first. This guide assumes the base
bot, virtualenv, and systemd deployment already exist.

---

## Architecture

```text
Phone / Laptop
     | HTTPS
     v
Cloudflare Access
     | Google / Google Workspace login
     | optional One-time PIN fallback
     v
Cloudflare Tunnel (cloudflared on EC2, outbound only)
     | loopback
     v
FastAPI dashboard on 127.0.0.1:8080
     | read-only
     v
positions, trades, IV history, health snapshots, Alpaca read-only keys
```

The dashboard does not place trades, cancel orders, or mutate config. It only
reads local files plus Alpaca account and position data through the dedicated
dashboard credentials.

---

## Security Defaults

These are intentional and should not be loosened:

| Rule | Where it lives |
|------|----------------|
| Dashboard binds to `127.0.0.1:8080` only | `scheduler/systemd/hawksoptions-dashboard.service` |
| Dashboard runs as a dedicated `hawksoptions-dash` user | dashboard systemd unit |
| Dashboard cannot read the trading env files | `InaccessiblePaths=` in the systemd unit |
| Auth defaults to `cloudflare` and fails closed if required vars are missing | `dashboard/config.py`, `dashboard/security.py` |
| Every request is logged with identity, IP, path, status, and latency | `dashboard/security.py` |
| Swagger/OpenAPI routes are disabled | `dashboard/app.py` |
| Dashboard Alpaca credentials use `ALPACA_OPTIONS_DASHBOARD_*` variables, separate from trading keys | `dashboard/alpaca_readonly.py` |

---

## Values You Will Need

Before you start, decide these values:

- Dashboard hostname, for example `options.example.com`
- Cloudflare Zero Trust team domain, for example `myteam.cloudflareaccess.com`
- Login type:
  - `Google` for a simple personal Gmail or single-user email allowlist
  - `Google Workspace` if all users are in a Workspace domain and you may want
    Workspace group-aware policies later
- A dedicated read-only Alpaca key pair for the dashboard

`CF_ACCESS_TEAM_DOMAIN` is only the hostname. Do not include `https://`.

---

## Phase 1 - Local-only Validation Over SSH

Run this once before adding Cloudflare. It confirms the dashboard works
locally, the service account permissions are correct, and the app stays on
loopback only.

### 1. Install dashboard dependencies

```bash
cd /home/ec2-user/HawksOptions
.venv/bin/pip install -r requirements-dashboard.txt
```

### 2. Create the `hawksoptions-dash` service user

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin hawksoptions-dash

sudo chgrp -R hawksoptions-dash /home/ec2-user/HawksOptions
sudo chmod -R g+rX /home/ec2-user/HawksOptions
sudo chmod -R g+rwX /home/ec2-user/HawksOptions/logs

# Allow the dashboard user to traverse /home/ec2-user without opening the home dir.
sudo setfacl -m u:hawksoptions-dash:--x /home/ec2-user
```

If `setfacl` is unavailable, the fallback is:

```bash
sudo chmod 711 /home/ec2-user
```

### 3. Install the dashboard env file in local mode

```bash
sudo install -d -m 0750 -o root -g hawksoptions-dash /etc/hawksoptions-dash
sudo install -m 0640 -o root -g hawksoptions-dash \
  /home/ec2-user/HawksOptions/scheduler/systemd/hawksoptions-dash.env.example \
  /etc/hawksoptions-dash/env

sudo nano /etc/hawksoptions-dash/env
```

For local validation, use:

```bash
DASHBOARD_AUTH_MODE=local

ALPACA_OPTIONS_DASHBOARD_PAPER_API_KEY=<read-only paper key>
ALPACA_OPTIONS_DASHBOARD_PAPER_SECRET_KEY=<read-only paper secret>
ALPACA_OPTIONS_DASHBOARD_LIVE_API_KEY=
ALPACA_OPTIONS_DASHBOARD_LIVE_SECRET_KEY=
```

If your base config runs in live mode, populate the `ALPACA_OPTIONS_DASHBOARD_LIVE_*`
pair instead of the paper pair.

### 4. Install and start the dashboard systemd unit

```bash
cd /home/ec2-user/HawksOptions

TMPDIR="$(mktemp -d)"
cp scheduler/systemd/hawksoptions-dashboard.service "$TMPDIR/"

# Skip this sed if your install path already matches /home/ec2-user/HawksOptions
sudo sed -i \
  -e "s|/home/ec2-user/HawksOptions|$HOME/HawksOptions|g" \
  "$TMPDIR/hawksoptions-dashboard.service"

sudo install -m 0644 "$TMPDIR/hawksoptions-dashboard.service" \
  /etc/systemd/system/hawksoptions-dashboard.service
sudo systemctl daemon-reload
rm -rf "$TMPDIR"

sudo systemctl enable --now hawksoptions-dashboard.service
sudo systemctl status hawksoptions-dashboard.service --no-pager
```

Install log rotation for access logs:

```bash
sudo install -m 0644 \
  /home/ec2-user/HawksOptions/cloud-setup/logrotate/hawksoptions-dashboard \
  /etc/logrotate.d/hawksoptions-dashboard

sudo logrotate -d /etc/logrotate.d/hawksoptions-dashboard
```

Confirm the dashboard is only listening on loopback:

```bash
sudo ss -tlnp | grep 8080
```

Expected:

```text
LISTEN 0 2048 127.0.0.1:8080 0.0.0.0:* users:(("uvicorn",pid=...,fd=3))
```

If you see `0.0.0.0:8080` or `*:8080`, stop and fix the unit before going any
further.

### 5. Verify through an SSH tunnel

From your laptop:

```bash
ssh -i /path/to/key.pem -N -L 8080:127.0.0.1:8080 ec2-user@YOUR_EC2_HOST
```

Then open `http://localhost:8080/`.

If it renders correctly, continue to Cloudflare.

---

## Phase 2 - Cloudflare Tunnel + Cloudflare Access

This phase keeps the origin private while making the dashboard reachable from a
phone or any other browser.

### 1. Find your Cloudflare team domain

In Cloudflare Zero Trust, go to `Settings -> Team name and domain`.

Your team name becomes:

```text
CF_ACCESS_TEAM_DOMAIN=<team-name>.cloudflareaccess.com
```

You will use the same team domain in the Google OAuth client settings.

### 2. Configure Google or Google Workspace as the login method

Choose one:

- `Google`: best for a single operator or simple email allowlist; no Workspace
  group sync
- `Google Workspace`: best if the allowed users live in a Workspace domain and
  you may later use Workspace groups

#### Option A - Google

1. In Google Cloud Console, create or open a project.
2. Go to `APIs & Services -> Credentials`.
3. Select `Configure Consent Screen`.
4. Create the consent screen with:
   - Audience type: `External`
   - App name: any name such as `Cloudflare Access`
   - Support email and contact email: your email
5. From the OAuth overview page, select `Create OAuth client`.
6. Choose `Web application`.
7. Set `Authorized JavaScript origins` to:

```text
https://<team-name>.cloudflareaccess.com
```

8. Set `Authorized redirect URIs` to:

```text
https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback
```

9. Copy the OAuth `Client ID` and `Client secret`.
10. In Cloudflare Zero Trust, go to `Integrations -> Identity providers`.
11. Select `Add new identity provider -> Google`.
12. Paste the Client ID and Client secret.
13. Optional: enable PKCE.
14. Save, then use `Test` to confirm the provider works.

#### Option B - Google Workspace

Use this if the user accounts are in your Workspace and you want the option to
use Workspace groups later.

1. In Google Cloud Console, create or open a project.
2. Go to `APIs & Services -> Enable APIs and Services`.
3. Search for `Admin SDK API` and enable it.
4. Return to `APIs & Services -> Credentials`.
5. Select `Configure Consent Screen`.
6. Create the consent screen with:
   - Audience type: `Internal`
   - App name: any name such as `Cloudflare Access`
   - Support email and contact email: your email
7. From the OAuth overview page, select `Create OAuth client`.
8. Choose `Web application`.
9. Set `Authorized JavaScript origins` to:

```text
https://<team-name>.cloudflareaccess.com
```

10. Set `Authorized redirect URIs` to:

```text
https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback
```

11. Copy the OAuth `Client ID` and `Client secret`.
12. In Google Admin, go to `Security -> Access and data control -> API controls -> Settings`.
13. Enable `Trust internal apps`.
14. In Cloudflare Zero Trust, go to `Integrations -> Identity providers`.
15. Select `Add new identity provider -> Google Workspace`.
16. Paste the Client ID and Client secret, then enter your Workspace domain.
17. Optional: enable PKCE.
18. Save. Cloudflare will generate an admin authorization link.
19. Open that link as a Workspace admin and approve the request.
20. Back in Cloudflare, use `Test` to confirm the provider works.

Note: Cloudflare's Google Workspace IdP integration is not supported when the
Workspace account itself is protected by Cloudflare Access.

### 3. Optional: add One-time PIN as a fallback login method

If you want email-code fallback in addition to Google:

1. In Cloudflare Zero Trust, go to `Integrations -> Identity providers`.
2. Select `Add new identity provider -> One-time PIN`.
3. Save it.

Important:

- Keep OTP restricted to explicit allowed emails in the Access policy.
- Do not pair OTP with a broad email-domain allow rule unless that is
  intentional.
- If your mail security product scans links, allowlist
  `noreply@notify.cloudflare.com`.

### 4. Create the Access application and copy the AUD tag

1. In Cloudflare Zero Trust, go to `Access controls -> Applications`.
2. Select `Add an application -> Self-hosted`.
3. Use these values:
   - Application name: `HawksOptions Dashboard`
   - Session duration: `24 hours`
   - Public hostname: your chosen hostname, for example `options.example.com`
4. Under authentication settings:
   - Select the Google or Google Workspace provider you created
   - If that is the only provider, you may also enable `Instant Auth`
   - If you enabled OTP, add it as an allowed identity provider too
5. Add an access policy:
   - Policy name: `Allow only me`
   - Action: `Allow`
   - Include -> `Emails` -> `you@example.com`
6. Save the application.
7. Open the application details page and copy the `Application Audience (AUD) tag`.

You will place that AUD value in `CF_ACCESS_AUD`.

### 5. Install `cloudflared` on EC2

```bash
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  CF_RPM="cloudflared-linux-x86_64.rpm"  ;;
  aarch64) CF_RPM="cloudflared-linux-aarch64.rpm" ;;
  *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac

curl -L --output /tmp/cloudflared.rpm \
  "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF_RPM"
sudo rpm -i /tmp/cloudflared.rpm
rm /tmp/cloudflared.rpm

cloudflared --version
```

### 6. Authenticate `cloudflared`, create the tunnel, and publish the hostname

Run the browser login once on the EC2 host:

```bash
cloudflared tunnel login
```

Then move the origin certificate into `/etc/cloudflared`:

```bash
sudo install -d -m 0750 /etc/cloudflared
sudo mv ~/.cloudflared/cert.pem /etc/cloudflared/cert.pem
sudo chown root:root /etc/cloudflared/cert.pem
sudo chmod 0640 /etc/cloudflared/cert.pem
```

Create the tunnel:

```bash
sudo cloudflared --origincert /etc/cloudflared/cert.pem tunnel create hawksoptions
```

Look up the created tunnel UUID:

```bash
UUID="$(sudo cloudflared --origincert /etc/cloudflared/cert.pem tunnel list \
  | awk '/hawksoptions/ {print $1; exit}')"
echo "$UUID"
```

Write the tunnel config. Replace `options.example.com` with your hostname:

```bash
sudo tee /etc/cloudflared/config.yml > /dev/null <<YAML
tunnel: $UUID
credentials-file: /etc/cloudflared/$UUID.json

ingress:
  - hostname: options.example.com
    service: http://127.0.0.1:8080
  - service: http_status:404
YAML
```

Create the DNS route:

```bash
sudo cloudflared --origincert /etc/cloudflared/cert.pem \
  tunnel route dns hawksoptions options.example.com
```

### 7. Install the `cloudflared` systemd service

Create the service account and lock down `/etc/cloudflared`:

```bash
sudo groupadd --system cloudflared 2>/dev/null || true
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
  -g cloudflared cloudflared 2>/dev/null || true

sudo chown -R root:cloudflared /etc/cloudflared
sudo chmod 0750 /etc/cloudflared
sudo chmod 0640 /etc/cloudflared/*.json /etc/cloudflared/cert.pem /etc/cloudflared/config.yml
```

Install the bundled unit:

```bash
sudo install -m 0644 \
  /home/ec2-user/HawksOptions/scheduler/systemd/hawksoptions-cloudflared.service \
  /etc/systemd/system/hawksoptions-cloudflared.service

sudo systemctl daemon-reload
sudo systemctl enable --now hawksoptions-cloudflared.service
sudo systemctl status hawksoptions-cloudflared.service --no-pager
```

### 8. Update the dashboard env file with the Cloudflare values

Edit the env file:

```bash
sudo nano /etc/hawksoptions-dash/env
```

Use:

```bash
DASHBOARD_AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=<team-name>.cloudflareaccess.com
CF_ACCESS_AUD=<paste the Application Audience tag from Cloudflare>
DASHBOARD_ALLOWED_EMAILS=you@example.com

ALPACA_OPTIONS_DASHBOARD_PAPER_API_KEY=<read-only paper key>
ALPACA_OPTIONS_DASHBOARD_PAPER_SECRET_KEY=<read-only paper secret>
ALPACA_OPTIONS_DASHBOARD_LIVE_API_KEY=
ALPACA_OPTIONS_DASHBOARD_LIVE_SECRET_KEY=
```

Restart the dashboard:

```bash
sudo systemctl restart hawksoptions-dashboard.service
sudo systemctl status hawksoptions-dashboard.service --no-pager
```

### 9. Verify end-to-end

From your laptop or phone:

1. Visit `https://options.example.com`
2. Log in with Google or Google Workspace
3. If Google account 2FA is enabled, Google will enforce it there
4. The dashboard should render

From the EC2 host, confirm both services are healthy:

```bash
journalctl -u hawksoptions-dashboard.service -n 50 --no-pager
journalctl -u hawksoptions-cloudflared.service -n 50 --no-pager
tail -f /home/ec2-user/HawksOptions/logs/dashboard_access_*.log
```

Expected access log entries will include the authenticated email:

```text
identity=you@example.com
```

---

## Troubleshooting

### `403 user not allowlisted`

The Google login succeeded, but `DASHBOARD_ALLOWED_EMAILS` or the Cloudflare
Access policy does not include that email exactly.

### `401 invalid Cloudflare JWT`

Usually one of these is wrong:

- `CF_ACCESS_TEAM_DOMAIN`
- `CF_ACCESS_AUD`
- the dashboard is behind a different Access application than the one whose AUD
  you copied

### `cloudflared` cannot read `/etc/cloudflared/<UUID>.json`

Re-apply the ownership and mode:

```bash
sudo chown root:cloudflared /etc/cloudflared/*.json /etc/cloudflared/config.yml /etc/cloudflared/cert.pem
sudo chmod 0750 /etc/cloudflared
sudo chmod 0640 /etc/cloudflared/*.json /etc/cloudflared/config.yml /etc/cloudflared/cert.pem
sudo systemctl restart hawksoptions-cloudflared.service
```

### OTP codes appear "already used"

Your email security product likely opened the login link before you did.
Allowlist `noreply@notify.cloudflare.com` and request a fresh code.

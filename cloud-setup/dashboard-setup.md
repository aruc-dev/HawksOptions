# HawksOptions Dashboard Setup

## Security Model

- FastAPI binds to `127.0.0.1:8080`
- Cloudflare Access or SSH tunnel authenticates users
- The app is read-only by route design and by credential scope
- Dashboard credentials are distinct from trading credentials

## Local Mode

Set `DASHBOARD_AUTH_MODE=local` only when using an SSH tunnel or local-only
binding.

## Cloudflare Mode

Required environment variables:

- `DASHBOARD_AUTH_MODE=cloudflare`
- `CF_ACCESS_TEAM_DOMAIN`
- `CF_ACCESS_AUD`
- `DASHBOARD_ALLOWED_EMAILS`

## Run

```bash
uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
```

Use the systemd unit in `scheduler/systemd/hawksoptions-dashboard.service`
for production.

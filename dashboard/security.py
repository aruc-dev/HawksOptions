"""Request auth + access logging for the dashboard.

Two auth modes:

- 'local' — for Phase 1 (SSH tunnel): all requests accepted with identity
  'local-ssh'. Use DASHBOARD_AUTH_MODE=local ONLY behind a local-binding
  (127.0.0.1) uvicorn server. Never use in production with a public tunnel.

- 'cloudflare' — Phase 2: verifies the Cloudflare Access application token
  against Cloudflare's JWKS. The token is accepted from the
  Cf-Access-Jwt-Assertion header or, for browser flows, the CF_Authorization
  cookie. Issuer + audience must match the configured values. This is
  defense-in-depth: Cloudflare Access already enforces identity at the edge,
  but the app verifies again so that a misconfigured tunnel cannot silently
  bypass auth.

Every request (successful or rejected) is logged to
logs/dashboard_access_YYYYMMDD.log for audit.
"""
from __future__ import annotations

import logging
import time
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from dashboard.config import (
    AUTH_MODE_CLOUDFLARE,
    AUTH_MODE_LOCAL,
    cfg,
    get_allowed_emails,
    get_auth_mode,
    get_cf_audience,
    get_cf_team_domain,
)

log = logging.getLogger("dashboard.security")


# ── Cloudflare JWT verification ──────────────────────────────────────────────

_JWKS_CLIENT = None  # Lazily initialized PyJWKClient


def _get_jwks_client():
    """Lazy-init so tests can run without network or pyjwt installed."""
    global _JWKS_CLIENT
    if _JWKS_CLIENT is not None:
        return _JWKS_CLIENT
    team_domain = get_cf_team_domain()
    if not team_domain:
        raise RuntimeError(
            "CF_ACCESS_TEAM_DOMAIN is not set; required in cloudflare auth mode"
        )
    # Import here so local-mode tests don't need pyjwt installed.
    from jwt import PyJWKClient  # type: ignore

    _JWKS_CLIENT = PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs")
    return _JWKS_CLIENT


def verify_cloudflare_jwt(token: str) -> str:
    """Verify a Cloudflare Access JWT and return the authenticated email.

    Raises HTTPException(401) on any validation failure.
    """
    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "missing Cloudflare Access token "
                "(Cf-Access-Jwt-Assertion header or CF_Authorization cookie)"
            ),
        )

    team_domain = get_cf_team_domain()
    audience = get_cf_audience()
    if not team_domain or not audience:
        raise HTTPException(
            status_code=500,
            detail="Cloudflare auth misconfigured: CF_ACCESS_TEAM_DOMAIN or CF_ACCESS_AUD missing",
        )

    try:
        import jwt  # type: ignore

        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=f"https://{team_domain}",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"invalid Cloudflare JWT: {e}")

    email = (claims.get("email") or "").lower().strip()
    if not email:
        raise HTTPException(status_code=401, detail="Cloudflare JWT missing email claim")

    allowed = get_allowed_emails()
    if allowed and email not in allowed:
        log.warning("Cloudflare JWT email %s not in DASHBOARD_ALLOWED_EMAILS", email)
        raise HTTPException(status_code=403, detail="user not allowlisted")
    return email


def _get_cloudflare_token(request: Request) -> str:
    """Return the Cloudflare Access token from header or browser cookie."""
    return (
        request.headers.get("Cf-Access-Jwt-Assertion", "")
        or request.cookies.get("CF_Authorization", "")
    )


# ── Dependency that enforces auth on protected routes ────────────────────────

async def require_auth(request: Request) -> str:
    """FastAPI dependency: returns the authenticated email (or 'local-ssh').

    In local mode, returns 'local-ssh' without network / JWT. In cloudflare
    mode, verifies the JWT via verify_cloudflare_jwt().
    """
    mode = get_auth_mode()
    if mode == AUTH_MODE_LOCAL:
        request.state.identity = "local-ssh"
        return "local-ssh"
    if mode == AUTH_MODE_CLOUDFLARE:
        token = _get_cloudflare_token(request)
        email = verify_cloudflare_jwt(token)
        # Stash on request.state for access logging.
        request.state.identity = email
        return email
    raise HTTPException(status_code=500, detail=f"unknown auth mode: {mode}")


# ── Access logging middleware ────────────────────────────────────────────────

class AccessLogMiddleware(BaseHTTPMiddleware):
    """Writes one line per request to logs/dashboard_access_YYYYMMDD.log."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.time()
        # Don't resolve identity here — it's set by require_auth if applicable.
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            duration_ms = int((time.time() - start) * 1000)
            identity = getattr(request.state, "identity", None) or "unauthenticated"
            ip = request.client.host if request.client else "-"
            await asyncio.to_thread(
                _write_access_log,
                identity=identity,
                ip=ip,
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=duration_ms,
            )
        return response


def _access_log_path(now: Optional[datetime] = None) -> Path:
    now = now or datetime.now(timezone.utc)
    logs_dir = cfg().logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"dashboard_access_{now.strftime('%Y%m%d')}.log"


def _write_access_log(
    identity: str,
    ip: str,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
) -> None:
    try:
        now = datetime.now(timezone.utc)
        line = (
            f"{now.isoformat(timespec='seconds')} "
            f"identity={identity} ip={ip} method={method} "
            f"path={path} status={status} duration_ms={duration_ms}\n"
        )
        with open(_access_log_path(now), "a") as f:
            f.write(line)
    except Exception as e:
        # Never let logging failures break the response.
        log.warning("Could not write access log: %s", e)


def assert_production_auth_safe() -> None:
    """Called at app startup. Fails fast on an unsafe configuration.

    Rules:
      - Auth mode must be one of the recognized values (config.get_auth_mode raises otherwise).
      - In cloudflare mode, CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD must be set.
      - In cloudflare mode, DASHBOARD_ALLOWED_EMAILS must be set (defense-in-depth).
    """
    mode = get_auth_mode()
    if mode == AUTH_MODE_CLOUDFLARE:
        if not get_cf_team_domain():
            raise RuntimeError("CF_ACCESS_TEAM_DOMAIN must be set in cloudflare mode")
        if not get_cf_audience():
            raise RuntimeError("CF_ACCESS_AUD must be set in cloudflare mode")
        if not get_allowed_emails():
            raise RuntimeError(
                "DASHBOARD_ALLOWED_EMAILS must be set (comma-separated) in cloudflare mode"
            )
    elif mode == AUTH_MODE_LOCAL:
        log.warning(
            "Dashboard running in LOCAL auth mode — safe only behind SSH tunnel / 127.0.0.1"
        )

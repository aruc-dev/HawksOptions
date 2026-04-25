from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

try:
    from fastapi import HTTPException
    from dashboard import security
except ImportError as exc:  # pragma: no cover - optional dependency
    raise unittest.SkipTest("fastapi not installed") from exc

from dashboard import config as dash_config


def _local_mode():
    return patch.dict(os.environ, {"DASHBOARD_AUTH_MODE": "local"}, clear=False)


class SecurityTests(unittest.TestCase):
    def test_unknown_mode_rejected(self):
        with patch.dict(os.environ, {"DASHBOARD_AUTH_MODE": "wide-open"}, clear=False):
            with self.assertRaises(ValueError):
                dash_config.get_auth_mode()

    def test_local_mode_identity(self):
        import asyncio

        with _local_mode():
            class FakeReq:
                state = type("State", (), {})()
                headers = {}
                cookies = {}

            identity = asyncio.run(security.require_auth(FakeReq()))
        self.assertEqual(identity, "local-ssh")

    def test_cloudflare_requires_allowlist(self):
        env = {
            "DASHBOARD_AUTH_MODE": "cloudflare",
            "CF_ACCESS_TEAM_DOMAIN": "x.cloudflareaccess.com",
            "CF_ACCESS_AUD": "aud",
            "DASHBOARD_ALLOWED_EMAILS": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                security.assert_production_auth_safe()

    def test_missing_token_rejected(self):
        env = {
            "DASHBOARD_AUTH_MODE": "cloudflare",
            "CF_ACCESS_TEAM_DOMAIN": "x.cloudflareaccess.com",
            "CF_ACCESS_AUD": "aud",
            "DASHBOARD_ALLOWED_EMAILS": "arun@example.com",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(HTTPException):
                security.verify_cloudflare_jwt("")

    def test_valid_jwt_accepted(self):
        try:
            import jwt as pyjwt
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            raise unittest.SkipTest("pyjwt/cryptography not installed")

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        class FakeSigningKey:
            def __init__(self, public_key):
                self.key = public_key

        class FakeJWKSClient:
            def get_signing_key_from_jwt(self, token):
                return FakeSigningKey(key.public_key())

        now = int(time.time())
        token = pyjwt.encode(
            {
                "iss": "https://test.cloudflareaccess.com",
                "aud": "aud-abc",
                "email": "arun@example.com",
                "exp": now + 60,
                "iat": now,
            },
            key,
            algorithm="RS256",
        )
        env = {
            "DASHBOARD_AUTH_MODE": "cloudflare",
            "CF_ACCESS_TEAM_DOMAIN": "test.cloudflareaccess.com",
            "CF_ACCESS_AUD": "aud-abc",
            "DASHBOARD_ALLOWED_EMAILS": "arun@example.com",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(security, "_JWKS_CLIENT", FakeJWKSClient()):
            self.assertEqual(security.verify_cloudflare_jwt(token), "arun@example.com")


if __name__ == "__main__":
    unittest.main()

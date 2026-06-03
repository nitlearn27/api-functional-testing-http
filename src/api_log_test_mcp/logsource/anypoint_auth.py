"""Anypoint platform token provider (OAuth2 client-credentials).

Acquires a platform bearer token from the Anypoint Access Management token endpoint and caches
it until shortly before expiry. Kept strictly separate from the target-API auth
(``tools/auth.py``) so the two credential sets never mix. The client secret and the token are
never logged.
"""

from __future__ import annotations

import time

import httpx

from ..config import AnypointSettings

# Refresh this many seconds before the reported expiry to avoid using a just-expired token.
_EXPIRY_SKEW_SECONDS = 60.0
_DEFAULT_TIMEOUT = 30.0


class AnypointAuthError(Exception):
    """Could not obtain a platform token."""


class AnypointAuthProvider:
    """Fetches and caches an Anypoint client-credentials token."""

    def __init__(self, settings: AnypointSettings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid bearer token, fetching/refreshing if needed."""
        if not force_refresh and self._token and time.monotonic() < self._expires_at:
            return self._token
        return self._fetch()

    def _fetch(self) -> str:
        s = self._settings
        if not (s.token_endpoint and s.client_id and s.client_secret):
            raise AnypointAuthError(
                "missing Anypoint credentials (token_endpoint/client_id/client_secret in .env)"
            )

        payload = {
            "grant_type": s.grant_type,
            "client_id": s.client_id,
            "client_secret": s.client_secret,
        }

        owns = self._client is None
        client = self._client or httpx.Client(timeout=_DEFAULT_TIMEOUT)
        try:
            # Prefer JSON; fall back to form-encoding if the endpoint rejects JSON.
            resp = client.post(s.token_endpoint, json=payload)
            if resp.status_code in (400, 415):
                resp = client.post(s.token_endpoint, data=payload)
        except httpx.HTTPError as exc:
            raise AnypointAuthError(f"token request failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if owns:
                client.close()

        if resp.status_code != 200:
            raise AnypointAuthError(f"token endpoint returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise AnypointAuthError("token response was not JSON") from exc

        token = data.get("access_token")
        if not token:
            raise AnypointAuthError("token response had no access_token")

        expires_in = float(data.get("expires_in", 3600))
        self._token = token
        self._expires_at = time.monotonic() + max(expires_in - _EXPIRY_SKEW_SECONDS, 0.0)
        return token

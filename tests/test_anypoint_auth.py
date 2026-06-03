"""AnypointAuthProvider tests with httpx MockTransport (no network)."""

import httpx
import pytest

from api_log_test_mcp.config import AnypointSettings
from api_log_test_mcp.logsource.anypoint_auth import AnypointAuthError, AnypointAuthProvider


def _settings() -> AnypointSettings:
    return AnypointSettings(
        token_endpoint="https://anypoint.test/accounts/api/v2/oauth2/token",
        client_id="cid",
        client_secret="secret",
    )


def _provider(handler) -> AnypointAuthProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AnypointAuthProvider(_settings(), client=client)


def test_fetches_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    provider = _provider(handler)
    assert provider.get_token() == "tok-1"
    assert seen["url"].endswith("/oauth2/token")


def test_caches_token():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"tok-{calls['n']}", "expires_in": 3600})

    provider = _provider(handler)
    assert provider.get_token() == "tok-1"
    assert provider.get_token() == "tok-1"  # cached, no second call
    assert calls["n"] == 1
    assert provider.get_token(force_refresh=True) == "tok-2"
    assert calls["n"] == 2


def test_falls_back_to_form_encoding_on_415():
    attempts = {"json": 0, "form": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("content-type", "").startswith("application/json"):
            attempts["json"] += 1
            return httpx.Response(415, json={"error": "unsupported"})
        attempts["form"] += 1
        return httpx.Response(200, json={"access_token": "tok-form", "expires_in": 3600})

    provider = _provider(handler)
    assert provider.get_token() == "tok-form"
    assert attempts["json"] == 1 and attempts["form"] == 1


def test_raises_on_missing_credentials():
    provider = AnypointAuthProvider(AnypointSettings())
    with pytest.raises(AnypointAuthError):
        provider.get_token()


def test_raises_on_error_status():
    provider = _provider(lambda req: httpx.Response(401, json={"error": "bad"}))
    with pytest.raises(AnypointAuthError):
        provider.get_token()

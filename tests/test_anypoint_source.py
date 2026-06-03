"""AnypointLogSource tests with httpx MockTransport (no network)."""

import httpx
import pytest

from api_log_test_mcp.cache.snapshot_store import SnapshotStore
from api_log_test_mcp.config import AnypointSettings
from api_log_test_mcp.logsource.anypoint_source import AnypointLogError, AnypointLogSource

TOKEN_URL = "https://anypoint.test/accounts/api/v2/oauth2/token"
LOG_URL = "https://anypoint.test/amc/.../logs/file"

SAMPLE_LOG = (
    "2026-06-04 10:00:01 INFO Order intake started [correlationId: TC-001-abc]\n"
    "2026-06-04 10:00:02 INFO Order ACCEPTED sku=ABC-100 qty=2 [correlationId: TC-001-abc]\n"
    "2026-06-04 10:00:03 INFO unrelated line [correlationId: other-1]\n"
)


def _settings() -> AnypointSettings:
    return AnypointSettings(
        token_endpoint=TOKEN_URL, application_logs_fetch_url=LOG_URL,
        client_id="cid", client_secret="secret",
    )


def _source(handler) -> AnypointLogSource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AnypointLogSource(_settings(), client=client, sleep=lambda _s: None)


def _token_or(log_response):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return log_response(request)
    return handler


def test_snapshot_parses_text_logs():
    source = _source(_token_or(lambda req: httpx.Response(200, text=SAMPLE_LOG)))
    snap = source.snapshot()
    lines = snap.lines_by_instance["cloudhub"]
    assert len(lines) == 3
    assert "Order ACCEPTED sku=ABC-100 qty=2" in lines[1]


def test_snapshot_sends_bearer_token():
    seen = {}

    def log_handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text=SAMPLE_LOG)

    source = _source(_token_or(log_handler))
    source.snapshot()
    assert seen["auth"] == "Bearer tok"


def test_snapshot_parses_json_logs():
    def log_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"message": "line A"}, {"message": "line B"}]})

    source = _source(_token_or(log_handler))
    snap = source.snapshot()
    assert snap.lines_by_instance["cloudhub"] == ["line A", "line B"]


def test_snapshot_retries_on_429_then_succeeds():
    state = {"n": 0}

    def log_handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text=SAMPLE_LOG)

    source = _source(_token_or(log_handler))
    snap = source.snapshot()
    assert state["n"] == 2
    assert snap.total_lines() == 3


def test_snapshot_raises_on_non_retryable_status():
    source = _source(_token_or(lambda req: httpx.Response(404, text="not found")))
    with pytest.raises(AnypointLogError):
        source.snapshot()


def test_end_to_end_with_snapshot_store_and_correlation():
    """Download -> correlation index -> the TC-001 lines are retrievable by id."""
    source = _source(_token_or(lambda req: httpx.Response(200, text=SAMPLE_LOG)))
    store = SnapshotStore()
    snap = store.create(source)
    assert len(snap.lines_for("TC-001-abc")) == 2
    assert snap.lines_for("other-1")  # other correlation indexed too

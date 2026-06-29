"""call_api tests using httpx MockTransport — no real network."""

import httpx
import pytest

from api_log_test_mcp.tools.http_runner import (
    CORRELATION_HEADER,
    ApiCallError,
    call_api,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_call_api_posts_json_and_stamps_correlation_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["corr"] = request.headers.get(CORRELATION_HEADER)
        seen["body"] = request.content
        return httpx.Response(201, json={"status": "ACCEPTED", "sku": "ABC-100"})

    resp = call_api(
        "post", "https://api.test/orders",
        headers={"Content-Type": "application/json"},
        body={"sku": "ABC-100", "qty": 2},
        correlation_id="TC-001-abc",
        client=_client(handler),
    )

    assert resp.status == 201
    assert resp.body == {"status": "ACCEPTED", "sku": "ABC-100"}
    assert seen["method"] == "POST"
    assert seen["corr"] == "TC-001-abc"
    assert b"ABC-100" in seen["body"]


def test_call_api_generates_correlation_id_when_absent():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["corr"] = request.headers.get(CORRELATION_HEADER)
        return httpx.Response(200, text="ok")

    resp = call_api("get", "https://api.test/health", client=_client(handler))
    assert resp.status == 200
    assert resp.body == "ok"  # non-JSON content-type -> text
    assert captured["corr"]  # auto-generated


def test_call_api_wraps_transport_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ApiCallError):
        call_api("get", "https://unreachable.test/", client=_client(handler))


def test_call_api_connection_error_says_app_not_running():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(ApiCallError) as exc_info:
        call_api("get", "http://localhost:8081/api/patients", client=_client(handler))

    message = str(exc_info.value)
    assert "App not running" in message
    assert "http://localhost:8081/api/patients" in message


def test_call_api_non_connection_error_keeps_plain_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ApiCallError) as exc_info:
        call_api("get", "https://api.test/slow", client=_client(handler))

    # A read timeout is not a connection failure, so it should NOT be labelled "App not running".
    assert "App not running" not in str(exc_info.value)
    assert "ReadTimeout" in str(exc_info.value)

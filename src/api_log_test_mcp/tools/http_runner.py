"""call_api: the HttpRunner.

Column-driven: it takes whatever method/url/headers/body the suite row provides and fires the
request, so a changed sheet flows through without code edits. It stamps an ``X-Correlation-ID``
(generating one if absent) and returns a normalized :class:`ApiResponse`. Transport/timeout
problems are raised as :class:`ApiCallError` so the runner can attribute them to one case
instead of aborting the whole suite.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from ..models import ApiResponse

DEFAULT_TIMEOUT_SECONDS = 30.0
CORRELATION_HEADER = "X-Correlation-ID"


class ApiCallError(Exception):
    """A request could not be completed (timeout, DNS, connection, etc.)."""


def call_api(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    correlation_id: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> ApiResponse:
    """Fire a single HTTP request and return a normalized response.

    ``body`` is sent as JSON when it is a dict/list, otherwise as raw text. ``client`` is
    injectable for testing (e.g. an httpx.Client backed by a MockTransport).
    """
    correlation_id = correlation_id or uuid.uuid4().hex
    request_headers = dict(headers or {})
    request_headers.setdefault(CORRELATION_HEADER, correlation_id)

    kwargs: dict[str, Any] = {"headers": request_headers}
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif body is not None:
        kwargs["content"] = body if isinstance(body, (bytes, str)) else str(body)

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.request(method.upper(), url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Nothing listening / host unreachable — surface it plainly so a down app reads as such.
        raise ApiCallError(
            f"App not running / unreachable at {url} ({type(exc).__name__}: {exc})"
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiCallError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    return ApiResponse(
        status=response.status_code,
        headers=dict(response.headers),
        body=_parse_body(response),
        latency_ms=_latency_ms(response),
    )


def _latency_ms(response: httpx.Response) -> float | None:
    """Elapsed time in ms; None when unavailable (e.g. mocked transport)."""
    try:
        return response.elapsed.total_seconds() * 1000
    except RuntimeError:
        return None


def _parse_body(response: httpx.Response) -> Any:
    """JSON-decode the body when the response declares JSON; otherwise return text."""
    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            return response.json()
        except ValueError:
            return response.text
    return response.text

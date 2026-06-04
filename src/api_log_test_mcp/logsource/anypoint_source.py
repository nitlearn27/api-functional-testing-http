"""AnypointLogSource — download CloudHub application logs from Anypoint.

Implements ``snapshot()`` against the CloudHub 2.0 log-file endpoint configured in ``.env``
(``application_logs_fetch_url``). One download per run (the snapshot store reuses it across all
cases), with a small bounded backoff on transient 429/500 responses.

The configured URL is a ``.../deployments/{id}/specs/{version}/logs/file`` template. The spec
``{version}`` changes on **every redeploy** (a new spec + new replicas), so a pinned version
silently serves the old, shut-down replica's logs. ``_log_url()`` therefore resolves the
deployment's *current* version at fetch time and rebuilds the URL, falling back to the
configured URL as-is if the shape is unexpected or resolution fails.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import AnypointSettings
from .anypoint_auth import AnypointAuthProvider
from .base import LogSource, RawSnapshot

_DEFAULT_TIMEOUT = 60.0
_INSTANCE = "cloudhub"
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


class AnypointLogError(Exception):
    """Could not download CloudHub logs."""


class AnypointLogSource(LogSource):
    """Real CloudHub log backend (CH2 log-file download)."""

    def __init__(
        self,
        settings: AnypointSettings,
        auth: AnypointAuthProvider | None = None,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ):
        self._settings = settings
        self._auth = auth or AnypointAuthProvider(settings, client=client)
        self._client = client
        self._sleep = sleep

    def discover_instances(self) -> list[str]:
        # Single fixed deployment for now; multi-replica discovery is a future enhancement.
        return [_INSTANCE]

    def snapshot(self, instances: list[str] | None = None) -> RawSnapshot:
        owns = self._client is None
        client = self._client or httpx.Client(timeout=_DEFAULT_TIMEOUT)
        try:
            url = self._log_url(client)
            if not url:
                raise AnypointLogError("application_logs_fetch_url is not set in .env")
            response = self._get_with_retry(client, url)
        finally:
            if owns:
                client.close()

        lines = _parse_log_body(response)
        return RawSnapshot(lines_by_instance={_INSTANCE: lines})

    def _log_url(self, client: httpx.Client) -> str | None:
        """Resolve the log-file URL, swapping the pinned spec for the deployment's live version.

        Reads ``desiredVersion`` (the running spec) from the deployment and rebuilds
        ``.../deployments/{id}/specs/{liveVersion}/logs/file``. If the configured URL is not in
        that shape, or the lookup fails, the configured URL is returned unchanged.
        """
        configured = self._settings.application_logs_fetch_url
        if not configured or "/specs/" not in configured:
            return configured

        base, spec_tail = configured.split("/specs/", 1)  # base=.../deployments/{id}
        tail = spec_tail.split("/", 1)[1] if "/" in spec_tail else "logs/file"  # "logs/file"
        try:
            version = self._current_version(client, base)
        except (httpx.HTTPError, AnypointLogError, ValueError, KeyError):
            return configured  # network/parse trouble: fall back to the pinned URL
        return f"{base}/specs/{version}/{tail}" if version else configured

    def _current_version(self, client: httpx.Client, deployment_url: str) -> str | None:
        """The deployment's currently-running spec version (``desiredVersion``)."""
        token = self._auth.get_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = client.get(deployment_url, headers=headers)
        if response.status_code != 200:
            raise AnypointLogError(f"deployment lookup returned HTTP {response.status_code}")
        data = response.json()
        return data.get("desiredVersion") or data.get("lastSuccessfulVersion")

    def _get_with_retry(self, client: httpx.Client, url: str) -> httpx.Response:
        last_status: int | None = None
        for attempt in range(_MAX_RETRIES):
            token = self._auth.get_token(force_refresh=attempt > 0 and last_status == 401)
            headers = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
            try:
                response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise AnypointLogError(f"log download failed: {type(exc).__name__}: {exc}") from exc

            if response.status_code == 200:
                return response
            last_status = response.status_code
            # Retry on transient throttle/file-limit/auth errors; otherwise fail fast.
            if response.status_code not in (401, 429, 500, 502, 503):
                raise AnypointLogError(f"log endpoint returned HTTP {response.status_code}")
            if attempt < _MAX_RETRIES - 1:
                self._sleep(_BACKOFF_BASE_SECONDS * (2**attempt))

        raise AnypointLogError(
            f"log download failed after {_MAX_RETRIES} attempts (last HTTP {last_status})"
        )


def _parse_log_body(response: httpx.Response) -> list[str]:
    """Turn the log response into a list of log lines.

    ``/logs/file`` typically returns the raw text log file; JSON shapes are handled as a
    fallback so the source still works if the endpoint returns structured records.
    """
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            return _lines_from_json(response.json())
        except ValueError:
            pass
    return response.text.splitlines()


def _lines_from_json(payload: Any) -> list[str]:
    """Extract log-line strings from common JSON shapes."""
    if isinstance(payload, dict):
        for key in ("data", "logs", "events", "records", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return [str(payload)]

    lines: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            for key in ("message", "line", "log", "logLine", "msg", "text"):
                if key in item:
                    lines.append(str(item[key]))
                    break
            else:
                lines.append(str(item))
        else:
            lines.append(str(item))
    return lines

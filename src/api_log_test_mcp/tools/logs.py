"""snapshot_logs and validate_logs: the download-once / validate-locally / discard pipeline.

``snapshot_logs`` downloads via the configured LogSource into the ephemeral store and returns
a ``snapshot_id`` handle. ``validate_logs`` filters that snapshot by correlation ID and
checks the expected strings — purely in memory, no further downloads.
"""

from __future__ import annotations

import re

from ..cache.snapshot_store import SnapshotStore
from ..config import Settings, get_anypoint_settings
from ..logsource.anypoint_source import AnypointLogSource
from ..logsource.base import LogSource
from ..logsource.file_source import FileLogSource
from ..models import LogMatchMode, LogValidationResult

# Module-level store so a snapshot_id from snapshot_logs is resolvable by validate_logs
# within the same server process.
_STORE = SnapshotStore()


def resolve_anypoint_logs_url() -> str | None:
    """Dynamically query Anypoint deployments to find the ID matching the project folder/pom.xml."""
    import xml.etree.ElementTree as ET
    from pathlib import Path

    import httpx

    settings = get_anypoint_settings()
    if not (
        settings.client_id
        and settings.client_secret
        and settings.token_endpoint
        and settings.deployments_base_url
    ):
        return None

    # Determine target app name from pom.xml or current directory name
    app_name = None
    cwd = Path.cwd()
    pom = cwd / "pom.xml"
    if pom.exists():
        try:
            tree = ET.parse(pom)
            root = tree.getroot()
            ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
            name_elem = root.find(f"{ns}name")
            if name_elem is not None and name_elem.text:
                app_name = name_elem.text.strip()
            else:
                art_elem = root.find(f"{ns}artifactId")
                if art_elem is not None and art_elem.text:
                    app_name = art_elem.text.strip()
        except Exception:
            pass
    if not app_name:
        app_name = cwd.name

    if not app_name:
        return None

    # Query Anypoint platform
    payload = {
        "grant_type": settings.grant_type,
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
    }
    try:
        resp = httpx.post(settings.token_endpoint, json=payload, timeout=10.0)
        if resp.status_code in (400, 415):
            resp = httpx.post(settings.token_endpoint, data=payload, timeout=10.0)
        if resp.status_code != 200:
            return None
        token = resp.json().get("access_token")
        if not token:
            return None

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        res = httpx.get(settings.deployments_base_url, headers=headers, timeout=15.0)
        if res.status_code != 200:
            return None

        deployments = res.json()
        items = deployments.get("items", []) if isinstance(deployments, dict) else deployments

        target = app_name.lower().strip()
        for item in items:
            name = item.get("name") or item.get("application", {}).get("name")
            if name and name.lower().strip() == target:
                dep_id = item.get("id")
                if dep_id:
                    return f"{settings.deployments_base_url.rstrip('/')}/{dep_id}"
    except Exception:
        pass
    return None


def build_log_source(
    log_source: str,
    settings: Settings,
    *,
    application_logs_fetch_url: str | None = None,
) -> LogSource:
    """Construct the LogSource named by a case's ``log_source`` column value.

    For ``anypoint`` the log-fetch URL comes from the suite sheet (``application_logs_fetch_url``).
    If missing, it attempts to resolve it dynamically by querying the Anypoint platform for a
    deployment matching the current application name.
    """
    name = (log_source or "").lower()
    if name == "file":
        if not settings.file_log_path:
            raise ValueError("ALT_FILE_LOG_PATH must be set when log_source=file")
        return FileLogSource(settings.file_log_path)
    if name == "anypoint":
        if not application_logs_fetch_url:
            application_logs_fetch_url = resolve_anypoint_logs_url()
        if not application_logs_fetch_url:
            raise ValueError(
                "application_logs_fetch_url not set in the suite and could not "
                "be resolved dynamically from Anypoint (add an "
                "'application_logs_fetch_url | <url>' metadata row)"
            )
        anypoint = get_anypoint_settings().model_copy(
            update={"application_logs_fetch_url": application_logs_fetch_url}
        )
        return AnypointLogSource(anypoint)
    raise ValueError(f"unsupported log source: {log_source!r}")


def snapshot_logs(
    settings: Settings,
    instances: list[str] | None = None,
    *,
    log_source: str | None = None,
    application_logs_fetch_url: str | None = None,
    store: SnapshotStore | None = None,
) -> str:
    """Download logs once and return a snapshot_id handle."""
    store = store or _STORE
    source = build_log_source(
        log_source or settings.log_backend.value,
        settings,
        application_logs_fetch_url=application_logs_fetch_url,
    )
    snap = store.create(source, instances)
    return snap.snapshot_id


def validate_logs(
    snapshot_id: str,
    correlation_id: str,
    expected: list[str],
    mode: LogMatchMode = LogMatchMode.CONTAINS,
    *,
    correlation_fallback: bool = True,
    store: SnapshotStore | None = None,
) -> LogValidationResult:
    """Check that every expected string appears in the snapshot's lines for the correlation ID.

    If no lines carry the correlation id and ``correlation_fallback`` is set, fall back to
    matching against the entire snapshot (and flag it).
    """
    store = store or _STORE
    snap = store.get(snapshot_id)
    lines = snap.lines_for(correlation_id)

    used_fallback = False
    if not lines and correlation_fallback:
        lines = snap.all_lines()
        used_fallback = True

    matched: list[str] = []
    missing: list[str] = []
    for needle in expected:
        if _matches(needle, lines, mode):
            matched.append(needle)
        else:
            missing.append(needle)

    # any_of passes if at least one string is found; every other mode requires all of them.
    passed = bool(matched) if mode is LogMatchMode.ANY_OF else not missing

    return LogValidationResult(
        passed=passed,
        correlation_id=correlation_id,
        matched=matched,
        missing=missing,
        lines_considered=len(lines),
        used_fallback=used_fallback,
    )


def matched_log_lines(
    snapshot_id: str,
    correlation_id: str,
    expected: list[str],
    mode: LogMatchMode = LogMatchMode.CONTAINS,
    *,
    correlation_fallback: bool = True,
    store: SnapshotStore | None = None,
) -> dict[str, list[str]]:
    """For each expected string, the snapshot lines it matches (as log evidence).

    Uses the same correlation-id scoping (and whole-log fallback) as :func:`validate_logs`, so
    the lines returned here are exactly the ones that decided the pass/fail. Each value is the
    full list of matching lines; an expected string with no match maps to an empty list.
    """
    store = store or _STORE
    snap = store.get(snapshot_id)
    lines = snap.lines_for(correlation_id)
    if not lines and correlation_fallback:
        lines = snap.all_lines()
    return {needle: _matching_lines(needle, lines, mode) for needle in expected}


def correlation_present(
    snapshot_id: str, correlation_id: str, *, store: SnapshotStore | None = None
) -> bool:
    """Whether the snapshot has any line carrying ``correlation_id`` (gates fetch retries)."""
    store = store or _STORE
    return bool(store.get(snapshot_id).lines_for(correlation_id))


def discard_snapshot(snapshot_id: str, *, store: SnapshotStore | None = None) -> bool:
    """Drop a snapshot from memory."""
    store = store or _STORE
    return store.discard(snapshot_id)


def _matching_lines(needle: str, lines: list[str], mode: LogMatchMode) -> list[str]:
    """Every line ``needle`` matches. regex mode treats it as a pattern; others are substring."""
    if mode is LogMatchMode.REGEX:
        pattern = re.compile(needle)
        return [line for line in lines if pattern.search(line)]
    # contains / all_of / any_of all use plain substring matching per string.
    return [line for line in lines if needle in line]


def _matches(needle: str, lines: list[str], mode: LogMatchMode) -> bool:
    """Does ``needle`` appear in any line? regex mode treats it as a pattern."""
    return bool(_matching_lines(needle, lines, mode))

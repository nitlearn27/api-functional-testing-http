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


def build_log_source(
    log_source: str,
    settings: Settings,
    *,
    application_logs_fetch_url: str | None = None,
) -> LogSource:
    """Construct the LogSource named by a case's ``log_source`` column value.

    For ``anypoint`` the log-fetch URL comes from the suite sheet
    (``application_logs_fetch_url``), not ``.env``; it is required, so an empty value raises.
    """
    name = (log_source or "").lower()
    if name == "file":
        if not settings.file_log_path:
            raise ValueError("ALT_FILE_LOG_PATH must be set when log_source=file")
        return FileLogSource(settings.file_log_path)
    if name == "anypoint":
        if not application_logs_fetch_url:
            raise ValueError(
                "application_logs_fetch_url not set in the suite "
                "(add an 'application_logs_fetch_url | <url>' metadata row)"
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

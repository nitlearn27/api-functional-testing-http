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


def build_log_source(log_source: str, settings: Settings) -> LogSource:
    """Construct the LogSource named by a case's ``log_source`` column value."""
    name = (log_source or "").lower()
    if name == "file":
        if not settings.file_log_path:
            raise ValueError("ALT_FILE_LOG_PATH must be set when log_source=file")
        return FileLogSource(settings.file_log_path)
    if name == "anypoint":
        return AnypointLogSource(get_anypoint_settings())
    raise ValueError(f"unsupported log source: {log_source!r}")


def snapshot_logs(
    settings: Settings,
    instances: list[str] | None = None,
    *,
    log_source: str | None = None,
    store: SnapshotStore | None = None,
) -> str:
    """Download logs once and return a snapshot_id handle."""
    store = store or _STORE
    source = build_log_source(log_source or settings.log_backend.value, settings)
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


def discard_snapshot(snapshot_id: str, *, store: SnapshotStore | None = None) -> bool:
    """Drop a snapshot from memory."""
    store = store or _STORE
    return store.discard(snapshot_id)


def _matches(needle: str, lines: list[str], mode: LogMatchMode) -> bool:
    """Does ``needle`` appear in any line? regex mode treats it as a pattern."""
    if mode is LogMatchMode.REGEX:
        pattern = re.compile(needle)
        return any(pattern.search(line) for line in lines)
    # contains / all_of / any_of all use plain substring matching per string.
    return any(needle in line for line in lines)

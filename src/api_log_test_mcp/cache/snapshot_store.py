"""Ephemeral snapshot cache and correlation-ID index.

Lifecycle: download once via a LogSource -> build a correlation-ID -> lines index in memory
-> validate locally as many times as needed -> discard. Nothing is persisted; a snapshot
lives only until ``discard`` (or process exit).

The correlation ID is extracted from each line with a configurable regex. The default
matches common Mule/MDC forms: ``correlationId: <id>``, ``correlationId=<id>``,
``[correlationId: <id>]`` (case-insensitive, optional quotes).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from ..logsource.base import LogSource, RawSnapshot

DEFAULT_CORRELATION_PATTERN = re.compile(
    r"""correlation[_-]?id["']?\s*[:=]\s*["']?([A-Za-z0-9._-]+)""",
    re.IGNORECASE,
)


@dataclass
class Snapshot:
    """One downloaded, indexed snapshot held in memory."""

    snapshot_id: str
    raw: RawSnapshot
    index: dict[str, list[str]] = field(default_factory=dict)

    def total_lines(self) -> int:
        return self.raw.total_lines()

    def lines_for(self, correlation_id: str) -> list[str]:
        return self.index.get(correlation_id, [])

    def all_lines(self) -> list[str]:
        """Every line across all instances (used for correlation fallback)."""
        return [line for lines in self.raw.lines_by_instance.values() for line in lines]


class SnapshotStore:
    """In-memory registry of active snapshots."""

    def __init__(self, pattern: re.Pattern[str] = DEFAULT_CORRELATION_PATTERN):
        self._pattern = pattern
        self._snapshots: dict[str, Snapshot] = {}

    def create(
        self, source: LogSource, instances: list[str] | None = None
    ) -> Snapshot:
        """Download once via ``source`` and build the correlation index."""
        raw = source.snapshot(instances)
        snapshot_id = uuid.uuid4().hex
        index = self._build_index(raw)
        snap = Snapshot(snapshot_id=snapshot_id, raw=raw, index=index)
        self._snapshots[snapshot_id] = snap
        return snap

    def get(self, snapshot_id: str) -> Snapshot:
        if snapshot_id not in self._snapshots:
            raise KeyError(f"unknown snapshot_id: {snapshot_id}")
        return self._snapshots[snapshot_id]

    def discard(self, snapshot_id: str) -> bool:
        """Drop a snapshot from memory. Returns True if it existed."""
        return self._snapshots.pop(snapshot_id, None) is not None

    def discard_all(self) -> None:
        self._snapshots.clear()

    def _build_index(self, raw: RawSnapshot) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for lines in raw.lines_by_instance.values():
            for line in lines:
                match = self._pattern.search(line)
                if match:
                    index.setdefault(match.group(1), []).append(line)
        return index

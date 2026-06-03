"""LogSource interface.

A LogSource knows how to *download* log lines for one or more instances exactly once per
run. It deliberately does no filtering or correlation indexing — that is the snapshot
store's job — so every backend (file mock, Anypoint, future CloudWatch) only implements the
download.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RawSnapshot:
    """The raw result of downloading logs: instance name -> list of log lines."""

    lines_by_instance: dict[str, list[str]] = field(default_factory=dict)

    def total_lines(self) -> int:
        return sum(len(v) for v in self.lines_by_instance.values())


class LogSource(ABC):
    """Abstract log backend. One ``snapshot`` call == one download per instance."""

    @abstractmethod
    def discover_instances(self) -> list[str]:
        """Return the active instance identifiers to download from."""

    @abstractmethod
    def snapshot(self, instances: list[str] | None = None) -> RawSnapshot:
        """Download log lines once. If ``instances`` is None, download from all discovered."""

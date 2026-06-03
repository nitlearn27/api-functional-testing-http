"""FileLogSource: mock backend that reads log lines from a local file.

Lets the entire log pipeline (snapshot -> correlation index -> validate) be built and
tested with zero backend access. The "instance" is just the file's stem.
"""

from __future__ import annotations

from pathlib import Path

from .base import LogSource, RawSnapshot


class FileLogSource(LogSource):
    """Reads one or more local log files. Each file maps to one instance."""

    def __init__(self, paths: str | Path | list[str | Path]):
        if isinstance(paths, (str, Path)):
            paths = [paths]
        self._paths = [Path(p) for p in paths]

    def discover_instances(self) -> list[str]:
        return [p.stem for p in self._paths]

    def snapshot(self, instances: list[str] | None = None) -> RawSnapshot:
        snap = RawSnapshot()
        for path in self._paths:
            if instances is not None and path.stem not in instances:
                continue
            if not path.exists():
                raise FileNotFoundError(f"log file not found: {path}")
            text = path.read_text(encoding="utf-8", errors="replace")
            snap.lines_by_instance[path.stem] = text.splitlines()
        return snap

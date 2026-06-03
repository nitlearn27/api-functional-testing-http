"""Pluggable log backends behind a single LogSource interface."""

from .base import LogSource, RawSnapshot
from .file_source import FileLogSource

__all__ = ["LogSource", "RawSnapshot", "FileLogSource"]

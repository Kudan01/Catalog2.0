from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .media_types import MediaType


@dataclass(frozen=True)
class FolderRecord:
    """Folder metadata produced by the read-only scanner."""

    rel_path: str
    path_key: str
    parent_rel: str | None
    parent_path_key: str | None
    name: str
    depth: int
    sort_key: str


@dataclass(frozen=True)
class MediaRecord:
    """Media metadata produced by the read-only scanner."""

    abs_path: Path
    rel_path: str
    path_key: str
    folder_rel: str
    folder_path_key: str
    file_name: str
    extension: str
    media_type: MediaType
    size_bytes: int
    modified_time: float
    sort_key: str


@dataclass(frozen=True)
class ScanErrorRecord:
    """A non-fatal filesystem error encountered during scanning."""

    rel_path: str | None
    operation: str
    error_type: str
    message: str


@dataclass(frozen=True)
class ScanSkipRecord:
    """A filesystem entry deliberately skipped by scanner policy."""

    rel_path: str
    reason: str

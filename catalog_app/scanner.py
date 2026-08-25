from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias

from .config import Config
from .media_types import classify_media, normalize_extension
from .models import (
    FolderRecord,
    MediaRecord,
    ScanErrorRecord,
    ScanSkipRecord,
)
from .paths import normalize_catalog_relative_path, safe_join_catalog_path
from .sorting import catalog_path_key, natural_sort_key


IGNORED_DIRECTORY_NAMES = frozenset({
    "__pycache__",
    ".git",
    ".idea",
    ".venv",
})

ScanRecord: TypeAlias = (
    FolderRecord | MediaRecord | ScanErrorRecord | ScanSkipRecord
)


class ScannerError(RuntimeError):
    """Raised when a requested scan scope cannot be read safely."""


@dataclass(frozen=True)
class SourceRootStatus:
    """Cheap runtime status of the configured source root."""

    path: Path
    available: bool
    exists: bool
    is_dir: bool
    readable: bool
    reason: str
    message: str


def source_root_status(config: Config) -> SourceRootStatus:
    """Return whether data_root is currently usable for scan/update work.

    This check is intentionally non-recursive. It only verifies that the
    configured source root itself exists, is a directory and can be opened.
    """
    root = config.data_root.expanduser()

    try:
        if not root.exists():
            return SourceRootStatus(
                path=root,
                available=False,
                exists=False,
                is_dir=False,
                readable=False,
                reason="missing",
                message=(
                    f"Zdrojová složka není dostupná: {root}. "
                    "Aktualizace nebyla spuštěna a katalog se nemění."
                ),
            )

        if not root.is_dir():
            return SourceRootStatus(
                path=root,
                available=False,
                exists=True,
                is_dir=False,
                readable=False,
                reason="not_directory",
                message=(
                    f"Zdrojová cesta není složka: {root}. "
                    "Aktualizace nebyla spuštěna a katalog se nemění."
                ),
            )

        try:
            with os.scandir(root):
                pass
        except OSError as exc:
            return SourceRootStatus(
                path=root,
                available=False,
                exists=True,
                is_dir=True,
                readable=False,
                reason="not_readable",
                message=(
                    f"Zdrojovou složku nelze přečíst: {root}: {exc}. "
                    "Aktualizace nebyla spuštěna a katalog se nemění."
                ),
            )

        return SourceRootStatus(
            path=root.resolve(strict=False),
            available=True,
            exists=True,
            is_dir=True,
            readable=True,
            reason="ok",
            message="Zdrojová složka je dostupná.",
        )
    except OSError as exc:
        return SourceRootStatus(
            path=root,
            available=False,
            exists=False,
            is_dir=False,
            readable=False,
            reason="error",
            message=(
                f"Zdrojovou složku nelze ověřit: {root}: {exc}. "
                "Aktualizace nebyla spuštěna a katalog se nemění."
            ),
        )


def ensure_source_root_available(config: Config) -> None:
    """Raise ScannerError if data_root is not safe to use for scan/update."""
    status = source_root_status(config)
    if not status.available:
        raise ScannerError(status.message)


def validate_scan_scope(config: Config, branch_rel_path: str = "") -> str:
    """
    Validate and normalize the requested catalog branch.

    The empty string means the whole data_root. Non-empty branches must point to
    an existing real directory inside data_root. The target itself must not be a
    symlink or Windows junction.
    """
    normalized = normalize_catalog_relative_path(
        branch_rel_path,
        allow_root=True,
    )
    ensure_source_root_available(config)
    root = config.data_root.expanduser().resolve(strict=True)
    target = safe_join_catalog_path(root, normalized, allow_root=True)

    if _is_path_link_or_junction(target):
        raise ScannerError(
            f"Scanovaná větev je odkaz nebo junction a nebude následována: "
            f"{normalized or '[kořen]'}"
        )

    try:
        if not target.exists():
            raise ScannerError(
                f"Scanovaná větev neexistuje: {normalized or '[kořen]'}"
            )
        if not target.is_dir():
            raise ScannerError(
                f"Scanovaná větev není složka: {normalized or '[kořen]'}"
            )
    except OSError as exc:
        raise ScannerError(
            f"Scanovanou větev nelze ověřit: {normalized or '[kořen]'}: {exc}"
        ) from exc

    return normalized


def scan_data_root(
    config: Config,
    *,
    branch_rel_path: str = "",
) -> Iterator[ScanRecord]:
    """
    Traverse data_root or a selected branch without modifying files.

    Directory symlinks, file symlinks and Windows junctions are skipped.
    Filesystem errors are returned as ScanErrorRecord and do not stop the scan.
    """
    branch_rel_path = validate_scan_scope(config, branch_rel_path)
    root = config.data_root.expanduser().resolve(strict=True)

    if branch_rel_path:
        start_path = safe_join_catalog_path(root, branch_rel_path)
        branch_parts = PurePosixPath(branch_rel_path).parts
        parent_rel = "/".join(branch_parts[:-1]) if len(branch_parts) > 1 else ""
        start_depth = len(branch_parts)
    else:
        start_path = root
        parent_rel = None
        start_depth = 0

    stack: list[tuple[Path, str, str | None, int]] = [
        (start_path, branch_rel_path, parent_rel, start_depth),
    ]

    while stack:
        current_path, rel_path, parent_rel, depth = stack.pop()
        parent_path_key = (
            catalog_path_key(parent_rel)
            if parent_rel is not None
            else None
        )
        folder_name = root.name if depth == 0 else current_path.name

        yield FolderRecord(
            rel_path=rel_path,
            path_key=catalog_path_key(rel_path),
            parent_rel=parent_rel,
            parent_path_key=parent_path_key,
            name=folder_name,
            depth=depth,
            sort_key=natural_sort_key(folder_name),
        )

        try:
            iterator = os.scandir(current_path)
        except OSError as exc:
            yield _scan_error(rel_path, "list_directory", exc)
            continue

        child_directories: list[tuple[Path, str, str, int]] = []

        try:
            with iterator:
                for entry in iterator:
                    entry_rel = _child_rel_path(rel_path, entry.name)

                    try:
                        if _is_link_or_junction(entry):
                            yield ScanSkipRecord(
                                rel_path=entry_rel,
                                reason="link_or_junction",
                            )
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in IGNORED_DIRECTORY_NAMES:
                                yield ScanSkipRecord(
                                    rel_path=entry_rel,
                                    reason="ignored_directory",
                                )
                                continue

                            child_directories.append(
                                (Path(entry.path), entry_rel, rel_path, depth + 1)
                            )
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            yield ScanSkipRecord(
                                rel_path=entry_rel,
                                reason="unsupported_filesystem_entry",
                            )
                            continue

                        try:
                            stat_result = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            yield _scan_error(entry_rel, "stat_file", exc)
                            continue

                        extension = normalize_extension(entry.name)

                        yield MediaRecord(
                            abs_path=Path(entry.path),
                            rel_path=entry_rel,
                            path_key=catalog_path_key(entry_rel),
                            folder_rel=rel_path,
                            folder_path_key=catalog_path_key(rel_path),
                            file_name=entry.name,
                            extension=extension,
                            media_type=classify_media(extension),
                            size_bytes=stat_result.st_size,
                            modified_time=stat_result.st_mtime,
                            sort_key=natural_sort_key(entry.name),
                        )

                    except OSError as exc:
                        yield _scan_error(entry_rel, "inspect_entry", exc)
        except OSError as exc:
            yield _scan_error(rel_path, "iterate_directory", exc)

        # LIFO stack: reverse to preserve the filesystem enumeration order.
        stack.extend(reversed(child_directories))


def _child_rel_path(parent_rel: str, name: str) -> str:
    raw = name if not parent_rel else f"{parent_rel}/{name}"
    return normalize_catalog_relative_path(raw)


def _is_link_or_junction(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True

    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(entry.path))


def _is_path_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True

    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _scan_error(
    rel_path: str | None,
    operation: str,
    exc: OSError,
) -> ScanErrorRecord:
    return ScanErrorRecord(
        rel_path=rel_path,
        operation=operation,
        error_type=type(exc).__name__,
        message=str(exc),
    )

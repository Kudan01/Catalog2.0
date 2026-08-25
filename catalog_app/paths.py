from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


class PathValidationError(ValueError):
    """Raised when a catalog-relative path is unsafe or invalid."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def normalize_catalog_relative_path(
    value: str | Path,
    *,
    allow_root: bool = False,
) -> str:
    """
    Normalize a catalog-relative path to forward slashes.

    The empty string represents the catalog root only when allow_root=True.
    Absolute paths, drive-qualified paths, UNC paths and ".." are rejected.
    """
    text = str(value)

    if "\x00" in text:
        raise PathValidationError("null_character", "Cesta nesmí obsahovat nulový znak.")

    if text == "":
        if allow_root:
            return ""
        raise PathValidationError("empty", "Prázdná cesta není povolená.")

    windows_path = PureWindowsPath(text)

    if windows_path.drive:
        raise PathValidationError(
            "windows_absolute",
            "Absolutní nebo disková Windows cesta není povolená.",
        )

    normalized_text = text.replace("\\", "/")

    if normalized_text.startswith("/"):
        raise PathValidationError("absolute", "Absolutní cesta není povolená.")

    parts: list[str] = []

    for part in normalized_text.split("/"):
        if part in ("", "."):
            continue

        if part == "..":
            raise PathValidationError("parent_reference", "Cesta nesmí obsahovat '..'.")

        parts.append(part)

    normalized = "/".join(parts)

    if not normalized and not allow_root:
        raise PathValidationError("empty", "Prázdná cesta není povolená.")

    return normalized


def safe_join_catalog_path(
    root: Path,
    relative_path: str | Path,
    *,
    allow_root: bool = False,
) -> Path:
    """
    Join a catalog-relative path to root and verify the resolved result.

    Resolving the result also protects against an existing symlink or junction
    that points outside the permitted root.
    """
    normalized = normalize_catalog_relative_path(
        relative_path,
        allow_root=allow_root,
    )

    resolved_root = root.expanduser().resolve(strict=False)

    if normalized:
        candidate = resolved_root.joinpath(*PurePosixPath(normalized).parts)
    else:
        candidate = resolved_root

    resolved_candidate = candidate.resolve(strict=False)

    if not is_path_within(resolved_candidate, resolved_root):
        raise PathValidationError("outside_root", "Výsledná cesta leží mimo povolený kořen.")

    return resolved_candidate


def is_path_within(path: Path, parent: Path) -> bool:
    """Return whether path is equal to or located below parent."""
    resolved_path = path.expanduser().resolve(strict=False)
    resolved_parent = parent.expanduser().resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_parent)
        return True
    except ValueError:
        return False


def same_path(first: Path, second: Path) -> bool:
    """Return whether two paths resolve to the same location."""
    return (
        first.expanduser().resolve(strict=False)
        == second.expanduser().resolve(strict=False)
    )

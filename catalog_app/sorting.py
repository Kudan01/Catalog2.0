from __future__ import annotations

import re
import unicodedata


_NUMBER_PATTERN = re.compile(r"(\d+)")
_TOKEN_SEPARATOR = "\x1f"


def natural_sort_key(text: str) -> str:
    """
    Build a stable text key for natural database ordering.

    Numeric parts are encoded by their significant length and value, so
    "Album 2" sorts before "Album 10" when SQLite uses ORDER BY sort_key.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []

    for part in _NUMBER_PATTERN.split(normalized):
        if not part:
            continue

        if part.isdigit():
            significant = part.lstrip("0") or "0"
            tokens.append(
                f"N:{len(significant):012d}:{significant}:{len(part):012d}"
            )
        else:
            tokens.append(f"T:{part}")

    return _TOKEN_SEPARATOR.join(tokens)


def catalog_path_key(rel_path: str) -> str:
    """
    Build a case-insensitive normalized key for a catalog-relative path.

    The original relative path remains available separately for display.
    """
    return unicodedata.normalize("NFKC", rel_path).casefold()

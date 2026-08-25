from __future__ import annotations

from pathlib import Path
from typing import Literal


MediaType = Literal["image", "gif", "video", "other"]

IMAGE_EXTENSIONS = frozenset({
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".png",
    ".webp",
    ".bmp",
})

GIF_EXTENSIONS = frozenset({
    ".gif",
})

VIDEO_EXTENSIONS = frozenset({
    ".mp4",
    ".m4v",
    ".mkv",
    ".wmv",
    ".avi",
    ".mov",
    ".mpg",
    ".mpeg",
    ".webm",
    ".flv",
    ".ogg",
    ".ogv",
})

HTML_PLAYABLE_VIDEO_EXTENSIONS = frozenset({
    ".mp4",
    ".m4v",
    ".webm",
    ".ogg",
    ".ogv",
})


def normalize_extension(path_or_extension: str | Path) -> str:
    """
    Return a lowercase extension including the leading dot.

    Accepts either a complete path or an extension such as ".JPEG".
    """
    text = str(path_or_extension)

    if (
        text.startswith(".")
        and "/" not in text
        and "\\" not in text
        and text.count(".") == 1
    ):
        return text.lower()

    return Path(text).suffix.lower()


def classify_media(path_or_extension: str | Path) -> MediaType:
    """Classify a path by its normalized file extension."""
    extension = normalize_extension(path_or_extension)

    if extension in IMAGE_EXTENSIONS:
        return "image"

    if extension in GIF_EXTENSIONS:
        return "gif"

    if extension in VIDEO_EXTENSIONS:
        return "video"

    return "other"


def is_html_playable_video(path_or_extension: str | Path) -> bool:
    """
    Return whether the video format is intended for direct HTML playback.

    A format can still be catalogued as video and opened externally even when
    this function returns False.
    """
    return normalize_extension(path_or_extension) in HTML_PLAYABLE_VIDEO_EXTENSIONS

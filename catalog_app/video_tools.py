from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import Config


class VideoToolsError(RuntimeError):
    """Raised when ffmpeg/ffprobe is required but not available."""


@dataclass(frozen=True)
class VideoTools:
    """Resolved paths to tools used for video preview generation."""

    ffmpeg_path: str
    ffprobe_path: str


def require_video_tools() -> VideoTools:
    """Return ffmpeg/ffprobe paths or raise a clear error for API/UI callers."""
    ffmpeg = probe_video_tool("ffmpeg")
    ffprobe = probe_video_tool("ffprobe")

    missing = []
    if not ffmpeg.found or ffmpeg.error is not None or ffmpeg.path is None:
        missing.append(ffmpeg.error or "ffmpeg is not available.")
    if not ffprobe.found or ffprobe.error is not None or ffprobe.path is None:
        missing.append(ffprobe.error or "ffprobe is not available.")

    if missing:
        raise VideoToolsError(
            "Video preview cannot be created because video tools are not available: "
            + " ".join(missing)
            + " Install FFmpeg and verify these commands: ffmpeg -version, ffprobe -version."
        )

    return VideoTools(ffmpeg_path=ffmpeg.path, ffprobe_path=ffprobe.path)


@dataclass(frozen=True)
class VideoToolProbe:
    """Read-only diagnostic result for one external video tool."""

    executable: str
    found: bool
    path: str | None
    version_line: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "found": self.found,
            "path": self.path,
            "version_line": self.version_line,
            "error": self.error,
        }


def video_tools_status(config: Config) -> dict[str, Any]:
    """Return read-only diagnostics for tools required by future video previews."""
    ffmpeg = probe_video_tool("ffmpeg")
    ffprobe = probe_video_tool("ffprobe")
    ready = ffmpeg.found and ffprobe.found and ffmpeg.error is None and ffprobe.error is None

    return {
        "ok": True,
        "video_previews_ready": ready,
        "ffmpeg": ffmpeg.to_dict(),
        "ffprobe": ffprobe.to_dict(),
        "config": {
            "video_preview_width": config.video_preview_width,
            "ffmpeg_timeout_seconds": config.ffmpeg_timeout_seconds,
            "ffmpeg_threads_per_job": config.ffmpeg_threads_per_job,
        },
        "planned_outputs": {
            "video_poster": {
                "thumbnail_type": "video_poster",
                "cache_class": "protected",
                "directory": str(config.video_poster_cache_dir),
            },
            "video_frame": {
                "thumbnail_type": "video_frame",
                "cache_class": "protected",
                "directory": str(config.video_frame_cache_dir),
                "planned_count_per_video": 4,
            },
        },
        "writes": {
            "catalog_db": False,
            "thumbnail_cache": False,
            "source_media": False,
        },
    }


def video_tools_status_lines(config: Config) -> list[str]:
    """Return a compact CLI report for video preview tool availability."""
    status = video_tools_status(config)
    lines = [
        "Catalog 2.0 – video tools check",
        "=" * 70,
        f"video_previews_ready: {status['video_previews_ready']}",
        "",
    ]

    for key in ("ffmpeg", "ffprobe"):
        tool = status[key]
        lines.extend(
            [
                f"{key}:",
                f"- found: {tool['found']}",
                f"- path: {tool['path']}",
                f"- version_line: {tool['version_line']}",
                f"- error: {tool['error']}",
                "",
            ]
        )

    lines.extend(
        [
            "Future video preview configuration:",
            f"- video_preview_width: {config.video_preview_width}",
            f"- ffmpeg_timeout_seconds: {config.ffmpeg_timeout_seconds}",
            f"- ffmpeg_threads_per_job: {config.ffmpeg_threads_per_job}",
            "",
            "Nothing was written or changed.",
        ]
    )
    return lines


def probe_video_tool(executable: str) -> VideoToolProbe:
    """Check whether an executable is available and can print its version."""
    path = shutil.which(executable)
    if path is None:
        return VideoToolProbe(
            executable=executable,
            found=False,
            path=None,
            version_line=None,
            error=f"{executable} nebyl nalezen v PATH.",
        )

    try:
        completed = subprocess.run(
            [path, "-version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return VideoToolProbe(
            executable=executable,
            found=True,
            path=path,
            version_line=None,
            error=f"{executable} did not respond within 5 seconds.",
        )
    except OSError as exc:
        return VideoToolProbe(
            executable=executable,
            found=True,
            path=path,
            version_line=None,
            error=str(exc),
        )

    output = (completed.stdout or completed.stderr or "").splitlines()
    version_line = output[0].strip() if output else None
    error = None
    if completed.returncode != 0:
        error = f"{executable} returned exit code {completed.returncode}."

    return VideoToolProbe(
        executable=executable,
        found=True,
        path=path,
        version_line=version_line,
        error=error,
    )

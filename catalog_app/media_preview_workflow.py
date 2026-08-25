from __future__ import annotations

from typing import Any

from .config import Config
from .database import validate_database_runtime
from .thumbnail_cache import (
    generate_gif_previews_for_scope,
    generate_video_frames_for_scope,
    generate_video_posters_for_scope,
)


class MediaPreviewWorkflowError(RuntimeError):
    """Raised when the controlled media-preview workflow cannot run."""


def build_media_previews_for_scope(config: Config, *, branch_rel_path: str = "") -> dict[str, Any]:
    """Generate protected GIF/video previews for an active scope.

    This workflow intentionally does not rebuild folder-preview selections and
    does not generate dynamic photo tiles. It only fills protected GIF/video
    preview cache entries for media that are already active in the catalog DB.
    An empty branch means the whole catalog.
    """
    # Verify DB identity before any thumbnail writes happen.
    validate_database_runtime(config.db_path)

    gif_result = generate_gif_previews_for_scope(
        config,
        branch_rel_path=branch_rel_path,
    )
    video_poster_result = generate_video_posters_for_scope(
        config,
        branch_rel_path=branch_rel_path,
    )
    video_frame_result = generate_video_frames_for_scope(
        config,
        branch_rel_path=branch_rel_path,
    )

    phase_results = (gif_result, video_poster_result, video_frame_result)
    return {
        "media_preview_workflow": True,
        "scope": "branch" if branch_rel_path else "full",
        "branch": branch_rel_path,
        "phases": phase_results,
        "totals": {
            "processed_media": sum(int(item.get("processed", 0)) for item in phase_results),
            "processed_frames": sum(int(item.get("frames_processed", 0)) for item in phase_results),
            "created": sum(int(item.get("created", 0)) for item in phase_results),
            "reused": sum(int(item.get("reused", 0)) for item in phase_results),
            "errors": sum(int(item.get("errors", 0)) for item in phase_results),
            "duration_seconds": sum(float(item.get("duration_seconds", 0.0)) for item in phase_results),
        },
        "writes": {
            "catalog_db": "thumbnails",
            "cache_files": "protected GIF/video preview cache",
            "source_media": False,
            "folder_preview_items": False,
            "dynamic_photo_tiles": False,
            "protected_cache_deleted": False,
        },
    }


def build_media_previews_for_branch(config: Config, *, branch_rel_path: str) -> dict[str, Any]:
    """Generate protected media previews for one active branch."""
    if not branch_rel_path:
        raise MediaPreviewWorkflowError(
            "media-preview-build-branch requires --branch with a concrete folder."
        )

    return build_media_previews_for_scope(config, branch_rel_path=branch_rel_path)


def media_preview_workflow_result_lines(result: dict[str, Any]) -> list[str]:
    """Return CLI lines for media-preview workflow result."""
    branch = str(result.get("branch", ""))
    totals = dict(result.get("totals", {}))
    lines = [
        "Catalog 2.0 – controlled branch media preview preparation",
        "=" * 70,
        f"branch: {branch}",
        "performed: GIF preview, video poster, video frame previews",
        "not performed: scan/update, folder previews, photo_tile, cache cleanup",
        "source data: unchanged",
        "",
        "Phases:",
    ]

    for index, phase in enumerate(result.get("phases", ()), start=1):
        phase_dict = dict(phase)
        thumbnail_type = str(phase_dict.get("thumbnail_type", ""))
        label = _thumbnail_type_label(thumbnail_type)
        lines.append(f"{index}. {label}")
        lines.append(f"   processed media: {int(phase_dict.get('processed', 0))}")
        if "frames_processed" in phase_dict:
            lines.append(f"   processed frame previews: {int(phase_dict.get('frames_processed', 0))}")
        lines.append(f"   created: {int(phase_dict.get('created', 0))}")
        lines.append(f"   reused from cache: {int(phase_dict.get('reused', 0))}")
        lines.append(f"   errors: {int(phase_dict.get('errors', 0))}")
        lines.append(f"   time: {float(phase_dict.get('duration_seconds', 0.0)):.3f} s")

        samples = phase_dict.get("error_samples", [])
        if samples:
            lines.append("   error samples:")
            for sample in list(samples)[:10]:
                sample_dict = dict(sample)
                path = str(sample_dict.get("path", ""))
                frame = sample_dict.get("frame")
                error = str(sample_dict.get("error", ""))
                if frame:
                    lines.append(f"   - {path} [{frame}]: {error}")
                else:
                    lines.append(f"   - {path}: {error}")
        lines.append("")

    lines.extend([
        "Summary:",
        f"  processed media total: {int(totals.get('processed_media', 0))}",
        f"  processed frame previews total: {int(totals.get('processed_frames', 0))}",
        f"  created total: {int(totals.get('created', 0))}",
        f"  reused from cache total: {int(totals.get('reused', 0))}",
        f"  errors total: {int(totals.get('errors', 0))}",
        f"  total time: {float(totals.get('duration_seconds', 0.0)):.3f} s",
        "",
        "Writes:",
        "  catalog.db: tabulka thumbnails",
        "  cache: protected GIF/video previews",
        "  original media: unchanged",
        "  folder_preview_items: unchanged",
        "  photo_tile cache: not generated",
    ])
    return lines


def _thumbnail_type_label(thumbnail_type: str) -> str:
    labels = {
        "gif_preview": "GIF preview",
        "video_poster": "video poster",
        "video_frame": "video frame previews",
    }
    return labels.get(thumbnail_type, thumbnail_type or "unknown phase")

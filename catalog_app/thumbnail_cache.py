from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from .config import Config
from .database import open_database
from .message_contract import build_backend_message
from .paths import PathValidationError, safe_join_catalog_path
from .sorting import catalog_path_key
from .video_tools import VideoToolsError, require_video_tools


ThumbnailCacheClass = Literal["dynamic", "protected"]
ThumbnailKind = Literal[
    "photo_tile",
    "gif_preview",
    "video_poster",
    "video_frame",
    "folder_preview",
]

THUMBNAIL_CACHE_VERSION = "v1"
PHOTO_TILE_VARIANT_KEY = "default"
PHOTO_TILE_ALGORITHM_VERSION = "photo_tile_v1_webp_fit"
PHOTO_TILE_FORMAT = "WEBP"
PHOTO_TILE_EXTENSION = ".webp"
PHOTO_TILE_QUALITY = 82

GIF_PREVIEW_VARIANT_KEY = "default"
GIF_PREVIEW_ALGORITHM_VERSION = "gif_preview_v1_webp_first_frame_fit"
GIF_PREVIEW_FORMAT = "WEBP"
GIF_PREVIEW_EXTENSION = ".webp"
GIF_PREVIEW_QUALITY = 82

VIDEO_POSTER_VARIANT_KEY = "default"
VIDEO_POSTER_ALGORITHM_VERSION = "video_poster_v2_webp_ffmpeg_20_fit"
VIDEO_POSTER_FORMAT = "WEBP"
VIDEO_POSTER_EXTENSION = ".webp"
VIDEO_POSTER_QUALITY = 82

VIDEO_FRAME_VARIANT_KEYS = ("frame_1", "frame_2", "frame_3", "frame_4")
VIDEO_FRAME_RELATIVE_POSITIONS = (0.35, 0.50, 0.65, 0.80)
VIDEO_FRAME_ALGORITHM_VERSION = "video_frame_v2_webp_ffmpeg_35_50_65_80_fit"
VIDEO_FRAME_FORMAT = "WEBP"
VIDEO_FRAME_EXTENSION = ".webp"
VIDEO_FRAME_QUALITY = 82

DYNAMIC_THUMBNAIL_KINDS: frozenset[str] = frozenset({
    "photo_tile",
})

PROTECTED_THUMBNAIL_KINDS: frozenset[str] = frozenset({
    "gif_preview",
    "video_poster",
    "video_frame",
    "folder_preview",
})


@dataclass
class _ThumbnailGenerationLockState:
    lock: threading.Lock
    users: int = 0


_thumbnail_generation_locks_guard = threading.Lock()
_thumbnail_generation_locks: dict[tuple[str, str], _ThumbnailGenerationLockState] = {}


@contextmanager
def _thumbnail_generation_lock(key: tuple[str, str]) -> Iterator[None]:
    """Serialize generation of the same thumbnail target inside one Python process.

    The local server uses threaded request handling. Without this guard, two
    near-simultaneous requests for the same missing on-demand thumbnail can do
    the same expensive Pillow work and race on the same output row/file.
    """
    with _thumbnail_generation_locks_guard:
        state = _thumbnail_generation_locks.get(key)
        if state is None:
            state = _ThumbnailGenerationLockState(lock=threading.Lock())
            _thumbnail_generation_locks[key] = state
        state.users += 1

    state.lock.acquire()
    try:
        yield
    finally:
        state.lock.release()
        with _thumbnail_generation_locks_guard:
            state.users -= 1
            if state.users <= 0 and not state.lock.locked():
                _thumbnail_generation_locks.pop(key, None)


def _unique_thumbnail_temp_path(destination: Path, suffix: str = "tmp") -> Path:
    """Return a process/thread-unique temporary path next to the final file."""
    token = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}"
    return destination.with_name(f".{destination.name}.{token}.{suffix}")


def _unique_thumbnail_frame_path(destination: Path) -> Path:
    """Return a unique temporary raw frame path next to the final thumbnail."""
    token = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}"
    return destination.with_name(f".{destination.stem}.{token}.frame.png")


def _cache_cleanup_status_messages() -> list[dict[str, object]]:
    return [
        build_backend_message(
            "cache.cleanup.scope.dynamic_only",
            severity="info",
        ),
        build_backend_message(
            "cache.cleanup.protected.excluded",
            severity="info",
        ),
        build_backend_message(
            "cache.cleanup.source_media.unchanged",
            severity="info",
        ),
        build_backend_message(
            "cache.cleanup.folder_preview_referenced.preserved",
            severity="info",
        ),
    ]


def _cache_cleanup_plan_primary_message(plan: dict[str, object]) -> dict[str, object]:
    params = {
        "limit_bytes": int(plan.get("limit_bytes") or 0),
        "bytes_over_dynamic_limit": int(plan.get("bytes_over_dynamic_limit") or 0),
        "candidate_entries": int(plan.get("candidate_entries") or 0),
        "candidate_bytes": int(plan.get("candidate_bytes") or 0),
        "skipped_referenced_entries": int(plan.get("skipped_referenced_entries") or 0),
    }

    if plan.get("needed") is True:
        return build_backend_message(
            "cache.cleanup.plan.needed",
            severity="warning",
            params=params,
        )

    return build_backend_message(
        "cache.cleanup.plan.not_needed",
        severity="success",
        params=params,
    )


def _enrich_cache_cleanup_plan_messages(plan: dict[str, object]) -> dict[str, object]:
    primary = _cache_cleanup_plan_primary_message(plan)
    warnings: list[dict[str, object]] = []

    if plan.get("blocked_by_referenced_dynamic") is True:
        warnings.append(build_backend_message(
            "cache.cleanup.plan.blocked_by_referenced_dynamic",
            severity="warning",
            params={
                "skipped_referenced_entries": int(plan.get("skipped_referenced_entries") or 0),
                "skipped_referenced_bytes": int(plan.get("skipped_referenced_bytes") or 0),
            },
        ))

    if plan.get("over_dynamic_limit_after_plan") is True:
        warnings.append(build_backend_message(
            "cache.cleanup.plan.still_over_after_safe_candidates",
            severity="warning",
            params={
                "dynamic_size_after_plan_bytes": int(plan.get("dynamic_size_after_plan_bytes") or 0),
                "limit_bytes": int(plan.get("limit_bytes") or 0),
            },
        ))

    plan["result_messages"] = [primary]
    plan["status_messages"] = _cache_cleanup_status_messages()
    plan["warning_messages"] = warnings
    plan["blocker_messages"] = []
    return plan


def _dynamic_cache_cleanup_execute_primary_message(payload: dict[str, object]) -> dict[str, object]:
    params = {
        "planned_entries": int(payload.get("planned_entries") or 0),
        "deleted_entries": int(payload.get("deleted_entries") or 0),
        "deleted_files": int(payload.get("deleted_files") or 0),
        "removed_bytes": int(payload.get("removed_bytes") or 0),
        "error_count": int(payload.get("error_count") or 0),
    }

    if int(payload.get("error_count") or 0) > 0:
        return build_backend_message(
            "cache.cleanup.execute.completed_with_errors",
            severity="warning",
            params=params,
        )

    if int(payload.get("planned_entries") or 0) <= 0:
        return build_backend_message(
            "cache.cleanup.execute.no_candidates",
            severity="success",
            params=params,
        )

    return build_backend_message(
        "cache.cleanup.execute.completed",
        severity="success",
        params=params,
    )


def _enrich_dynamic_cache_cleanup_execute_messages(payload: dict[str, object]) -> dict[str, object]:
    primary = _dynamic_cache_cleanup_execute_primary_message(payload)
    warnings: list[dict[str, object]] = []

    if int(payload.get("error_count") or 0) > 0:
        warnings.append(build_backend_message(
            "cache.cleanup.execute.errors_present",
            severity="warning",
            params={"error_count": int(payload.get("error_count") or 0)},
        ))

    if payload.get("over_dynamic_limit_after") is True:
        warnings.append(build_backend_message(
            "cache.cleanup.execute.still_over_after_cleanup",
            severity="warning",
            params={
                "dynamic_size_after_bytes": int(payload.get("dynamic_size_after_bytes") or 0),
                "limit_bytes": int(payload.get("limit_bytes") or 0),
            },
        ))

    payload["result_messages"] = [primary]
    payload["status_messages"] = _cache_cleanup_status_messages()
    payload["warning_messages"] = warnings
    payload["blocker_messages"] = []
    return payload


class ThumbnailCacheError(RuntimeError):
    """Raised when thumbnail cache generation or lookup fails."""


@dataclass(frozen=True)
class ThumbnailCacheDirectory:
    kind: ThumbnailKind
    cache_class: ThumbnailCacheClass
    path: Path


@dataclass(frozen=True)
class ThumbnailCacheLayout:
    root_dir: Path
    dynamic_dir: Path
    protected_dir: Path
    limit_gb: float
    limit_bytes: int
    directories: tuple[ThumbnailCacheDirectory, ...]


@dataclass(frozen=True)
class ThumbnailResource:
    rel_path: str
    thumbnail_type: ThumbnailKind
    cache_class: ThumbnailCacheClass
    variant_key: str
    filesystem_path: Path
    file_name: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
    generated: bool


@dataclass(frozen=True)
class ThumbnailCleanupResult:
    limit_bytes: int
    total_size_before_bytes: int
    total_size_after_bytes: int
    dynamic_size_before_bytes: int
    dynamic_size_after_bytes: int
    protected_size_before_bytes: int
    protected_size_after_bytes: int
    over_limit_before: bool
    over_limit_after: bool
    deleted_entries: int
    deleted_files: int
    removed_bytes: int
    error_count: int
    preserved_output_rel_path: str | None = None


@dataclass(frozen=True)
class DynamicCacheCleanupExecuteResult:
    limit_bytes: int
    dynamic_size_before_bytes: int
    dynamic_size_after_bytes: int
    protected_size_before_bytes: int
    total_size_before_bytes: int
    total_size_after_bytes: int
    planned_entries: int
    planned_bytes: int
    deleted_entries: int
    deleted_files: int
    missing_files: int
    skipped_referenced_entries: int
    skipped_referenced_bytes: int
    removed_bytes: int
    error_count: int
    duration_seconds: float
    sample_deleted: tuple[dict[str, object], ...]
    sample_errors: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "dynamic_cache_cleanup_execute": True,
            "version": 1,
            "cache_class": "dynamic",
            "protection_model": "protected_while_folder_preview_referenced",
            "limit_bytes": self.limit_bytes,
            "dynamic_size_before_bytes": self.dynamic_size_before_bytes,
            "dynamic_size_after_bytes": self.dynamic_size_after_bytes,
            "protected_size_before_bytes": self.protected_size_before_bytes,
            "total_size_before_bytes": self.total_size_before_bytes,
            "total_size_after_bytes": self.total_size_after_bytes,
            "planned_entries": self.planned_entries,
            "planned_bytes": self.planned_bytes,
            "deleted_entries": self.deleted_entries,
            "deleted_files": self.deleted_files,
            "missing_files": self.missing_files,
            "skipped_referenced_entries": self.skipped_referenced_entries,
            "skipped_referenced_bytes": self.skipped_referenced_bytes,
            "removed_bytes": self.removed_bytes,
            "error_count": self.error_count,
            "duration_seconds": self.duration_seconds,
            "over_dynamic_limit_before": self.dynamic_size_before_bytes > self.limit_bytes,
            "over_dynamic_limit_after": self.dynamic_size_after_bytes > self.limit_bytes,
            "protected_included": False,
            "folder_preview_referenced_included": False,
            "sample_deleted": list(self.sample_deleted),
            "sample_errors": list(self.sample_errors),
            "writes": {
                "source_media": False,
                "protected_cache": False,
                "folder_preview_referenced_dynamic": False,
                "dynamic_cache_files": True,
                "catalog_db": "delete unreferenced dynamic thumbnail rows only",
                "settings_json": False,
                "config_json": False,
            },
        }
        return _enrich_dynamic_cache_cleanup_execute_messages(payload)


def thumbnail_cache_layout(config: Config) -> ThumbnailCacheLayout:
    return ThumbnailCacheLayout(
        root_dir=config.thumbnail_cache_dir,
        dynamic_dir=config.thumbnail_dynamic_cache_dir,
        protected_dir=config.thumbnail_protected_cache_dir,
        limit_gb=config.thumbnail_cache_limit_gb,
        limit_bytes=int(config.thumbnail_cache_limit_gb * 1024 * 1024 * 1024),
        directories=(
            ThumbnailCacheDirectory(
                kind="photo_tile",
                cache_class="dynamic",
                path=config.photo_tile_cache_dir,
            ),
            ThumbnailCacheDirectory(
                kind="gif_preview",
                cache_class="protected",
                path=config.gif_preview_cache_dir,
            ),
            ThumbnailCacheDirectory(
                kind="video_poster",
                cache_class="protected",
                path=config.video_poster_cache_dir,
            ),
            ThumbnailCacheDirectory(
                kind="video_frame",
                cache_class="protected",
                path=config.video_frame_cache_dir,
            ),
            ThumbnailCacheDirectory(
                kind="folder_preview",
                cache_class="protected",
                path=config.folder_preview_cache_dir,
            ),
        ),
    )


def thumbnail_cache_class(kind: str) -> ThumbnailCacheClass:
    if kind in DYNAMIC_THUMBNAIL_KINDS:
        return "dynamic"

    if kind in PROTECTED_THUMBNAIL_KINDS:
        return "protected"

    raise ValueError(f"Unknown thumbnail type: {kind}")


def ensure_thumbnail_cache_directories(config: Config) -> tuple[Path, ...]:
    """Create all configured thumbnail cache directories."""
    layout = thumbnail_cache_layout(config)
    paths = (layout.root_dir, layout.dynamic_dir, layout.protected_dir) + tuple(
        item.path for item in layout.directories
    )

    for path in paths:
        path.mkdir(parents=True, exist_ok=True)

    return paths


def thumbnail_cache_layout_dict(config: Config) -> dict[str, object]:
    layout = thumbnail_cache_layout(config)
    return {
        "version": THUMBNAIL_CACHE_VERSION,
        "limit_gb": layout.limit_gb,
        "limit_bytes": layout.limit_bytes,
        "root_dir": str(layout.root_dir),
        "dynamic_dir": str(layout.dynamic_dir),
        "protected_dir": str(layout.protected_dir),
        "cleanup_policy": {
            "dynamic": "automatic",
            "protected": "manual",
        },
        "directories": [
            {
                "kind": item.kind,
                "cache_class": item.cache_class,
                "path": str(item.path),
            }
            for item in layout.directories
        ],
    }



def ready_cached_thumbnail_resource(
    config: Config,
    *,
    media_id: int,
    thumbnail_type: ThumbnailKind,
    variant_key: str,
    diagnostic_timings: dict[str, float] | None = None,
) -> ThumbnailResource | None:
    """Return an already-ready thumbnail by trusting DB state during browsing.

    This fast path intentionally does not touch the original media file and does
    not update usage metadata. Scan/activation logic is responsible for marking
    thumbnails stale when source files change.
    """
    lookup_started = time.perf_counter() if diagnostic_timings is not None else 0.0
    try:
        with open_database(config.db_path, read_only=True, validate=False) as connection:
            row = connection.execute(
                """
                SELECT
                    output_rel_path,
                    width,
                    height,
                    file_size_bytes,
                    cache_class,
                    status
                FROM thumbnails
                WHERE media_id = ?
                  AND thumbnail_type = ?
                  AND variant_key = ?
                """,
                (media_id, thumbnail_type, variant_key),
            ).fetchone()
    finally:
        if diagnostic_timings is not None:
            diagnostic_timings["thumbnail_lookup_ms"] = (
                time.perf_counter() - lookup_started
            ) * 1000.0

    if row is None or str(row["status"]) != "ready":
        return None

    check_started = time.perf_counter() if diagnostic_timings is not None else 0.0
    try:
        path = _thumbnail_filesystem_path(config, str(row["output_rel_path"]))
        if not path.exists() or not path.is_file():
            return None

        stat_result = path.stat()
    finally:
        if diagnostic_timings is not None:
            diagnostic_timings["cache_file_check_ms"] = (
                time.perf_counter() - check_started
            ) * 1000.0
    return ThumbnailResource(
        rel_path=str(row["output_rel_path"]),
        thumbnail_type=thumbnail_type,
        cache_class=str(row["cache_class"]),
        variant_key=variant_key,
        filesystem_path=path,
        file_name=path.name,
        mime_type="image/webp",
        size_bytes=int(stat_result.st_size),
        width=int(row["width"]),
        height=int(row["height"]),
        generated=False,
    )

def photo_tile_resource(
    config: Config,
    *,
    media_id: int,
    rel_path: str,
    source_path: Path,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource:
    """Return a ready photo_tile thumbnail, creating it on demand when needed."""
    existing = _ready_existing_photo_tile(
        config,
        media_id=media_id,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )
    if existing is not None:
        return existing

    destination = _photo_tile_destination(
        config,
        rel_path=rel_path,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )
    lock_key = ("photo_tile", str(destination))

    with _thumbnail_generation_lock(lock_key):
        # A previous near-simultaneous request may have created the tile while
        # this request was waiting. Check DB/cache again before opening the
        # original image and doing expensive Pillow work.
        existing = _ready_existing_photo_tile(
            config,
            media_id=media_id,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
        )
        if existing is not None:
            return existing

        return _generate_photo_tile(
            config,
            media_id=media_id,
            rel_path=rel_path,
            source_path=source_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            destination=destination,
        )



def gif_preview_existing_resource(
    config: Config,
    *,
    media_id: int,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource | None:
    """Return an existing ready GIF preview without generating it on demand."""
    return _ready_existing_gif_preview(
        config,
        media_id=media_id,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )



def gif_preview_resource(
    config: Config,
    *,
    media_id: int,
    rel_path: str,
    source_path: Path,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource:
    """Return a ready GIF preview, creating it on demand when needed."""
    existing = _ready_existing_gif_preview(
        config,
        media_id=media_id,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )
    if existing is not None:
        return existing

    return _generate_gif_preview(
        config,
        media_id=media_id,
        rel_path=rel_path,
        source_path=source_path,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )



def video_poster_resource(
    config: Config,
    *,
    media_id: int,
    rel_path: str,
    source_path: Path,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource:
    """Return a ready video_poster thumbnail, creating it on demand when ffmpeg is available."""
    existing = _ready_existing_video_poster(
        config,
        media_id=media_id,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )
    if existing is not None:
        return existing

    return _generate_video_poster(
        config,
        media_id=media_id,
        rel_path=rel_path,
        source_path=source_path,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )



def video_poster_existing_resource(
    config: Config,
    *,
    media_id: int,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource | None:
    """Return an existing ready video_poster without generating it on demand."""
    return _ready_existing_video_poster(
        config,
        media_id=media_id,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
        update_usage=False,
        mark_stale=False,
    )


def video_frame_existing_resource(
    config: Config,
    *,
    media_id: int,
    frame_index: int,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource | None:
    """Return an existing ready video_frame without generating it on demand."""
    return _ready_existing_video_frame(
        config,
        media_id=media_id,
        variant_key=_video_frame_variant_key(frame_index),
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
        update_usage=False,
        mark_stale=False,
    )


def generate_gif_previews_for_scope(
    config: Config,
    *,
    branch_rel_path: str = "",
) -> dict[str, object]:
    """Generate only missing, stale, failed, or outdated protected GIF previews."""
    started_at = time.time()
    ensure_thumbnail_cache_directories(config)

    rows = _gif_preview_work_rows(config, branch_rel_path=branch_rel_path)
    processed = 0
    created = 0
    reused = 0
    errors = 0
    samples: list[dict[str, str]] = []

    for row in rows:
        processed += 1
        rel_path = str(row["rel_path"])
        media_id = int(row["id"])
        source_size = int(row["size_bytes"])
        source_modified = float(row["modified_time"])

        try:
            source_path = safe_join_catalog_path(config.data_root, rel_path, allow_root=False)
            if not source_path.exists() or not source_path.is_file():
                raise ThumbnailCacheError(f"Source GIF is not available on disk: {rel_path}")

            stat_result = source_path.stat()
            if not _source_metadata_matches(
                source_size,
                source_modified,
                int(stat_result.st_size),
                float(stat_result.st_mtime),
            ):
                raise ThumbnailCacheError(
                    f"Source GIF changed after the last catalog update: {rel_path}. "
                    "Run catalog update first."
                )

            _generate_gif_preview(
                config,
                media_id=media_id,
                rel_path=rel_path,
                source_path=source_path,
                source_size_bytes=source_size,
                source_modified_time=source_modified,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if len(samples) < 10:
                samples.append({"path": rel_path, "technical_detail": str(exc)})
            try:
                output_rel_path = _output_relative_path(
                    config,
                    _gif_preview_destination(
                        config,
                        rel_path=rel_path,
                        source_size_bytes=source_size,
                        source_modified_time=source_modified,
                    ),
                )
                _record_gif_preview_error(
                    config,
                    media_id=media_id,
                    output_rel_path=output_rel_path,
                    source_size_bytes=source_size,
                    source_modified_time=source_modified,
                    message=str(exc),
                )
            except Exception:  # noqa: BLE001
                pass

    duration = time.time() - started_at
    return {
        "thumbnail_job": True,
        "thumbnail_type": "gif_preview",
        "cache_class": "protected",
        "scope": "branch" if branch_rel_path else "full",
        "branch": branch_rel_path,
        "processed": processed,
        "created": created,
        "reused": reused,
        "errors": errors,
        "error_samples": samples,
        "duration_seconds": duration,
        "writes": {
            "catalog_db": "thumbnails",
            "cache_files": str(config.gif_preview_cache_dir),
            "source_media": False,
            "protected_cache_deleted": False,
        },
    }


def generate_video_posters_for_scope(
    config: Config,
    *,
    branch_rel_path: str = "",
) -> dict[str, object]:
    """Generate only missing, stale, failed, or outdated protected video posters."""
    started_at = time.time()
    ensure_thumbnail_cache_directories(config)

    rows = _video_poster_work_rows(config, branch_rel_path=branch_rel_path)
    processed = 0
    created = 0
    reused = 0
    errors = 0
    samples: list[dict[str, str]] = []

    for row in rows:
        processed += 1
        rel_path = str(row["rel_path"])
        media_id = int(row["id"])
        source_size = int(row["size_bytes"])
        source_modified = float(row["modified_time"])

        try:
            source_path = safe_join_catalog_path(config.data_root, rel_path, allow_root=False)
            if not source_path.exists() or not source_path.is_file():
                raise ThumbnailCacheError(f"Source video is not available on disk: {rel_path}")

            stat_result = source_path.stat()
            if not _source_metadata_matches(
                source_size,
                source_modified,
                int(stat_result.st_size),
                float(stat_result.st_mtime),
            ):
                raise ThumbnailCacheError(
                    f"Source video changed after the last catalog update: {rel_path}. "
                    "Run catalog update first."
                )

            _generate_video_poster(
                config,
                media_id=media_id,
                rel_path=rel_path,
                source_path=source_path,
                source_size_bytes=source_size,
                source_modified_time=source_modified,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if len(samples) < 10:
                samples.append({"path": rel_path, "technical_detail": str(exc)})
            try:
                output_rel_path = _output_relative_path(
                    config,
                    _video_poster_destination(
                        config,
                        rel_path=rel_path,
                        source_size_bytes=source_size,
                        source_modified_time=source_modified,
                    ),
                )
                _record_video_poster_error(
                    config,
                    media_id=media_id,
                    output_rel_path=output_rel_path,
                    source_size_bytes=source_size,
                    source_modified_time=source_modified,
                    message=str(exc),
                )
            except Exception:  # noqa: BLE001
                pass

    duration = time.time() - started_at
    return {
        "thumbnail_job": True,
        "thumbnail_type": "video_poster",
        "cache_class": "protected",
        "scope": "branch" if branch_rel_path else "full",
        "branch": branch_rel_path,
        "processed": processed,
        "created": created,
        "reused": reused,
        "errors": errors,
        "error_samples": samples,
        "duration_seconds": duration,
        "writes": {
            "catalog_db": "thumbnails",
            "cache_files": str(config.video_poster_cache_dir),
            "source_media": False,
            "protected_cache_deleted": False,
        },
    }


def generate_video_frames_for_scope(
    config: Config,
    *,
    branch_rel_path: str = "",
) -> dict[str, object]:
    """Generate only missing, stale, failed, or outdated protected video frames."""
    started_at = time.time()
    ensure_thumbnail_cache_directories(config)

    rows = _video_frame_work_rows(config, branch_rel_path=branch_rel_path)
    grouped: dict[int, dict[str, object]] = {}
    for row in rows:
        media_id = int(row["id"])
        item = grouped.setdefault(media_id, {
            "id": media_id,
            "rel_path": str(row["rel_path"]),
            "size_bytes": int(row["size_bytes"]),
            "modified_time": float(row["modified_time"]),
            "variants": [],
        })
        item["variants"].append(str(row["variant_key"]))

    processed = 0
    frames_processed = 0
    created = 0
    reused = 0
    errors = 0
    samples: list[dict[str, str]] = []

    for item in grouped.values():
        processed += 1
        rel_path = str(item["rel_path"])
        media_id = int(item["id"])
        source_size = int(item["size_bytes"])
        source_modified = float(item["modified_time"])
        variants = list(item["variants"])

        try:
            source_path = safe_join_catalog_path(config.data_root, rel_path, allow_root=False)
            if not source_path.exists() or not source_path.is_file():
                raise ThumbnailCacheError(f"Source video is not available on disk: {rel_path}")

            stat_result = source_path.stat()
            if not _source_metadata_matches(
                source_size,
                source_modified,
                int(stat_result.st_size),
                float(stat_result.st_mtime),
            ):
                raise ThumbnailCacheError(
                    f"Source video changed after the last catalog update: {rel_path}. "
                    "Run catalog update first."
                )

            try:
                tools = require_video_tools()
                duration = _probe_video_duration(
                    tools.ffprobe_path,
                    source_path,
                    timeout_seconds=min(config.ffmpeg_timeout_seconds, 30),
                )
            except VideoToolsError:
                duration = None

            for variant_key in variants:
                frame_index = VIDEO_FRAME_VARIANT_KEYS.index(variant_key) + 1
                frames_processed += 1
                try:
                    _generate_video_frame(
                        config,
                        media_id=media_id,
                        rel_path=rel_path,
                        source_path=source_path,
                        source_size_bytes=source_size,
                        source_modified_time=source_modified,
                        variant_key=variant_key,
                        seek_time=_video_frame_seek_time(duration, frame_index),
                    )
                    created += 1
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    if len(samples) < 10:
                        samples.append({"path": rel_path, "frame": variant_key, "technical_detail": str(exc)})
                    try:
                        output_rel_path = _output_relative_path(
                            config,
                            _video_frame_destination(
                                config,
                                rel_path=rel_path,
                                source_size_bytes=source_size,
                                source_modified_time=source_modified,
                                variant_key=variant_key,
                            ),
                        )
                        _record_video_frame_error(
                            config,
                            media_id=media_id,
                            variant_key=variant_key,
                            output_rel_path=output_rel_path,
                            source_size_bytes=source_size,
                            source_modified_time=source_modified,
                            message=str(exc),
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            errors += len(variants)
            frames_processed += len(variants)
            if len(samples) < 10:
                samples.append({"path": rel_path, "technical_detail": str(exc)})

    duration = time.time() - started_at
    return {
        "thumbnail_job": True,
        "thumbnail_type": "video_frame",
        "cache_class": "protected",
        "scope": "branch" if branch_rel_path else "full",
        "branch": branch_rel_path,
        "processed": processed,
        "frames_processed": frames_processed,
        "created": created,
        "reused": reused,
        "errors": errors,
        "error_samples": samples,
        "frame_positions_percent": [35, 50, 65, 80],
        "duration_seconds": duration,
        "writes": {
            "catalog_db": "thumbnails",
            "cache_files": str(config.video_frame_cache_dir),
            "source_media": False,
            "protected_cache_deleted": False,
        },
    }


def thumbnail_cache_cleanup_plan(connection, *, limit_bytes: int, sample_limit: int = 12) -> dict[str, object]:
    """Return a read-only dynamic cache cleanup plan without deleting files or DB rows.

    Automatic cleanup candidates are only unreferenced dynamic cache entries.
    Dynamic photo_tile entries currently referenced by folder_preview_items are
    protected while referenced, because deleting them would visually degrade
    folder cards. Protected cache is reported, but never becomes a candidate.
    """
    totals = _cache_size_totals(connection)
    limit = max(0, int(limit_bytes))
    dynamic_size_before = int(totals["dynamic"])
    protected_size_before = int(totals["protected"])
    total_size_before = int(totals["total"])
    bytes_over_dynamic_limit = max(0, dynamic_size_before - limit)

    plan: dict[str, object] = {
        "version": 2,
        "mode": "read_only_plan",
        "cache_class": "dynamic",
        "protection_model": "protected_while_folder_preview_referenced",
        "needed": bytes_over_dynamic_limit > 0,
        "limit_bytes": limit,
        "dynamic_size_before_bytes": dynamic_size_before,
        "protected_size_before_bytes": protected_size_before,
        "total_size_before_bytes": total_size_before,
        "bytes_over_dynamic_limit": bytes_over_dynamic_limit,
        "candidate_entries": 0,
        "candidate_bytes": 0,
        "skipped_referenced_entries": 0,
        "skipped_referenced_bytes": 0,
        "dynamic_size_after_plan_bytes": dynamic_size_before,
        "protected_size_after_plan_bytes": protected_size_before,
        "total_size_after_plan_bytes": total_size_before,
        "over_dynamic_limit_after_plan": bytes_over_dynamic_limit > 0,
        "blocked_by_referenced_dynamic": False,
        "protected_included": False,
        "folder_preview_referenced_included": False,
        "source": "thumbnails table evidence + folder_preview_items references",
        "sample_limit": int(sample_limit),
        "sample_candidates": [],
        "sample_skipped_referenced": [],
        "by_status": {},
        "by_type": {},
        "skipped_referenced_by_status": {},
    }

    if bytes_over_dynamic_limit <= 0:
        return _enrich_cache_cleanup_plan_messages(plan)

    referenced_summary = _sum_row(
        connection,
        """
        SELECT COUNT(*) AS entries,
               COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        WHERE cache_class = 'dynamic'
          AND EXISTS (
              SELECT 1
              FROM folder_preview_items AS fpi
              WHERE fpi.media_id = thumbnails.media_id
          )
        """,
    )
    skipped_referenced_entries_total = int(referenced_summary["entries"])
    skipped_referenced_bytes_total = int(referenced_summary["size_bytes"])

    skipped_samples = [
        {
            "output_rel_path": str(row["output_rel_path"]),
            "thumbnail_type": str(row["thumbnail_type"]),
            "status": str(row["status"]),
            "size_bytes": int(row["file_size_bytes"]),
            "folder_preview_referenced": True,
        }
        for row in connection.execute(
            """
            SELECT
                thumbnail_type,
                output_rel_path,
                file_size_bytes,
                status,
                created_at,
                updated_at,
                last_used_at
            FROM thumbnails
            WHERE cache_class = 'dynamic'
              AND EXISTS (
                  SELECT 1
                  FROM folder_preview_items AS fpi
                  WHERE fpi.media_id = thumbnails.media_id
              )
            ORDER BY
                CASE status
                    WHEN 'missing' THEN 0
                    WHEN 'stale' THEN 1
                    WHEN 'error' THEN 2
                    ELSE 3
                END,
                COALESCE(last_used_at, updated_at, created_at, 0),
                id
            LIMIT ?
            """,
            (int(sample_limit),),
        ).fetchall()
    ]

    rows = connection.execute(
        """
        SELECT
            id,
            thumbnail_type,
            output_rel_path,
            file_size_bytes,
            status,
            created_at,
            updated_at,
            last_used_at
        FROM thumbnails
        WHERE cache_class = 'dynamic'
          AND NOT EXISTS (
              SELECT 1
              FROM folder_preview_items AS fpi
              WHERE fpi.media_id = thumbnails.media_id
          )
        ORDER BY
            CASE status
                WHEN 'missing' THEN 0
                WHEN 'stale' THEN 1
                WHEN 'error' THEN 2
                ELSE 3
            END,
            COALESCE(last_used_at, updated_at, created_at, 0),
            id
        """
    ).fetchall()

    current_dynamic = dynamic_size_before
    candidate_entries = 0
    candidate_bytes = 0
    samples: list[dict[str, object]] = []
    by_status: dict[str, dict[str, int]] = {}
    by_type: dict[str, dict[str, int]] = {}
    skipped_by_status: dict[str, dict[str, int]] = {}

    for row in connection.execute(
        """
        SELECT status,
               COUNT(*) AS entries,
               COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        WHERE cache_class = 'dynamic'
          AND EXISTS (
              SELECT 1
              FROM folder_preview_items AS fpi
              WHERE fpi.media_id = thumbnails.media_id
          )
        GROUP BY status
        """
    ):
        skipped_by_status[str(row["status"])] = {
            "entries": int(row["entries"]),
            "size_bytes": int(row["size_bytes"]),
        }

    for row in rows:
        if current_dynamic <= limit:
            break

        status = str(row["status"])
        thumbnail_type = str(row["thumbnail_type"])
        size_bytes = int(row["file_size_bytes"])

        candidate_entries += 1
        candidate_bytes += size_bytes
        current_dynamic = max(0, current_dynamic - size_bytes)

        status_bucket = by_status.setdefault(status, {"entries": 0, "size_bytes": 0})
        status_bucket["entries"] += 1
        status_bucket["size_bytes"] += size_bytes

        type_bucket = by_type.setdefault(thumbnail_type, {"entries": 0, "size_bytes": 0})
        type_bucket["entries"] += 1
        type_bucket["size_bytes"] += size_bytes

        if len(samples) < int(sample_limit):
            samples.append({
                "output_rel_path": str(row["output_rel_path"]),
                "thumbnail_type": thumbnail_type,
                "status": status,
                "size_bytes": size_bytes,
                "folder_preview_referenced": False,
            })

    dynamic_after = max(0, dynamic_size_before - candidate_bytes)
    total_after = protected_size_before + dynamic_after

    plan.update({
        "candidate_entries": candidate_entries,
        "candidate_bytes": candidate_bytes,
        "skipped_referenced_entries": skipped_referenced_entries_total,
        "skipped_referenced_bytes": skipped_referenced_bytes_total,
        "dynamic_size_after_plan_bytes": dynamic_after,
        "protected_size_after_plan_bytes": protected_size_before,
        "total_size_after_plan_bytes": total_after,
        "over_dynamic_limit_after_plan": dynamic_after > limit,
        "blocked_by_referenced_dynamic": dynamic_after > limit and skipped_referenced_entries_total > 0,
        "sample_candidates": samples,
        "sample_skipped_referenced": skipped_samples,
        "by_status": by_status,
        "by_type": by_type,
        "skipped_referenced_by_status": skipped_by_status,
    })
    return _enrich_cache_cleanup_plan_messages(plan)

def execute_dynamic_thumbnail_cache_cleanup(config: Config, *, sample_limit: int = 12) -> DynamicCacheCleanupExecuteResult:
    """Delete only unreferenced dynamic thumbnail cache candidates.

    This is the destructive counterpart of thumbnail_cache_cleanup_plan(). It
    intentionally rebuilds the same plan immediately before execution and then
    deletes only dynamic rows that are not currently referenced by
    folder_preview_items. Protected cache and folder-preview-referenced dynamic
    cache are never candidates. Source media are never touched.
    """
    started = time.monotonic()
    limit_bytes = int(config.thumbnail_cache_limit_gb * 1024 * 1024 * 1024)
    sample_cap = max(0, int(sample_limit))

    with open_database(config.db_path, read_only=False) as connection:
        plan = thumbnail_cache_cleanup_plan(
            connection,
            limit_bytes=limit_bytes,
            sample_limit=sample_cap,
        )
        totals_before = _cache_size_totals(connection)
        dynamic_before = int(totals_before["dynamic"])
        protected_before = int(totals_before["protected"])
        total_before = int(totals_before["total"])
        skipped_referenced_entries = int(plan.get("skipped_referenced_entries") or 0)
        skipped_referenced_bytes = int(plan.get("skipped_referenced_bytes") or 0)

        if int(plan.get("candidate_entries") or 0) <= 0:
            totals_after = _cache_size_totals(connection)
            return DynamicCacheCleanupExecuteResult(
                limit_bytes=limit_bytes,
                dynamic_size_before_bytes=dynamic_before,
                dynamic_size_after_bytes=int(totals_after["dynamic"]),
                protected_size_before_bytes=protected_before,
                total_size_before_bytes=total_before,
                total_size_after_bytes=int(totals_after["total"]),
                planned_entries=0,
                planned_bytes=0,
                deleted_entries=0,
                deleted_files=0,
                missing_files=0,
                skipped_referenced_entries=skipped_referenced_entries,
                skipped_referenced_bytes=skipped_referenced_bytes,
                removed_bytes=0,
                error_count=0,
                duration_seconds=time.monotonic() - started,
                sample_deleted=(),
                sample_errors=(),
            )

        rows = connection.execute(
            """
            SELECT
                id,
                thumbnail_type,
                output_rel_path,
                file_size_bytes,
                status,
                created_at,
                updated_at,
                last_used_at
            FROM thumbnails
            WHERE cache_class = 'dynamic'
              AND NOT EXISTS (
                  SELECT 1
                  FROM folder_preview_items AS fpi
                  WHERE fpi.media_id = thumbnails.media_id
              )
            ORDER BY
                CASE status
                    WHEN 'missing' THEN 0
                    WHEN 'stale' THEN 1
                    WHEN 'error' THEN 2
                    ELSE 3
                END,
                COALESCE(last_used_at, updated_at, created_at, 0),
                id
            """
        ).fetchall()

        current_dynamic = dynamic_before
        planned_entries = 0
        planned_bytes = 0
        deleted_entries = 0
        deleted_files = 0
        missing_files = 0
        removed_bytes = 0
        error_count = 0
        sample_deleted: list[dict[str, object]] = []
        sample_errors: list[dict[str, object]] = []

        for row in rows:
            if current_dynamic <= limit_bytes:
                break

            row_id = int(row["id"])
            output_rel_path = str(row["output_rel_path"])
            stored_size = int(row["file_size_bytes"])
            status = str(row["status"])
            thumbnail_type = str(row["thumbnail_type"])
            planned_entries += 1
            planned_bytes += stored_size

            try:
                thumbnail_path = _dynamic_thumbnail_filesystem_path(config, output_rel_path)
                file_existed = thumbnail_path.exists() and thumbnail_path.is_file()
                if file_existed:
                    thumbnail_path.unlink()
                    deleted_files += 1
                    _remove_empty_cache_parents(
                        thumbnail_path.parent,
                        stop_dir=config.thumbnail_dynamic_cache_dir,
                    )
                else:
                    missing_files += 1

                connection.execute(
                    "DELETE FROM thumbnails WHERE id = ?",
                    (row_id,),
                )
                deleted_entries += 1
                removed_bytes += stored_size
                current_dynamic = max(0, current_dynamic - stored_size)

                if len(sample_deleted) < sample_cap:
                    sample_deleted.append({
                        "output_rel_path": output_rel_path,
                        "thumbnail_type": thumbnail_type,
                        "status": status,
                        "size_bytes": stored_size,
                        "file_missing": not file_existed,
                    })
            except (OSError, ThumbnailCacheError) as exc:
                error_count += 1
                if len(sample_errors) < sample_cap:
                    sample_errors.append({
                        "output_rel_path": output_rel_path,
                        "thumbnail_type": thumbnail_type,
                        "status": status,
                        "size_bytes": stored_size,
                        "technical_detail": str(exc),
                    })

        connection.commit()
        totals_after = _cache_size_totals(connection)

    return DynamicCacheCleanupExecuteResult(
        limit_bytes=limit_bytes,
        dynamic_size_before_bytes=dynamic_before,
        dynamic_size_after_bytes=int(totals_after["dynamic"]),
        protected_size_before_bytes=protected_before,
        total_size_before_bytes=total_before,
        total_size_after_bytes=int(totals_after["total"]),
        planned_entries=planned_entries,
        planned_bytes=planned_bytes,
        deleted_entries=deleted_entries,
        deleted_files=deleted_files,
        missing_files=missing_files,
        skipped_referenced_entries=skipped_referenced_entries,
        skipped_referenced_bytes=skipped_referenced_bytes,
        removed_bytes=removed_bytes,
        error_count=error_count,
        duration_seconds=time.monotonic() - started,
        sample_deleted=tuple(sample_deleted),
        sample_errors=tuple(sample_errors),
    )


def thumbnail_cache_database_summary(connection, *, limit_bytes: int) -> dict[str, object]:
    """Return DB evidence summary for thumbnail cache without touching files."""
    total = _sum_row(
        connection,
        """
        SELECT COUNT(*) AS entries,
               COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        """,
    )

    by_cache_class = {
        "dynamic": {"entries": 0, "size_bytes": 0},
        "protected": {"entries": 0, "size_bytes": 0},
    }
    for row in connection.execute(
        """
        SELECT cache_class,
               COUNT(*) AS entries,
               COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        GROUP BY cache_class
        """
    ):
        by_cache_class[str(row["cache_class"])] = {
            "entries": int(row["entries"]),
            "size_bytes": int(row["size_bytes"]),
        }

    by_status = {
        status: {"entries": 0, "size_bytes": 0}
        for status in ("pending", "ready", "stale", "missing", "error")
    }
    for row in connection.execute(
        """
        SELECT status,
               COUNT(*) AS entries,
               COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        GROUP BY status
        """
    ):
        by_status[str(row["status"])] = {
            "entries": int(row["entries"]),
            "size_bytes": int(row["size_bytes"]),
        }

    by_type = []
    for row in connection.execute(
        """
        SELECT thumbnail_type,
               cache_class,
               status,
               COUNT(*) AS entries,
               COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        GROUP BY thumbnail_type, cache_class, status
        ORDER BY thumbnail_type, cache_class, status
        """
    ):
        by_type.append({
            "thumbnail_type": str(row["thumbnail_type"]),
            "cache_class": str(row["cache_class"]),
            "status": str(row["status"]),
            "entries": int(row["entries"]),
            "size_bytes": int(row["size_bytes"]),
        })

    total_size = int(total["size_bytes"])
    return {
        "entries": int(total["entries"]),
        "size_bytes": total_size,
        "limit_bytes": int(limit_bytes),
        "over_limit": total_size > int(limit_bytes),
        "by_cache_class": by_cache_class,
        "by_status": by_status,
        "by_type": by_type,
        "cleanup_scope": {
            "automatic": "dynamic",
            "manual": "protected",
        },
    }




def _protected_audit_sample(samples: list[dict[str, object]], item: dict[str, object], sample_limit: int) -> None:
    if len(samples) < sample_limit:
        samples.append(item)


def _increment_audit_bucket(buckets: dict[str, dict[str, int]], key: str, *, size_bytes: int = 0) -> None:
    bucket = buckets.setdefault(key, {"entries": 0, "size_bytes": 0})
    bucket["entries"] += 1
    bucket["size_bytes"] += max(0, int(size_bytes))


def _is_temporary_cache_file(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith(".") and (
        name.endswith(".tmp")
        or ".tmp." in name
        or name.endswith(".frame.png")
        or ".frame." in name
    )


def _path_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def thumbnail_cache_protected_orphan_audit(config: Config, *, sample_limit: int = 12) -> dict[str, object]:
    """Return a read-only protected cache orphan audit.

    The audit intentionally does not delete files, rewrite DB rows, generate
    thumbnails or touch source media. It compares protected thumbnail DB rows
    with protected cache files on disk and reports potential cleanup candidates
    for a later, separately confirmed plan/execute step.
    """
    started_at = time.time()
    sample_limit = max(0, int(sample_limit))
    layout = thumbnail_cache_layout(config)
    protected_dirs = tuple(
        directory.path
        for directory in layout.directories
        if directory.cache_class == "protected"
    )

    db_rel_paths: set[str] = set()
    by_type: dict[str, dict[str, int]] = {}
    by_status: dict[str, dict[str, int]] = {}

    db_rows_total = 0
    db_rows_recorded_bytes = 0
    db_rows_with_file = 0
    db_rows_with_file_bytes = 0
    db_rows_missing_file = 0
    db_rows_missing_file_recorded_bytes = 0
    db_rows_invalid_path = 0
    db_rows_media_missing = 0
    db_rows_media_unavailable = 0
    db_rows_status_not_ready = 0
    valid_rows = 0
    valid_rows_bytes = 0
    potential_orphan_db_rows = 0
    potential_orphan_db_rows_with_file = 0
    potential_orphan_db_bytes = 0

    sample_db_rows_missing_file: list[dict[str, object]] = []
    sample_db_rows_invalid_path: list[dict[str, object]] = []
    sample_media_unavailable: list[dict[str, object]] = []
    sample_status_not_ready: list[dict[str, object]] = []

    with open_database(config.db_path, read_only=True, validate=False) as connection:
        rows = connection.execute(
            """
            SELECT
                t.id,
                t.media_id,
                t.thumbnail_type,
                t.variant_key,
                t.output_rel_path,
                t.file_size_bytes,
                t.status,
                t.created_at,
                t.updated_at,
                t.last_used_at,
                m.rel_path AS media_rel_path,
                m.is_available AS media_is_available
            FROM thumbnails AS t
            LEFT JOIN media_files AS m
              ON m.id = t.media_id
            WHERE t.cache_class = 'protected'
            ORDER BY t.thumbnail_type, t.status, t.id
            """
        ).fetchall()

    for row in rows:
        db_rows_total += 1
        thumbnail_type = str(row["thumbnail_type"])
        status = str(row["status"])
        output_rel_path = str(row["output_rel_path"])
        recorded_size = int(row["file_size_bytes"] or 0)
        db_rows_recorded_bytes += recorded_size
        db_rel_paths.add(output_rel_path)
        _increment_audit_bucket(by_type, thumbnail_type, size_bytes=recorded_size)
        _increment_audit_bucket(by_status, status, size_bytes=recorded_size)

        base_sample = {
            "id": int(row["id"]),
            "media_id": int(row["media_id"]),
            "thumbnail_type": thumbnail_type,
            "variant_key": str(row["variant_key"]),
            "status": status,
            "output_rel_path": output_rel_path,
            "recorded_size_bytes": recorded_size,
            "media_rel_path": str(row["media_rel_path"] or ""),
        }

        try:
            path = _thumbnail_filesystem_path(config, output_rel_path)
            path_valid = True
        except ThumbnailCacheError as exc:
            path = None
            path_valid = False
            db_rows_invalid_path += 1
            _protected_audit_sample(
                sample_db_rows_invalid_path,
                {**base_sample, "technical_detail": str(exc)},
                sample_limit,
            )

        file_exists = False
        actual_size = 0
        if path is not None:
            try:
                file_exists = path.exists() and path.is_file()
                if file_exists:
                    actual_size = int(path.stat().st_size)
            except OSError as exc:
                file_exists = False
                _protected_audit_sample(
                    sample_db_rows_missing_file,
                    {**base_sample, "technical_detail": str(exc)},
                    sample_limit,
                )

        if file_exists:
            db_rows_with_file += 1
            db_rows_with_file_bytes += actual_size
        elif path_valid:
            db_rows_missing_file += 1
            db_rows_missing_file_recorded_bytes += recorded_size
            _protected_audit_sample(sample_db_rows_missing_file, base_sample, sample_limit)

        media_available_raw = row["media_is_available"]
        media_missing = media_available_raw is None
        media_unavailable = media_available_raw is not None and int(media_available_raw) != 1
        status_not_ready = status != "ready"

        if media_missing:
            db_rows_media_missing += 1
            _protected_audit_sample(sample_media_unavailable, {**base_sample, "reason_code": "cache.protected.reason.media_row_missing"}, sample_limit)
        elif media_unavailable:
            db_rows_media_unavailable += 1
            _protected_audit_sample(sample_media_unavailable, {**base_sample, "reason_code": "cache.protected.reason.media_unavailable"}, sample_limit)

        if status_not_ready:
            db_rows_status_not_ready += 1
            _protected_audit_sample(sample_status_not_ready, base_sample, sample_limit)

        is_valid = path_valid and file_exists and not media_missing and not media_unavailable and not status_not_ready
        if is_valid:
            valid_rows += 1
            valid_rows_bytes += actual_size
        elif path_valid and file_exists:
            potential_orphan_db_rows += 1
            potential_orphan_db_rows_with_file += 1
            potential_orphan_db_bytes += actual_size
        elif path_valid and not file_exists:
            potential_orphan_db_rows += 1
        elif not path_valid:
            potential_orphan_db_rows += 1

    filesystem_files_total = 0
    filesystem_files_bytes = 0
    filesystem_files_without_db = 0
    filesystem_files_without_db_bytes = 0
    filesystem_temporary_files = 0
    filesystem_temporary_files_bytes = 0
    filesystem_unexpected_files = 0
    filesystem_unexpected_files_bytes = 0
    sample_files_without_db: list[dict[str, object]] = []
    sample_temporary_files: list[dict[str, object]] = []
    sample_unexpected_files: list[dict[str, object]] = []

    protected_root = config.thumbnail_protected_cache_dir
    if protected_root.exists():
        for path in protected_root.rglob("*"):
            if not path.is_file():
                continue

            try:
                size_bytes = int(path.stat().st_size)
            except OSError:
                size_bytes = 0

            filesystem_files_total += 1
            filesystem_files_bytes += size_bytes

            try:
                rel_path = path.resolve(strict=False).relative_to(
                    config.output_root.resolve(strict=False)
                ).as_posix()
            except ValueError:
                rel_path = str(path)

            sample = {
                "output_rel_path": rel_path,
                "size_bytes": size_bytes,
            }

            if _is_temporary_cache_file(path):
                filesystem_temporary_files += 1
                filesystem_temporary_files_bytes += size_bytes
                _protected_audit_sample(sample_temporary_files, sample, sample_limit)
                continue

            if not _path_under_any(path, protected_dirs):
                filesystem_unexpected_files += 1
                filesystem_unexpected_files_bytes += size_bytes
                _protected_audit_sample(sample_unexpected_files, sample, sample_limit)

            if rel_path not in db_rel_paths:
                filesystem_files_without_db += 1
                filesystem_files_without_db_bytes += size_bytes
                _protected_audit_sample(sample_files_without_db, sample, sample_limit)

    estimated_reclaimable_bytes = potential_orphan_db_bytes + filesystem_files_without_db_bytes + filesystem_temporary_files_bytes
    potential_orphan_entries = potential_orphan_db_rows + filesystem_files_without_db + filesystem_temporary_files

    return {
        "ok": True,
        "protected_orphan_audit": True,
        "version": 1,
        "mode": "read_only_audit",
        "cache_class": "protected",
        "protected_root": str(protected_root),
        "duration_seconds": time.time() - started_at,
        "sample_limit": sample_limit,
        "db": {
            "rows": db_rows_total,
            "recorded_size_bytes": db_rows_recorded_bytes,
            "valid_rows": valid_rows,
            "valid_rows_bytes": valid_rows_bytes,
            "rows_with_file": db_rows_with_file,
            "rows_with_file_bytes": db_rows_with_file_bytes,
            "rows_missing_file": db_rows_missing_file,
            "rows_missing_file_recorded_bytes": db_rows_missing_file_recorded_bytes,
            "rows_invalid_path": db_rows_invalid_path,
            "rows_media_missing": db_rows_media_missing,
            "rows_media_unavailable": db_rows_media_unavailable,
            "rows_status_not_ready": db_rows_status_not_ready,
            "by_type": by_type,
            "by_status": by_status,
        },
        "filesystem": {
            "protected_root_exists": protected_root.exists(),
            "files": filesystem_files_total,
            "size_bytes": filesystem_files_bytes,
            "files_without_db": filesystem_files_without_db,
            "files_without_db_bytes": filesystem_files_without_db_bytes,
            "temporary_files": filesystem_temporary_files,
            "temporary_files_bytes": filesystem_temporary_files_bytes,
            "unexpected_files": filesystem_unexpected_files,
            "unexpected_files_bytes": filesystem_unexpected_files_bytes,
        },
        "potential_orphans": {
            "entries": potential_orphan_entries,
            "db_rows": potential_orphan_db_rows,
            "db_rows_with_file": potential_orphan_db_rows_with_file,
            "db_rows_with_file_bytes": potential_orphan_db_bytes,
            "files_without_db": filesystem_files_without_db,
            "files_without_db_bytes": filesystem_files_without_db_bytes,
            "temporary_files": filesystem_temporary_files,
            "temporary_files_bytes": filesystem_temporary_files_bytes,
            "estimated_reclaimable_bytes": estimated_reclaimable_bytes,
        },
        "samples": {
            "db_rows_missing_file": sample_db_rows_missing_file,
            "db_rows_invalid_path": sample_db_rows_invalid_path,
            "media_unavailable": sample_media_unavailable,
            "status_not_ready": sample_status_not_ready,
            "files_without_db": sample_files_without_db,
            "temporary_files": sample_temporary_files,
            "unexpected_files": sample_unexpected_files,
        },
        "writes": {
            "source_media": False,
            "dynamic_cache": False,
            "protected_cache": False,
            "catalog_db": False,
            "settings_json": False,
            "config_json": False,
        },
    }




def _protected_cache_cleanup_candidate_plan(config: Config, *, sample_limit: int = 12) -> dict[str, object]:
    """Build the shared protected orphan cleanup plan used by read-only plan and execute.

    This helper is the single decision point for protected orphan cleanup. The
    public read-only plan formats its output. The execute step uses the same
    candidate lists and then performs only those safe actions, with final
    existence checks immediately before deleting.
    """
    sample_limit = max(0, int(sample_limit))
    audit = thumbnail_cache_protected_orphan_audit(config, sample_limit=sample_limit)
    layout = thumbnail_cache_layout(config)
    protected_dirs = tuple(
        directory.path
        for directory in layout.directories
        if directory.cache_class == "protected"
    )

    db_rel_paths: set[str] = set()
    safe_db_rows: list[dict[str, object]] = []
    review_required: list[dict[str, object]] = []

    safe_db_rows_missing_file = 0
    safe_db_rows_missing_file_recorded_bytes = 0
    safe_db_rows_invalid_path = 0
    review_media_unavailable_or_missing = 0
    review_status_not_ready = 0

    with open_database(config.db_path, read_only=True, validate=False) as connection:
        rows = connection.execute(
            """
            SELECT
                t.id,
                t.media_id,
                t.thumbnail_type,
                t.variant_key,
                t.output_rel_path,
                t.file_size_bytes,
                t.status,
                m.rel_path AS media_rel_path,
                m.is_available AS media_is_available
            FROM thumbnails AS t
            LEFT JOIN media_files AS m
              ON m.id = t.media_id
            WHERE t.cache_class = 'protected'
            ORDER BY t.thumbnail_type, t.status, t.id
            """
        ).fetchall()

    for row in rows:
        row_id = int(row["id"])
        output_rel_path = str(row["output_rel_path"])
        recorded_size = int(row["file_size_bytes"] or 0)
        status = str(row["status"])
        db_rel_paths.add(output_rel_path)
        base = {
            "id": row_id,
            "media_id": int(row["media_id"]),
            "thumbnail_type": str(row["thumbnail_type"]),
            "variant_key": str(row["variant_key"]),
            "status": status,
            "output_rel_path": output_rel_path,
            "recorded_size_bytes": recorded_size,
            "media_rel_path": str(row["media_rel_path"] or ""),
        }

        try:
            path = _thumbnail_filesystem_path(config, output_rel_path)
            path_valid = True
        except ThumbnailCacheError as exc:
            safe_db_rows_invalid_path += 1
            safe_db_rows.append({**base, "category": "invalid_path", "technical_detail": str(exc)})
            continue

        try:
            file_exists = path.exists() and path.is_file()
        except OSError as exc:
            file_exists = False
            safe_db_rows_missing_file_recorded_bytes += recorded_size
            safe_db_rows_missing_file += 1
            safe_db_rows.append({**base, "category": "missing_file", "technical_detail": str(exc)})
            continue

        if path_valid and not file_exists:
            safe_db_rows_missing_file += 1
            safe_db_rows_missing_file_recorded_bytes += recorded_size
            safe_db_rows.append({**base, "category": "missing_file"})
            continue

        media_available_raw = row["media_is_available"]
        media_missing = media_available_raw is None
        media_unavailable = media_available_raw is not None and int(media_available_raw) != 1
        status_not_ready = status != "ready"

        if media_missing or media_unavailable:
            review_media_unavailable_or_missing += 1
            review_required.append({
                **base,
                "category": "media_unavailable_or_missing",
                "reason_code": "cache.protected.reason.media_row_missing" if media_missing else "media unavailable",
            })
        elif status_not_ready:
            review_status_not_ready += 1
            review_required.append({**base, "category": "status_not_ready"})

    safe_files: list[dict[str, object]] = []
    safe_files_without_db = 0
    safe_files_without_db_bytes = 0
    safe_temporary_files = 0
    safe_temporary_files_bytes = 0
    review_unexpected_files = 0
    review_unexpected_files_bytes = 0

    protected_root = config.thumbnail_protected_cache_dir
    if protected_root.exists():
        for path in protected_root.rglob("*"):
            if not path.is_file():
                continue

            try:
                size_bytes = int(path.stat().st_size)
            except OSError:
                size_bytes = 0

            try:
                rel_path = path.resolve(strict=False).relative_to(
                    config.output_root.resolve(strict=False)
                ).as_posix()
            except ValueError:
                rel_path = str(path)

            is_temporary = _is_temporary_cache_file(path)
            is_expected = _path_under_any(path, protected_dirs)
            has_db_row = rel_path in db_rel_paths
            item = {
                "output_rel_path": rel_path,
                "filesystem_path": str(path),
                "size_bytes": size_bytes,
            }

            if is_temporary:
                safe_temporary_files += 1
                safe_temporary_files_bytes += size_bytes
                safe_files.append({**item, "category": "temporary_file"})
                continue

            if not is_expected:
                review_unexpected_files += 1
                review_unexpected_files_bytes += size_bytes
                review_required.append({**item, "category": "unexpected_file"})
                continue

            if not has_db_row:
                safe_files_without_db += 1
                safe_files_without_db_bytes += size_bytes
                safe_files.append({**item, "category": "file_without_db"})

    safe_to_delete_file_entries = len(safe_files)
    safe_to_delete_file_bytes = sum(int(item.get("size_bytes") or 0) for item in safe_files)
    safe_to_delete_db_rows = len(safe_db_rows)
    review_required_entries = len(review_required)

    samples = {
        "delete_files_without_db": [
            {k: v for k, v in item.items() if k != "filesystem_path"}
            for item in safe_files
            if item.get("category") == "file_without_db"
        ][:sample_limit],
        "delete_temporary_files": [
            {k: v for k, v in item.items() if k != "filesystem_path"}
            for item in safe_files
            if item.get("category") == "temporary_file"
        ][:sample_limit],
        "delete_db_rows_missing_file": [
            item for item in safe_db_rows if item.get("category") == "missing_file"
        ][:sample_limit],
        "delete_db_rows_invalid_path": [
            item for item in safe_db_rows if item.get("category") == "invalid_path"
        ][:sample_limit],
        "review_media_unavailable": [
            item for item in review_required if item.get("category") == "media_unavailable_or_missing"
        ][:sample_limit],
        "review_status_not_ready": [
            item for item in review_required if item.get("category") == "status_not_ready"
        ][:sample_limit],
        "review_unexpected_files": [
            {k: v for k, v in item.items() if k != "filesystem_path"}
            for item in review_required
            if item.get("category") == "unexpected_file"
        ][:sample_limit],
    }

    return {
        "audit": audit,
        "safe_file_candidates": safe_files,
        "safe_db_row_candidates": safe_db_rows,
        "review_required_candidates": review_required,
        "summary": {
            "safe_to_delete_entries": safe_to_delete_file_entries + safe_to_delete_db_rows,
            "safe_to_delete_files": safe_to_delete_file_entries,
            "safe_to_delete_db_rows": safe_to_delete_db_rows,
            "estimated_reclaimable_bytes": safe_to_delete_file_bytes,
            "review_required_entries": review_required_entries,
            "blocked_entries": 0,
        },
        "safe_to_delete_files": {
            "entries": safe_to_delete_file_entries,
            "estimated_reclaimable_bytes": safe_to_delete_file_bytes,
            "files_without_db": safe_files_without_db,
            "files_without_db_bytes": safe_files_without_db_bytes,
            "temporary_files": safe_temporary_files,
            "temporary_files_bytes": safe_temporary_files_bytes,
        },
        "safe_to_delete_db_rows": {
            "entries": safe_to_delete_db_rows,
            "rows_missing_file": safe_db_rows_missing_file,
            "rows_missing_file_recorded_bytes": safe_db_rows_missing_file_recorded_bytes,
            "rows_invalid_path": safe_db_rows_invalid_path,
        },
        "review_required": {
            "entries": review_required_entries,
            "media_unavailable_or_missing": review_media_unavailable_or_missing,
            "status_not_ready": review_status_not_ready,
            "unexpected_files": review_unexpected_files,
            "unexpected_files_bytes": review_unexpected_files_bytes,
        },
        "blocked": {
            "entries": 0,
            "reasons": [],
        },
        "samples": samples,
    }

def _protected_cache_cleanup_plan_payload(
    candidate_plan: dict[str, object],
    *,
    sample_limit: int = 12,
    started_at: float | None = None,
) -> dict[str, object]:
    """Format a protected-cache cleanup candidate plan for the public API."""
    started = time.time() if started_at is None else float(started_at)
    audit = candidate_plan["audit"]
    summary = dict(candidate_plan["summary"])
    plan_needed = int(summary.get("safe_to_delete_entries") or 0) > 0

    return {
        "ok": True,
        "protected_orphan_cleanup_plan": True,
        "version": 3,
        "mode": "read_only_cleanup_plan",
        "cache_class": "protected",
        "plan_needed": plan_needed,
        "duration_seconds": time.time() - started,
        "audit_duration_seconds": float(audit.get("duration_seconds") or 0.0),
        "sample_limit": max(0, int(sample_limit)),
        "summary": summary,
        "safe_to_delete_files": candidate_plan["safe_to_delete_files"],
        "safe_to_delete_db_rows": candidate_plan["safe_to_delete_db_rows"],
        "review_required": candidate_plan["review_required"],
        "blocked": candidate_plan["blocked"],
        "samples": candidate_plan["samples"],
        "writes": {
            "source_media": False,
            "dynamic_cache": False,
            "protected_cache": False,
            "catalog_db": False,
            "settings_json": False,
            "config_json": False,
        },
        "execute_contract": {
            "execute_step_required": True,
            "same_plan_logic_required": True,
            "automatic_cleanup": False,
            "protected_to_dynamic_reclassification": False,
        },
        "audit": audit,
    }


def thumbnail_cache_protected_orphan_cleanup_plan_bundle(
    config: Config,
    *,
    sample_limit: int = 12,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return both the public cleanup plan and the full executable candidate plan.

    The UI can show the public plan while the local server keeps the candidate
    plan in memory for the immediately following execute step. This avoids
    scanning the whole protected cache again during clean.
    """
    started_at = time.time()
    candidate_plan = _protected_cache_cleanup_candidate_plan(config, sample_limit=sample_limit)
    return (
        _protected_cache_cleanup_plan_payload(
            candidate_plan,
            sample_limit=sample_limit,
            started_at=started_at,
        ),
        candidate_plan,
    )


def thumbnail_cache_protected_orphan_cleanup_plan(config: Config, *, sample_limit: int = 12) -> dict[str, object]:
    """Return a read-only protected cache cleanup plan.

    This is deliberately a plan, not an executor. It uses the same candidate
    builder as the later execute step and groups candidates by operation.
    Source media, dynamic cache and settings are never touched.
    """
    plan, _candidate_plan = thumbnail_cache_protected_orphan_cleanup_plan_bundle(
        config,
        sample_limit=sample_limit,
    )
    return plan


def execute_protected_thumbnail_cache_orphan_cleanup(
    config: Config,
    *,
    sample_limit: int = 12,
    candidate_plan: dict[str, object] | None = None,
) -> dict[str, object]:
    """Execute the confirmed protected orphan cleanup using the same plan builder.

    The function deletes only candidates from the shared safe plan:
    protected files without DB rows, temporary protected files, and protected DB
    rows that no longer point to an existing cache file or have invalid cache
    paths. It does not touch source media, dynamic cache, review-required items,
    settings or config.
    """
    started_at = time.time()
    sample_limit = max(0, int(sample_limit))
    used_supplied_plan = candidate_plan is not None
    if candidate_plan is None:
        candidate_plan = _protected_cache_cleanup_candidate_plan(config, sample_limit=sample_limit)
    safe_files = list(candidate_plan["safe_file_candidates"])
    safe_db_rows = list(candidate_plan["safe_db_row_candidates"])

    deleted_files = 0
    deleted_db_rows = 0
    missing_files = 0
    skipped_changed = 0
    removed_bytes = 0
    error_count = 0
    sample_deleted_files: list[dict[str, object]] = []
    sample_deleted_db_rows: list[dict[str, object]] = []
    sample_skipped: list[dict[str, object]] = []
    sample_errors: list[dict[str, object]] = []

    protected_root = config.thumbnail_protected_cache_dir.resolve(strict=False)

    with open_database(config.db_path, read_only=False, validate=False) as connection:
        for item in safe_files:
            rel_path = str(item.get("output_rel_path") or "")
            category = str(item.get("category") or "")
            path = Path(str(item.get("filesystem_path") or ""))
            planned_size = int(item.get("size_bytes") or 0)

            try:
                resolved = path.resolve(strict=False)
                resolved.relative_to(protected_root)

                if category == "file_without_db":
                    still_without_db = connection.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM thumbnails
                        WHERE cache_class = 'protected'
                          AND output_rel_path = ?
                        """,
                        (rel_path,),
                    ).fetchone()
                    if int(still_without_db["count"] or 0) > 0:
                        skipped_changed += 1
                        if len(sample_skipped) < sample_limit:
                            sample_skipped.append({
                                "output_rel_path": rel_path,
                                "category": category,
                                "reason_code": "cache.protected.reason.db_row_appeared_before_execute",
                            })
                        continue

                if not path.exists() or not path.is_file():
                    missing_files += 1
                    continue

                actual_size = int(path.stat().st_size)
                path.unlink()
                deleted_files += 1
                removed_bytes += actual_size
                _remove_empty_cache_parents(path.parent, stop_dir=config.thumbnail_protected_cache_dir)

                if len(sample_deleted_files) < sample_limit:
                    sample_deleted_files.append({
                        "output_rel_path": rel_path,
                        "category": category,
                        "size_bytes": actual_size or planned_size,
                    })
            except (OSError, ValueError, ThumbnailCacheError) as exc:
                error_count += 1
                if len(sample_errors) < sample_limit:
                    sample_errors.append({
                        "output_rel_path": rel_path,
                        "category": category,
                        "size_bytes": planned_size,
                        "technical_detail": str(exc),
                    })

        for item in safe_db_rows:
            row_id = int(item.get("id") or 0)
            rel_path = str(item.get("output_rel_path") or "")
            category = str(item.get("category") or "")
            try:
                row = connection.execute(
                    """
                    SELECT id, output_rel_path, cache_class
                    FROM thumbnails
                    WHERE id = ?
                      AND cache_class = 'protected'
                    """,
                    (row_id,),
                ).fetchone()
                if row is None:
                    skipped_changed += 1
                    if len(sample_skipped) < sample_limit:
                        sample_skipped.append({
                            "id": row_id,
                            "output_rel_path": rel_path,
                            "category": category,
                            "reason_code": "cache.protected.reason.db_row_no_longer_exists",
                        })
                    continue

                can_delete = False
                if category == "invalid_path":
                    can_delete = True
                elif category == "missing_file":
                    try:
                        path = _thumbnail_filesystem_path(config, str(row["output_rel_path"]))
                        can_delete = not (path.exists() and path.is_file())
                    except ThumbnailCacheError:
                        can_delete = True

                if not can_delete:
                    skipped_changed += 1
                    if len(sample_skipped) < sample_limit:
                        sample_skipped.append({
                            "id": row_id,
                            "output_rel_path": rel_path,
                            "category": category,
                            "reason_code": "cache.protected.reason.db_row_no_longer_safe_candidate",
                        })
                    continue

                connection.execute("DELETE FROM thumbnails WHERE id = ?", (row_id,))
                deleted_db_rows += 1
                if len(sample_deleted_db_rows) < sample_limit:
                    sample_deleted_db_rows.append({
                        "id": row_id,
                        "output_rel_path": rel_path,
                        "category": category,
                    })
            except (OSError, ThumbnailCacheError) as exc:
                error_count += 1
                if len(sample_errors) < sample_limit:
                    sample_errors.append({
                        "id": row_id,
                        "output_rel_path": rel_path,
                        "category": category,
                        "technical_detail": str(exc),
                    })

        connection.commit()

    duration_seconds = time.time() - started_at
    result = {
        "ok": error_count == 0,
        "protected_orphan_cleanup_execute": True,
        "version": 1,
        "mode": "confirmed_execute",
        "cache_class": "protected",
        "duration_seconds": duration_seconds,
        "planned": candidate_plan["summary"],
        "used_supplied_plan": used_supplied_plan,
        "deleted_files": deleted_files,
        "deleted_db_rows": deleted_db_rows,
        "missing_files": missing_files,
        "skipped_changed": skipped_changed,
        "removed_bytes": removed_bytes,
        "error_count": error_count,
        "sample_deleted_files": sample_deleted_files,
        "sample_deleted_db_rows": sample_deleted_db_rows,
        "sample_skipped": sample_skipped,
        "sample_errors": sample_errors,
        "writes": {
            "source_media": False,
            "dynamic_cache": False,
            "protected_cache_files": deleted_files > 0,
            "catalog_db": deleted_db_rows > 0,
            "settings_json": False,
            "config_json": False,
        },
    }
    completion_params = {
        "deleted_files": deleted_files,
        "deleted_db_rows": deleted_db_rows,
        "removed_bytes": removed_bytes,
        "error_count": error_count,
    }
    if error_count > 0:
        completion = build_backend_message(
            "cache.protected.cleanup.execute.completed_with_errors",
            severity="warning",
            params=completion_params,
        )
    elif deleted_files == 0 and deleted_db_rows == 0:
        completion = build_backend_message(
            "cache.protected.cleanup.execute.no_candidates",
            severity="success",
            params=completion_params,
        )
    else:
        completion = build_backend_message(
            "cache.protected.cleanup.execute.completed",
            severity="success",
            params=completion_params,
        )
    result["result_messages"] = [completion]
    return result


def _gif_preview_work_rows(config: Config, *, branch_rel_path: str):
    """Return only GIF media whose protected preview needs work according to DB state."""
    branch_key = catalog_path_key(branch_rel_path)
    with open_database(config.db_path, read_only=True) as connection:
        scope_sql = ""
        params: list[object] = [GIF_PREVIEW_VARIANT_KEY]
        if branch_rel_path:
            scope_sql = """
              AND folder.is_available = 1
              AND (folder.path_key = ? OR folder.path_key LIKE ? ESCAPE '\\')
            """
            params.extend((branch_key, _like_branch_pattern(branch_key)))
        params.append(GIF_PREVIEW_ALGORITHM_VERSION)
        return connection.execute(
            f"""
            SELECT media.id, media.rel_path, media.size_bytes, media.modified_time
            FROM media_files AS media
            JOIN folders AS folder ON folder.id = media.folder_id
            LEFT JOIN thumbnails AS thumb
              ON thumb.media_id = media.id
             AND thumb.thumbnail_type = 'gif_preview'
             AND thumb.variant_key = ?
            WHERE media.is_available = 1
              AND media.media_type = 'gif'
              {scope_sql}
              AND (
                    thumb.id IS NULL
                 OR thumb.status <> 'ready'
                 OR thumb.source_size_bytes <> media.size_bytes
                 OR thumb.source_modified_time <> media.modified_time
                 OR thumb.algorithm_version <> ?
              )
            ORDER BY folder.sort_key, media.sort_key, media.path_key
            """,
            tuple(params),
        ).fetchall()


def _video_poster_work_rows(config: Config, *, branch_rel_path: str):
    """Return only videos whose protected poster needs work according to DB state."""
    branch_key = catalog_path_key(branch_rel_path)
    with open_database(config.db_path, read_only=True) as connection:
        scope_sql = ""
        params: list[object] = [VIDEO_POSTER_VARIANT_KEY]
        if branch_rel_path:
            scope_sql = """
              AND folder.is_available = 1
              AND (folder.path_key = ? OR folder.path_key LIKE ? ESCAPE '\\')
            """
            params.extend((branch_key, _like_branch_pattern(branch_key)))
        params.append(VIDEO_POSTER_ALGORITHM_VERSION)
        return connection.execute(
            f"""
            SELECT media.id, media.rel_path, media.size_bytes, media.modified_time
            FROM media_files AS media
            JOIN folders AS folder ON folder.id = media.folder_id
            LEFT JOIN thumbnails AS thumb
              ON thumb.media_id = media.id
             AND thumb.thumbnail_type = 'video_poster'
             AND thumb.variant_key = ?
            WHERE media.is_available = 1
              AND media.media_type = 'video'
              {scope_sql}
              AND (
                    thumb.id IS NULL
                 OR thumb.status <> 'ready'
                 OR thumb.source_size_bytes <> media.size_bytes
                 OR thumb.source_modified_time <> media.modified_time
                 OR thumb.algorithm_version <> ?
              )
            ORDER BY folder.sort_key, media.sort_key, media.path_key
            """,
            tuple(params),
        ).fetchall()


def _video_frame_work_rows(config: Config, *, branch_rel_path: str):
    """Return one row per video-frame variant that needs work according to DB state."""
    branch_key = catalog_path_key(branch_rel_path)
    with open_database(config.db_path, read_only=True) as connection:
        scope_sql = ""
        params: list[object] = []
        if branch_rel_path:
            scope_sql = """
              AND folder.is_available = 1
              AND (folder.path_key = ? OR folder.path_key LIKE ? ESCAPE '\\')
            """
            params.extend((branch_key, _like_branch_pattern(branch_key)))
        params.append(VIDEO_FRAME_ALGORITHM_VERSION)
        return connection.execute(
            f"""
            WITH variants(variant_key) AS (
                VALUES ('frame_1'), ('frame_2'), ('frame_3'), ('frame_4')
            )
            SELECT
                media.id,
                media.rel_path,
                media.size_bytes,
                media.modified_time,
                variants.variant_key
            FROM media_files AS media
            JOIN folders AS folder ON folder.id = media.folder_id
            CROSS JOIN variants
            LEFT JOIN thumbnails AS thumb
              ON thumb.media_id = media.id
             AND thumb.thumbnail_type = 'video_frame'
             AND thumb.variant_key = variants.variant_key
            WHERE media.is_available = 1
              AND media.media_type = 'video'
              {scope_sql}
              AND (
                    thumb.id IS NULL
                 OR thumb.status <> 'ready'
                 OR thumb.source_size_bytes <> media.size_bytes
                 OR thumb.source_modified_time <> media.modified_time
                 OR thumb.algorithm_version <> ?
              )
            ORDER BY folder.sort_key, media.sort_key, media.path_key, variants.variant_key
            """,
            tuple(params),
        ).fetchall()


def _gif_media_rows(config: Config, *, branch_rel_path: str):
    branch_key = catalog_path_key(branch_rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        if not branch_rel_path:
            return connection.execute(
                """
                SELECT
                    id,
                    rel_path,
                    size_bytes,
                    modified_time
                FROM media_files
                WHERE is_available = 1
                  AND media_type = 'gif'
                ORDER BY sort_key, path_key
                """
            ).fetchall()

        return connection.execute(
            """
            SELECT
                media.id,
                media.rel_path,
                media.size_bytes,
                media.modified_time
            FROM media_files AS media
            JOIN folders AS folder
              ON folder.id = media.folder_id
            WHERE media.is_available = 1
              AND media.media_type = 'gif'
              AND folder.is_available = 1
              AND (
                  folder.path_key = ?
                  OR folder.path_key LIKE ? ESCAPE '\\'
              )
            ORDER BY folder.sort_key, media.sort_key, media.path_key
            """,
            (branch_key, _like_branch_pattern(branch_key)),
        ).fetchall()



def _video_media_rows(config: Config, *, branch_rel_path: str):
    branch_key = catalog_path_key(branch_rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        if not branch_rel_path:
            return connection.execute(
                """
                SELECT
                    id,
                    rel_path,
                    size_bytes,
                    modified_time
                FROM media_files
                WHERE is_available = 1
                  AND media_type = 'video'
                ORDER BY sort_key, path_key
                """
            ).fetchall()

        return connection.execute(
            """
            SELECT
                media.id,
                media.rel_path,
                media.size_bytes,
                media.modified_time
            FROM media_files AS media
            JOIN folders AS folder
              ON folder.id = media.folder_id
            WHERE media.is_available = 1
              AND media.media_type = 'video'
              AND folder.is_available = 1
              AND (
                  folder.path_key = ?
                  OR folder.path_key LIKE ? ESCAPE '\\'
              )
            ORDER BY folder.sort_key, media.sort_key, media.path_key
            """,
            (branch_key, _like_branch_pattern(branch_key)),
        ).fetchall()


def _like_branch_pattern(path_key: str) -> str:
    escaped = (
        path_key
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"{escaped}/%"


def _ready_existing_photo_tile(
    config: Config,
    *,
    media_id: int,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource | None:
    with open_database(config.db_path, read_only=False) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                status
            FROM thumbnails
            WHERE media_id = ?
              AND thumbnail_type = 'photo_tile'
              AND variant_key = ?
            """,
            (media_id, PHOTO_TILE_VARIANT_KEY),
        ).fetchone()

        if row is None:
            return None

        path = _thumbnail_filesystem_path(config, str(row["output_rel_path"]))
        source_matches = _source_metadata_matches(
            int(row["source_size_bytes"]),
            float(row["source_modified_time"]),
            source_size_bytes,
            source_modified_time,
        )

        if str(row["status"]) == "ready" and source_matches and path.exists() and path.is_file():
            stat_result = path.stat()
            now = time.time()
            connection.execute(
                """
                UPDATE thumbnails
                SET last_used_at = ?,
                    updated_at = ?,
                    file_size_bytes = ?
                WHERE id = ?
                """,
                (now, now, int(stat_result.st_size), int(row["id"])),
            )
            connection.commit()
            return ThumbnailResource(
                rel_path=str(row["output_rel_path"]),
                thumbnail_type="photo_tile",
                cache_class="dynamic",
                variant_key=PHOTO_TILE_VARIANT_KEY,
                filesystem_path=path,
                file_name=path.name,
                mime_type="image/webp",
                size_bytes=int(stat_result.st_size),
                width=int(row["width"]),
                height=int(row["height"]),
                generated=False,
            )

        # Stale/missing cache evidence is kept for later cleanup/regeneration logic.
        # For photo_tile this request can regenerate immediately from the available source.
        if not source_matches and str(row["status"]) != "stale":
            now = time.time()
            connection.execute(
                """
                UPDATE thumbnails
                SET status = 'stale',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, int(row["id"])),
            )
            connection.commit()

    return None


def _generate_photo_tile(
    config: Config,
    *,
    media_id: int,
    rel_path: str,
    source_path: Path,
    source_size_bytes: int,
    source_modified_time: float,
    destination: Path | None = None,
) -> ThumbnailResource:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ThumbnailCacheError(
            "Pillow is not installed. Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc

    if destination is None:
        destination = _photo_tile_destination(
            config,
            rel_path=rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_rel_path = _output_relative_path(config, destination)
    temp_path = _unique_thumbnail_temp_path(destination)

    try:
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(config.image_thumb_size, Image.Resampling.LANCZOS)

            if image.mode not in {"RGB", "RGBA"}:
                if "A" in image.getbands() or "transparency" in image.info:
                    image = image.convert("RGBA")
                else:
                    image = image.convert("RGB")

            width, height = image.size
            image.save(
                temp_path,
                format=PHOTO_TILE_FORMAT,
                quality=PHOTO_TILE_QUALITY,
                method=4,
            )

        temp_path.replace(destination)
        file_size = destination.stat().st_size
        now = time.time()

        with open_database(config.db_path, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO thumbnails (
                    media_id,
                    thumbnail_type,
                    cache_class,
                    variant_key,
                    output_rel_path,
                    width,
                    height,
                    file_size_bytes,
                    source_size_bytes,
                    source_modified_time,
                    algorithm_version,
                    status,
                    created_at,
                    updated_at,
                    last_used_at,
                    error_message
                )
                VALUES (?, 'photo_tile', 'dynamic', ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, NULL)
                ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                    cache_class = excluded.cache_class,
                    output_rel_path = excluded.output_rel_path,
                    width = excluded.width,
                    height = excluded.height,
                    file_size_bytes = excluded.file_size_bytes,
                    source_size_bytes = excluded.source_size_bytes,
                    source_modified_time = excluded.source_modified_time,
                    algorithm_version = excluded.algorithm_version,
                    status = 'ready',
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at,
                    error_message = NULL
                """,
                (
                    media_id,
                    PHOTO_TILE_VARIANT_KEY,
                    output_rel_path,
                    int(width),
                    int(height),
                    int(file_size),
                    int(source_size_bytes),
                    float(source_modified_time),
                    PHOTO_TILE_ALGORITHM_VERSION,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()

        cleanup_dynamic_thumbnail_cache(
            config,
            preserve_output_rel_path=output_rel_path,
        )

        return ThumbnailResource(
            rel_path=output_rel_path,
            thumbnail_type="photo_tile",
            cache_class="dynamic",
            variant_key=PHOTO_TILE_VARIANT_KEY,
            filesystem_path=destination,
            file_name=destination.name,
            mime_type="image/webp",
            size_bytes=int(file_size),
            width=int(width),
            height=int(height),
            generated=True,
        )

    except Exception as exc:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        _record_photo_tile_error(
            config,
            media_id=media_id,
            output_rel_path=output_rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=str(exc),
        )
        if isinstance(exc, ThumbnailCacheError):
            raise
        raise ThumbnailCacheError(f"Could not create thumbnail: {exc}") from exc




def _ready_existing_gif_preview(
    config: Config,
    *,
    media_id: int,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource | None:
    with open_database(config.db_path, read_only=False) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                status
            FROM thumbnails
            WHERE media_id = ?
              AND thumbnail_type = 'gif_preview'
              AND variant_key = ?
            """,
            (media_id, GIF_PREVIEW_VARIANT_KEY),
        ).fetchone()

        if row is None:
            return None

        path = _thumbnail_filesystem_path(config, str(row["output_rel_path"]))
        source_matches = _source_metadata_matches(
            int(row["source_size_bytes"]),
            float(row["source_modified_time"]),
            source_size_bytes,
            source_modified_time,
        )

        if str(row["status"]) == "ready" and source_matches and path.exists() and path.is_file():
            stat_result = path.stat()
            now = time.time()
            connection.execute(
                """
                UPDATE thumbnails
                SET last_used_at = ?,
                    updated_at = ?,
                    file_size_bytes = ?
                WHERE id = ?
                """,
                (now, now, int(stat_result.st_size), int(row["id"])),
            )
            connection.commit()
            return ThumbnailResource(
                rel_path=str(row["output_rel_path"]),
                thumbnail_type="gif_preview",
                cache_class="protected",
                variant_key=GIF_PREVIEW_VARIANT_KEY,
                filesystem_path=path,
                file_name=path.name,
                mime_type="image/webp",
                size_bytes=int(stat_result.st_size),
                width=int(row["width"]),
                height=int(row["height"]),
                generated=False,
            )

        if not source_matches and str(row["status"]) != "stale":
            now = time.time()
            connection.execute(
                """
                UPDATE thumbnails
                SET status = 'stale',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, int(row["id"])),
            )
            connection.commit()

    return None


def _generate_gif_preview(
    config: Config,
    *,
    media_id: int,
    rel_path: str,
    source_path: Path,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ThumbnailCacheError(
            "Pillow is not installed. Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc

    destination = _gif_preview_destination(
        config,
        rel_path=rel_path,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_rel_path = _output_relative_path(config, destination)
    temp_path = _unique_thumbnail_temp_path(destination)

    try:
        with Image.open(source_path) as image:
            image.seek(0)
            frame = image.convert("RGBA")
            frame.thumbnail(config.gif_thumb_size, Image.Resampling.LANCZOS)
            width, height = frame.size
            frame.save(
                temp_path,
                format=GIF_PREVIEW_FORMAT,
                quality=GIF_PREVIEW_QUALITY,
                method=4,
            )

        temp_path.replace(destination)
        file_size = destination.stat().st_size
        now = time.time()

        with open_database(config.db_path, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO thumbnails (
                    media_id,
                    thumbnail_type,
                    cache_class,
                    variant_key,
                    output_rel_path,
                    width,
                    height,
                    file_size_bytes,
                    source_size_bytes,
                    source_modified_time,
                    algorithm_version,
                    status,
                    created_at,
                    updated_at,
                    last_used_at,
                    error_message
                )
                VALUES (?, 'gif_preview', 'protected', ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, NULL)
                ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                    cache_class = excluded.cache_class,
                    output_rel_path = excluded.output_rel_path,
                    width = excluded.width,
                    height = excluded.height,
                    file_size_bytes = excluded.file_size_bytes,
                    source_size_bytes = excluded.source_size_bytes,
                    source_modified_time = excluded.source_modified_time,
                    algorithm_version = excluded.algorithm_version,
                    status = 'ready',
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at,
                    error_message = NULL
                """,
                (
                    media_id,
                    GIF_PREVIEW_VARIANT_KEY,
                    output_rel_path,
                    int(width),
                    int(height),
                    int(file_size),
                    int(source_size_bytes),
                    float(source_modified_time),
                    GIF_PREVIEW_ALGORITHM_VERSION,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()

        return ThumbnailResource(
            rel_path=output_rel_path,
            thumbnail_type="gif_preview",
            cache_class="protected",
            variant_key=GIF_PREVIEW_VARIANT_KEY,
            filesystem_path=destination,
            file_name=destination.name,
            mime_type="image/webp",
            size_bytes=int(file_size),
            width=int(width),
            height=int(height),
            generated=True,
        )

    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        _record_gif_preview_error(
            config,
            media_id=media_id,
            output_rel_path=output_rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=str(exc),
        )
        raise ThumbnailCacheError(f"Could not create GIF preview for {rel_path}: {exc}") from exc



def _ready_existing_video_poster(
    config: Config,
    *,
    media_id: int,
    source_size_bytes: int,
    source_modified_time: float,
    update_usage: bool = True,
    mark_stale: bool = True,
) -> ThumbnailResource | None:
    write_needed = update_usage or mark_stale
    with open_database(config.db_path, read_only=not write_needed) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                status
            FROM thumbnails
            WHERE media_id = ?
              AND thumbnail_type = 'video_poster'
              AND variant_key = ?
            """,
            (media_id, VIDEO_POSTER_VARIANT_KEY),
        ).fetchone()

        if row is None:
            return None

        path = _thumbnail_filesystem_path(config, str(row["output_rel_path"]))
        source_matches = _source_metadata_matches(
            int(row["source_size_bytes"]),
            float(row["source_modified_time"]),
            source_size_bytes,
            source_modified_time,
        )

        if str(row["status"]) == "ready" and source_matches and path.exists() and path.is_file():
            stat_result = path.stat()
            if update_usage:
                now = time.time()
                connection.execute(
                    """
                    UPDATE thumbnails
                    SET last_used_at = ?,
                        updated_at = ?,
                        file_size_bytes = ?
                    WHERE id = ?
                    """,
                    (now, now, int(stat_result.st_size), int(row["id"])),
                )
                connection.commit()
            return ThumbnailResource(
                rel_path=str(row["output_rel_path"]),
                thumbnail_type="video_poster",
                cache_class="protected",
                variant_key=VIDEO_POSTER_VARIANT_KEY,
                filesystem_path=path,
                file_name=path.name,
                mime_type="image/webp",
                size_bytes=int(stat_result.st_size),
                width=int(row["width"]),
                height=int(row["height"]),
                generated=False,
            )

        if mark_stale and not source_matches and str(row["status"]) != "stale":
            now = time.time()
            connection.execute(
                """
                UPDATE thumbnails
                SET status = 'stale',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, int(row["id"])),
            )
            connection.commit()

    return None


def _ready_existing_video_frame(
    config: Config,
    *,
    media_id: int,
    variant_key: str,
    source_size_bytes: int,
    source_modified_time: float,
    update_usage: bool = True,
    mark_stale: bool = True,
) -> ThumbnailResource | None:
    write_needed = update_usage or mark_stale
    with open_database(config.db_path, read_only=not write_needed) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                status
            FROM thumbnails
            WHERE media_id = ?
              AND thumbnail_type = 'video_frame'
              AND variant_key = ?
            """,
            (media_id, variant_key),
        ).fetchone()

        if row is None:
            return None

        path = _thumbnail_filesystem_path(config, str(row["output_rel_path"]))
        source_matches = _source_metadata_matches(
            int(row["source_size_bytes"]),
            float(row["source_modified_time"]),
            source_size_bytes,
            source_modified_time,
        )

        if str(row["status"]) == "ready" and source_matches and path.exists() and path.is_file():
            stat_result = path.stat()
            if update_usage:
                now = time.time()
                connection.execute(
                    """
                    UPDATE thumbnails
                    SET last_used_at = ?,
                        updated_at = ?,
                        file_size_bytes = ?
                    WHERE id = ?
                    """,
                    (now, now, int(stat_result.st_size), int(row["id"])),
                )
                connection.commit()
            return ThumbnailResource(
                rel_path=str(row["output_rel_path"]),
                thumbnail_type="video_frame",
                cache_class="protected",
                variant_key=variant_key,
                filesystem_path=path,
                file_name=path.name,
                mime_type="image/webp",
                size_bytes=int(stat_result.st_size),
                width=int(row["width"]),
                height=int(row["height"]),
                generated=False,
            )

        if mark_stale and not source_matches and str(row["status"]) != "stale":
            now = time.time()
            connection.execute(
                """
                UPDATE thumbnails
                SET status = 'stale',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, int(row["id"])),
            )
            connection.commit()

    return None


def _generate_video_poster(
    config: Config,
    *,
    media_id: int,
    rel_path: str,
    source_path: Path,
    source_size_bytes: int,
    source_modified_time: float,
) -> ThumbnailResource:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ThumbnailCacheError(
            "Pillow is not installed. Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc

    try:
        tools = require_video_tools()
    except VideoToolsError as exc:
        destination = _video_poster_destination(
            config,
            rel_path=rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
        )
        _record_video_poster_error(
            config,
            media_id=media_id,
            output_rel_path=_output_relative_path(config, destination),
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=str(exc),
        )
        raise ThumbnailCacheError(str(exc)) from exc

    destination = _video_poster_destination(
        config,
        rel_path=rel_path,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_rel_path = _output_relative_path(config, destination)
    raw_frame_path = _unique_thumbnail_frame_path(destination)
    temp_path = _unique_thumbnail_temp_path(destination)

    try:
        duration = _probe_video_duration(
            tools.ffprobe_path,
            source_path,
            timeout_seconds=min(config.ffmpeg_timeout_seconds, 30),
        )
        seek_time = _video_poster_seek_time(duration)

        command = [
            tools.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{seek_time:.3f}",
            "-threads",
            str(config.ffmpeg_threads_per_job),
            "-i",
            str(source_path),
            "-frames:v",
            "1",
            str(raw_frame_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=config.ffmpeg_timeout_seconds,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            raise ThumbnailCacheError(
                f"ffmpeg did not create a video poster for {rel_path}: {stderr or 'unknown error'}"
            )
        if not raw_frame_path.exists() or not raw_frame_path.is_file():
            raise ThumbnailCacheError(f"ffmpeg did not produce an output frame for {rel_path}")

        with Image.open(raw_frame_path) as image:
            image.thumbnail((config.video_preview_width, config.video_preview_width), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            width, height = image.size
            image.save(
                temp_path,
                format=VIDEO_POSTER_FORMAT,
                quality=VIDEO_POSTER_QUALITY,
                method=4,
            )

        temp_path.replace(destination)
        file_size = destination.stat().st_size
        now = time.time()

        with open_database(config.db_path, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO thumbnails (
                    media_id,
                    thumbnail_type,
                    cache_class,
                    variant_key,
                    output_rel_path,
                    width,
                    height,
                    file_size_bytes,
                    source_size_bytes,
                    source_modified_time,
                    algorithm_version,
                    status,
                    created_at,
                    updated_at,
                    last_used_at,
                    error_message
                )
                VALUES (?, 'video_poster', 'protected', ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, NULL)
                ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                    cache_class = excluded.cache_class,
                    output_rel_path = excluded.output_rel_path,
                    width = excluded.width,
                    height = excluded.height,
                    file_size_bytes = excluded.file_size_bytes,
                    source_size_bytes = excluded.source_size_bytes,
                    source_modified_time = excluded.source_modified_time,
                    algorithm_version = excluded.algorithm_version,
                    status = 'ready',
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at,
                    error_message = NULL
                """,
                (
                    media_id,
                    VIDEO_POSTER_VARIANT_KEY,
                    output_rel_path,
                    int(width),
                    int(height),
                    int(file_size),
                    int(source_size_bytes),
                    float(source_modified_time),
                    VIDEO_POSTER_ALGORITHM_VERSION,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()

        return ThumbnailResource(
            rel_path=output_rel_path,
            thumbnail_type="video_poster",
            cache_class="protected",
            variant_key=VIDEO_POSTER_VARIANT_KEY,
            filesystem_path=destination,
            file_name=destination.name,
            mime_type="image/webp",
            size_bytes=int(file_size),
            width=int(width),
            height=int(height),
            generated=True,
        )

    except subprocess.TimeoutExpired as exc:
        message = f"ffmpeg exceeded the {config.ffmpeg_timeout_seconds} s timeout while creating a poster: {rel_path}"
        _record_video_poster_error(
            config,
            media_id=media_id,
            output_rel_path=output_rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=message,
        )
        raise ThumbnailCacheError(message) from exc
    except Exception as exc:
        _record_video_poster_error(
            config,
            media_id=media_id,
            output_rel_path=output_rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=str(exc),
        )
        if isinstance(exc, ThumbnailCacheError):
            raise
        raise ThumbnailCacheError(f"Could not create video poster for {rel_path}: {exc}") from exc
    finally:
        for path in (raw_frame_path, temp_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


def _generate_video_frame(
    config: Config,
    *,
    media_id: int,
    rel_path: str,
    source_path: Path,
    source_size_bytes: int,
    source_modified_time: float,
    variant_key: str,
    seek_time: float,
) -> ThumbnailResource:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ThumbnailCacheError(
            "Pillow is not installed. Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc

    try:
        tools = require_video_tools()
    except VideoToolsError as exc:
        destination = _video_frame_destination(
            config,
            rel_path=rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            variant_key=variant_key,
        )
        _record_video_frame_error(
            config,
            media_id=media_id,
            variant_key=variant_key,
            output_rel_path=_output_relative_path(config, destination),
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=str(exc),
        )
        raise ThumbnailCacheError(str(exc)) from exc

    destination = _video_frame_destination(
        config,
        rel_path=rel_path,
        source_size_bytes=source_size_bytes,
        source_modified_time=source_modified_time,
        variant_key=variant_key,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_rel_path = _output_relative_path(config, destination)
    raw_frame_path = _unique_thumbnail_frame_path(destination)
    temp_path = _unique_thumbnail_temp_path(destination)

    try:
        command = [
            tools.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{seek_time:.3f}",
            "-threads",
            str(config.ffmpeg_threads_per_job),
            "-i",
            str(source_path),
            "-frames:v",
            "1",
            str(raw_frame_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=config.ffmpeg_timeout_seconds,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            raise ThumbnailCacheError(
                f"ffmpeg did not create video frame {variant_key} for {rel_path}: {stderr or 'unknown error'}"
            )
        if not raw_frame_path.exists() or not raw_frame_path.is_file():
            raise ThumbnailCacheError(f"ffmpeg did not produce output video frame {variant_key} for {rel_path}")

        with Image.open(raw_frame_path) as image:
            image.thumbnail((config.video_preview_width, config.video_preview_width), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            width, height = image.size
            image.save(
                temp_path,
                format=VIDEO_FRAME_FORMAT,
                quality=VIDEO_FRAME_QUALITY,
                method=4,
            )

        temp_path.replace(destination)
        file_size = destination.stat().st_size
        now = time.time()

        with open_database(config.db_path, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO thumbnails (
                    media_id,
                    thumbnail_type,
                    cache_class,
                    variant_key,
                    output_rel_path,
                    width,
                    height,
                    file_size_bytes,
                    source_size_bytes,
                    source_modified_time,
                    algorithm_version,
                    status,
                    created_at,
                    updated_at,
                    last_used_at,
                    error_message
                )
                VALUES (?, 'video_frame', 'protected', ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, NULL)
                ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                    cache_class = excluded.cache_class,
                    output_rel_path = excluded.output_rel_path,
                    width = excluded.width,
                    height = excluded.height,
                    file_size_bytes = excluded.file_size_bytes,
                    source_size_bytes = excluded.source_size_bytes,
                    source_modified_time = excluded.source_modified_time,
                    algorithm_version = excluded.algorithm_version,
                    status = 'ready',
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at,
                    error_message = NULL
                """,
                (
                    media_id,
                    variant_key,
                    output_rel_path,
                    int(width),
                    int(height),
                    int(file_size),
                    int(source_size_bytes),
                    float(source_modified_time),
                    VIDEO_FRAME_ALGORITHM_VERSION,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()

        return ThumbnailResource(
            rel_path=output_rel_path,
            thumbnail_type="video_frame",
            cache_class="protected",
            variant_key=variant_key,
            filesystem_path=destination,
            file_name=destination.name,
            mime_type="image/webp",
            size_bytes=int(file_size),
            width=int(width),
            height=int(height),
            generated=True,
        )

    except subprocess.TimeoutExpired as exc:
        message = f"ffmpeg exceeded the {config.ffmpeg_timeout_seconds} s timeout while creating video frame {variant_key}: {rel_path}"
        _record_video_frame_error(
            config,
            media_id=media_id,
            variant_key=variant_key,
            output_rel_path=output_rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=message,
        )
        raise ThumbnailCacheError(message) from exc
    except Exception as exc:
        _record_video_frame_error(
            config,
            media_id=media_id,
            variant_key=variant_key,
            output_rel_path=output_rel_path,
            source_size_bytes=source_size_bytes,
            source_modified_time=source_modified_time,
            message=str(exc),
        )
        if isinstance(exc, ThumbnailCacheError):
            raise
        raise ThumbnailCacheError(f"Could not create video frame {variant_key} for {rel_path}: {exc}") from exc
    finally:
        for path in (raw_frame_path, temp_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


def cleanup_dynamic_thumbnail_cache(
    config: Config,
    *,
    preserve_output_rel_path: str | None = None,
) -> ThumbnailCleanupResult:
    """
    Keep dynamic thumbnail cache under the configured dynamic limit.

    Protected cache is never deleted by this automatic cleanup and does not force
    additional dynamic deletion. A freshly returned thumbnail can be preserved so
    the request that created it does not delete its own response when the limit is
    very small. Dynamic thumbnails used by current folder previews are protected
    while referenced.
    """
    limit_bytes = int(config.thumbnail_cache_limit_gb * 1024 * 1024 * 1024)

    with open_database(config.db_path, read_only=False) as connection:
        total_before = _cache_size_totals(connection)
        total_size_before = total_before["total"]
        dynamic_size_before = total_before["dynamic"]
        protected_size_before = total_before["protected"]

        if dynamic_size_before <= limit_bytes:
            return ThumbnailCleanupResult(
                limit_bytes=limit_bytes,
                total_size_before_bytes=total_size_before,
                total_size_after_bytes=total_size_before,
                dynamic_size_before_bytes=dynamic_size_before,
                dynamic_size_after_bytes=dynamic_size_before,
                protected_size_before_bytes=protected_size_before,
                protected_size_after_bytes=protected_size_before,
                over_limit_before=False,
                over_limit_after=False,
                deleted_entries=0,
                deleted_files=0,
                removed_bytes=0,
                error_count=0,
                preserved_output_rel_path=preserve_output_rel_path,
            )

        rows = connection.execute(
            """
            SELECT
                id,
                output_rel_path,
                file_size_bytes,
                status,
                created_at,
                updated_at,
                last_used_at
            FROM thumbnails
            WHERE cache_class = 'dynamic'
              AND (? IS NULL OR output_rel_path <> ?)
              AND NOT EXISTS (
                  SELECT 1
                  FROM folder_preview_items AS fpi
                  WHERE fpi.media_id = thumbnails.media_id
              )
            ORDER BY
                CASE status
                    WHEN 'missing' THEN 0
                    WHEN 'stale' THEN 1
                    WHEN 'error' THEN 2
                    ELSE 3
                END,
                COALESCE(last_used_at, updated_at, created_at, 0),
                id
            """,
            (preserve_output_rel_path, preserve_output_rel_path),
        ).fetchall()

        current_dynamic = dynamic_size_before
        deleted_entries = 0
        deleted_files = 0
        removed_bytes = 0
        error_count = 0

        for row in rows:
            if current_dynamic <= limit_bytes:
                break

            output_rel_path = str(row["output_rel_path"])
            stored_size = int(row["file_size_bytes"])

            try:
                thumbnail_path = _dynamic_thumbnail_filesystem_path(config, output_rel_path)
                file_existed = thumbnail_path.exists()
                if file_existed and thumbnail_path.is_file():
                    thumbnail_path.unlink()
                    deleted_files += 1
                    _remove_empty_cache_parents(
                        thumbnail_path.parent,
                        stop_dir=config.thumbnail_dynamic_cache_dir,
                    )

                connection.execute(
                    "DELETE FROM thumbnails WHERE id = ?",
                    (int(row["id"]),),
                )
                deleted_entries += 1
                removed_bytes += stored_size
                current_dynamic = max(0, current_dynamic - stored_size)
            except OSError:
                error_count += 1
            except ThumbnailCacheError:
                error_count += 1

        connection.commit()

        total_after = _cache_size_totals(connection)
        return ThumbnailCleanupResult(
            limit_bytes=limit_bytes,
            total_size_before_bytes=total_size_before,
            total_size_after_bytes=total_after["total"],
            dynamic_size_before_bytes=dynamic_size_before,
            dynamic_size_after_bytes=total_after["dynamic"],
            protected_size_before_bytes=protected_size_before,
            protected_size_after_bytes=total_after["protected"],
            over_limit_before=dynamic_size_before > limit_bytes,
            over_limit_after=total_after["dynamic"] > limit_bytes,
            deleted_entries=deleted_entries,
            deleted_files=deleted_files,
            removed_bytes=removed_bytes,
            error_count=error_count,
            preserved_output_rel_path=preserve_output_rel_path,
        )


def _cache_size_totals(connection) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        """
    ).fetchone()
    total = int(row["size_bytes"]) if row is not None else 0

    by_class = {"dynamic": 0, "protected": 0}
    for class_row in connection.execute(
        """
        SELECT cache_class,
               COALESCE(SUM(file_size_bytes), 0) AS size_bytes
        FROM thumbnails
        GROUP BY cache_class
        """
    ):
        by_class[str(class_row["cache_class"])] = int(class_row["size_bytes"])

    return {
        "total": total,
        "dynamic": by_class["dynamic"],
        "protected": by_class["protected"],
    }


def _dynamic_thumbnail_filesystem_path(config: Config, output_rel_path: str) -> Path:
    path = _thumbnail_filesystem_path(config, output_rel_path)
    try:
        path.relative_to(config.thumbnail_dynamic_cache_dir.resolve(strict=False))
    except ValueError as exc:
        raise ThumbnailCacheError("Dynamic cache path is outside the dynamic thumbnail cache directory.") from exc
    return path


def _remove_empty_cache_parents(path: Path, *, stop_dir: Path) -> None:
    stop = stop_dir.resolve(strict=False)
    current = path.resolve(strict=False)

    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _record_photo_tile_error(
    config: Config,
    *,
    media_id: int,
    output_rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
    message: str,
) -> None:
    now = time.time()
    with open_database(config.db_path, read_only=False) as connection:
        connection.execute(
            """
            INSERT INTO thumbnails (
                media_id,
                thumbnail_type,
                cache_class,
                variant_key,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                algorithm_version,
                status,
                created_at,
                updated_at,
                last_used_at,
                error_message
            )
            VALUES (?, 'photo_tile', 'dynamic', ?, ?, 1, 1, 0, ?, ?, ?, 'error', ?, ?, NULL, ?)
            ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                cache_class = excluded.cache_class,
                output_rel_path = excluded.output_rel_path,
                file_size_bytes = 0,
                source_size_bytes = excluded.source_size_bytes,
                source_modified_time = excluded.source_modified_time,
                algorithm_version = excluded.algorithm_version,
                status = 'error',
                updated_at = excluded.updated_at,
                error_message = excluded.error_message
            """,
            (
                media_id,
                PHOTO_TILE_VARIANT_KEY,
                output_rel_path,
                int(source_size_bytes),
                float(source_modified_time),
                PHOTO_TILE_ALGORITHM_VERSION,
                now,
                now,
                message[:1000],
            ),
        )
        connection.commit()




def _record_video_poster_error(
    config: Config,
    *,
    media_id: int,
    output_rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
    message: str,
) -> None:
    now = time.time()
    with open_database(config.db_path, read_only=False) as connection:
        connection.execute(
            """
            INSERT INTO thumbnails (
                media_id,
                thumbnail_type,
                cache_class,
                variant_key,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                algorithm_version,
                status,
                created_at,
                updated_at,
                last_used_at,
                error_message
            )
            VALUES (?, 'video_poster', 'protected', ?, ?, 1, 1, 0, ?, ?, ?, 'error', ?, ?, NULL, ?)
            ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                cache_class = excluded.cache_class,
                output_rel_path = excluded.output_rel_path,
                file_size_bytes = 0,
                source_size_bytes = excluded.source_size_bytes,
                source_modified_time = excluded.source_modified_time,
                algorithm_version = excluded.algorithm_version,
                status = 'error',
                updated_at = excluded.updated_at,
                error_message = excluded.error_message
            """,
            (
                media_id,
                VIDEO_POSTER_VARIANT_KEY,
                output_rel_path,
                int(source_size_bytes),
                float(source_modified_time),
                VIDEO_POSTER_ALGORITHM_VERSION,
                now,
                now,
                message[:1000],
            ),
        )
        connection.commit()


def _record_video_frame_error(
    config: Config,
    *,
    media_id: int,
    variant_key: str,
    output_rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
    message: str,
) -> None:
    now = time.time()
    with open_database(config.db_path, read_only=False) as connection:
        connection.execute(
            """
            INSERT INTO thumbnails (
                media_id,
                thumbnail_type,
                cache_class,
                variant_key,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                algorithm_version,
                status,
                created_at,
                updated_at,
                last_used_at,
                error_message
            )
            VALUES (?, 'video_frame', 'protected', ?, ?, 1, 1, 0, ?, ?, ?, 'error', ?, ?, NULL, ?)
            ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                cache_class = excluded.cache_class,
                output_rel_path = excluded.output_rel_path,
                file_size_bytes = 0,
                source_size_bytes = excluded.source_size_bytes,
                source_modified_time = excluded.source_modified_time,
                algorithm_version = excluded.algorithm_version,
                status = 'error',
                updated_at = excluded.updated_at,
                error_message = excluded.error_message
            """,
            (
                media_id,
                variant_key,
                output_rel_path,
                int(source_size_bytes),
                float(source_modified_time),
                VIDEO_FRAME_ALGORITHM_VERSION,
                now,
                now,
                message[:1000],
            ),
        )
        connection.commit()


def _record_gif_preview_error(
    config: Config,
    *,
    media_id: int,
    output_rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
    message: str,
) -> None:
    now = time.time()
    with open_database(config.db_path, read_only=False) as connection:
        connection.execute(
            """
            INSERT INTO thumbnails (
                media_id,
                thumbnail_type,
                cache_class,
                variant_key,
                output_rel_path,
                width,
                height,
                file_size_bytes,
                source_size_bytes,
                source_modified_time,
                algorithm_version,
                status,
                created_at,
                updated_at,
                last_used_at,
                error_message
            )
            VALUES (?, 'gif_preview', 'protected', ?, ?, 1, 1, 0, ?, ?, ?, 'error', ?, ?, NULL, ?)
            ON CONFLICT(media_id, thumbnail_type, variant_key) DO UPDATE SET
                cache_class = excluded.cache_class,
                output_rel_path = excluded.output_rel_path,
                file_size_bytes = 0,
                source_size_bytes = excluded.source_size_bytes,
                source_modified_time = excluded.source_modified_time,
                algorithm_version = excluded.algorithm_version,
                status = 'error',
                updated_at = excluded.updated_at,
                error_message = excluded.error_message
            """,
            (
                media_id,
                GIF_PREVIEW_VARIANT_KEY,
                output_rel_path,
                int(source_size_bytes),
                float(source_modified_time),
                GIF_PREVIEW_ALGORITHM_VERSION,
                now,
                now,
                message[:1000],
            ),
        )
        connection.commit()



def _video_poster_destination(
    config: Config,
    *,
    rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
) -> Path:
    digest = hashlib.sha256(
        "|".join(
            (
                THUMBNAIL_CACHE_VERSION,
                "video_poster",
                VIDEO_POSTER_VARIANT_KEY,
                VIDEO_POSTER_ALGORITHM_VERSION,
                rel_path,
                str(int(source_size_bytes)),
                f"{float(source_modified_time):.6f}",
            )
        ).encode("utf-8")
    ).hexdigest()

    return config.video_poster_cache_dir / digest[:2] / digest[2:4] / f"{digest}{VIDEO_POSTER_EXTENSION}"


def _video_frame_destination(
    config: Config,
    *,
    rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
    variant_key: str,
) -> Path:
    digest = hashlib.sha256(
        "|".join(
            (
                THUMBNAIL_CACHE_VERSION,
                "video_frame",
                variant_key,
                VIDEO_FRAME_ALGORITHM_VERSION,
                rel_path,
                str(int(source_size_bytes)),
                f"{float(source_modified_time):.6f}",
            )
        ).encode("utf-8")
    ).hexdigest()

    return config.video_frame_cache_dir / variant_key / digest[:2] / digest[2:4] / f"{digest}{VIDEO_FRAME_EXTENSION}"


def _probe_video_duration(ffprobe_path: str, source_path: Path, *, timeout_seconds: int) -> float | None:
    try:
        completed = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source_path),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    try:
        duration = float((completed.stdout or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return None

    if duration <= 0:
        return None
    return duration


def _video_poster_seek_time(duration: float | None) -> float:
    if duration is None:
        return 1.0
    if duration <= 1.0:
        return 0.0

    # Keep the poster separate from the first video frame thumbnail.
    # Prototype behavior: poster around 20 %, frame thumbnails later in the video.
    safe_end = max(0.5, duration - 0.5)
    return min(max(0.5, duration * 0.20), safe_end)


def _video_frame_variant_key(frame_index: int) -> str:
    if frame_index < 1 or frame_index > len(VIDEO_FRAME_VARIANT_KEYS):
        raise ThumbnailCacheError("Video frame preview index must be between 1 and 4.")
    return VIDEO_FRAME_VARIANT_KEYS[frame_index - 1]


def _video_frame_seek_time(duration: float | None, frame_index: int) -> float:
    variant_index = frame_index - 1
    if variant_index < 0 or variant_index >= len(VIDEO_FRAME_RELATIVE_POSITIONS):
        raise ThumbnailCacheError("Video frame preview index must be between 1 and 4.")

    if duration is None:
        fallback_seconds = (2.0, 3.5, 5.0, 6.5)
        return fallback_seconds[variant_index]

    if duration <= 1.0:
        return 0.0

    position = VIDEO_FRAME_RELATIVE_POSITIONS[variant_index]
    # Keep a small margin before the physical end of the file. Some containers fail
    # when seeking exactly to the tail of the video.
    safe_end = max(0.5, duration - 0.5)
    return min(max(0.5, duration * position), safe_end)


def _gif_preview_destination(
    config: Config,
    *,
    rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
) -> Path:
    digest = hashlib.sha256(
        "|".join(
            (
                THUMBNAIL_CACHE_VERSION,
                "gif_preview",
                GIF_PREVIEW_VARIANT_KEY,
                GIF_PREVIEW_ALGORITHM_VERSION,
                rel_path,
                str(int(source_size_bytes)),
                f"{float(source_modified_time):.6f}",
            )
        ).encode("utf-8")
    ).hexdigest()

    return config.gif_preview_cache_dir / digest[:2] / digest[2:4] / f"{digest}{GIF_PREVIEW_EXTENSION}"


def _photo_tile_destination(
    config: Config,
    *,
    rel_path: str,
    source_size_bytes: int,
    source_modified_time: float,
) -> Path:
    digest = hashlib.sha256(
        "|".join(
            (
                THUMBNAIL_CACHE_VERSION,
                "photo_tile",
                PHOTO_TILE_VARIANT_KEY,
                PHOTO_TILE_ALGORITHM_VERSION,
                rel_path,
                str(int(source_size_bytes)),
                f"{float(source_modified_time):.6f}",
            )
        ).encode("utf-8")
    ).hexdigest()

    return config.photo_tile_cache_dir / digest[:2] / digest[2:4] / f"{digest}{PHOTO_TILE_EXTENSION}"


def _thumbnail_filesystem_path(config: Config, output_rel_path: str) -> Path:
    try:
        path = safe_join_catalog_path(
            config.output_root,
            output_rel_path,
            allow_root=False,
        )
    except PathValidationError as exc:
        raise ThumbnailCacheError(f"Invalid thumbnail cache path in the database: {exc}") from exc

    try:
        path.relative_to(config.thumbnail_cache_dir.resolve(strict=False))
    except ValueError as exc:
        raise ThumbnailCacheError("Thumbnail cache path is outside the thumbnail cache directory.") from exc

    return path


def _output_relative_path(config: Config, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(
            config.output_root.resolve(strict=False)
        ).as_posix()
    except ValueError as exc:
        raise ThumbnailCacheError("Output thumbnail is outside output_root.") from exc


def _source_metadata_matches(
    stored_size: int,
    stored_modified_time: float,
    current_size: int,
    current_modified_time: float,
) -> bool:
    return stored_size == current_size and abs(stored_modified_time - current_modified_time) < 0.000001


def _sum_row(connection, query: str) -> dict[str, int]:
    row = connection.execute(query).fetchone()
    if row is None:
        return {"entries": 0, "size_bytes": 0}

    return {
        "entries": int(row["entries"]),
        "size_bytes": int(row["size_bytes"]),
    }

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .message_contract import BackendMessage, MessageSeverity, build_backend_message

from .config import Config
from .database import open_database
from .diagnostics import (
    diagnostic_request_active,
    diagnostic_request_sql_snapshot,
    diagnostic_set_request_detail,
)
from .folder_preview_candidates import (
    FOLDER_PREVIEW_REQUESTED_COUNT,
    FOLDER_PREVIEW_SELECTION_VARIANT,
    build_folder_preview_tree_plan,
    effective_folder_preview_rows,
    folder_preview_visual_media_count_maps,
    FolderPreviewTreePlan,
)
from .media_types import MediaType, classify_media, is_html_playable_video, normalize_extension
from .paths import PathValidationError, is_path_within, normalize_catalog_relative_path, safe_join_catalog_path, same_path
from .scan_lock import scan_lock_path
from .scan_activate import _calculate_folder_statistics
from .scan_store import active_table_counts, read_scan_status
from .scanner import IGNORED_DIRECTORY_NAMES, source_root_status
from .sorting import catalog_path_key, natural_sort_key
from .thumbnail_cache import (
    ThumbnailCacheError,
    ThumbnailResource,
    gif_preview_existing_resource,
    photo_tile_resource,
    ready_cached_thumbnail_resource,
    thumbnail_cache_cleanup_plan,
    thumbnail_cache_database_summary,
    thumbnail_cache_layout_dict,
    thumbnail_cache_protected_orphan_audit,
    thumbnail_cache_protected_orphan_cleanup_plan,
    thumbnail_cache_protected_orphan_cleanup_plan_bundle,
    thumbnail_cache_filesystem_path,
    execute_protected_thumbnail_cache_orphan_cleanup,
    video_frame_existing_resource,
    video_poster_existing_resource,
    video_poster_resource,
)
from .video_tools import video_tools_status


class ApiError(RuntimeError):
    """Raised when an API request cannot be served with a canonical UI message."""

    def __init__(
        self,
        status_code: int,
        message_object: BackendMessage,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message_object["code"])
        self.status_code = status_code
        self.message_object = message_object
        self.payload = dict(payload or {})

    @classmethod
    def from_message(
        cls,
        status_code: int,
        code: str,
        *,
        params: dict[str, Any] | None = None,
        severity: MessageSeverity = "error",
        payload: dict[str, Any] | None = None,
    ) -> "ApiError":
        return cls(
            status_code,
            build_backend_message(code, severity=severity, params=params),
            payload,
        )

    def to_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "code": self.message_object["code"],
            "message_object": self.message_object,
        }
        result.update(self.payload)
        return result


def _rename_check_message(
    entity: str,
    key: str,
    passed: bool,
    params: dict[str, Any],
) -> BackendMessage:
    status = "passed" if passed else "blocked"
    return build_backend_message(
        f"rename.{entity}.plan.check.{key}.{status}",
        severity="success" if passed else "blocker",
        params=params,
    )


def _rename_plan_status_message(
    entity: str,
    can_execute: bool,
    entity_id: int,
) -> BackendMessage:
    status = "executable" if can_execute else "blocked"
    id_key = "media_id" if entity == "media" else "folder_id"
    return build_backend_message(
        f"rename.{entity}.plan.{status}",
        severity="success" if can_execute else "blocker",
        params={id_key: entity_id},
    )


def _rename_plan_response_message(entity: str, can_execute: bool) -> BackendMessage:
    status = "executable" if can_execute else "blocked"
    return build_backend_message(
        f"rename.{entity}.plan.response.{status}",
        severity="success" if can_execute else "blocker",
    )


def _rename_execute_result_message(
    entity: str,
    executed: bool,
    entity_id: int,
) -> BackendMessage:
    result = "success" if executed else "plan_blocked"
    id_key = "media_id" if entity == "media" else "folder_id"
    return build_backend_message(
        f"rename.{entity}.execute.{result}",
        severity="success" if executed else "blocker",
        params={id_key: entity_id},
    )

def _os_winerror(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    return None


def _is_file_locked_error(exc: BaseException) -> bool:
    """Return True for the common Windows lock error during filesystem rename."""
    return isinstance(exc, PermissionError) and _os_winerror(exc) == 32


def _technical_error_detail(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"


_SOURCE_ROOT_STATUS_SEVERITIES: dict[str, MessageSeverity] = {
    "ok": "success",
    "missing": "blocker",
    "not_directory": "blocker",
    "not_readable": "blocker",
}


def _source_root_status_message(status: Any) -> BackendMessage:
    reason = str(getattr(status, "reason", "") or "error")
    severity: MessageSeverity = (
        "error" if reason == "error" else _SOURCE_ROOT_STATUS_SEVERITIES.get(reason, "error")
    )
    return build_backend_message(
        f"source_root.status.{reason}",
        severity=severity,
        params={
            "path": str(getattr(status, "path", "") or ""),
            "reason": reason,
        },
    )


def _folder_filesystem_message(
    reason: str,
    rel_path: str,
    *,
    detail: str = "",
) -> BackendMessage:
    if reason == "ok":
        severity: MessageSeverity = "success"
    elif reason == "error":
        severity = "error"
    else:
        severity = "blocker"
    return build_backend_message(
        f"folder.filesystem.{reason}",
        severity=severity,
        params={"path": rel_path or "/", "detail": detail},
    )


def _source_root_filesystem_check_message(
    key: str,
    passed: bool,
    params: dict[str, Any],
) -> BackendMessage:
    status = "passed" if passed else "blocked"
    return build_backend_message(
        f"source_root.verify.filesystem.check.{key}.{status}",
        severity="success" if passed else "blocker",
        params=params,
    )


def _source_root_filesystem_result_message(
    reason: str,
    passed: bool,
    params: dict[str, Any],
) -> BackendMessage:
    severity_by_reason: dict[str, MessageSeverity] = {
        "passed": "success",
        "same_as_output": "blocker",
        "data_inside_output": "blocker",
        "output_inside_data": "blocker",
        "missing": "blocker",
        "not_dir": "blocker",
        "not_readable": "blocker",
        "failed": "blocker",
    }
    severity: MessageSeverity = (
        "success"
        if passed
        else "error"
        if reason == "error"
        else severity_by_reason.get(reason, "blocker")
    )
    return build_backend_message(
        f"source_root.verify.filesystem.{reason}",
        severity=severity,
        params=params,
    )


def _source_root_match_result_message(
    reason: str,
    passed: bool,
    params: dict[str, Any],
) -> BackendMessage:
    severity_by_reason: dict[str, MessageSeverity] = {
        "not_checked": "info",
        "database_missing": "info",
        "no_references": "info",
        "passed": "success",
        "low_match": "blocker",
    }
    severity = severity_by_reason.get(reason, "info")
    if not passed and severity == "info":
        severity = "blocker"
    return build_backend_message(
        f"source_root.verify.match.{reason}",
        severity=severity,
        params=params,
    )


def _source_root_verify_result_message(
    reason: str,
    can_save: bool,
    params: dict[str, Any],
) -> BackendMessage:
    severity_by_reason: dict[str, MessageSeverity] = {
        "can_save": "success",
        "blocked_filesystem": "blocker",
        "blocked_match": "blocker",
    }
    return build_backend_message(
        f"source_root.verify.result.{reason}",
        severity="success" if can_save else severity_by_reason.get(reason, "blocker"),
        params=params,
    )


@dataclass(frozen=True)
class MediaPageParams:
    folder_rel_path: str
    media_type: str
    page: int
    page_size: int
    offset: int


@dataclass(frozen=True)
class FolderPageParams:
    parent_rel_path: str
    page: int
    page_size: int
    offset: int


@dataclass(frozen=True)
class OriginalMediaResource:
    rel_path: str
    file_name: str
    extension: str
    media_type: str
    filesystem_path: Path
    size_bytes: int
    modified_time: float


@dataclass(frozen=True)
class FolderOpenResource:
    rel_path: str
    filesystem_path: Path


@dataclass(frozen=True)
class FavoritePageParams:
    media_type: str
    page: int
    page_size: int
    offset: int


@dataclass(frozen=True)
class SearchPageParams:
    query: str
    query_pattern: str
    folder_rel_path: str
    content_filter: str
    media_type: str | None
    page: int
    page_size: int
    offset: int



def protected_cache_orphan_audit_status(config: Config) -> dict[str, Any]:
    """Return a read-only protected cache orphan audit for Settings."""
    audit = thumbnail_cache_protected_orphan_audit(config)
    return {
        "ok": True,
        "protected_orphan_audit": audit,
        "writes": audit.get("writes", {}),
    }


def protected_cache_orphan_cleanup_plan_status(config: Config) -> dict[str, Any]:
    """Return a read-only protected cache orphan cleanup plan for Settings."""
    plan = thumbnail_cache_protected_orphan_cleanup_plan(config)
    return {
        "ok": True,
        "protected_orphan_cleanup_plan": plan,
        "writes": plan.get("writes", {}),
    }


def protected_cache_orphan_cleanup_plan_status_with_candidate_plan(
    config: Config,
) -> tuple[dict[str, Any], dict[str, object]]:
    """Return the public protected cleanup plan and its executable candidates."""
    plan, candidate_plan = thumbnail_cache_protected_orphan_cleanup_plan_bundle(config)
    return (
        {
            "ok": True,
            "protected_orphan_cleanup_plan": plan,
            "writes": plan.get("writes", {}),
        },
        candidate_plan,
    )


def protected_cache_orphan_cleanup_execute_action(
    config: Config,
    *,
    candidate_plan: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Execute confirmed protected cache orphan cleanup for Settings."""
    result = execute_protected_thumbnail_cache_orphan_cleanup(
        config,
        candidate_plan=candidate_plan,
    )
    return {
        "ok": bool(result.get("ok", False)),
        "protected_orphan_cleanup_execute": result,
        "writes": result.get("writes", {}),
    }

def thumbnail_cache_status(config: Config) -> dict[str, Any]:
    """Return read-only cache and effective settings status.

    Step 8.5e remains non-destructive: the endpoint does not create,
    delete, rebuild or rewrite cache files, DB rows or runtime settings.
    """
    layout = thumbnail_cache_layout_dict(config)

    with open_database(config.db_path, read_only=True) as connection:
        database = thumbnail_cache_database_summary(
            connection,
            limit_bytes=int(layout["limit_bytes"]),
        )
        cleanup_plan = thumbnail_cache_cleanup_plan(
            connection,
            limit_bytes=int(layout["limit_bytes"]),
        )

    return {
        "ok": True,
        "thumbnail_cache": layout,
        "database": database,
        "cleanup_plan": cleanup_plan,
        "settings": {
            "mode": "read_only_status",
            "runtime_settings_enabled": config.runtime_settings_active,
            "runtime_settings_path": str(config.settings_json),
            "runtime_settings_exists": config.settings_json.exists(),
            "active_settings_source": "settings.json" if config.runtime_settings_active else "config.json",
            "thumbnail_cache_limit_gb": config.thumbnail_cache_limit_gb,
            "thumbnail_cache_limit_source": config.thumbnail_cache_limit_source,
            "config_path": str(config.config_path),
            "config_version": config.config_version,
            "source_root": source_root_settings_status(config),
            "page_sizes": {
                "all_page_size": config.all_page_size,
                "photo_page_size": config.photo_page_size,
                "video_page_size": config.video_page_size,
                "gif_page_size": config.gif_page_size,
                "other_page_size": config.other_page_size,
                "folder_page_size": config.folder_page_size,
            },
            "page_size_sources": dict(config.page_size_sources),
            "gallery_density": config.gallery_density,
            "gallery_density_source": config.gallery_density_source,
            "thumbnail_sizes": {
                "image_thumb_size": list(config.image_thumb_size),
                "gif_thumb_size": list(config.gif_thumb_size),
                "video_preview_width": config.video_preview_width,
            },
            "video": {
                "ffmpeg_timeout_seconds": config.ffmpeg_timeout_seconds,
                "ffmpeg_threads_per_job": config.ffmpeg_threads_per_job,
            },
            "thumbnail_video_param_sources": dict(config.thumbnail_video_param_sources),
            "ui_locale": config.ui_locale,
            "ui_locale_source": config.ui_locale_source,
            "ui_theme": config.ui_theme,
            "ui_theme_source": config.ui_theme_source,
            "catalog_title": config.catalog_title,
            "catalog_title_source": config.catalog_title_source,
        },
    }


def runtime_ui_locale_status(config: Config) -> dict[str, Any]:
    """Return the currently effective frontend UI preferences without touching the database."""
    return {
        "ok": True,
        "ui_locale": config.ui_locale,
        "ui_locale_source": config.ui_locale_source,
        "ui_theme": config.ui_theme,
        "ui_theme_source": config.ui_theme_source,
        "catalog_title": config.catalog_title,
        "catalog_title_source": config.catalog_title_source,
        "gallery_density": config.gallery_density,
        "gallery_density_source": config.gallery_density_source,
        "supported_locales": ["en", "cs"],
        "supported_themes": ["original", "serious-light", "serious-dark", "vivid-content", "dark-cinema"],
        "supported_gallery_densities": ["compact", "comfortable", "large", "extra-large"],
        "settings": {
            "runtime_settings_enabled": config.runtime_settings_active,
            "runtime_settings_path": str(config.settings_json),
            "runtime_settings_exists": config.settings_json.exists(),
            "config_path": str(config.config_path),
        },
        "writes": {
            "settings_json": False,
            "config_json": False,
            "catalog_db": False,
            "cache": False,
            "source_data": False,
        },
    }


def source_root_settings_status(config: Config) -> dict[str, Any]:
    """Return read-only status of effective data_root for Settings UI."""
    status = source_root_status(config)
    status_message = _source_root_status_message(status)
    return {
        "data_root": str(config.data_root),
        "data_root_source": getattr(config, "data_root_source", "config.json:data_root"),
        "available": status.available,
        "exists": status.exists,
        "is_dir": status.is_dir,
        "readable": status.readable,
        "reason": status.reason,
        "status_message": status_message,
        "result_messages": [status_message],
    }


def source_root_setting_verify(config: Config, raw_data_root: Any) -> dict[str, Any]:
    """Verify a proposed runtime data_root without saving it.

    This is intended as a guard for fixing the path to the same source archive,
    not as automatic path repair or switching the catalog to a different dataset.
    """
    candidate_text = str(raw_data_root or "").strip()
    if not candidate_text:
        raise ApiError.from_message(
            400,
            "settings.source_root.path_required",
            params={},
        )

    candidate = Path(candidate_text).expanduser()
    filesystem = _verify_source_root_filesystem(candidate, config.output_root)
    match_check = _source_root_match_check(config, candidate) if filesystem["passed"] else _empty_source_root_match_check()
    can_save = bool(filesystem["passed"] and match_check["passed"])

    if can_save:
        result_reason = "can_save"
    elif not filesystem["passed"]:
        result_reason = "blocked_filesystem"
    else:
        result_reason = "blocked_match"

    result_message = _source_root_verify_result_message(
        result_reason,
        can_save,
        {
            "request_path": candidate_text,
            "candidate_path": str(candidate),
        },
    )

    return {
        "ok": True,
        "action": "source-root-verified",
        "request_path": candidate_text,
        "candidate_path": str(candidate),
        "can_save": can_save,
        "result_reason": result_reason,
        "result_messages": [result_message],
        "filesystem": filesystem,
        "match_check": match_check,
    }


def _verify_source_root_filesystem(candidate: Path, output_root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    check_messages: list[BackendMessage] = []

    def add_check(key: str, passed: bool) -> None:
        params = {"path": str(candidate), "output_root": str(output_root)}
        check_message = _source_root_filesystem_check_message(key, bool(passed), params)
        checks.append({
            "key": key,
            "passed": bool(passed),
        })
        check_messages.append(check_message)

    try:
        exists = candidate.exists()
        is_dir = candidate.is_dir() if exists else False
        readable = False

        if exists and is_dir:
            try:
                with os.scandir(candidate):
                    pass
                readable = True
            except OSError:
                readable = False

        same_as_output = same_path(candidate, output_root)
        data_inside_output = is_path_within(candidate, output_root) and not same_as_output
        output_inside_data = is_path_within(output_root, candidate) and not same_as_output

        add_check("exists", exists)
        add_check("is_dir", is_dir)
        add_check("readable", readable)
        add_check("not_output_root", not same_as_output)
        add_check("not_inside_output_root", not data_inside_output)
        add_check("output_not_inside_data_root", not output_inside_data)

        passed = all(check["passed"] for check in checks)
        if passed:
            result_reason = "passed"
        elif same_as_output:
            result_reason = "same_as_output"
        elif data_inside_output:
            result_reason = "data_inside_output"
        elif output_inside_data:
            result_reason = "output_inside_data"
        elif not exists:
            result_reason = "missing"
        elif not is_dir:
            result_reason = "not_dir"
        elif not readable:
            result_reason = "not_readable"
        else:
            result_reason = "failed"

        result_message = _source_root_filesystem_result_message(
            result_reason,
            passed,
            {"path": str(candidate), "output_root": str(output_root)},
        )

        return {
            "passed": passed,
            "reason": result_reason,
            "path": str(candidate),
            "resolved_path": str(candidate.resolve(strict=False)),
            "checks": checks,
            "result_messages": [result_message],
            "check_messages": check_messages,
        }
    except OSError as exc:
        result_message = _source_root_filesystem_result_message(
            "error",
            False,
            {"path": str(candidate), "output_root": str(output_root)},
        )
        return {
            "passed": False,
            "reason": "error",
            "path": str(candidate),
            "resolved_path": str(candidate),
            "checks": checks,
            "result_messages": [result_message],
            "check_messages": check_messages,
            "technical_detail": str(exc),
        }


def _source_root_match_response(
    *,
    passed: bool,
    reference_type: str,
    sample_count: int,
    matched_count: int,
    match_ratio: float,
    threshold: float,
    sample: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    result_message = _source_root_match_result_message(
        reason,
        passed,
        {
            "reference_type": reference_type,
            "sample": sample_count,
            "matched": matched_count,
            "match_ratio": round(match_ratio, 4),
            "threshold": threshold,
            "threshold_percent": round(threshold * 100),
        },
    )
    return {
        "passed": passed,
        "reason": reason,
        "reference_type": reference_type,
        "sample_count": sample_count,
        "matched_count": matched_count,
        "match_ratio": match_ratio,
        "threshold": threshold,
        "result_messages": [result_message],
        "sample": sample,
    }


def _empty_source_root_match_check() -> dict[str, Any]:
    return _source_root_match_response(
        passed=False,
        reference_type="none",
        sample_count=0,
        matched_count=0,
        match_ratio=0,
        threshold=0.60,
        sample=[],
        reason="not_checked",
    )


def _source_root_match_check(config: Config, candidate: Path) -> dict[str, Any]:
    threshold = 0.60

    if not config.db_path.exists():
        return _source_root_match_response(
            passed=True,
            reference_type="none",
            sample_count=0,
            matched_count=0,
            match_ratio=1,
            threshold=threshold,
            sample=[],
            reason="database_missing",
        )

    try:
        with open_database(config.db_path, read_only=True) as connection:
            references = _source_root_media_references(connection)
            reference_type = "media"
            if not references:
                references = _source_root_folder_references(connection)
                reference_type = "folder"
    except sqlite3.Error as exc:
        raise ApiError.from_message(
            500,
            "source_root.verify.match.database_error",
            params={},
            payload={"technical_detail": str(exc)},
        ) from exc

    if not references:
        return _source_root_match_response(
            passed=True,
            reference_type="none",
            sample_count=0,
            matched_count=0,
            match_ratio=1,
            threshold=threshold,
            sample=[],
            reason="no_references",
        )

    matched = 0
    sample: list[dict[str, Any]] = []

    for rel_path in references:
        exists = _source_root_reference_exists(candidate, rel_path, reference_type)
        if exists:
            matched += 1
        if len(sample) < 12:
            sample.append({"rel_path": rel_path, "exists": exists})

    sample_count = len(references)
    ratio = matched / sample_count if sample_count else 1
    passed = ratio >= threshold
    reason = "passed" if passed else "low_match"

    return _source_root_match_response(
        passed=passed,
        reference_type=reference_type,
        sample_count=sample_count,
        matched_count=matched,
        match_ratio=ratio,
        threshold=threshold,
        sample=sample,
        reason=reason,
    )


def _source_root_media_references(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT rel_path
        FROM media_files
        WHERE is_available = 1
        ORDER BY id
        LIMIT 1000
        """
    ).fetchall()
    return [str(row["rel_path"] or "") for row in rows if str(row["rel_path"] or "")]


def _source_root_folder_references(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT rel_path
        FROM folders
        WHERE is_available = 1 AND rel_path <> ''
        ORDER BY id
        LIMIT 1000
        """
    ).fetchall()
    return [str(row["rel_path"] or "") for row in rows if str(row["rel_path"] or "")]


def _source_root_reference_exists(candidate: Path, rel_path: str, reference_type: str) -> bool:
    parts = PurePosixPath(rel_path).parts
    target = candidate.joinpath(*parts) if parts else candidate
    try:
        if reference_type == "folder":
            return target.is_dir()
        return target.is_file()
    except OSError:
        return False



WINDOWS_RESERVED_FILE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*')


def safe_media_rename_plan_page(
    config: Config,
    raw_media_id: Any,
    raw_new_name: Any,
) -> dict[str, Any]:
    """Return a read-only plan for renaming one media file.

    Step 8.7b intentionally does not write to the filesystem, catalog.db,
    favorites.json, thumbnail cache or scan state. The returned plan is the
    single source of truth for a later execute endpoint.
    """
    media_id = _positive_int(str(raw_media_id or "").strip(), field_name="media_id")
    new_name = str(raw_new_name or "").strip()

    with open_database(config.db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT
                mf.id,
                mf.rel_path,
                mf.path_key,
                mf.folder_id,
                mf.file_name,
                mf.extension,
                mf.media_type,
                mf.size_bytes,
                mf.modified_time,
                mf.last_successful_scan_id,
                mf.is_available,
                f.rel_path AS folder_rel_path,
                f.name AS folder_name
            FROM media_files AS mf
            JOIN folders AS f ON f.id = mf.folder_id
            WHERE mf.id = ?
            """,
            (media_id,),
        ).fetchone()

        if row is None:
            raise ApiError.from_message(
                404,
                "rename.media.not_found",
                params={"media_id": media_id},
            )

        plan = _build_safe_media_rename_plan(
            config=config,
            connection=connection,
            media=row,
            new_name=new_name,
        )

    response_message = _rename_plan_response_message("media", bool(plan.get("can_execute")))

    return {
        "ok": True,
        "action": "safe-media-rename-plan",
        "read_only": True,
        "plan": plan,
        "result_messages": [response_message],
    }



def safe_folder_rename_plan_page(
    config: Config,
    raw_folder_id: Any,
    raw_new_name: Any,
) -> dict[str, Any]:
    """Return a read-only plan for renaming one folder branch.

    Step 8.8b intentionally does not write to the filesystem, catalog.db,
    favorites.json, thumbnail cache or scan state. The plan is the single
    source of truth for a later execute endpoint.
    """
    folder_id = _positive_int(str(raw_folder_id or "").strip(), field_name="folder_id")
    new_name = str(raw_new_name or "").strip()

    with open_database(config.db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                rel_path,
                path_key,
                parent_id,
                name,
                depth,
                sort_key,
                last_successful_scan_id,
                is_available
            FROM folders
            WHERE id = ?
            """,
            (folder_id,),
        ).fetchone()

        if row is None:
            raise ApiError.from_message(
                404,
                "rename.folder.not_found",
                params={"folder_id": folder_id},
            )

        plan = _build_safe_folder_rename_plan(
            config=config,
            connection=connection,
            folder=row,
            new_name=new_name,
        )

    response_message = _rename_plan_response_message("folder", bool(plan.get("can_execute")))

    return {
        "ok": True,
        "action": "safe-folder-rename-plan",
        "read_only": True,
        "plan": plan,
        "result_messages": [response_message],
    }


def safe_folder_rename_execute_action(
    config: Config,
    raw_folder_id: Any,
    raw_new_name: Any,
) -> dict[str, Any]:
    """Execute one safe folder rename using the shared folder rename plan.

    Step 8.8c intentionally supports only one folder branch, same parent
    folder and no merge. The function rebuilds the same plan used by the
    read-only endpoint, then applies only that plan. On failures after the
    filesystem rename it attempts to roll the folder and favorites.json back.
    """
    folder_id = _positive_int(str(raw_folder_id or "").strip(), field_name="folder_id")
    new_name = str(raw_new_name or "").strip()

    original_favorites = _read_favorite_entries(config)
    favorites_written = False
    filesystem_renamed = False
    rollback: dict[str, Any] = {
        "attempted": False,
        "filesystem_restored": None,
        "favorites_restored": None,
        "messages": [],
    }

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")

            row = connection.execute(
                """
                SELECT
                    id,
                    rel_path,
                    path_key,
                    parent_id,
                    name,
                    depth,
                    sort_key,
                    last_successful_scan_id,
                    is_available
                FROM folders
                WHERE id = ?
                """,
                (folder_id,),
            ).fetchone()

            if row is None:
                raise ApiError.from_message(
                404,
                "rename.folder.not_found",
                params={"folder_id": folder_id},
            )

            plan = _build_safe_folder_rename_plan(
                config=config,
                connection=connection,
                folder=row,
                new_name=new_name,
            )

            if not plan.get("can_execute"):
                connection.rollback()
                result_message = _rename_execute_result_message("folder", False, folder_id)
                return {
                    "ok": True,
                    "action": "safe-folder-rename-execute",
                    "executed": False,
                    "plan": plan,
                    "writes": {
                        "source_data_rename": False,
                        "catalog_db_folders": False,
                        "catalog_db_media_files": False,
                        "favorites_json": False,
                        "thumbnail_cache": False,
                        "folder_preview_items": False,
                        "video_previews": False,
                        "scan_data": False,
                        "config_json": False,
                        "settings_json": False,
                    },
                    "rollback": rollback,
                    "result_messages": [result_message],
                }

            old_path = Path(str(plan["old"]["filesystem_path"]))
            new_path = Path(str(plan["new"]["filesystem_path"]))
            old_rel_path = str(plan["old"]["rel_path"])
            old_path_key = str(plan["old"]["path_key"])
            new_rel_path = str(plan["new"]["rel_path"])
            new_folder_name = str(plan["new"]["name"])

            old_prefix = old_path_key + "/"
            old_rel_prefix = old_rel_path + "/"
            new_rel_prefix = new_rel_path + "/"

            folder_rows = connection.execute(
                """
                SELECT id, rel_path, path_key, name
                FROM folders
                WHERE path_key = ? OR substr(path_key, 1, ?) = ?
                ORDER BY depth, rel_path
                """,
                (old_path_key, len(old_prefix), old_prefix),
            ).fetchall()
            media_rows = connection.execute(
                """
                SELECT id, rel_path, path_key, file_name
                FROM media_files
                WHERE substr(path_key, 1, ?) = ?
                ORDER BY rel_path
                """,
                (len(old_prefix), old_prefix),
            ).fetchall()

            old_path.rename(new_path)
            filesystem_renamed = True

            for folder_row in folder_rows:
                current_rel_path = str(folder_row["rel_path"] or "")
                current_name = str(folder_row["name"] or "")
                if current_rel_path == old_rel_path:
                    next_rel_path = new_rel_path
                    next_name = new_folder_name
                elif current_rel_path.startswith(old_rel_prefix):
                    next_rel_path = new_rel_prefix + current_rel_path[len(old_rel_prefix):]
                    next_name = current_name
                else:
                    raise ApiError.from_message(
                        500,
                        "rename.folder.execute.folder_outside_branch",
                        params={"path": current_rel_path},
                    )

                connection.execute(
                    """
                    UPDATE folders
                    SET rel_path = ?,
                        path_key = ?,
                        name = ?,
                        sort_key = ?
                    WHERE id = ?
                    """,
                    (
                        next_rel_path,
                        catalog_path_key(next_rel_path),
                        next_name,
                        natural_sort_key(next_name),
                        int(folder_row["id"]),
                    ),
                )

            for media_row in media_rows:
                current_rel_path = str(media_row["rel_path"] or "")
                if not current_rel_path.startswith(old_rel_prefix):
                    raise ApiError.from_message(
                        500,
                        "rename.folder.execute.media_outside_branch",
                        params={"path": current_rel_path},
                    )
                next_rel_path = new_rel_prefix + current_rel_path[len(old_rel_prefix):]
                connection.execute(
                    """
                    UPDATE media_files
                    SET rel_path = ?,
                        path_key = ?
                    WHERE id = ?
                    """,
                    (
                        next_rel_path,
                        catalog_path_key(next_rel_path),
                        int(media_row["id"]),
                    ),
                )

            updated_favorites = _favorite_entries_with_folder_branch_renamed(
                original_favorites,
                old_rel_path=old_rel_path,
                old_path_key=old_path_key,
                new_rel_path=new_rel_path,
            )
            favorites_changed = updated_favorites != original_favorites
            if favorites_changed:
                _write_favorite_entries(config, updated_favorites)
                favorites_written = True

            connection.commit()

        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass

            if filesystem_renamed:
                rollback["attempted"] = True
                try:
                    if new_path.exists() and not old_path.exists():
                        new_path.rename(old_path)
                        rollback["filesystem_restored"] = True
                        rollback["messages"].append("Složka byla vrácena na původní název.")
                    else:
                        rollback["filesystem_restored"] = False
                        rollback["messages"].append("Složku nebylo možné jednoznačně vrátit na původní název.")
                except OSError as rollback_exc:
                    rollback["filesystem_restored"] = False
                    rollback["messages"].append(f"Rollback složky selhal: {rollback_exc}")

            if favorites_written:
                rollback["attempted"] = True
                try:
                    _write_favorite_entries(config, original_favorites)
                    rollback["favorites_restored"] = True
                    rollback["messages"].append("favorites.json byl vrácen do původního stavu.")
                except Exception as rollback_exc:
                    rollback["favorites_restored"] = False
                    rollback["messages"].append(f"Rollback favorites.json selhal: {rollback_exc}")

            if isinstance(exc, ApiError) and not filesystem_renamed and not favorites_written:
                raise

            if _is_file_locked_error(exc) and not filesystem_renamed and not favorites_written:
                raise ApiError.from_message(
                    409,
                    "rename.folder.execute.file_locked",
                    params={"path": str(locals().get("old_rel_path", ""))},
                    payload={
                        "technical_detail": _technical_error_detail(exc),
                        "rollback": rollback,
                    },
                ) from exc

            raise ApiError.from_message(
                500,
                "rename.folder.execute.failed",
                params={"folder_id": folder_id},
                payload={
                    "technical_detail": _technical_error_detail(exc),
                    "rollback": rollback,
                },
            ) from exc

    result_message = _rename_execute_result_message("folder", True, folder_id)

    return {
        "ok": True,
        "action": "safe-folder-rename-execute",
        "executed": True,
        "folder_id": folder_id,
        "old_path": old_rel_path,
        "new_path": new_rel_path,
        "plan": plan,
        "writes": {
            "source_data_rename": True,
            "catalog_db_folders": bool(folder_rows),
            "catalog_db_media_files": bool(media_rows),
            "favorites_json": favorites_changed,
            "thumbnail_cache": False,
            "folder_preview_items": False,
            "video_previews": False,
            "scan_data": False,
            "config_json": False,
            "settings_json": False,
        },
        "rollback": rollback,
        "result_messages": [result_message],
    }


def safe_media_rename_execute_action(
    config: Config,
    raw_media_id: Any,
    raw_new_name: Any,
) -> dict[str, Any]:
    """Execute one safe media rename using the shared rename plan.

    Step 8.7c intentionally supports only one file, same folder and same
    extension. The function rebuilds the same plan used by the read-only
    endpoint, then applies only that plan. On failures after the filesystem
    rename it attempts to roll the source file and favorites.json back.
    """
    media_id = _positive_int(str(raw_media_id or "").strip(), field_name="media_id")
    new_name = str(raw_new_name or "").strip()

    original_favorites = _read_favorite_entries(config)
    favorites_written = False
    filesystem_renamed = False
    rollback: dict[str, Any] = {
        "attempted": False,
        "filesystem_restored": None,
        "favorites_restored": None,
        "messages": [],
    }

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")

            row = connection.execute(
                """
                SELECT
                    mf.id,
                    mf.rel_path,
                    mf.path_key,
                    mf.folder_id,
                    mf.file_name,
                    mf.extension,
                    mf.media_type,
                    mf.size_bytes,
                    mf.modified_time,
                    mf.last_successful_scan_id,
                    mf.is_available,
                    f.rel_path AS folder_rel_path,
                    f.name AS folder_name
                FROM media_files AS mf
                JOIN folders AS f ON f.id = mf.folder_id
                WHERE mf.id = ?
                """,
                (media_id,),
            ).fetchone()

            if row is None:
                raise ApiError.from_message(
                404,
                "rename.media.not_found",
                params={"media_id": media_id},
            )

            plan = _build_safe_media_rename_plan(
                config=config,
                connection=connection,
                media=row,
                new_name=new_name,
            )

            if not plan.get("can_execute"):
                connection.rollback()
                result_message = _rename_execute_result_message("media", False, media_id)
                return {
                    "ok": True,
                    "action": "safe-media-rename-execute",
                    "executed": False,
                    "plan": plan,
                    "writes": {
                        "source_data_rename": False,
                        "catalog_db_media_files": False,
                        "favorites_json": False,
                        "thumbnail_cache": False,
                        "folder_preview_items": False,
                        "video_previews": False,
                        "scan_data": False,
                        "config_json": False,
                        "settings_json": False,
                    },
                    "rollback": rollback,
                    "result_messages": [result_message],
                }

            old_path = Path(str(plan["old"]["filesystem_path"]))
            new_path = Path(str(plan["new"]["filesystem_path"]))
            old_rel_path = str(plan["old"]["rel_path"])
            old_path_key = str(plan["old"]["path_key"])
            new_rel_path = str(plan["new"]["rel_path"])
            new_path_key = str(plan["new"]["path_key"])
            new_file_name = str(plan["new"]["file_name"])

            old_path.rename(new_path)
            filesystem_renamed = True

            connection.execute(
                """
                UPDATE media_files
                SET rel_path = ?,
                    path_key = ?,
                    file_name = ?,
                    sort_key = ?
                WHERE id = ?
                """,
                (
                    new_rel_path,
                    new_path_key,
                    new_file_name,
                    natural_sort_key(new_file_name),
                    media_id,
                ),
            )

            updated_favorites = _favorite_entries_with_media_path_renamed(
                original_favorites,
                old_path_key=old_path_key,
                new_rel_path=new_rel_path,
            )
            favorites_changed = updated_favorites != original_favorites
            if favorites_changed:
                _write_favorite_entries(config, updated_favorites)
                favorites_written = True

            connection.commit()

        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass

            if filesystem_renamed:
                rollback["attempted"] = True
                try:
                    if new_path.exists() and not old_path.exists():
                        new_path.rename(old_path)
                        rollback["filesystem_restored"] = True
                        rollback["messages"].append("Soubor byl vrácen na původní název.")
                    else:
                        rollback["filesystem_restored"] = False
                        rollback["messages"].append("Soubor nebylo možné jednoznačně vrátit na původní název.")
                except OSError as rollback_exc:
                    rollback["filesystem_restored"] = False
                    rollback["messages"].append(f"Rollback souboru selhal: {rollback_exc}")

            if favorites_written:
                rollback["attempted"] = True
                try:
                    _write_favorite_entries(config, original_favorites)
                    rollback["favorites_restored"] = True
                    rollback["messages"].append("favorites.json byl vrácen do původního stavu.")
                except Exception as rollback_exc:
                    rollback["favorites_restored"] = False
                    rollback["messages"].append(f"Rollback favorites.json selhal: {rollback_exc}")

            if isinstance(exc, ApiError) and not filesystem_renamed and not favorites_written:
                raise

            if _is_file_locked_error(exc) and not filesystem_renamed and not favorites_written:
                raise ApiError.from_message(
                    409,
                    "rename.media.execute.file_locked",
                    params={"path": str(locals().get("old_rel_path", ""))},
                    payload={
                        "technical_detail": _technical_error_detail(exc),
                        "rollback": rollback,
                    },
                ) from exc

            raise ApiError.from_message(
                500,
                "rename.media.execute.failed",
                params={"media_id": media_id},
                payload={
                    "technical_detail": _technical_error_detail(exc),
                    "rollback": rollback,
                },
            ) from exc

    result_message = _rename_execute_result_message("media", True, media_id)

    return {
        "ok": True,
        "action": "safe-media-rename-execute",
        "executed": True,
        "media_id": media_id,
        "old_path": old_rel_path,
        "new_path": new_rel_path,
        "plan": plan,
        "writes": {
            "source_data_rename": True,
            "catalog_db_media_files": True,
            "favorites_json": favorites_changed,
            "thumbnail_cache": False,
            "folder_preview_items": False,
            "video_previews": False,
            "scan_data": False,
            "config_json": False,
            "settings_json": False,
        },
        "rollback": rollback,
        "result_messages": [result_message],
    }


def _build_safe_media_rename_plan(
    *,
    config: Config,
    connection: sqlite3.Connection,
    media: sqlite3.Row,
    new_name: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blocker_messages: list[BackendMessage] = []
    warning_messages: list[BackendMessage] = []

    media_id = int(media["id"])
    old_rel_path = str(media["rel_path"])
    old_path_key = str(media["path_key"])
    old_file_name = str(media["file_name"])
    old_extension = normalize_extension(str(media["extension"]))
    folder_rel_path = str(media["folder_rel_path"] or "")

    def add_check(key: str, passed: bool) -> None:
        check_passed = bool(passed)
        message_obj = _rename_check_message(
            "media",
            key,
            check_passed,
            {
                "media_id": media_id,
                "old_rel_path": old_rel_path,
                "new_name": new_name,
            },
        )
        checks.append({"key": key, "passed": check_passed, "message_object": message_obj})
        if not check_passed:
            blocker_messages.append(message_obj)

    source_status = source_root_status(config)
    add_check("source_root_available", source_status.available)

    add_check("media_available", int(media["is_available"]) == 1)

    filename_validation = _validate_safe_media_rename_filename(new_name)
    for item in filename_validation["checks"]:
        add_check(str(item["key"]), bool(item["passed"]))

    new_extension = normalize_extension(new_name) if filename_validation["basic_name_valid"] else ""
    same_extension = bool(new_extension and new_extension == old_extension)
    add_check("same_extension", same_extension)

    try:
        old_filesystem_path = safe_join_catalog_path(
            config.data_root,
            old_rel_path,
            allow_root=False,
        )
    except PathValidationError as exc:
        old_filesystem_path = config.data_root / old_rel_path
        add_check("old_path_inside_data_root", False)
    else:
        add_check("old_path_inside_data_root", True)

    if folder_rel_path:
        new_rel_path = f"{folder_rel_path}/{new_name}" if new_name else folder_rel_path + "/"
    else:
        new_rel_path = new_name

    try:
        normalized_new_rel_path = normalize_catalog_relative_path(new_rel_path, allow_root=False)
    except PathValidationError as exc:
        normalized_new_rel_path = new_rel_path
        add_check("new_path_valid", False)
    else:
        add_check("new_path_valid", True)

    try:
        new_filesystem_path = safe_join_catalog_path(
            config.data_root,
            normalized_new_rel_path,
            allow_root=False,
        )
    except PathValidationError as exc:
        new_filesystem_path = config.data_root / normalized_new_rel_path
        add_check("new_path_inside_data_root", False)
    else:
        add_check("new_path_inside_data_root", True)

    new_path_key = catalog_path_key(normalized_new_rel_path)
    new_parent_rel_path = _parent_rel_path_from_rel_path(normalized_new_rel_path)
    same_folder = new_parent_rel_path == folder_rel_path
    add_check("same_folder", same_folder)

    same_exact_path = normalized_new_rel_path == old_rel_path
    same_catalog_key = new_path_key == old_path_key
    case_only_rename = same_catalog_key and not same_exact_path

    add_check("new_name_differs", not same_exact_path)
    add_check("not_case_only_rename", not case_only_rename)

    source_exists = False
    source_is_file = False
    target_exists = False
    target_is_same_as_source = False

    try:
        source_exists = old_filesystem_path.exists()
        source_is_file = old_filesystem_path.is_file() if source_exists else False
    except OSError:
        source_exists = False
        source_is_file = False

    try:
        target_exists = new_filesystem_path.exists()
    except OSError:
        target_exists = True

    try:
        target_is_same_as_source = same_path(old_filesystem_path, new_filesystem_path)
    except OSError:
        target_is_same_as_source = False

    add_check("source_file_exists", source_exists)
    add_check("source_is_file", source_is_file)
    add_check("target_file_absent", not target_exists or target_is_same_as_source)

    database_collision = connection.execute(
        """
        SELECT id, rel_path
        FROM media_files
        WHERE path_key = ?
          AND id <> ?
        """,
        (new_path_key, media_id),
    ).fetchone()
    add_check("no_catalog_collision", database_collision is None)

    favorite_entries = _read_favorite_entries(config)
    favorite_matches = [entry for entry in favorite_entries if catalog_path_key(entry["path"]) == old_path_key]
    thumbnail_count = _count(connection, "SELECT COUNT(*) FROM thumbnails WHERE media_id = ?", (media_id,))
    video_preview_count = _count(connection, "SELECT COUNT(*) FROM video_previews WHERE media_id = ?", (media_id,))
    folder_preview_rows = connection.execute(
        """
        SELECT
            fpi.folder_id,
            f.rel_path AS folder_rel_path,
            fpi.selection_type,
            fpi.position
        FROM folder_preview_items AS fpi
        JOIN folders AS f ON f.id = fpi.folder_id
        WHERE fpi.media_id = ?
        ORDER BY f.rel_path, fpi.selection_type, fpi.position
        LIMIT 20
        """,
        (media_id,),
    ).fetchall()
    folder_preview_count = _count(
        connection,
        "SELECT COUNT(*) FROM folder_preview_items WHERE media_id = ?",
        (media_id,),
    )

    can_execute = not blocker_messages

    def add_warning(code: str, params: dict[str, Any]) -> None:
        warning_messages.append(
            build_backend_message(code, severity="warning", params=params)
        )

    if thumbnail_count:
        add_warning("rename.media.plan.thumbnails_preserved", {"media_id": media_id, "count": thumbnail_count})
    if video_preview_count:
        add_warning("rename.media.plan.video_previews_preserved", {"media_id": media_id, "count": video_preview_count})
    if folder_preview_count:
        add_warning("rename.media.plan.folder_previews_preserved", {"media_id": media_id, "count": folder_preview_count})

    result_message = _rename_plan_status_message("media", can_execute, media_id)

    return {
        "read_only": True,
        "can_execute": can_execute,
        "checks": checks,
        "result_messages": [result_message],
        "warning_messages": warning_messages,
        "blocker_messages": blocker_messages,
        "media_id": media_id,
        "old": {
            "rel_path": old_rel_path,
            "path_key": old_path_key,
            "file_name": old_file_name,
            "extension": old_extension,
            "folder_rel_path": folder_rel_path,
            "filesystem_path": str(old_filesystem_path),
            "exists": source_exists,
            "is_file": source_is_file,
        },
        "new": {
            "rel_path": normalized_new_rel_path,
            "path_key": new_path_key,
            "file_name": new_name,
            "extension": new_extension,
            "folder_rel_path": folder_rel_path,
            "filesystem_path": str(new_filesystem_path),
            "exists": target_exists,
            "same_folder": same_folder,
            "same_extension": same_extension,
            "case_only_rename": case_only_rename,
        },
        "filesystem": {
            "source_root_available": source_status.available,
            "source_root_message": source_status.message,
            "source_exists": source_exists,
            "source_is_file": source_is_file,
            "target_exists": target_exists,
            "target_is_same_as_source": target_is_same_as_source,
        },
        "impact": {
            "favorites": {
                "matched": len(favorite_matches),
                "will_update_if_executed": bool(favorite_matches),
                "entries": favorite_matches[:5],
            },
            "thumbnails": {
                "count": thumbnail_count,
                "will_delete_if_executed": False,
            },
            "video_previews": {
                "count": video_preview_count,
                "will_delete_if_executed": False,
            },
            "folder_preview_items": {
                "count": folder_preview_count,
                "will_change_if_executed": False,
                "sample": [
                    {
                        "folder_id": int(row["folder_id"]),
                        "folder_rel_path": str(row["folder_rel_path"]),
                        "selection_type": str(row["selection_type"]),
                        "position": int(row["position"]),
                    }
                    for row in folder_preview_rows
                ],
            },
            "catalog_collision": None
            if database_collision is None
            else {
                "media_id": int(database_collision["id"]),
                "rel_path": str(database_collision["rel_path"]),
            },
        },
        "writes_if_executed": {
            "source_data_rename": True,
            "catalog_db_media_files": True,
            "favorites_json": bool(favorite_matches),
            "thumbnail_cache": False,
            "folder_preview_items": False,
            "video_previews": False,
            "scan_data": False,
            "config_json": False,
            "settings_json": False,
        },
        "unchanged_if_executed": [
            "folders",
            "folder counts",
            "scan_sessions",
            "thumbnails",
            "video_previews",
            "folder_preview_items",
            "cache files",
            "config.json",
            "settings.json",
        ],
    }


def _build_safe_folder_rename_plan(
    *,
    config: Config,
    connection: sqlite3.Connection,
    folder: sqlite3.Row,
    new_name: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blocker_messages: list[BackendMessage] = []
    warning_messages: list[BackendMessage] = []

    folder_id = int(folder["id"])
    old_rel_path = str(folder["rel_path"] or "")
    old_path_key = str(folder["path_key"] or "")
    old_name = str(folder["name"] or "")
    old_depth = int(folder["depth"])
    parent_id = folder["parent_id"]
    parent_rel_path = _parent_rel_path_from_rel_path(old_rel_path)
    parent_path_key = catalog_path_key(parent_rel_path) if parent_rel_path else ""

    def add_check(key: str, passed: bool) -> None:
        check_passed = bool(passed)
        message_obj = _rename_check_message(
            "folder",
            key,
            check_passed,
            {
                "folder_id": folder_id,
                "old_rel_path": old_rel_path,
                "new_name": new_name,
            },
        )
        checks.append({"key": key, "passed": check_passed, "message_object": message_obj})
        if not check_passed:
            blocker_messages.append(message_obj)

    source_status = source_root_status(config)
    add_check("source_root_available", source_status.available)
    add_check("folder_available", int(folder["is_available"]) == 1)
    add_check("not_root_folder", bool(old_rel_path) and old_depth > 0)

    name_validation = _validate_safe_folder_rename_name(new_name)
    for item in name_validation["checks"]:
        add_check(str(item["key"]), bool(item["passed"]))

    try:
        old_filesystem_path = safe_join_catalog_path(
            config.data_root,
            old_rel_path,
            allow_root=False,
        )
    except PathValidationError as exc:
        old_filesystem_path = config.data_root / old_rel_path
        add_check("old_path_inside_data_root", False)
    else:
        add_check("old_path_inside_data_root", True)

    if parent_rel_path:
        new_rel_path = f"{parent_rel_path}/{new_name}" if new_name else parent_rel_path + "/"
    else:
        new_rel_path = new_name

    try:
        normalized_new_rel_path = normalize_catalog_relative_path(new_rel_path, allow_root=False)
    except PathValidationError as exc:
        normalized_new_rel_path = new_rel_path
        add_check("new_path_valid", False)
    else:
        add_check("new_path_valid", True)

    try:
        new_filesystem_path = safe_join_catalog_path(
            config.data_root,
            normalized_new_rel_path,
            allow_root=False,
        )
    except PathValidationError as exc:
        new_filesystem_path = config.data_root / normalized_new_rel_path
        add_check("new_path_inside_data_root", False)
    else:
        add_check("new_path_inside_data_root", True)

    new_path_key = catalog_path_key(normalized_new_rel_path)
    new_parent_rel_path = _parent_rel_path_from_rel_path(normalized_new_rel_path)
    same_parent = new_parent_rel_path == parent_rel_path
    add_check("same_parent_folder", same_parent)

    same_exact_path = normalized_new_rel_path == old_rel_path
    same_catalog_key = new_path_key == old_path_key
    case_only_rename = same_catalog_key and not same_exact_path
    add_check("new_name_differs", not same_exact_path)
    add_check("not_case_only_rename", not case_only_rename)

    source_exists = False
    source_is_dir = False
    target_exists = False
    target_is_same_as_source = False
    try:
        source_exists = old_filesystem_path.exists()
        source_is_dir = old_filesystem_path.is_dir() if source_exists else False
    except OSError:
        source_exists = False
        source_is_dir = False
    try:
        target_exists = new_filesystem_path.exists()
    except OSError:
        target_exists = True
    try:
        target_is_same_as_source = same_path(old_filesystem_path, new_filesystem_path)
    except OSError:
        target_is_same_as_source = False

    add_check("source_folder_exists", source_exists)
    add_check("source_is_folder", source_is_dir)
    add_check("target_folder_absent", not target_exists or target_is_same_as_source)

    old_prefix = old_path_key + "/" if old_path_key else ""
    new_prefix = new_path_key + "/" if new_path_key else ""

    folder_collision = connection.execute(
        """
        SELECT id, rel_path
        FROM folders
        WHERE id <> ?
          AND (path_key = ? OR substr(path_key, 1, ?) = ?)
          AND NOT (path_key = ? OR substr(path_key, 1, ?) = ?)
        ORDER BY rel_path
        LIMIT 1
        """,
        (
            folder_id,
            new_path_key,
            len(new_prefix),
            new_prefix,
            old_path_key,
            len(old_prefix),
            old_prefix,
        ),
    ).fetchone()
    add_check("no_folder_catalog_collision", folder_collision is None)

    media_collision = connection.execute(
        """
        SELECT id, rel_path
        FROM media_files
        WHERE substr(path_key, 1, ?) = ?
          AND NOT substr(path_key, 1, ?) = ?
        ORDER BY rel_path
        LIMIT 1
        """,
        (len(new_prefix), new_prefix, len(old_prefix), old_prefix),
    ).fetchone()
    add_check("no_media_catalog_collision", media_collision is None)

    branch_folder_count = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM folders
        WHERE is_available = 1
          AND (path_key = ? OR substr(path_key, 1, ?) = ?)
        """,
        (old_path_key, len(old_prefix), old_prefix),
    )
    branch_media_count = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM media_files
        WHERE is_available = 1
          AND substr(path_key, 1, ?) = ?
        """,
        (len(old_prefix), old_prefix),
    )
    descendant_folder_count = max(branch_folder_count - 1, 0)

    favorite_entries = _read_favorite_entries(config)
    favorite_matches = [
        entry
        for entry in favorite_entries
        if catalog_path_key(entry["path"]).startswith(old_prefix)
    ]
    thumbnail_count = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM thumbnails AS t
        JOIN media_files AS mf ON mf.id = t.media_id
        WHERE mf.is_available = 1
          AND substr(mf.path_key, 1, ?) = ?
        """,
        (len(old_prefix), old_prefix),
    )
    video_preview_count = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM video_previews AS vp
        JOIN media_files AS mf ON mf.id = vp.media_id
        WHERE mf.is_available = 1
          AND substr(mf.path_key, 1, ?) = ?
        """,
        (len(old_prefix), old_prefix),
    )
    folder_preview_for_folders_count = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM folder_preview_items AS fpi
        JOIN folders AS f ON f.id = fpi.folder_id
        WHERE f.is_available = 1
          AND (f.path_key = ? OR substr(f.path_key, 1, ?) = ?)
        """,
        (old_path_key, len(old_prefix), old_prefix),
    )
    folder_preview_using_media_count = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM folder_preview_items AS fpi
        JOIN media_files AS mf ON mf.id = fpi.media_id
        WHERE mf.is_available = 1
          AND substr(mf.path_key, 1, ?) = ?
        """,
        (len(old_prefix), old_prefix),
    )
    folder_preview_rows = connection.execute(
        """
        SELECT
            fpi.folder_id,
            f.rel_path AS folder_rel_path,
            fpi.selection_type,
            fpi.position,
            mf.rel_path AS media_rel_path
        FROM folder_preview_items AS fpi
        JOIN folders AS f ON f.id = fpi.folder_id
        JOIN media_files AS mf ON mf.id = fpi.media_id
        WHERE (f.path_key = ? OR substr(f.path_key, 1, ?) = ?)
           OR substr(mf.path_key, 1, ?) = ?
        ORDER BY f.rel_path, fpi.selection_type, fpi.position
        LIMIT 20
        """,
        (old_path_key, len(old_prefix), old_prefix, len(old_prefix), old_prefix),
    ).fetchall()

    can_execute = not blocker_messages

    def add_warning(code: str, params: dict[str, Any]) -> None:
        warning_messages.append(
            build_backend_message(code, severity="warning", params=params)
        )

    if branch_folder_count:
        add_warning("rename.folder.plan.branch_paths_changed", {"folder_id": folder_id, "count": branch_folder_count})
    if favorite_matches:
        add_warning("rename.folder.plan.favorites_updated", {"folder_id": folder_id, "count": len(favorite_matches)})
    if thumbnail_count or video_preview_count or folder_preview_for_folders_count or folder_preview_using_media_count:
        add_warning("rename.folder.plan.previews_preserved", {
                "folder_id": folder_id,
                "thumbnails": thumbnail_count,
                "video_previews": video_preview_count,
                "folder_previews": folder_preview_for_folders_count + folder_preview_using_media_count,
            })

    result_message = _rename_plan_status_message("folder", can_execute, folder_id)

    return {
        "read_only": True,
        "can_execute": can_execute,
        "checks": checks,
        "result_messages": [result_message],
        "warning_messages": warning_messages,
        "blocker_messages": blocker_messages,
        "folder_id": folder_id,
        "old": {
            "rel_path": old_rel_path,
            "path_key": old_path_key,
            "name": old_name,
            "parent_id": None if parent_id is None else int(parent_id),
            "parent_rel_path": parent_rel_path,
            "parent_path_key": parent_path_key,
            "depth": old_depth,
            "filesystem_path": str(old_filesystem_path),
            "exists": source_exists,
            "is_dir": source_is_dir,
        },
        "new": {
            "rel_path": normalized_new_rel_path,
            "path_key": new_path_key,
            "name": new_name,
            "parent_id": None if parent_id is None else int(parent_id),
            "parent_rel_path": parent_rel_path,
            "parent_path_key": parent_path_key,
            "depth": old_depth,
            "filesystem_path": str(new_filesystem_path),
            "exists": target_exists,
            "same_parent": same_parent,
            "case_only_rename": case_only_rename,
        },
        "filesystem": {
            "source_root_available": source_status.available,
            "source_root_message": source_status.message,
            "source_exists": source_exists,
            "source_is_dir": source_is_dir,
            "target_exists": target_exists,
            "target_is_same_as_source": target_is_same_as_source,
        },
        "impact": {
            "folders": {
                "count": branch_folder_count,
                "descendant_count": descendant_folder_count,
                "will_update_if_executed": branch_folder_count > 0,
            },
            "media_files": {
                "count": branch_media_count,
                "will_update_if_executed": branch_media_count > 0,
            },
            "favorites": {
                "matched": len(favorite_matches),
                "will_update_if_executed": bool(favorite_matches),
                "entries": favorite_matches[:5],
            },
            "thumbnails": {
                "count": thumbnail_count,
                "will_delete_if_executed": False,
            },
            "video_previews": {
                "count": video_preview_count,
                "will_delete_if_executed": False,
            },
            "folder_preview_items": {
                "for_renamed_folders_count": folder_preview_for_folders_count,
                "using_renamed_media_count": folder_preview_using_media_count,
                "will_change_if_executed": False,
                "sample": [
                    {
                        "folder_id": int(row["folder_id"]),
                        "folder_rel_path": str(row["folder_rel_path"]),
                        "selection_type": str(row["selection_type"]),
                        "position": int(row["position"]),
                        "media_rel_path": str(row["media_rel_path"]),
                    }
                    for row in folder_preview_rows
                ],
            },
            "catalog_collision": {
                "folder": None
                if folder_collision is None
                else {
                    "folder_id": int(folder_collision["id"]),
                    "rel_path": str(folder_collision["rel_path"]),
                },
                "media": None
                if media_collision is None
                else {
                    "media_id": int(media_collision["id"]),
                    "rel_path": str(media_collision["rel_path"]),
                },
            },
        },
        "writes_if_executed": {
            "source_data_rename": True,
            "catalog_db_folders": branch_folder_count > 0,
            "catalog_db_media_files": branch_media_count > 0,
            "favorites_json": bool(favorite_matches),
            "thumbnail_cache": False,
            "folder_preview_items": False,
            "video_previews": False,
            "scan_data": False,
            "config_json": False,
            "settings_json": False,
        },
        "unchanged_if_executed": [
            "folder_id",
            "media_id",
            "scan_sessions",
            "thumbnails",
            "video_previews",
            "folder_preview_items",
            "cache files",
            "config.json",
            "settings.json",
        ],
    }



def _validate_safe_media_rename_filename(new_name: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(key: str, passed: bool) -> None:
        checks.append({"key": key, "passed": bool(passed)})

    add("not_empty", bool(new_name))
    add("no_slashes", "/" not in new_name and "\\" not in new_name)
    add("not_dot_name", new_name not in {".", ".."})
    add("no_invalid_windows_chars", not any(char in WINDOWS_INVALID_FILENAME_CHARS for char in new_name))
    add("no_control_chars", not any(ord(char) < 32 for char in new_name))
    add("not_trailing_space_or_dot", bool(new_name) and not new_name.endswith((" ", ".")))

    stem = Path(new_name).stem.upper()
    add("not_windows_reserved_name", stem not in WINDOWS_RESERVED_FILE_NAMES)

    basic_name_valid = all(item["passed"] for item in checks)
    return {"basic_name_valid": basic_name_valid, "checks": checks}


def _validate_safe_folder_rename_name(new_name: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(key: str, passed: bool) -> None:
        checks.append({"key": key, "passed": bool(passed)})

    add("not_empty", bool(new_name))
    add("no_slashes", "/" not in new_name and "\\" not in new_name)
    add("not_dot_name", new_name not in {".", ".."})
    add("no_invalid_windows_chars", not any(char in WINDOWS_INVALID_FILENAME_CHARS for char in new_name))
    add("no_control_chars", not any(ord(char) < 32 for char in new_name))
    add("not_trailing_space_or_dot", bool(new_name) and not new_name.endswith((" ", ".")))

    reserved_name = new_name.upper()
    add("not_windows_reserved_name", reserved_name not in WINDOWS_RESERVED_FILE_NAMES)

    basic_name_valid = all(item["passed"] for item in checks)
    return {"basic_name_valid": basic_name_valid, "checks": checks}



def video_tools_status_page(config: Config) -> dict[str, Any]:
    """Return read-only diagnostics for future video preview generation."""
    return video_tools_status(config)


def missing_folder_delete_plan_page(
    config: Config,
    raw_old_path: str,
) -> dict[str, Any]:
    """Return a read-only plan for removing one missing folder branch from the catalog DB.

    Step 8.1h intentionally does not write to catalog.db, favorites.json,
    thumbnails or source data. It only describes what a later confirmed delete
    action would affect.
    """
    old_rel_path = _normalize_api_path(raw_old_path, allow_root=False)

    with open_database(config.db_path, read_only=True) as connection:
        old_folder = _folder_by_path_key(
            connection,
            catalog_path_key(old_rel_path),
            available_only=True,
        )
        if old_folder is None:
            raise ApiError.from_message(
                404,
                "missing_folder.delete.not_active",
                params={"path": old_rel_path},
            )

        plan = _build_missing_folder_delete_plan(
            config=config,
            connection=connection,
            old_folder=old_folder,
            old_rel_path=old_rel_path,
        )

    return {
        "ok": True,
        "action": "missing-folder-delete-plan",
        "read_only": True,
        "old_path": old_rel_path,
        "plan": plan,
    }


def missing_folder_delete_execute_action(
    config: Config,
    raw_old_path: str,
) -> dict[str, Any]:
    """Remove one missing active folder branch from catalog.db after explicit confirmation.

    Source files are never changed. Matching favorites.json entries are removed
    together with the database branch so the UI cannot keep dead favorite items.
    """
    old_rel_path = _normalize_api_path(raw_old_path, allow_root=False)
    old_key = catalog_path_key(old_rel_path)

    original_favorites = _read_favorite_entries(config)
    kept_favorites = _favorite_entries_without_branch_path_key(original_favorites, old_key)
    favorites_removed = len(original_favorites) - len(kept_favorites)
    favorites_written = False

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")

            old_folder = _folder_by_path_key(
                connection,
                old_key,
                available_only=True,
            )
            if old_folder is None:
                raise ApiError.from_message(
                404,
                "missing_folder.delete.not_active",
                params={"path": old_rel_path},
            )

            plan = _build_missing_folder_delete_plan(
                config=config,
                connection=connection,
                old_folder=old_folder,
                old_rel_path=old_rel_path,
            )
            blocker_messages = list(plan.get("blocker_messages") or [])
            if blocker_messages:
                raise ApiError.from_message(
                    409,
                    "missing_folder.delete.blocked",
                    params={
                        "path": old_rel_path,
                        "blocker_count": len(blocker_messages),
                    },
                    payload={"blocker_messages": blocker_messages},
                )

            deleted_media, deleted_folders = _delete_folder_branch_from_catalog_db(
                connection,
                old_key,
            )
            _calculate_folder_statistics(connection)

            if favorites_removed:
                _write_favorite_entries(config, kept_favorites)
                favorites_written = True

            connection.commit()

        except Exception:
            connection.rollback()
            if favorites_written:
                try:
                    _write_favorite_entries(config, original_favorites)
                except Exception:
                    # Prefer surfacing the original failure. A failed rollback of
                    # favorites.json would need manual inspection, but source data
                    # are still untouched.
                    pass
            raise

    return {
        "ok": True,
        "action": "missing-folder-delete-execute",
        "old_path": old_rel_path,
        "parent_path": _parent_rel_path(old_rel_path),
        "deleted": {
            "folders": deleted_folders,
            "media": deleted_media,
            "favorites": favorites_removed,
        },
        "writes": {
            "catalog_db": True,
            "favorites_json": favorites_removed > 0,
            "thumbnail_cache": False,
            "scan_data": False,
            "source_data": False,
        },
        "result_messages": [
            build_backend_message(
                "missing_folder.delete.success",
                severity="success",
                params={
                    "path": old_rel_path,
                    "folders": deleted_folders,
                    "media": deleted_media,
                    "favorites": favorites_removed,
                },
            )
        ],
    }


def _build_missing_folder_delete_plan(
    *,
    config: Config,
    connection: sqlite3.Connection,
    old_folder: sqlite3.Row,
    old_rel_path: str,
) -> dict[str, Any]:
    """Return a read-only plan for deleting a missing active branch from catalog.db."""
    old_key = catalog_path_key(old_rel_path)
    old_status = _folder_filesystem_status(config, old_rel_path)
    blocker_messages: list[BackendMessage] = []

    def add_blocker(code: str) -> None:
        blocker_messages.append(
            build_backend_message(
                code,
                severity="blocker",
                params={"path": old_rel_path or "/"},
            )
        )

    if old_rel_path == "":
        add_blocker("missing_folder.delete.plan.root_forbidden")

    if old_status.get("reason") == "source_root_unavailable":
        add_blocker("missing_folder.delete.plan.source_root_unavailable")

    if int(old_folder["is_available"]) != 1:
        add_blocker("missing_folder.delete.plan.folder_inactive")

    if old_status.get("is_usable") is True:
        add_blocker("missing_folder.delete.plan.folder_still_available")

    impact = {
        "folders": _count_all_folders_in_branch(connection, old_key),
        "media": _count_all_media_in_branch(connection, old_key),
        "favorites": _count_favorites_in_branch(config, old_key),
    }

    return {
        "read_only": True,
        "action": "missing-folder-delete-plan",
        "old_path": old_rel_path,
        "old_name": _folder_name_from_rel_path(old_rel_path),
        "old_filesystem": old_status,
        "impact": impact,
        "blocker_messages": blocker_messages,
        "can_execute_now": not blocker_messages,
        "writes": {
            "catalog_db": False,
            "favorites_json": False,
            "thumbnail_cache": False,
            "scan_data": False,
            "source_data": False,
        },
    }

def _count_all_folders_in_branch(connection: sqlite3.Connection, branch_path_key: str) -> int:
    prefix = branch_path_key + "/"
    return _count(
        connection,
        """
        SELECT COUNT(*)
        FROM folders
        WHERE path_key = ? OR substr(path_key, 1, ?) = ?
        """,
        (branch_path_key, len(prefix), prefix),
    )


def _count_all_media_in_branch(connection: sqlite3.Connection, branch_path_key: str) -> int:
    prefix = branch_path_key + "/"
    return _count(
        connection,
        """
        SELECT COUNT(*)
        FROM media_files
        WHERE substr(path_key, 1, ?) = ?
        """,
        (len(prefix), prefix),
    )


def _delete_folder_branch_from_catalog_db(
    connection: sqlite3.Connection,
    branch_path_key: str,
) -> tuple[int, int]:
    """Delete one folder branch from catalog.db and return (media, folders)."""
    prefix = branch_path_key + "/"

    media_to_delete = _count_all_media_in_branch(connection, branch_path_key)
    folders_to_delete = _count_all_folders_in_branch(connection, branch_path_key)

    connection.execute(
        """
        DELETE FROM media_files
        WHERE substr(path_key, 1, ?) = ?
        """,
        (len(prefix), prefix),
    )
    connection.execute(
        """
        DELETE FROM folders
        WHERE path_key = ? OR substr(path_key, 1, ?) = ?
        """,
        (branch_path_key, len(prefix), prefix),
    )

    return media_to_delete, folders_to_delete


def _parent_rel_path(rel_path: str) -> str:
    parent = PurePosixPath(rel_path).parent
    if str(parent) == ".":
        return ""
    return str(parent)


def _folder_by_path_key(
    connection: sqlite3.Connection,
    path_key: str,
    *,
    available_only: bool,
) -> sqlite3.Row | None:
    sql = """
        SELECT id, rel_path, path_key, parent_id, name, depth, is_available
        FROM folders
        WHERE path_key = ?
    """
    if available_only:
        sql += " AND is_available = 1"
    return connection.execute(sql, (path_key,)).fetchone()


def _count_folders_in_branch(connection: sqlite3.Connection, branch_path_key: str) -> int:
    prefix = branch_path_key + "/"
    return _count(
        connection,
        """
        SELECT COUNT(*)
        FROM folders
        WHERE is_available = 1
          AND (path_key = ? OR substr(path_key, 1, ?) = ?)
        """,
        (branch_path_key, len(prefix), prefix),
    )


def _count_media_in_branch(connection: sqlite3.Connection, branch_path_key: str) -> int:
    prefix = branch_path_key + "/"
    return _count(
        connection,
        """
        SELECT COUNT(*)
        FROM media_files
        WHERE is_available = 1
          AND substr(path_key, 1, ?) = ?
        """,
        (len(prefix), prefix),
    )


def _count_favorites_in_branch(config: Config, branch_path_key: str) -> int:
    prefix = branch_path_key + "/"
    count = 0
    for entry in _read_favorite_entries(config):
        path_key = catalog_path_key(entry["path"])
        if path_key.startswith(prefix):
            count += 1
    return count


def _parent_rel_path_from_rel_path(rel_path: str) -> str:
    parts = PurePosixPath(rel_path).parts
    if len(parts) <= 1:
        return ""
    return "/".join(parts[:-1])


def _folder_name_from_rel_path(rel_path: str) -> str:
    if rel_path == "":
        return "Hlavní stránka"
    parts = PurePosixPath(rel_path).parts
    return str(parts[-1]) if parts else ""




def folder_preview_build_tree_plan_page(
    config: Config,
    raw_folder: str,
) -> dict[str, Any]:
    """Return a read-only UI summary of the unified folder preview tree plan."""
    plan = build_folder_preview_tree_plan(
        config,
        branch_rel_path=raw_folder,
        variant=FOLDER_PREVIEW_SELECTION_VARIANT,
        requested_count=FOLDER_PREVIEW_REQUESTED_COUNT,
    )
    return {
        "ok": True,
        "action": "folder-preview-build-tree-plan",
        "plan": _folder_preview_tree_plan_dict(plan),
    }


def _folder_preview_tree_plan_dict(plan: FolderPreviewTreePlan) -> dict[str, Any]:
    """Serialize a folder-preview tree plan for the browser UI."""
    planned_auto_items = [item for item in plan.auto_items if item.report is not None]
    skipped_auto_items = [item for item in plan.auto_items if item.report is None]
    planned_parent_items = [item for item in plan.parent_items if item.report is not None]
    skipped_parent_items = [item for item in plan.parent_items if item.report is None]

    total_auto_selected = sum(item.selected_count for item in planned_auto_items)
    total_auto_ready = sum(item.ready_count for item in planned_auto_items)
    total_auto_missing = sum(item.missing_count for item in planned_auto_items)
    total_parent_source = sum(item.source_preview_item_count for item in planned_parent_items)
    total_parent_selected = sum(item.selected_count for item in planned_parent_items)
    total_parent_ready = sum(item.ready_count for item in planned_parent_items)
    total_parent_missing = sum(item.missing_count for item in planned_parent_items)

    return {
        "branch": plan.branch_rel_path,
        "name": plan.branch_name,
        "variant": plan.variant,
        "requested_count": plan.requested_count,
        "candidate_folder_count": plan.candidate_folder_count,
        "auto": {
            "folders_to_build": len(planned_auto_items),
            "folders_skipped": len(skipped_auto_items),
            "selected_items": total_auto_selected,
            "ready_thumbnail_files": total_auto_ready,
            "missing_or_not_ready": total_auto_missing,
            "rows_to_delete_in_scope": plan.existing_auto_rows_in_scope,
            "skip_reasons": _preview_skip_reason_counts(skipped_auto_items),
        },
        "parent": {
            "folders_to_apply": len(planned_parent_items),
            "folders_skipped": len(skipped_parent_items),
            "source_preview_items": total_parent_source,
            "selected_items": total_parent_selected,
            "ready_thumbnail_files": total_parent_ready,
            "missing_or_not_ready": total_parent_missing,
            "rows_to_delete_in_scope": plan.existing_auto_parent_rows_in_scope,
            "skip_reasons": _preview_skip_reason_counts(skipped_parent_items),
        },
        "writes": {
            "catalog_db": "folder_preview_items auto/auto_parent in selected branch",
            "thumbnail_cache": "missing selected auto candidates only",
            "source_media": False,
            "scan_data": False,
            "manual_preview_rows": False,
        },
        "read_only": True,
    }


def _preview_skip_reason_counts(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        reason = str(getattr(item, "skip_reason", "") or "no reason")
        counts[reason] = counts.get(reason, 0) + 1
    return counts

def catalog_status(config: Config) -> dict[str, Any]:
    """Return a read-only summary of the active catalog."""
    with open_database(config.db_path, read_only=True) as connection:
        active_scan_id = connection.execute(
            """
            SELECT last_successful_scan_id
            FROM folders
            WHERE path_key = ''
            """
        ).fetchone()

        source_status = source_root_status(config)
        folder_count, available_folder_count = active_table_counts(
            connection,
            "folders",
        )
        media_count, available_media_count = active_table_counts(
            connection,
            "media_files",
        )

        return {
            "ok": True,
            "source_root": {
                "path": str(source_status.path),
                "available": source_status.available,
                "exists": source_status.exists,
                "is_dir": source_status.is_dir,
                "readable": source_status.readable,
                "reason": source_status.reason,
                "message_object": _source_root_status_message(source_status),
            },
            "active_scan_id": (
                int(active_scan_id[0]) if active_scan_id is not None else None
            ),
            "available_folders": available_folder_count,
            "available_media": available_media_count,
            "unavailable_folders": folder_count - available_folder_count,
            "unavailable_media": media_count - available_media_count,
        }


def job_status(config: Config, runtime_job: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Return a read-only summary of the current long-job state.

    Step 5.1 only reports state. It does not start, stop, activate, or modify
    a scan. The current job model is intentionally limited to scan operations.
    """
    status = read_scan_status(config.db_path)
    lock = _scan_lock_status(scan_lock_path(config.db_path))

    latest_scan = None
    if status.scan_id is not None:
        latest_scan = {
            "scan_id": status.scan_id,
            "scan_type": status.scan_type,
            "scope_rel_path": status.scope_rel_path or "",
            "status": status.status,
            "started_at": status.started_at,
            "started_at_iso": _unix_time_iso(status.started_at),
            "finished_at": status.finished_at,
            "finished_at_iso": _unix_time_iso(status.finished_at),
            "active_scan_id_at_stage": status.active_scan_id_at_stage,
            "declared": {
                "folders": status.declared_folder_count,
                "media": status.declared_media_count,
                "errors": status.declared_error_count,
            },
            "staged": {
                "folders": status.staged_folder_count,
                "media": status.staged_media_count,
                "errors": status.staged_error_count,
            },
        }

    current_job = runtime_job or _current_scan_job_summary(
        latest_status=status.status,
        lock_held=lock["held"],
    )

    return {
        "ok": True,
        "current_job": current_job,
        "scan_lock": lock,
        "latest_scan": latest_scan,
        "active_catalog": {
            "active_scan_id": status.active_scan_id,
            "folders": {
                "available": status.available_folder_count,
                "unavailable": status.unavailable_folder_count,
                "total": status.active_folder_count,
            },
            "media": {
                "available": status.available_media_count,
                "unavailable": status.unavailable_media_count,
                "total": status.active_media_count,
            },
        },
    }


def folder_detail(config: Config, raw_path: str) -> dict[str, Any]:
    """Return metadata for one available folder."""
    rel_path = _normalize_api_path(raw_path, allow_root=True)
    path_key = catalog_path_key(rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                rel_path,
                path_key,
                parent_id,
                name,
                depth,
                last_successful_scan_id,
                direct_child_count,
                direct_image_count,
                direct_gif_count,
                direct_video_count,
                direct_other_count,
                recursive_folder_count,
                recursive_image_count,
                recursive_gif_count,
                recursive_video_count,
                recursive_other_count
            FROM folders
            WHERE path_key = ?
              AND is_available = 1
            """,
            (path_key,),
        ).fetchone()

        if row is None:
            if rel_path == "":
                disk_candidates = _disk_top_level_folder_candidates(config, set())
                folder = _synthetic_root_folder_dict(direct_folder_count=len(disk_candidates))
                _attach_folder_filesystem_status(config, folder)
                return {
                    "ok": True,
                    "folder": folder,
                    "breadcrumb": [_synthetic_root_breadcrumb_item()],
                    "synthetic_root": True,
                }
            raise ApiError.from_message(
                404,
                "navigation.folder.unavailable",
                params={"path": rel_path or "/"},
            )

        folder = _folder_dict(row)
        _attach_folder_filesystem_status(config, folder)
        return {
            "ok": True,
            "folder": folder,
            "breadcrumb": _breadcrumb(connection, int(row["id"])),
        }


@dataclass
class _FolderBrowseDiagnostics:
    """Collect non-overlapping /api/folders phases for the active request trace."""

    source_root_status_ms: float = 0.0
    folder_fs_status_ms: float = 0.0
    root_enumeration_ms: float = 0.0
    preview_metadata_ms: float = 0.0
    preview_sql_duration_ms: float = 0.0
    preview_query_ms: float = 0.0
    preview_cache_file_checks_ms: float = 0.0
    preview_count_maps_ms: float = 0.0
    preview_composition_ms: float = 0.0
    preview_query_row_count: int = 0
    preview_cache_file_check_count: int = 0
    source_root_status_checks: int = 0
    folder_fs_checks: int = 0
    folder_fs_batch_enumerations: int = 0
    folder_fs_fallback_checks: int = 0
    folder_fs_batch_ms: float = 0.0

    @classmethod
    def create(cls) -> _FolderBrowseDiagnostics | None:
        return cls() if diagnostic_request_active() else None

    def source_root_status(self, config: Config) -> Any:
        started = time.perf_counter()
        try:
            return source_root_status(config)
        finally:
            self.source_root_status_ms += (time.perf_counter() - started) * 1000.0
            self.source_root_status_checks += 1

    def begin_preview(self) -> tuple[float, tuple[int, float]]:
        return time.perf_counter(), diagnostic_request_sql_snapshot()

    def end_preview(self, started: tuple[float, tuple[int, float]]) -> None:
        clock_started, (_sql_count, sql_started_ms) = started
        self.preview_metadata_ms += (time.perf_counter() - clock_started) * 1000.0
        self.preview_sql_duration_ms += max(
            0.0,
            diagnostic_request_sql_snapshot()[1] - sql_started_ms,
        )

    def finish(
        self,
        params: FolderPageParams,
        include_previews: bool,
        result: dict[str, Any],
    ) -> None:
        preview_tracked_ms = (
            self.preview_query_ms
            + self.preview_cache_file_checks_ms
            + self.preview_count_maps_ms
            + self.preview_composition_ms
        )
        # Sub-phase timers are disjoint; measurement overhead remains visible
        # in preview_other instead of being silently attributed to useful work.
        preview_other_ms = max(0.0, self.preview_metadata_ms - preview_tracked_ms)
        diagnostic_set_request_detail(
            "folder_browse",
            {
                "folder": (params.parent_rel_path or "<root>")[:300],
                "is_root": params.parent_rel_path == "",
                "page": params.page,
                "page_size": params.page_size,
                "include_previews": include_previews,
                "returned_folders": len(result.get("folders", [])),
                "source_root_status_ms": round(self.source_root_status_ms, 3),
                "source_root_status_checks": self.source_root_status_checks,
                "folder_fs_status_ms": round(self.folder_fs_status_ms, 3),
                "folder_fs_checks": self.folder_fs_checks,
                "folder_fs_batch_ms": round(self.folder_fs_batch_ms, 3),
                "folder_fs_batch_enumerations": self.folder_fs_batch_enumerations,
                "folder_fs_fallback_checks": self.folder_fs_fallback_checks,
                "root_enumeration_ms": round(self.root_enumeration_ms, 3),
                "preview_metadata_ms": round(self.preview_metadata_ms, 3),
                "preview_sql_duration_ms": round(self.preview_sql_duration_ms, 3),
                "preview_query_ms": round(self.preview_query_ms, 3),
                "preview_query_row_count": self.preview_query_row_count,
                "preview_cache_file_checks_ms": round(self.preview_cache_file_checks_ms, 3),
                "preview_cache_file_check_count": self.preview_cache_file_check_count,
                "preview_count_maps_ms": round(self.preview_count_maps_ms, 3),
                "preview_composition_ms": round(self.preview_composition_ms, 3),
                "preview_other_ms": round(preview_other_ms, 3),
            },
        )


def child_folders(
    config: Config,
    raw_parent: str,
    raw_page: str | None = None,
    raw_page_size: str | None = None,
    raw_include_previews: str | None = None,
) -> dict[str, Any]:
    """Return one read-only page of direct available child folders.

    For the catalog root, also include top-level directories that exist on disk
    but are not yet present in the active catalog. These entries are read-only
    discovery candidates; they allow the UI to start update-branch for a new
    top-level root without first running a full scan.
    """
    diagnostics = _FolderBrowseDiagnostics.create()
    params = _folder_page_params(
        config,
        raw_parent=raw_parent,
        raw_page=raw_page,
        raw_page_size=raw_page_size,
    )
    parent_key = catalog_path_key(params.parent_rel_path)
    include_previews = _folder_previews_requested(raw_include_previews)

    with open_database(config.db_path, read_only=True) as connection:
        parent = _available_folder_by_key(connection, parent_key)

        if params.parent_rel_path == "":
            result = _root_child_folders_with_disk_candidates(
                config=config,
                connection=connection,
                parent=parent,
                params=params,
                include_previews=include_previews,
                diagnostics=diagnostics,
            )
            if diagnostics is not None:
                diagnostics.finish(params, include_previews, result)
            return result

        if parent is None:
            raise ApiError.from_message(
                404,
                "navigation.parent_folder.unavailable",
                params={"path": params.parent_rel_path or "/"},
            )

        total = _count(
            connection,
            """
            SELECT COUNT(*)
            FROM folders
            WHERE parent_id = ?
              AND is_available = 1
            """,
            (int(parent["id"]),),
        )

        rows = connection.execute(
            """
            SELECT
                id,
                rel_path,
                path_key,
                parent_id,
                name,
                depth,
                last_successful_scan_id,
                direct_child_count,
                direct_image_count,
                direct_gif_count,
                direct_video_count,
                direct_other_count,
                recursive_folder_count,
                recursive_image_count,
                recursive_gif_count,
                recursive_video_count,
                recursive_other_count
            FROM folders
            WHERE parent_id = ?
              AND is_available = 1
            ORDER BY sort_key, name
            LIMIT ? OFFSET ?
            """,
            (int(parent["id"]), params.page_size, params.offset),
        ).fetchall()

        pages = math.ceil(total / params.page_size) if total else 0
        if include_previews and diagnostics is not None:
            preview_started = diagnostics.begin_preview()
            previews_by_folder = _folder_preview_items_by_folder(
                config,
                connection,
                [int(row["id"]) for row in rows],
                diagnostics=diagnostics,
            )
            diagnostics.end_preview(preview_started)
        elif include_previews:
            previews_by_folder = _folder_preview_items_by_folder(
                config,
                connection,
                [int(row["id"]) for row in rows],
            )
        else:
            previews_by_folder = {}
        folders = [_folder_dict(row) for row in rows]
        for row, folder in zip(rows, folders, strict=True):
            if include_previews:
                folder["folder_previews"] = previews_by_folder.get(int(row["id"]), [])

        result = {
            "ok": True,
            "parent": {
                "rel_path": str(parent["rel_path"]),
                "name": str(parent["name"]),
            },
            "count": len(rows),
            "total": total,
            "page": params.page,
            "page_size": params.page_size,
            "pages": pages,
            "folders": folders,
            "includes_previews": include_previews,
        }
        if diagnostics is not None:
            diagnostics.finish(params, include_previews, result)
        return result


def _root_child_folders_with_disk_candidates(
    *,
    config: Config,
    connection: sqlite3.Connection,
    parent: sqlite3.Row | None,
    params: FolderPageParams,
    include_previews: bool,
    diagnostics: _FolderBrowseDiagnostics | None = None,
) -> dict[str, Any]:
    """Return root children plus disk-only top-level folders.

    Disk candidates are intentionally shallow: only immediate directories under
    data_root are listed. The function does not recurse, does not scan media and
    does not write to the database.
    """
    active_rows: list[sqlite3.Row] = []
    active_path_keys: set[str] = set()

    if parent is not None:
        active_rows = connection.execute(
            """
            SELECT
                id,
                rel_path,
                path_key,
                parent_id,
                name,
                depth,
                last_successful_scan_id,
                direct_child_count,
                direct_image_count,
                direct_gif_count,
                direct_video_count,
                direct_other_count,
                recursive_folder_count,
                recursive_image_count,
                recursive_gif_count,
                recursive_video_count,
                recursive_other_count
            FROM folders
            WHERE parent_id = ?
              AND is_available = 1
            ORDER BY sort_key, name
            """,
            (int(parent["id"]),),
        ).fetchall()
        active_path_keys = {str(row["path_key"]) for row in active_rows}

    active_folders = [_folder_dict(row) for row in active_rows]
    snapshot = _attach_folder_filesystem_status_batch(
        config,
        "",
        active_folders,
        diagnostics=diagnostics,
    )
    items: list[tuple[str, dict[str, Any], int | None]] = []

    for row, folder in zip(active_rows, active_folders, strict=True):
        if include_previews:
            folder["folder_previews"] = []
        items.append(("active", folder, int(row["id"])))

    enumeration_started = time.perf_counter() if diagnostics is not None else 0.0
    enumeration_source_before = diagnostics.source_root_status_ms if diagnostics is not None else 0.0
    for folder in _disk_top_level_folder_candidates(
        config,
        active_path_keys,
        snapshot=snapshot,
    ):
        items.append(("disk", folder, None))
    if diagnostics is not None:
        diagnostics.root_enumeration_ms += max(
            0.0,
            (time.perf_counter() - enumeration_started) * 1000.0
            - (diagnostics.source_root_status_ms - enumeration_source_before),
        )

    items.sort(key=lambda item: (natural_sort_key(str(item[1]["name"])), str(item[1]["rel_path"])))

    total = len(items)
    pages = math.ceil(total / params.page_size) if total else 0
    page_items = items[params.offset : params.offset + params.page_size]

    active_ids = [folder_id for _kind, _folder, folder_id in page_items if folder_id is not None]
    if include_previews and diagnostics is not None:
        preview_started = diagnostics.begin_preview()
        previews_by_folder = _folder_preview_items_by_folder(
            config,
            connection,
            active_ids,
            diagnostics=diagnostics,
        )
        diagnostics.end_preview(preview_started)
    elif include_previews:
        previews_by_folder = _folder_preview_items_by_folder(config, connection, active_ids)
    else:
        previews_by_folder = {}

    folders: list[dict[str, Any]] = []
    for kind, folder, folder_id in page_items:
        if include_previews and kind == "active" and folder_id is not None:
            folder["folder_previews"] = previews_by_folder.get(folder_id, [])
        elif not include_previews:
            folder.pop("folder_previews", None)
        folders.append(folder)

    return {
        "ok": True,
        "parent": {
            "rel_path": "",
            "name": "Hlavní stránka",
        },
        "count": len(folders),
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
        "pages": pages,
        "folders": folders,
        "includes_disk_candidates": True,
        "includes_previews": include_previews,
    }


@dataclass(frozen=True)
class _FolderFilesystemEntrySnapshot:
    name: str
    rel_path: str
    path_key: str
    is_dir: bool
    is_link_or_junction: bool


@dataclass(frozen=True)
class _FolderFilesystemSnapshot:
    source_status: Any
    resolved_root: Path | None
    entries: dict[str, _FolderFilesystemEntrySnapshot]
    ambiguous_path_keys: frozenset[str]
    complete: bool
    iteration_error: str | None = None


def _folder_filesystem_snapshot(
    config: Config,
    parent_rel_path: str,
    *,
    diagnostics: _FolderBrowseDiagnostics | None = None,
) -> _FolderFilesystemSnapshot:
    """Read one direct-child filesystem snapshot for a folder browse request."""
    source_status = (
        diagnostics.source_root_status(config)
        if diagnostics is not None
        else source_root_status(config)
    )
    if not source_status.available:
        return _FolderFilesystemSnapshot(source_status, None, {}, frozenset(), True)

    started = time.perf_counter() if diagnostics is not None else 0.0

    def finish(snapshot: _FolderFilesystemSnapshot) -> _FolderFilesystemSnapshot:
        if diagnostics is not None:
            diagnostics.folder_fs_batch_ms += (time.perf_counter() - started) * 1000.0
        return snapshot

    try:
        resolved_root = config.data_root.expanduser().resolve(strict=True)
        parent_path = safe_join_catalog_path(
            resolved_root,
            parent_rel_path,
            allow_root=True,
        )
    except (OSError, PathValidationError):
        return finish(_FolderFilesystemSnapshot(source_status, None, {}, frozenset(), False))

    if diagnostics is not None:
        diagnostics.folder_fs_batch_enumerations += 1

    try:
        iterator = os.scandir(parent_path)
    except (FileNotFoundError, NotADirectoryError):
        return finish(
            _FolderFilesystemSnapshot(source_status, resolved_root, {}, frozenset(), True)
        )
    except OSError:
        return finish(
            _FolderFilesystemSnapshot(source_status, resolved_root, {}, frozenset(), False)
        )

    entries: dict[str, _FolderFilesystemEntrySnapshot] = {}
    ambiguous_path_keys: set[str] = set()
    try:
        with iterator:
            for entry in iterator:
                try:
                    rel_path = normalize_catalog_relative_path(
                        f"{parent_rel_path}/{entry.name}" if parent_rel_path else entry.name,
                        allow_root=False,
                    )
                except PathValidationError:
                    continue

                path_key = catalog_path_key(rel_path)
                if path_key in entries:
                    # A case/path-key collision cannot be assigned safely from
                    # one directory snapshot, so both candidates use fallback.
                    ambiguous_path_keys.add(path_key)
                    continue

                try:
                    is_link_or_junction = _is_dir_entry_link_or_junction(entry)
                    is_dir = entry.is_dir(follow_symlinks=is_link_or_junction)
                except OSError:
                    ambiguous_path_keys.add(path_key)
                    continue

                entries[path_key] = _FolderFilesystemEntrySnapshot(
                    name=entry.name,
                    rel_path=rel_path,
                    path_key=path_key,
                    is_dir=is_dir,
                    is_link_or_junction=is_link_or_junction,
                )
    except OSError as exc:
        return finish(
            _FolderFilesystemSnapshot(
                source_status,
                resolved_root,
                {},
                frozenset(),
                False,
                _technical_error_detail(exc),
            )
        )

    return finish(
        _FolderFilesystemSnapshot(
            source_status,
            resolved_root,
            entries,
            frozenset(ambiguous_path_keys),
            True,
        )
    )


def _disk_top_level_folder_candidates(
    config: Config,
    active_path_keys: set[str],
    *,
    snapshot: _FolderFilesystemSnapshot | None = None,
) -> list[dict[str, Any]]:
    """Return immediate data_root directories that are not active catalog roots."""
    candidates: list[dict[str, Any]] = []

    if snapshot is None:
        snapshot = _folder_filesystem_snapshot(config, "")
    if snapshot.iteration_error:
        raise ApiError.from_message(
            500,
            "navigation.data_root.scan_failed",
            payload={"technical_detail": snapshot.iteration_error},
        )
    if not snapshot.source_status.available or not snapshot.complete:
        return candidates

    for entry in snapshot.entries.values():
        if (
            entry.name in IGNORED_DIRECTORY_NAMES
            or entry.is_link_or_junction
            or not entry.is_dir
            or entry.path_key in active_path_keys
        ):
            continue
        candidates.append(_disk_root_candidate_dict(rel_path=entry.rel_path, name=entry.name))

    return candidates


def _is_dir_entry_link_or_junction(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True

    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(entry.path))


def _disk_root_candidate_dict(*, rel_path: str, name: str) -> dict[str, Any]:
    return {
        "id": None,
        "rel_path": rel_path,
        "name": name,
        "depth": 1,
        "last_successful_scan_id": None,
        "is_active_catalog_folder": False,
        "is_disk_candidate": True,
        "filesystem": {
            "checked": True,
            "exists": True,
            "is_dir": True,
            "is_link_or_junction": False,
            "is_usable": True,
            "reason": "ok",
            "message_object": _folder_filesystem_message("ok", rel_path),
        },
        "folder_previews": [],
        "direct": {
            "folders": 0,
            "images": 0,
            "gifs": 0,
            "videos": 0,
            "other": 0,
        },
        "recursive": {
            "folders": 0,
            "images": 0,
            "gifs": 0,
            "videos": 0,
            "other": 0,
        },
    }



def _attach_folder_filesystem_status(
    config: Config,
    folder: dict[str, Any],
    *,
    diagnostics: _FolderBrowseDiagnostics | None = None,
) -> None:
    """Attach a cheap runtime filesystem check for one displayed folder.

    This checks only the concrete folder path. It does not recurse, scan media,
    count files or write to the database.
    """
    folder["filesystem"] = _folder_filesystem_status(
        config,
        str(folder.get("rel_path") or ""),
        diagnostics=diagnostics,
    )


def _attach_folder_filesystem_status_batch(
    config: Config,
    parent_rel_path: str,
    folders: list[dict[str, Any]],
    *,
    diagnostics: _FolderBrowseDiagnostics | None = None,
) -> _FolderFilesystemSnapshot:
    """Attach statuses from one parent enumeration, with per-item fallback."""
    started = time.perf_counter() if diagnostics is not None else 0.0
    source_before = diagnostics.source_root_status_ms if diagnostics is not None else 0.0
    snapshot = _folder_filesystem_snapshot(
        config,
        parent_rel_path,
        diagnostics=diagnostics,
    )

    for folder in folders:
        rel_path = str(folder.get("rel_path") or "")
        status = _folder_filesystem_status_from_snapshot(snapshot, rel_path)
        if status is None:
            # Preserve the original safety checks for ambiguous snapshot data,
            # but reuse the request-scoped source-root result.
            if diagnostics is not None:
                diagnostics.folder_fs_fallback_checks += 1
            status = _folder_filesystem_status(
                config,
                rel_path,
                precomputed_source_status=snapshot.source_status,
                resolved_root=snapshot.resolved_root,
            )
        folder["filesystem"] = status

    if diagnostics is not None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        source_ms = diagnostics.source_root_status_ms - source_before
        diagnostics.folder_fs_status_ms += max(0.0, elapsed_ms - source_ms)
        diagnostics.folder_fs_checks += len(folders)
    return snapshot


def _attach_folder_filesystem_status_targeted(
    config: Config,
    folders: list[dict[str, Any]],
    *,
    diagnostics: _FolderBrowseDiagnostics | None = None,
) -> None:
    """Check only DB folders on a non-root page with shared request state."""
    if not folders:
        return

    started = time.perf_counter() if diagnostics is not None else 0.0
    source_before = diagnostics.source_root_status_ms if diagnostics is not None else 0.0
    source_status_value = (
        diagnostics.source_root_status(config)
        if diagnostics is not None
        else source_root_status(config)
    )

    resolved_root = Path(source_status_value.path) if source_status_value.available else None

    for folder in folders:
        rel_path = str(folder.get("rel_path") or "")
        folder["filesystem"] = _folder_filesystem_status(
            config,
            rel_path,
            precomputed_source_status=source_status_value,
            resolved_root=resolved_root,
        )

    if diagnostics is not None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        source_ms = diagnostics.source_root_status_ms - source_before
        diagnostics.folder_fs_status_ms += max(0.0, elapsed_ms - source_ms)
        diagnostics.folder_fs_checks += len(folders)


def _folder_filesystem_status_from_snapshot(
    snapshot: _FolderFilesystemSnapshot,
    rel_path: str,
) -> dict[str, Any] | None:
    try:
        normalized = normalize_catalog_relative_path(rel_path, allow_root=True)
    except PathValidationError:
        return None

    if not snapshot.source_status.available:
        return _folder_filesystem_status_payload("source_root_unavailable", normalized)
    if not snapshot.complete:
        return None

    path_key = catalog_path_key(normalized)
    if path_key in snapshot.ambiguous_path_keys:
        return None

    entry = snapshot.entries.get(path_key)
    if entry is None:
        return _folder_filesystem_status_payload("missing", normalized)
    if entry.is_link_or_junction:
        return _folder_filesystem_status_payload(
            "link_or_junction",
            normalized,
            exists=True,
            is_dir=entry.is_dir,
            is_link_or_junction=True,
        )
    if not entry.is_dir:
        return _folder_filesystem_status_payload(
            "not_directory",
            normalized,
            exists=True,
        )
    return _folder_filesystem_status_payload("ok", normalized, exists=True, is_dir=True)


def _folder_filesystem_status_payload(
    reason: str,
    rel_path: str,
    *,
    exists: bool = False,
    is_dir: bool = False,
    is_link_or_junction: bool = False,
) -> dict[str, Any]:
    return {
        "checked": True,
        "exists": exists,
        "is_dir": is_dir,
        "is_link_or_junction": is_link_or_junction,
        "is_usable": reason == "ok",
        "reason": reason,
        "message_object": _folder_filesystem_message(reason, rel_path),
    }


def _folder_filesystem_status(
    config: Config,
    rel_path: str,
    *,
    diagnostics: _FolderBrowseDiagnostics | None = None,
    precomputed_source_status: Any | None = None,
    resolved_root: Path | None = None,
) -> dict[str, Any]:
    started = time.perf_counter() if diagnostics is not None else 0.0
    source_before = diagnostics.source_root_status_ms if diagnostics is not None else 0.0
    try:
        normalized = normalize_catalog_relative_path(rel_path, allow_root=True)
        source_status_value = precomputed_source_status
        if source_status_value is None:
            source_status_value = (
                diagnostics.source_root_status(config)
                if diagnostics is not None
                else source_root_status(config)
            )
        if not source_status_value.available:
            return {
                "checked": True,
                "exists": False,
                "is_dir": False,
                "is_link_or_junction": False,
                "is_usable": False,
                "reason": "source_root_unavailable",
                "message_object": _folder_filesystem_message(
                    "source_root_unavailable", normalized
                ),
            }

        root = resolved_root or config.data_root.expanduser().resolve(strict=True)
        unresolved_target = root.joinpath(*PurePosixPath(normalized).parts)
        if _is_path_link_or_junction_path(unresolved_target):
            return {
                "checked": True,
                "exists": unresolved_target.exists(),
                "is_dir": unresolved_target.is_dir() if unresolved_target.exists() else False,
                "is_link_or_junction": True,
                "is_usable": False,
                "reason": "link_or_junction",
                "message_object": _folder_filesystem_message(
                    "link_or_junction", normalized
                ),
            }
        target = safe_join_catalog_path(root, normalized, allow_root=True)

        if _is_path_link_or_junction_path(target):
            return {
                "checked": True,
                "exists": target.exists(),
                "is_dir": target.is_dir() if target.exists() else False,
                "is_link_or_junction": True,
                "is_usable": False,
                "reason": "link_or_junction",
                "message_object": _folder_filesystem_message(
                    "link_or_junction", normalized
                ),
            }

        if not target.exists():
            return {
                "checked": True,
                "exists": False,
                "is_dir": False,
                "is_link_or_junction": False,
                "is_usable": False,
                "reason": "missing",
                "message_object": _folder_filesystem_message("missing", normalized),
            }

        if not target.is_dir():
            return {
                "checked": True,
                "exists": True,
                "is_dir": False,
                "is_link_or_junction": False,
                "is_usable": False,
                "reason": "not_directory",
                "message_object": _folder_filesystem_message(
                    "not_directory", normalized
                ),
            }

        return {
            "checked": True,
            "exists": True,
            "is_dir": True,
            "is_link_or_junction": False,
            "is_usable": True,
            "reason": "ok",
            "message_object": _folder_filesystem_message("ok", normalized),
        }
    except (OSError, PathValidationError) as exc:
        return {
            "checked": True,
            "exists": False,
            "is_dir": False,
            "is_link_or_junction": False,
            "is_usable": False,
            "reason": "error",
            "message_object": _folder_filesystem_message(
                "error", rel_path, detail=str(exc)
            ),
            "technical_detail": _technical_error_detail(exc),
        }
    finally:
        if diagnostics is not None:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            source_ms = diagnostics.source_root_status_ms - source_before
            diagnostics.folder_fs_status_ms += max(0.0, elapsed_ms - source_ms)
            diagnostics.folder_fs_checks += 1

def _is_path_link_or_junction_path(path: Path) -> bool:
    if path.is_symlink():
        return True

    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))

def _folder_preview_items_by_folder(
    config: Config,
    connection: sqlite3.Connection,
    folder_ids: list[int],
    *,
    diagnostics: _FolderBrowseDiagnostics | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Return stored automatic folder preview items for child folder cards.

    This read-only UI helper combines direct auto previews with parent-derived
    auto_parent previews into one deterministic representation. It does not
    generate thumbnails and only returns items whose thumbnail is ready.
    """
    if not folder_ids:
        return {}

    placeholders = ",".join("?" for _ in folder_ids)
    query_started = time.perf_counter() if diagnostics is not None else 0.0
    rows = connection.execute(
        f"""
        SELECT
            fpi.folder_id AS folder_id,
            fpi.position AS position,
            t.output_rel_path AS output_rel_path,
            fpi.selection_type AS selection_type
        -- CROSS JOIN keeps the small requested folder-preview set as SQLite's
        -- outer loop; starting from all ready thumbnails is prohibitively broad
        -- on large catalogs even though the final result contains few rows.
        FROM folder_preview_items AS fpi
        CROSS JOIN thumbnails AS t INDEXED BY idx_thumbnails_media_kind
        WHERE fpi.selection_type IN ('auto', 'auto_parent')
          AND fpi.folder_id IN ({placeholders})
          AND t.media_id = fpi.media_id
          AND t.status = 'ready'
          AND t.thumbnail_type IN ('photo_tile', 'gif_preview', 'video_poster')
          AND t.variant_key = 'default'
        ORDER BY fpi.folder_id, fpi.selection_type, fpi.position
        """,
        folder_ids,
    ).fetchall()
    if diagnostics is not None:
        diagnostics.preview_query_ms += (time.perf_counter() - query_started) * 1000.0
        diagnostics.preview_query_row_count += len(rows)

    grouped: dict[int, dict[str, list[dict[str, Any]]]] = {}

    grouping_started = time.perf_counter() if diagnostics is not None else 0.0
    for row in rows:
        # Cache-file validation is deferred to each thumbnail request so folder
        # browsing does not perform hundreds of cold filesystem probes.
        folder_id = int(row["folder_id"])
        selection_type = str(row["selection_type"])
        if selection_type not in {"auto", "auto_parent"}:
            continue

        grouped.setdefault(folder_id, {"auto": [], "auto_parent": []})[selection_type].append({
            "position": int(row["position"]),
            "thumbnail_cache_path": str(row["output_rel_path"]),
        })
    if diagnostics is not None:
        diagnostics.preview_composition_ms += (time.perf_counter() - grouping_started) * 1000.0

    count_maps_started = time.perf_counter() if diagnostics is not None else 0.0
    direct_visual_counts, recursive_visual_counts = folder_preview_visual_media_count_maps(connection)
    if diagnostics is not None:
        diagnostics.preview_count_maps_ms += (time.perf_counter() - count_maps_started) * 1000.0

    composition_started = time.perf_counter() if diagnostics is not None else 0.0
    result: dict[int, list[dict[str, Any]]] = {}
    for folder_id, by_type in grouped.items():
        auto_rows = sorted(by_type["auto"], key=lambda item: int(item["position"]))
        parent_rows = sorted(by_type["auto_parent"], key=lambda item: int(item["position"]))
        direct_count = int(direct_visual_counts.get(folder_id, 0))
        recursive_count = int(recursive_visual_counts.get(folder_id, direct_count))
        selected = effective_folder_preview_rows(
            auto_rows,
            parent_rows,
            requested_count=FOLDER_PREVIEW_REQUESTED_COUNT,
            variant=FOLDER_PREVIEW_SELECTION_VARIANT,
            direct_visual_media_count=direct_count,
            descendant_visual_media_count=max(0, recursive_count - direct_count),
        )
        result[folder_id] = [dict(item) for item in selected]

    if diagnostics is not None:
        diagnostics.preview_composition_ms += (time.perf_counter() - composition_started) * 1000.0

    return result

def _folder_previews_requested(raw_value: str | None) -> bool:
    """Return whether child-folder cards should include stored previews.

    The public endpoint keeps its historical default. The navigation tree sends
    ``include_previews=0`` so it receives only folder structure and counts.
    """
    if raw_value is None or str(raw_value).strip() == "":
        return True

    return str(raw_value).strip().lower() not in {"0", "false", "no", "off"}


def media_page(
    config: Config,
    *,
    raw_folder: str,
    raw_media_type: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return one read-only page of available media in a folder."""
    params = _media_page_params(
        config,
        raw_folder=raw_folder,
        raw_media_type=raw_media_type,
        raw_page=raw_page,
        raw_page_size=raw_page_size,
    )
    folder_key = catalog_path_key(params.folder_rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        folder = _available_folder_by_key(connection, folder_key)
        if folder is None:
            if params.folder_rel_path == "":
                return {
                    "ok": True,
                    "folder": {
                        "rel_path": "",
                        "name": "Hlavní stránka",
                    },
                    "type": params.media_type,
                    "page": params.page,
                    "page_size": params.page_size,
                    "total": 0,
                    "pages": 0,
                    "media": [],
                    "synthetic_root": True,
                }
            raise ApiError.from_message(
                404,
                "navigation.folder.unavailable",
                params={"path": params.folder_rel_path or "/"},
            )

        where_sql = "folder_id = ? AND is_available = 1"
        values: list[object] = [int(folder["id"])]

        if params.media_type != "all":
            where_sql += " AND media_type = ?"
            values.append(params.media_type)

        total = _count(
            connection,
            f"SELECT COUNT(*) FROM media_files WHERE {where_sql}",
            values,
        )

        rows = connection.execute(
            f"""
            SELECT
                id,
                rel_path,
                path_key,
                folder_id,
                file_name,
                extension,
                media_type,
                size_bytes,
                modified_time,
                last_successful_scan_id,
                is_available
            FROM media_files
            WHERE {where_sql}
            ORDER BY sort_key, file_name
            LIMIT ? OFFSET ?
            """,
            (*values, params.page_size, params.offset),
        ).fetchall()

        pages = math.ceil(total / params.page_size) if total else 0

        return {
            "ok": True,
            "folder": {
                "rel_path": str(folder["rel_path"]),
                "name": str(folder["name"]),
            },
            "type": params.media_type,
            "page": params.page,
            "page_size": params.page_size,
            "total": total,
            "pages": pages,
            "media": _annotate_favorite_media(config, [_media_dict(row) for row in rows]),
        }




def media_page_anchor(
    config: Config,
    *,
    raw_folder: str,
    raw_media_type: str,
    raw_path: str,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return the media page containing one concrete item in a folder.

    This is used by the modal when the visible UI page is not the same as
    the logical image/GIF gallery page, e.g. when the user opens a photo
    from the mixed "all" media filter.
    """
    params = _media_page_params(
        config,
        raw_folder=raw_folder,
        raw_media_type=raw_media_type,
        raw_page="1",
        raw_page_size=raw_page_size,
    )

    if params.media_type not in {"image", "gif"}:
        raise ApiError.from_message(
            400,
            "navigation.media_anchor.type_unsupported",
            params={"allowed": "image, gif"},
        )

    item_rel_path = _normalize_api_path(raw_path, allow_root=False)
    item_key = catalog_path_key(item_rel_path)
    folder_key = catalog_path_key(params.folder_rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        folder = _available_folder_by_key(connection, folder_key)
        if folder is None:
            raise ApiError.from_message(
                404,
                "navigation.folder.unavailable",
                params={"path": params.folder_rel_path or "/"},
            )

        item = connection.execute(
            """
            SELECT
                id,
                rel_path,
                path_key,
                folder_id,
                file_name,
                extension,
                media_type,
                size_bytes,
                modified_time,
                sort_key,
                last_successful_scan_id,
                is_available
            FROM media_files
            WHERE folder_id = ?
              AND path_key = ?
              AND media_type = ?
              AND is_available = 1
            """,
            (int(folder["id"]), item_key, params.media_type),
        ).fetchone()

        if item is None:
            raise ApiError.from_message(
                404,
                "navigation.media_anchor.item_unavailable",
            )

        total = _count(
            connection,
            """
            SELECT COUNT(*)
            FROM media_files
            WHERE folder_id = ?
              AND media_type = ?
              AND is_available = 1
            """,
            (int(folder["id"]), params.media_type),
        )

        # One-based position in the same order as /api/media for a typed folder view.
        position = _count(
            connection,
            """
            SELECT COUNT(*)
            FROM media_files
            WHERE folder_id = ?
              AND media_type = ?
              AND is_available = 1
              AND (
                sort_key < ?
                OR (sort_key = ? AND file_name < ?)
                OR (sort_key = ? AND file_name = ? AND id <= ?)
              )
            """,
            (
                int(folder["id"]),
                params.media_type,
                str(item["sort_key"]),
                str(item["sort_key"]),
                str(item["file_name"]),
                str(item["sort_key"]),
                str(item["file_name"]),
                int(item["id"]),
            ),
        )

        pages = math.ceil(total / params.page_size) if total else 0
        page = max(1, math.ceil(position / params.page_size)) if position else 1
        offset = (page - 1) * params.page_size
        index = max(0, position - offset - 1)

        rows = connection.execute(
            """
            SELECT
                id,
                rel_path,
                path_key,
                folder_id,
                file_name,
                extension,
                media_type,
                size_bytes,
                modified_time,
                last_successful_scan_id,
                is_available
            FROM media_files
            WHERE folder_id = ?
              AND media_type = ?
              AND is_available = 1
            ORDER BY sort_key, file_name, id
            LIMIT ? OFFSET ?
            """,
            (int(folder["id"]), params.media_type, params.page_size, offset),
        ).fetchall()

        return {
            "ok": True,
            "folder": {
                "rel_path": str(folder["rel_path"]),
                "name": str(folder["name"]),
            },
            "type": params.media_type,
            "anchor_path": str(item["rel_path"]),
            "anchor_index": index,
            "page": page,
            "page_size": params.page_size,
            "total": total,
            "pages": pages,
            "media": _annotate_favorite_media(config, [_media_dict(row) for row in rows]),
        }

def favorites_page(
    config: Config,
    *,
    raw_media_type: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return one page of favorite media resolved against the active catalog."""
    params = _favorite_page_params(
        config,
        raw_media_type=raw_media_type,
        raw_page=raw_page,
        raw_page_size=raw_page_size,
    )
    entries = _read_favorite_entries(config)
    media = _favorite_media_items(config, entries)

    if params.media_type != "all":
        media = [item for item in media if item["media_type"] == params.media_type]

    total = len(media)
    pages = math.ceil(total / params.page_size) if total else 0
    page_items = media[params.offset : params.offset + params.page_size]

    return {
        "ok": True,
        "view": "favorites",
        "type": params.media_type,
        "page": params.page,
        "page_size": params.page_size,
        "total": total,
        "pages": pages,
        "media": page_items,
    }




def favorites_media_page(
    config: Config,
    *,
    raw_media_type: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return one read-only page of favorite image/GIF media for modal navigation."""
    params = _favorite_page_params(
        config,
        raw_media_type=raw_media_type,
        raw_page=raw_page,
        raw_page_size=raw_page_size,
    )

    if params.media_type not in {"image", "gif"}:
        raise ApiError.from_message(
            400,
            "favorites.media_page.type_unsupported",
            params={"allowed": "image, gif"},
        )

    media = _favorite_modal_media_items(config, params.media_type)
    total = len(media)
    pages = math.ceil(total / params.page_size) if total else 0
    page_items = media[params.offset : params.offset + params.page_size]

    return {
        "ok": True,
        "view": "favorites-media",
        "type": params.media_type,
        "page": params.page,
        "page_size": params.page_size,
        "total": total,
        "pages": pages,
        "media": page_items,
    }


def favorites_page_anchor(
    config: Config,
    *,
    raw_media_type: str,
    raw_path: str,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return the favorite image/GIF page containing one concrete favorite item."""
    params = _favorite_page_params(
        config,
        raw_media_type=raw_media_type,
        raw_page="1",
        raw_page_size=raw_page_size,
    )

    if params.media_type not in {"image", "gif"}:
        raise ApiError.from_message(
            400,
            "favorites.anchor.type_unsupported",
            params={"allowed": "image, gif"},
        )

    item_rel_path = _normalize_api_path(raw_path, allow_root=False)
    item_key = catalog_path_key(item_rel_path)

    media = _favorite_modal_media_items(config, params.media_type)
    anchor_position = -1
    for index, item in enumerate(media):
        if catalog_path_key(str(item["rel_path"])) == item_key:
            anchor_position = index
            break

    if anchor_position < 0:
        raise ApiError.from_message(
            404,
            "favorites.anchor.item_unavailable",
        )

    total = len(media)
    pages = math.ceil(total / params.page_size) if total else 0
    page = (anchor_position // params.page_size) + 1
    offset = (page - 1) * params.page_size
    anchor_index = anchor_position - offset
    page_items = media[offset : offset + params.page_size]

    return {
        "ok": True,
        "view": "favorites-media",
        "type": params.media_type,
        "anchor_path": str(media[anchor_position]["rel_path"]),
        "anchor_index": anchor_index,
        "page": page,
        "page_size": params.page_size,
        "total": total,
        "pages": pages,
        "media": page_items,
    }


def search_page(
    config: Config,
    *,
    raw_query: str,
    raw_folder: str,
    raw_content_filter: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return one read-only page of folder and media search results."""
    params = _search_page_params(
        raw_query=raw_query,
        raw_folder=raw_folder,
        raw_content_filter=raw_content_filter,
        raw_page=raw_page,
        raw_page_size=raw_page_size,
    )

    with open_database(config.db_path, read_only=True) as connection:
        branch_key = catalog_path_key(params.folder_rel_path)
        if _available_folder_by_key(connection, branch_key) is None:
            raise ApiError.from_message(
                404,
                "search.folder.unavailable",
                params={"path": params.folder_rel_path or "/"},
            )

        include_folders = params.content_filter in {"all", "folders"}
        include_media = params.content_filter != "folders"
        if include_folders:
            folder_where, folder_values = _search_folder_where(params)
        else:
            folder_where, folder_values = "0", []
        if include_media:
            media_where, media_values = _search_media_where(params)
        else:
            media_where, media_values = "0", []

        folder_total = (
            _count(
                connection,
                f"SELECT COUNT(*) FROM folders WHERE {folder_where}",
                folder_values,
            )
            if include_folders
            else 0
        )
        media_total = (
            _count(
                connection,
                f"""
                SELECT COUNT(*)
                FROM media_files AS media
                JOIN folders AS parent ON parent.id = media.folder_id
                WHERE {media_where}
                """,
                media_values,
            )
            if include_media
            else 0
        )
        total = folder_total + media_total

        rows = connection.execute(
            f"""
            SELECT *
            FROM (
                SELECT
                    'folder' AS kind,
                    0 AS sort_group,
                    id,
                    rel_path,
                    path_key,
                    parent_id,
                    name,
                    depth,
                    NULL AS folder_id,
                    NULL AS file_name,
                    NULL AS extension,
                    NULL AS media_type,
                    NULL AS size_bytes,
                    NULL AS modified_time,
                    last_successful_scan_id,
                    is_available,
                    direct_child_count,
                    direct_image_count,
                    direct_gif_count,
                    direct_video_count,
                    direct_other_count,
                    recursive_folder_count,
                    recursive_image_count,
                    recursive_gif_count,
                    recursive_video_count,
                    recursive_other_count,
                    sort_key
                FROM folders
                WHERE {folder_where}

                UNION ALL

                SELECT
                    'media' AS kind,
                    1 AS sort_group,
                    media.id,
                    media.rel_path,
                    media.path_key,
                    NULL AS parent_id,
                    media.file_name AS name,
                    NULL AS depth,
                    media.folder_id,
                    media.file_name,
                    media.extension,
                    media.media_type,
                    media.size_bytes,
                    media.modified_time,
                    media.last_successful_scan_id,
                    media.is_available,
                    NULL AS direct_child_count,
                    NULL AS direct_image_count,
                    NULL AS direct_gif_count,
                    NULL AS direct_video_count,
                    NULL AS direct_other_count,
                    NULL AS recursive_folder_count,
                    NULL AS recursive_image_count,
                    NULL AS recursive_gif_count,
                    NULL AS recursive_video_count,
                    NULL AS recursive_other_count,
                    media.sort_key
                FROM media_files AS media
                JOIN folders AS parent ON parent.id = media.folder_id
                WHERE {media_where}
            ) AS result
            ORDER BY sort_group, sort_key, name, id
            LIMIT ? OFFSET ?
            """,
            (*folder_values, *media_values, params.page_size, params.offset),
        ).fetchall()

        pages = math.ceil(total / params.page_size) if total else 0
        favorite_path_keys = _favorite_path_key_set(config)
        results = [_search_result_dict(row, favorite_path_keys) for row in rows]
        folder_results = [item for item in results if item["kind"] == "folder"]
        if folder_results:
            previews_by_folder = _folder_preview_items_by_folder(
                config,
                connection,
                [int(item["id"]) for item in folder_results],
            )
            for item in folder_results:
                item["folder_previews"] = previews_by_folder.get(int(item["id"]), [])

        return {
            "ok": True,
            "query": params.query,
            "folder": params.folder_rel_path,
            "type": params.content_filter,
            "page": params.page,
            "page_size": params.page_size,
            "total": total,
            "pages": pages,
            "counts": {
                "folders": folder_total,
                "media": media_total,
            },
            "results": results,
        }


def search_media_page(
    config: Config,
    *,
    raw_query: str,
    raw_folder: str,
    raw_media_type: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return one read-only page of media-only search results for modal navigation."""
    params = _search_page_params(
        raw_query=raw_query,
        raw_folder=raw_folder,
        raw_content_filter=raw_media_type,
        raw_page=raw_page,
        raw_page_size=raw_page_size,
    )

    if params.media_type not in {"image", "gif"}:
        raise ApiError.from_message(
            400,
            "search.media_page.type_unsupported",
            params={"allowed": "image, gif"},
        )

    with open_database(config.db_path, read_only=True) as connection:
        branch_key = catalog_path_key(params.folder_rel_path)
        if _available_folder_by_key(connection, branch_key) is None:
            raise ApiError.from_message(
                404,
                "search.folder.unavailable",
                params={"path": params.folder_rel_path or "/"},
            )

        media_where, media_values = _search_media_where(params)

        total = _count(
            connection,
            f"""
            SELECT COUNT(*)
            FROM media_files AS media
            JOIN folders AS parent ON parent.id = media.folder_id
            WHERE {media_where}
            """,
            media_values,
        )

        rows = connection.execute(
            f"""
            SELECT
                media.id,
                media.rel_path,
                media.path_key,
                media.folder_id,
                media.file_name,
                media.extension,
                media.media_type,
                media.size_bytes,
                media.modified_time,
                media.last_successful_scan_id,
                media.is_available
            FROM media_files AS media
            JOIN folders AS parent ON parent.id = media.folder_id
            WHERE {media_where}
            ORDER BY media.sort_key, media.file_name, media.id
            LIMIT ? OFFSET ?
            """,
            (*media_values, params.page_size, params.offset),
        ).fetchall()

        pages = math.ceil(total / params.page_size) if total else 0

        return {
            "ok": True,
            "view": "search-media",
            "query": params.query,
            "folder": params.folder_rel_path,
            "type": params.media_type,
            "page": params.page,
            "page_size": params.page_size,
            "total": total,
            "pages": pages,
            "media": _annotate_favorite_media(config, [_media_dict(row) for row in rows]),
        }


def search_page_anchor(
    config: Config,
    *,
    raw_query: str,
    raw_folder: str,
    raw_media_type: str,
    raw_path: str,
    raw_page_size: str | None,
) -> dict[str, Any]:
    """Return the media-only search page containing one concrete image/GIF result."""
    params = _search_page_params(
        raw_query=raw_query,
        raw_folder=raw_folder,
        raw_content_filter=raw_media_type,
        raw_page="1",
        raw_page_size=raw_page_size,
    )

    if params.media_type not in {"image", "gif"}:
        raise ApiError.from_message(
            400,
            "search.anchor.type_unsupported",
            params={"allowed": "image, gif"},
        )

    item_rel_path = _normalize_api_path(raw_path, allow_root=False)
    item_key = catalog_path_key(item_rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        branch_key = catalog_path_key(params.folder_rel_path)
        if _available_folder_by_key(connection, branch_key) is None:
            raise ApiError.from_message(
                404,
                "search.folder.unavailable",
                params={"path": params.folder_rel_path or "/"},
            )

        media_where, media_values = _search_media_where(params)

        item = connection.execute(
            f"""
            SELECT
                media.id,
                media.rel_path,
                media.path_key,
                media.folder_id,
                media.file_name,
                media.extension,
                media.media_type,
                media.size_bytes,
                media.modified_time,
                media.sort_key,
                media.last_successful_scan_id,
                media.is_available
            FROM media_files AS media
            JOIN folders AS parent ON parent.id = media.folder_id
            WHERE {media_where}
              AND media.path_key = ?
            """,
            (*media_values, item_key),
        ).fetchone()

        if item is None:
            raise ApiError.from_message(
                404,
                "search.anchor.item_unavailable",
            )

        total = _count(
            connection,
            f"""
            SELECT COUNT(*)
            FROM media_files AS media
            JOIN folders AS parent ON parent.id = media.folder_id
            WHERE {media_where}
            """,
            media_values,
        )

        position = _count(
            connection,
            f"""
            SELECT COUNT(*)
            FROM media_files AS media
            JOIN folders AS parent ON parent.id = media.folder_id
            WHERE {media_where}
              AND (
                media.sort_key < ?
                OR (media.sort_key = ? AND media.file_name < ?)
                OR (media.sort_key = ? AND media.file_name = ? AND media.id <= ?)
              )
            """,
            (
                *media_values,
                str(item["sort_key"]),
                str(item["sort_key"]),
                str(item["file_name"]),
                str(item["sort_key"]),
                str(item["file_name"]),
                int(item["id"]),
            ),
        )

        pages = math.ceil(total / params.page_size) if total else 0
        page = max(1, math.ceil(position / params.page_size)) if position else 1
        offset = (page - 1) * params.page_size
        index = max(0, position - offset - 1)

        rows = connection.execute(
            f"""
            SELECT
                media.id,
                media.rel_path,
                media.path_key,
                media.folder_id,
                media.file_name,
                media.extension,
                media.media_type,
                media.size_bytes,
                media.modified_time,
                media.last_successful_scan_id,
                media.is_available
            FROM media_files AS media
            JOIN folders AS parent ON parent.id = media.folder_id
            WHERE {media_where}
            ORDER BY media.sort_key, media.file_name, media.id
            LIMIT ? OFFSET ?
            """,
            (*media_values, params.page_size, offset),
        ).fetchall()

        return {
            "ok": True,
            "view": "search-media",
            "query": params.query,
            "folder": params.folder_rel_path,
            "type": params.media_type,
            "anchor_path": str(item["rel_path"]),
            "anchor_index": index,
            "page": page,
            "page_size": params.page_size,
            "total": total,
            "pages": pages,
            "media": _annotate_favorite_media(config, [_media_dict(row) for row in rows]),
        }


def favorite_add_action(config: Config, raw_path: str) -> dict[str, Any]:
    """Add one catalog media path to favorites.json."""
    rel_path = _normalize_api_path(raw_path, allow_root=False)
    path_key = catalog_path_key(rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT rel_path
            FROM media_files
            WHERE path_key = ?
            """,
            (path_key,),
        ).fetchone()

    if row is None:
        raise ApiError.from_message(
            404,
            "favorites.item.not_found",
            params={"path": rel_path},
        )

    normalized_rel_path = str(row["rel_path"])
    entries = _favorite_entries_without_path_key(
        _read_favorite_entries(config),
        path_key,
    )
    entries.insert(
        0,
        {
            "path": normalized_rel_path,
            "added_at": _utc_timestamp(),
        },
    )
    _write_favorite_entries(config, entries)

    return {
        "ok": True,
        "action": "favorite-add",
        "path": normalized_rel_path,
        "is_favorite": True,
    }


def favorite_remove_action(config: Config, raw_path: str) -> dict[str, Any]:
    """Remove one catalog path from favorites.json."""
    rel_path = _normalize_api_path(raw_path, allow_root=False)
    path_key = catalog_path_key(rel_path)
    entries = _read_favorite_entries(config)
    kept_entries = _favorite_entries_without_path_key(entries, path_key)
    removed = len(kept_entries) != len(entries)
    _write_favorite_entries(config, kept_entries)

    return {
        "ok": True,
        "action": "favorite-remove",
        "path": rel_path,
        "removed": removed,
        "is_favorite": False,
    }


def original_media_resource(config: Config, raw_path: str) -> OriginalMediaResource:
    """Resolve one available catalog media file to a safe filesystem path."""
    rel_path = _normalize_api_path(raw_path, allow_root=False)
    path_key = catalog_path_key(rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT
                rel_path,
                file_name,
                extension,
                media_type,
                size_bytes,
                modified_time
            FROM media_files
            WHERE path_key = ?
              AND is_available = 1
            """,
            (path_key,),
        ).fetchone()

    if row is None:
        raise ApiError.from_message(
            404,
            "media.catalog_item.unavailable",
            params={"path": rel_path},
        )

    try:
        filesystem_path = safe_join_catalog_path(
            config.data_root,
            rel_path,
            allow_root=False,
        )
    except PathValidationError as exc:
        raise ApiError.from_message(
            400,
            f"request.catalog_path.{exc.reason}",
        ) from exc

    if not filesystem_path.exists():
        raise ApiError.from_message(
            404,
            "media.source_file.unavailable",
            params={"path": rel_path},
        )

    if not filesystem_path.is_file():
        raise ApiError.from_message(
            404,
            "media.source_file.not_file",
            params={"path": rel_path},
        )

    stat_result = filesystem_path.stat()

    return OriginalMediaResource(
        rel_path=str(row["rel_path"]),
        file_name=str(row["file_name"]),
        extension=str(row["extension"]),
        media_type=str(row["media_type"]),
        filesystem_path=filesystem_path,
        size_bytes=int(stat_result.st_size),
        modified_time=float(stat_result.st_mtime),
    )



@dataclass(frozen=True)
class FolderPreviewCacheResource:
    filesystem_path: Path
    file_name: str
    size_bytes: int


def folder_preview_cache_resource(
    config: Config,
    raw_cache_path: str,
) -> FolderPreviewCacheResource:
    """Return one existing WebP from the thumbnail cache without database access."""
    try:
        filesystem_path = thumbnail_cache_filesystem_path(config, raw_cache_path)
    except ThumbnailCacheError as exc:
        raise ApiError.from_message(
            400,
            "request.catalog_path.outside_root",
            payload={"technical_detail": str(exc)},
        ) from exc

    if filesystem_path.suffix.lower() != ".webp":
        raise ApiError.from_message(400, "request.catalog_path.outside_root")
    if not filesystem_path.exists() or not filesystem_path.is_file():
        raise ApiError.from_message(
            404,
            "thumbnail.cache.missing",
            params={"path": raw_cache_path, "variant": "folder_preview"},
        )

    return FolderPreviewCacheResource(
        filesystem_path=filesystem_path,
        file_name=filesystem_path.name,
        size_bytes=int(filesystem_path.stat().st_size),
    )


def thumbnail_media_resource(
    config: Config,
    raw_path: str,
    raw_variant: str = "photo_tile",
    *,
    existing_only: bool = False,
    frame_index: int | None = None,
) -> ThumbnailResource:
    """Resolve a thumbnail and publish phase timings for an active HTTP trace."""
    if not diagnostic_request_active():
        return _thumbnail_media_resource_impl(
            config,
            raw_path,
            raw_variant,
            existing_only=existing_only,
            frame_index=frame_index,
        )

    timings = {
        "media_lookup_ms": 0.0,
        "thumbnail_lookup_ms": 0.0,
        "cache_file_check_ms": 0.0,
    }
    result = "error"
    try:
        resource = _thumbnail_media_resource_impl(
            config,
            raw_path,
            raw_variant,
            existing_only=existing_only,
            frame_index=frame_index,
            diagnostic_timings=timings,
        )
        result = "generated" if resource.generated else "existing"
        return resource
    except ApiError as exc:
        result = str(exc.message_object.get("code") or "api_error")
        raise
    finally:
        diagnostic_set_request_detail(
            "thumbnail",
            {
                "existing_only": bool(existing_only),
                "thumbnail_type": (raw_variant or "photo_tile").strip().lower(),
                "result": result,
                **{key: round(max(0.0, value), 3) for key, value in timings.items()},
            },
        )


def _thumbnail_media_resource_impl(
    config: Config,
    raw_path: str,
    raw_variant: str = "photo_tile",
    *,
    existing_only: bool = False,
    frame_index: int | None = None,
    diagnostic_timings: dict[str, float] | None = None,
) -> ThumbnailResource:
    """Resolve a supported thumbnail resource for a catalog media file."""
    variant = (raw_variant or "photo_tile").strip().lower()
    if variant not in {"photo_tile", "gif_preview", "video_poster", "video_frame"}:
        raise ApiError.from_message(
            400,
            "thumbnail.variant.invalid",
            params={"allowed": "photo_tile, gif_preview, video_poster, video_frame"},
        )

    rel_path = _normalize_api_path(raw_path, allow_root=False)
    path_key = catalog_path_key(rel_path)

    lookup_started = time.perf_counter() if diagnostic_timings is not None else 0.0
    try:
        with open_database(config.db_path, read_only=True, validate=False) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    rel_path,
                    file_name,
                    extension,
                    media_type
                FROM media_files
                WHERE path_key = ?
                  AND is_available = 1
                """,
                (path_key,),
            ).fetchone()
    finally:
        if diagnostic_timings is not None:
            diagnostic_timings["media_lookup_ms"] = (
                time.perf_counter() - lookup_started
            ) * 1000.0

    if row is None:
        raise ApiError.from_message(
            404,
            "media.catalog_item.unavailable",
            params={"path": rel_path},
        )

    media_type = str(row["media_type"])
    if variant == "photo_tile" and media_type != "image":
        raise ApiError.from_message(
            400,
            "thumbnail.photo_tile.photo_required",
        )
    if variant == "gif_preview" and media_type != "gif":
        raise ApiError.from_message(
            400,
            "thumbnail.gif_preview.gif_required",
        )
    if variant == "video_poster" and media_type != "video":
        raise ApiError.from_message(
            400,
            "thumbnail.video_poster.video_required",
        )
    if variant == "video_frame" and media_type != "video":
        raise ApiError.from_message(
            400,
            "thumbnail.video_frame.video_required",
        )
    if variant == "video_frame" and frame_index is None:
        raise ApiError.from_message(
            400,
            "thumbnail.video_frame.frame_required",
            params={"min": 1, "max": 4},
        )
    if variant == "video_frame" and frame_index not in {1, 2, 3, 4}:
        raise ApiError.from_message(
            400,
            "thumbnail.video_frame.frame_invalid",
            params={"min": 1, "max": 4},
        )

    fast_variant_key = "default"
    if variant == "video_frame":
        fast_variant_key = f"frame_{frame_index}"

    try:
        cached_resource = ready_cached_thumbnail_resource(
            config,
            media_id=int(row["id"]),
            thumbnail_type=variant,
            variant_key=fast_variant_key,
            diagnostic_timings=diagnostic_timings,
        )
    except ThumbnailCacheError as exc:
        raise ApiError.from_message(
            500,
            "thumbnail.cache.resolve_failed",
            params={"path": rel_path, "variant": variant},
            payload={"technical_detail": str(exc)},
        ) from exc

    if cached_resource is not None:
        return cached_resource

    if existing_only:
        raise ApiError.from_message(
            404,
            "thumbnail.cache.missing",
            params={"path": rel_path, "variant": variant},
        )

    try:
        filesystem_path = safe_join_catalog_path(
            config.data_root,
            rel_path,
            allow_root=False,
        )
    except PathValidationError as exc:
        raise ApiError.from_message(
            400,
            f"request.catalog_path.{exc.reason}",
        ) from exc

    if not filesystem_path.exists():
        raise ApiError.from_message(
            404,
            "media.source_file.unavailable",
            params={"path": rel_path},
        )

    if not filesystem_path.is_file():
        raise ApiError.from_message(
            404,
            "media.source_file.not_file",
            params={"path": rel_path},
        )

    stat_result = filesystem_path.stat()

    try:
        if variant == "photo_tile":
            return photo_tile_resource(
                config,
                media_id=int(row["id"]),
                rel_path=str(row["rel_path"]),
                source_path=filesystem_path,
                source_size_bytes=int(stat_result.st_size),
                source_modified_time=float(stat_result.st_mtime),
            )

        if variant == "gif_preview":
            resource = gif_preview_existing_resource(
                config,
                media_id=int(row["id"]),
                source_size_bytes=int(stat_result.st_size),
                source_modified_time=float(stat_result.st_mtime),
            )
            if resource is None:
                raise ApiError.from_message(
                    404,
                    "thumbnail.gif_preview.missing",
                    params={"path": rel_path},
                )
            return resource

        if variant == "video_frame":
            resource = video_frame_existing_resource(
                config,
                media_id=int(row["id"]),
                frame_index=int(frame_index),
                source_size_bytes=int(stat_result.st_size),
                source_modified_time=float(stat_result.st_mtime),
            )
            if resource is None:
                raise ApiError.from_message(
                    404,
                    "thumbnail.video_frame.missing",
                    params={"path": rel_path, "frame": int(frame_index)},
                )
            return resource

        if existing_only:
            resource = video_poster_existing_resource(
                config,
                media_id=int(row["id"]),
                source_size_bytes=int(stat_result.st_size),
                source_modified_time=float(stat_result.st_mtime),
            )
            if resource is None:
                raise ApiError.from_message(
                    404,
                    "thumbnail.video_poster.missing",
                    params={"path": rel_path},
                )
            return resource

        return video_poster_resource(
            config,
            media_id=int(row["id"]),
            rel_path=str(row["rel_path"]),
            source_path=filesystem_path,
            source_size_bytes=int(stat_result.st_size),
            source_modified_time=float(stat_result.st_mtime),
        )
    except ThumbnailCacheError as exc:
        raise ApiError.from_message(
            500,
            "thumbnail.generation.failed",
            params={"path": rel_path, "variant": variant},
            payload={"technical_detail": str(exc)},
        ) from exc



def folder_open_resource(config: Config, raw_path: str) -> FolderOpenResource:
    """Resolve an available catalog folder or media parent folder for opening."""
    rel_path = _normalize_api_path(raw_path, allow_root=True)
    path_key = catalog_path_key(rel_path)

    with open_database(config.db_path, read_only=True) as connection:
        folder = connection.execute(
            """
            SELECT rel_path
            FROM folders
            WHERE path_key = ?
              AND is_available = 1
            """,
            (path_key,),
        ).fetchone()

    if folder is not None:
        folder_rel_path = str(folder["rel_path"])
        try:
            filesystem_path = safe_join_catalog_path(
                config.data_root,
                folder_rel_path,
                allow_root=True,
            )
        except PathValidationError as exc:
            raise ApiError.from_message(
                400,
                f"request.catalog_path.{exc.reason}",
            ) from exc

        display_path = folder_rel_path or "/"
        if not filesystem_path.exists():
            raise ApiError.from_message(
                404,
                "folder.source.unavailable",
                params={"path": display_path},
            )
        if not filesystem_path.is_dir():
            raise ApiError.from_message(
                404,
                "folder.source.not_directory",
                params={"path": display_path},
            )

        return FolderOpenResource(
            rel_path=folder_rel_path,
            filesystem_path=filesystem_path,
        )

    if not rel_path:
        raise ApiError.from_message(
            404,
            "folder.catalog_root.unavailable",
        )

    try:
        folder_filesystem_path = safe_join_catalog_path(
            config.data_root,
            rel_path,
            allow_root=True,
        )
    except PathValidationError as exc:
        raise ApiError.from_message(
            400,
            f"request.catalog_path.{exc.reason}",
        ) from exc

    if folder_filesystem_path.exists() and folder_filesystem_path.is_dir():
        return FolderOpenResource(
            rel_path=rel_path,
            filesystem_path=folder_filesystem_path,
        )

    media = original_media_resource(config, rel_path)
    parent_path = media.filesystem_path.parent
    if not parent_path.exists() or not parent_path.is_dir():
        raise ApiError.from_message(
            404,
            "folder.media_parent.unavailable",
            params={"path": media.rel_path},
        )

    parent_rel_path = str(Path(media.rel_path).parent).replace("\\", "/")
    if parent_rel_path == ".":
        parent_rel_path = ""

    return FolderOpenResource(
        rel_path=parent_rel_path,
        filesystem_path=parent_path,
    )


def open_original_action(config: Config, raw_path: str) -> dict[str, Any]:
    """Open one available catalog media file with the operating system."""
    resource = original_media_resource(config, raw_path)
    _open_filesystem_path(resource.filesystem_path)
    return {
        "ok": True,
        "action": "open-original",
        "rel_path": resource.rel_path,
    }


def open_folder_action(config: Config, raw_path: str) -> dict[str, Any]:
    """Open an available catalog folder, or a media file's parent folder."""
    resource = folder_open_resource(config, raw_path)
    _open_filesystem_path(resource.filesystem_path)
    return {
        "ok": True,
        "action": "open-folder",
        "rel_path": resource.rel_path,
    }


def _current_scan_job_summary(
    *,
    latest_status: str | None,
    lock_held: bool | None,
) -> dict[str, Any]:
    if lock_held is True:
        state = "running"
        source = "scan-lock"
    elif lock_held is None and latest_status == "running":
        state = "running-lock-unknown"
        source = "scan-session"
    elif latest_status == "running":
        state = "stale-running-record"
        source = "scan-session"
    else:
        state = "idle"
        source = "none"

    return {
        "kind": "scan",
        "state": state,
        "is_running": state in {"running", "running-lock-unknown"},
        "source": source,
    }


def _scan_lock_status(lock_path: Path) -> dict[str, Any]:
    exists = lock_path.exists()
    metadata_text = None
    metadata: dict[str, str] = {}

    if exists and lock_path.is_file():
        try:
            metadata_text = lock_path.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            metadata_text = None
        metadata = _parse_lock_metadata(metadata_text or "")

    held, check_error = _scan_lock_held(lock_path) if exists else (False, None)

    return {
        "name": lock_path.name,
        "exists": exists,
        "held": held,
        "metadata": metadata,
        "metadata_text": metadata_text,
        "check_error": check_error,
    }


def _parse_lock_metadata(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in text.split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key:
            result[key] = value
    return result


def _scan_lock_held(lock_path: Path) -> tuple[bool | None, str | None]:
    if not lock_path.exists() or not lock_path.is_file():
        return False, None

    try:
        with lock_path.open("r+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True, None

                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                return False, None

            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True, None

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            return False, None
    except OSError as exc:
        return None, str(exc)


def _unix_time_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat()


def _open_filesystem_path(path: Path) -> None:
    """Ask the local operating system to open a validated file or folder."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise ApiError.from_message(
            500,
            "filesystem.open.failed",
            params={"path": str(path)},
            payload={"technical_detail": str(exc)},
        ) from exc


def _normalize_api_path(raw_path: str, *, allow_root: bool) -> str:
    try:
        return normalize_catalog_relative_path(raw_path, allow_root=allow_root)
    except PathValidationError as exc:
        raise ApiError.from_message(
            400,
            f"request.catalog_path.{exc.reason}",
        ) from exc


def _folder_page_params(
    config: Config,
    *,
    raw_parent: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> FolderPageParams:
    parent_rel_path = _normalize_api_path(raw_parent, allow_root=True)
    page = _positive_int(raw_page or "1", field_name="page")

    if raw_page_size is None or raw_page_size == "":
        page_size = config.folder_page_size
    else:
        page_size = _positive_int(raw_page_size, field_name="page_size")

    if page_size > 500:
        raise ApiError.from_message(
            400,
            "pagination.page_size.too_large",
            params={"max": 500},
        )

    return FolderPageParams(
        parent_rel_path=parent_rel_path,
        page=page,
        page_size=page_size,
        offset=(page - 1) * page_size,
    )


def _media_page_params(
    config: Config,
    *,
    raw_folder: str,
    raw_media_type: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> MediaPageParams:
    folder_rel_path = _normalize_api_path(raw_folder, allow_root=True)
    media_type = (raw_media_type or "all").strip().lower()

    if media_type not in {"all", "image", "gif", "video", "other"}:
        raise ApiError.from_message(
            400,
            "navigation.media_type.invalid",
            params={"allowed": "all, image, gif, video, other"},
        )

    page = _positive_int(raw_page or "1", field_name="page")

    if raw_page_size is None or raw_page_size == "":
        page_size = _default_page_size(config, media_type)
    else:
        page_size = _positive_int(raw_page_size, field_name="page_size")

    if page_size > 200:
        raise ApiError.from_message(
            400,
            "pagination.page_size.too_large",
            params={"max": 200},
        )

    return MediaPageParams(
        folder_rel_path=folder_rel_path,
        media_type=media_type,
        page=page,
        page_size=page_size,
        offset=(page - 1) * page_size,
    )


def _favorite_page_params(
    config: Config,
    *,
    raw_media_type: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> FavoritePageParams:
    media_type = (raw_media_type or "all").strip().lower()

    if media_type not in {"all", "image", "gif", "video", "other"}:
        raise ApiError.from_message(
            400,
            "navigation.media_type.invalid",
            params={"allowed": "all, image, gif, video, other"},
        )

    page = _positive_int(raw_page or "1", field_name="page")

    if raw_page_size is None or raw_page_size == "":
        page_size = _default_page_size(config, media_type)
    else:
        page_size = _positive_int(raw_page_size, field_name="page_size")

    if page_size > 200:
        raise ApiError.from_message(
            400,
            "pagination.page_size.too_large",
            params={"max": 200},
        )

    return FavoritePageParams(
        media_type=media_type,
        page=page,
        page_size=page_size,
        offset=(page - 1) * page_size,
    )


def _search_page_params(
    *,
    raw_query: str,
    raw_folder: str,
    raw_content_filter: str,
    raw_page: str | None,
    raw_page_size: str | None,
) -> SearchPageParams:
    query = (raw_query or "").strip()
    if not query:
        raise ApiError.from_message(
            400,
            "search.query.required",
        )
    if len(query) > 200:
        raise ApiError.from_message(
            400,
            "search.query.too_long",
            params={"max": 200},
        )

    folder_rel_path = _normalize_api_path(raw_folder, allow_root=True)
    content_filter = (raw_content_filter or "all").strip().lower()

    if content_filter not in {"all", "folders", "image", "gif", "video", "other"}:
        raise ApiError.from_message(
            400,
            "navigation.media_type.invalid",
            params={"allowed": "all, folders, image, gif, video, other"},
        )

    page = _positive_int(raw_page or "1", field_name="page")

    if raw_page_size is None or raw_page_size == "":
        page_size = 50
    else:
        page_size = _positive_int(raw_page_size, field_name="page_size")

    if page_size > 200:
        raise ApiError.from_message(
            400,
            "pagination.page_size.too_large",
            params={"max": 200},
        )

    return SearchPageParams(
        query=query,
        query_pattern=_like_contains_pattern(catalog_path_key(query)),
        folder_rel_path=folder_rel_path,
        content_filter=content_filter,
        media_type=None if content_filter == "folders" else content_filter,
        page=page,
        page_size=page_size,
        offset=(page - 1) * page_size,
    )


def _search_folder_where(params: SearchPageParams) -> tuple[str, list[object]]:
    where = "is_available = 1 AND path_key LIKE ? ESCAPE '\\'"
    values: list[object] = [params.query_pattern]

    branch_key = catalog_path_key(params.folder_rel_path)
    if branch_key:
        where += " AND (path_key = ? OR path_key LIKE ? ESCAPE '\\')"
        values.extend([branch_key, _like_prefix_pattern(branch_key + "/")])

    return where, values


def _search_media_where(params: SearchPageParams) -> tuple[str, list[object]]:
    where = """
        media.is_available = 1
        AND parent.is_available = 1
        AND (
            media.path_key LIKE ? ESCAPE '\\'
            OR media.media_type LIKE ? ESCAPE '\\'
        )
    """
    values: list[object] = [params.query_pattern, params.query_pattern]

    if params.media_type != "all":
        where += " AND media.media_type = ?"
        values.append(params.media_type)

    branch_key = catalog_path_key(params.folder_rel_path)
    if branch_key:
        where += " AND (parent.path_key = ? OR parent.path_key LIKE ? ESCAPE '\\')"
        values.extend([branch_key, _like_prefix_pattern(branch_key + "/")])

    return where, values


def _like_contains_pattern(value: str) -> str:
    return f"%{_escape_like(value)}%"


def _like_prefix_pattern(value: str) -> str:
    return f"{_escape_like(value)}%"


def _escape_like(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _default_page_size(config: Config, media_type: str) -> int:
    if media_type == "all":
        return config.all_page_size
    if media_type == "image":
        return config.photo_page_size
    if media_type == "gif":
        return config.gif_page_size
    if media_type == "video":
        return config.video_page_size
    if media_type == "other":
        return config.other_page_size
    return config.all_page_size


def _positive_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError.from_message(
            400,
            "request.integer.invalid",
            params={"field": field_name},
        ) from exc

    if parsed <= 0:
        raise ApiError.from_message(
            400,
            "request.integer.positive_required",
            params={"field": field_name},
        )

    return parsed


def _available_folder_by_key(
    connection: sqlite3.Connection,
    path_key: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, rel_path, name
        FROM folders
        WHERE path_key = ?
          AND is_available = 1
        """,
        (path_key,),
    ).fetchone()


def _breadcrumb(
    connection: sqlite3.Connection,
    folder_id: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        WITH RECURSIVE ancestors(id, rel_path, name, parent_id, depth) AS (
            SELECT id, rel_path, name, parent_id, depth
            FROM folders
            WHERE id = ?
              AND is_available = 1

            UNION ALL

            SELECT f.id, f.rel_path, f.name, f.parent_id, f.depth
            FROM folders AS f
            JOIN ancestors AS a ON a.parent_id = f.id
            WHERE f.is_available = 1
        )
        SELECT rel_path, name, depth
        FROM ancestors
        ORDER BY depth
        """,
        (folder_id,),
    ).fetchall()

    return [
        {
            "rel_path": str(row["rel_path"]),
            "name": _display_folder_name(row),
        }
        for row in rows
    ]


def _synthetic_root_folder_dict(*, direct_folder_count: int) -> dict[str, Any]:
    return {
        "id": None,
        "rel_path": "",
        "name": "Hlavní stránka",
        "depth": 0,
        "last_successful_scan_id": None,
        "is_active_catalog_folder": False,
        "is_disk_candidate": False,
        "direct": {
            "folders": direct_folder_count,
            "images": 0,
            "gifs": 0,
            "videos": 0,
            "other": 0,
        },
        "recursive": {
            "folders": direct_folder_count,
            "images": 0,
            "gifs": 0,
            "videos": 0,
            "other": 0,
        },
    }


def _synthetic_root_breadcrumb_item() -> dict[str, Any]:
    return {
        "rel_path": "",
        "name": "Hlavní stránka",
    }


def _display_folder_name(row: sqlite3.Row) -> str:
    return "Hlavní stránka" if int(row["depth"]) == 0 else str(row["name"])


def _folder_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "rel_path": str(row["rel_path"]),
        "name": _display_folder_name(row),
        "depth": int(row["depth"]),
        "last_successful_scan_id": int(row["last_successful_scan_id"]),
        "is_active_catalog_folder": True,
        "is_disk_candidate": False,
        "direct": {
            "folders": int(row["direct_child_count"]),
            "images": int(row["direct_image_count"]),
            "gifs": int(row["direct_gif_count"]),
            "videos": int(row["direct_video_count"]),
            "other": int(row["direct_other_count"]),
        },
        "recursive": {
            "folders": int(row["recursive_folder_count"]),
            "images": int(row["recursive_image_count"]),
            "gifs": int(row["recursive_gif_count"]),
            "videos": int(row["recursive_video_count"]),
            "other": int(row["recursive_other_count"]),
        },
    }


def _media_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": int(row["id"]),
        "rel_path": str(row["rel_path"]),
        "file_name": str(row["file_name"]),
        "extension": str(row["extension"]),
        "media_type": str(row["media_type"]),
        "is_html_playable": (
            str(row["media_type"]) == "video"
            and is_html_playable_video(str(row["extension"]))
        ),
        "size_bytes": int(row["size_bytes"]),
        "modified_time": float(row["modified_time"]),
        "last_successful_scan_id": int(row["last_successful_scan_id"]),
        "is_available": bool(row["is_available"]) if "is_available" in keys else True,
        "is_favorite": False,
        "favorite_added_at": None,
    }


def _search_result_dict(
    row: sqlite3.Row,
    favorite_path_keys: set[str],
) -> dict[str, Any]:
    kind = str(row["kind"])

    if kind == "folder":
        item = _folder_dict(row)
        item["kind"] = "folder"
        return item

    if kind == "media":
        item = _media_dict(row)
        item["kind"] = "media"
        item["is_favorite"] = str(row["path_key"]) in favorite_path_keys
        return item

    raise ApiError.from_message(
        500,
        "search.result.kind_unknown",
        params={"kind": kind},
    )


def _read_favorite_entries(config: Config) -> list[dict[str, str]]:
    path = config.favorites_json
    if not path.exists():
        return []
    if not path.is_file():
        raise ApiError.from_message(
            500,
            "favorites.storage.not_file",
            params={"path": str(path)},
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiError.from_message(
            500,
            "favorites.storage.invalid_json",
            params={"path": str(path)},
            payload={"technical_detail": str(exc)},
        ) from exc

    if not isinstance(payload, dict):
        raise ApiError.from_message(
            500,
            "favorites.storage.root_not_object",
        )

    raw_entries = payload.get("favorites", [])
    if not isinstance(raw_entries, list):
        raise ApiError.from_message(
            500,
            "favorites.storage.entries_not_list",
        )

    entries: list[dict[str, str]] = []
    seen_path_keys: set[str] = set()

    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ApiError.from_message(
                500,
                "favorites.storage.entry_invalid",
            )

        raw_path = raw_entry.get("path")
        if not isinstance(raw_path, str):
            raise ApiError.from_message(
                500,
                "favorites.storage.path_not_string",
            )

        rel_path = _normalize_api_path(raw_path, allow_root=False)
        path_key = catalog_path_key(rel_path)
        if path_key in seen_path_keys:
            continue

        added_at = raw_entry.get("added_at")
        if not isinstance(added_at, str) or not added_at:
            added_at = ""

        entries.append({"path": rel_path, "added_at": added_at})
        seen_path_keys.add(path_key)

    return entries


def _favorite_entries_with_media_path_renamed(
    entries: list[dict[str, str]],
    *,
    old_path_key: str,
    new_rel_path: str,
) -> list[dict[str, str]]:
    """Return favorites entries with one media path renamed and duplicates removed."""
    result: list[dict[str, str]] = []
    seen_path_keys: set[str] = set()

    for entry in entries:
        path = str(entry.get("path") or "")
        added_at = str(entry.get("added_at") or "")
        path_key = catalog_path_key(path)
        next_path = new_rel_path if path_key == old_path_key else path
        next_key = catalog_path_key(next_path)
        if next_key in seen_path_keys:
            continue
        result.append({"path": next_path, "added_at": added_at})
        seen_path_keys.add(next_key)

    return result


def _favorite_entries_with_folder_branch_renamed(
    entries: list[dict[str, str]],
    *,
    old_rel_path: str,
    old_path_key: str,
    new_rel_path: str,
) -> list[dict[str, str]]:
    """Return favorites entries with a renamed folder branch path prefix."""
    old_prefix_key = old_path_key + "/"
    old_prefix_text = old_rel_path + "/"
    new_prefix_text = new_rel_path + "/"
    old_part_count = len(PurePosixPath(old_rel_path).parts)
    result: list[dict[str, str]] = []
    seen_path_keys: set[str] = set()

    for entry in entries:
        path = str(entry.get("path") or "")
        added_at = str(entry.get("added_at") or "")
        path_key = catalog_path_key(path)

        if path_key.startswith(old_prefix_key):
            if path.startswith(old_prefix_text):
                suffix = path[len(old_prefix_text):]
            else:
                suffix_parts = PurePosixPath(path).parts[old_part_count:]
                suffix = "/".join(suffix_parts)
            next_path = new_prefix_text + suffix if suffix else new_rel_path
        else:
            next_path = path

        next_key = catalog_path_key(next_path)
        if next_key in seen_path_keys:
            continue
        result.append({"path": next_path, "added_at": added_at})
        seen_path_keys.add(next_key)

    return result


def _write_favorite_entries(config: Config, entries: list[dict[str, str]]) -> None:
    payload = {
        "version": 1,
        "favorites": entries,
    }
    target_path = config.favorites_json
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_name(target_path.name + ".tmp")

    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, target_path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ApiError.from_message(
            500,
            "favorites.storage.write_failed",
            params={"path": str(target_path)},
            payload={"technical_detail": str(exc)},
        ) from exc


def _favorite_media_items(
    config: Config,
    entries: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if not entries:
        return []

    path_keys = [catalog_path_key(entry["path"]) for entry in entries]
    placeholders = ", ".join("?" for _ in path_keys)

    with open_database(config.db_path, read_only=True) as connection:
        rows = connection.execute(
            f"""
            SELECT
                id,
                rel_path,
                path_key,
                folder_id,
                file_name,
                extension,
                media_type,
                size_bytes,
                modified_time,
                last_successful_scan_id,
                is_available
            FROM media_files
            WHERE path_key IN ({placeholders})
            """,
            path_keys,
        ).fetchall()

    rows_by_path_key = {str(row["path_key"]): row for row in rows}
    result: list[dict[str, Any]] = []

    for entry in entries:
        rel_path = entry["path"]
        row = rows_by_path_key.get(catalog_path_key(rel_path))

        if row is None:
            item = _missing_favorite_media_dict(rel_path)
        else:
            item = _media_dict(row)

        item["is_favorite"] = True
        item["favorite_added_at"] = entry["added_at"] or None
        result.append(item)

    return result


def _missing_favorite_media_dict(rel_path: str) -> dict[str, Any]:
    extension = normalize_extension(rel_path)
    return {
        "id": None,
        "rel_path": rel_path,
        "file_name": Path(rel_path).name,
        "extension": extension,
        "media_type": classify_media(extension),
        "is_html_playable": classify_media(extension) == "video" and is_html_playable_video(extension),
        "size_bytes": 0,
        "modified_time": 0.0,
        "last_successful_scan_id": None,
        "is_available": False,
        "is_favorite": True,
        "favorite_added_at": None,
    }




def _favorite_modal_media_items(
    config: Config,
    media_type: str,
) -> list[dict[str, Any]]:
    """Return available favorite image/GIF items in favorites.json order."""
    entries = _read_favorite_entries(config)
    media = _favorite_media_items(config, entries)
    return [
        item
        for item in media
        if item["media_type"] == media_type and item.get("is_available") is not False
    ]


def _favorite_path_key_set(config: Config) -> set[str]:
    return {catalog_path_key(entry["path"]) for entry in _read_favorite_entries(config)}


def _favorite_entries_without_path_key(
    entries: list[dict[str, str]],
    path_key: str,
) -> list[dict[str, str]]:
    return [
        entry
        for entry in entries
        if catalog_path_key(entry["path"]) != path_key
    ]


def _favorite_entries_without_branch_path_key(
    entries: list[dict[str, str]],
    branch_path_key: str,
) -> list[dict[str, str]]:
    prefix = branch_path_key + "/"
    return [
        entry
        for entry in entries
        if not catalog_path_key(entry["path"]).startswith(prefix)
    ]


def _annotate_favorite_media(
    config: Config,
    media: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    favorite_path_keys = _favorite_path_key_set(config)
    for item in media:
        item["is_favorite"] = catalog_path_key(item["rel_path"]) in favorite_path_keys
    return media


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _count(
    connection: sqlite3.Connection,
    sql: str,
    values: list[object] | tuple[object, ...] = (),
) -> int:
    return int(connection.execute(sql, values).fetchone()[0])

from __future__ import annotations

import json
import mimetypes
from html import escape
import os
import shutil
import socket
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .api import (
    ApiError,
    catalog_status,
    child_folders,
    favorite_add_action,
    favorite_remove_action,
    favorites_media_page,
    favorites_page,
    favorites_page_anchor,
    folder_detail,
    folder_preview_cache_resource,
    folder_preview_build_tree_plan_page,
    missing_folder_delete_plan_page,
    missing_folder_delete_execute_action,
    job_status,
    media_page,
    media_page_anchor,
    open_folder_action,
    open_original_action,
    original_media_resource,
    protected_cache_orphan_audit_status,
    protected_cache_orphan_cleanup_plan_status,
    protected_cache_orphan_cleanup_plan_status_with_candidate_plan,
    protected_cache_orphan_cleanup_execute_action,
    search_media_page,
    search_page,
    search_page_anchor,
    safe_folder_rename_execute_action,
    safe_folder_rename_plan_page,
    safe_media_rename_execute_action,
    safe_media_rename_plan_page,
    runtime_ui_locale_status,
    source_root_setting_verify,
    thumbnail_cache_status,
    thumbnail_media_resource,
    video_tools_status_page,
)
from .config import (
    Config,
    ConfigError,
    save_runtime_page_sizes,
    save_runtime_thumbnail_cache_limit,
    save_runtime_thumbnail_video_params,
    reset_runtime_thumbnail_video_params,
    save_runtime_data_root,
    save_runtime_ui_locale,
    save_runtime_ui_theme,
    save_runtime_catalog_title,
)
from .database import validate_database_runtime
from .diagnostics import (
    diagnostic_http_handler,
    diagnostic_record,
    diagnostics_output_lines,
    finish_diagnostics_session,
    get_diagnostics_session,
)
from .instance_runtime import (
    INSTANCE_PROTOCOL_VERSION,
    InstanceProcessLock,
    build_runtime_info,
    instance_id_for_config,
    instance_lock_path,
    remove_runtime_info,
    runtime_info_path,
    write_runtime_info,
)
from .jobs import JobBusyError, JobManager
from .message_contract import build_backend_message
from .scanner import ScannerError


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class ServerError(RuntimeError):
    """Raised when the local HTTP server cannot start."""


class CatalogHTTPServer(ThreadingHTTPServer):
    """HTTP server that requires exclusive ownership of its local port."""

    # POSIX needs SO_REUSEADDR for immediate restart after shutdown. Windows
    # instead requires SO_EXCLUSIVEADDRUSE so a second process cannot bind the
    # same address and port while this server is running.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt":
            exclusive_option = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive_option is None:
                raise OSError("Windows exclusive socket binding is unavailable.")
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive_option, 1)
        super().server_bind()


def is_client_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return True

    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror in {10053, 10054, 10058}:
            return True
        if getattr(exc, "errno", None) in {32, 104}:
            return True

    return False


def api_message_error(
    status_code: int,
    code: str,
    *,
    params: dict[str, Any] | None = None,
    technical_detail: str | None = None,
) -> ApiError:
    payload: dict[str, Any] = {}
    if technical_detail:
        payload["technical_detail"] = technical_detail
    return ApiError.from_message(
        status_code,
        code,
        params=params,
        payload=payload,
    )


def api_message_payload(
    code: str,
    *,
    params: dict[str, Any] | None = None,
    technical_detail: str | None = None,
) -> dict[str, Any]:
    message_object = build_backend_message(code, severity="error", params=params)
    payload: dict[str, Any] = {
        "ok": False,
        "code": message_object["code"],
        "message_object": message_object,
    }
    if technical_detail:
        payload["technical_detail"] = technical_detail
    return payload


INDEX_TEMPLATE_REPLACEMENTS = {
    "__CATALOG2_UI_LOCALE__": 1,
    "__CATALOG2_UI_THEME__": 1,
    "__CATALOG2_GALLERY_DENSITY__": 1,
    "__CATALOG2_CATALOG_TITLE__": 2,
    "__CATALOG2_RUNTIME_BOOTSTRAP__": 1,
}


def startup_runtime_settings(config: Config) -> dict[str, Any]:
    """Return the trusted runtime UI values embedded in the first HTML response."""
    return {
        "ui_locale": config.ui_locale,
        "ui_locale_source": config.ui_locale_source,
        "ui_theme": config.ui_theme,
        "ui_theme_source": config.ui_theme_source,
        "gallery_density": config.gallery_density,
        "gallery_density_source": config.gallery_density_source,
        "catalog_title": config.catalog_title,
        "catalog_title_source": config.catalog_title_source,
    }


def render_index_html(template: str, config: Config) -> str:
    """Render index.html with runtime UI settings before the browser first paints it."""
    for token, expected_count in INDEX_TEMPLATE_REPLACEMENTS.items():
        actual_count = template.count(token)
        if actual_count != expected_count:
            raise RuntimeError(
                f"index.html runtime token {token!r} occurs {actual_count} times; "
                f"expected {expected_count}."
            )

    runtime = startup_runtime_settings(config)
    bootstrap_json = json.dumps(
        runtime,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # JSON is placed in a script[type=application/json] element. Escape HTML-
    # significant characters so a catalog title cannot terminate that element.
    bootstrap_json = (
        bootstrap_json
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )

    rendered = template
    rendered = rendered.replace(
        "__CATALOG2_UI_LOCALE__",
        escape(str(runtime["ui_locale"]), quote=True),
    )
    rendered = rendered.replace(
        "__CATALOG2_UI_THEME__",
        escape(str(runtime["ui_theme"]), quote=True),
    )
    rendered = rendered.replace(
        "__CATALOG2_GALLERY_DENSITY__",
        escape(str(runtime["gallery_density"]), quote=True),
    )
    rendered = rendered.replace(
        "__CATALOG2_CATALOG_TITLE__",
        escape(str(runtime["catalog_title"]), quote=True),
    )
    rendered = rendered.replace("__CATALOG2_RUNTIME_BOOTSTRAP__", bootstrap_json)
    return rendered


class CatalogRequestHandler(BaseHTTPRequestHandler):
    """Minimal local HTTP handler for Catalog 2.0 UI and read-only API."""

    server_version = "Catalog2HTTP/0.1"

    @diagnostic_http_handler("GET")
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path).rstrip("/") or "/"

            if path == "/":
                self._send_index_html()
                return

            if path.startswith("/static/"):
                self._send_static_asset(path.removeprefix("/static/"))
                return

            if path == "/media/original":
                query = parse_qs(parsed.query, keep_blank_values=True)
                self._send_original_media(
                    _single_query_value(query, "path", default="")
                )
                return

            if path == "/media/thumbnail":
                query = parse_qs(parsed.query, keep_blank_values=True)
                variant = _single_query_value(
                    query,
                    "variant",
                    default=_single_query_value(query, "type", default="photo_tile"),
                )
                self._send_thumbnail_media(
                    _single_query_value(query, "path", default=""),
                    variant,
                    existing_only=_query_flag_value(query, "existing_only"),
                    frame_index=_optional_int_query_value(query, "frame"),
                )
                return

            if path == "/media/folder-preview":
                query = parse_qs(parsed.query, keep_blank_values=True)
                self._send_folder_preview_media(
                    _single_query_value(query, "path", default="")
                )
                return

            payload, status_code = self._handle_get(parsed=parsed)
            self._send_json(payload, status_code)
        except ApiError as exc:
            self._send_json(exc.to_payload(), exc.status_code)
        except ScannerError as exc:
            self._send_json(
                api_message_payload(
                    "scan.scope.unavailable",
                    technical_detail=str(exc),
                ),
                HTTPStatus.CONFLICT,
            )
        except Exception as exc:
            if is_client_disconnect(exc):
                return
            self._send_json(
                api_message_payload(
                    "server.internal_error",
                    technical_detail=f"{exc.__class__.__name__}: {exc}",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


    @diagnostic_http_handler("POST")
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/api/diagnostics/events":
                session = get_diagnostics_session()
                if session is None:
                    raise api_message_error(404, "server.endpoint.unknown", params={"path": path})
                query = parse_qs(parsed.query, keep_blank_values=True)
                token = _single_query_value(query, "token", default="")
                raw_body = self._read_raw_body(max_bytes=512 * 1024)
                try:
                    accepted = session.accept_frontend_events(token=token, raw_body=raw_body)
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise api_message_error(400, "request.json.invalid", technical_detail=str(exc)) from exc
                self._send_json({"ok": True, "accepted": accepted}, HTTPStatus.ACCEPTED)
                return

            if path == "/api/open-original":
                raw_path = self._request_path_value(parsed=parsed, allow_missing=False)
                self._send_json(open_original_action(self.config, raw_path), HTTPStatus.OK)
                return

            if path == "/api/open-folder":
                raw_path = self._request_path_value(parsed=parsed, allow_missing=True)
                self._send_json(open_folder_action(self.config, raw_path), HTTPStatus.OK)
                return

            if path == "/api/favorites/add":
                raw_path = self._request_path_value(parsed=parsed, allow_missing=False)
                self._send_json(favorite_add_action(self.config, raw_path), HTTPStatus.OK)
                return

            if path == "/api/favorites/remove":
                raw_path = self._request_path_value(parsed=parsed, allow_missing=False)
                self._send_json(favorite_remove_action(self.config, raw_path), HTTPStatus.OK)
                return

            if path == "/api/folder/missing-delete-execute":
                raw_path = self._request_path_value(parsed=parsed, allow_missing=False)
                self._send_json(missing_folder_delete_execute_action(self.config, raw_path), HTTPStatus.OK)
                return

            if path == "/api/media/rename/plan":
                raw_media_id, raw_new_name = self._request_media_rename_plan_values(parsed=parsed)
                self._send_json(
                    safe_media_rename_plan_page(self.config, raw_media_id, raw_new_name),
                    HTTPStatus.OK,
                )
                return

            if path == "/api/media/rename/execute":
                raw_media_id, raw_new_name = self._request_media_rename_plan_values(parsed=parsed)
                self._send_json(
                    safe_media_rename_execute_action(self.config, raw_media_id, raw_new_name),
                    HTTPStatus.OK,
                )
                return

            if path == "/api/folders/rename/plan":
                raw_folder_id, raw_new_name = self._request_folder_rename_plan_values(parsed=parsed)
                self._send_json(
                    safe_folder_rename_plan_page(self.config, raw_folder_id, raw_new_name),
                    HTTPStatus.OK,
                )
                return

            if path == "/api/folders/rename/execute":
                raw_folder_id, raw_new_name = self._request_folder_rename_plan_values(parsed=parsed)
                self._send_json(
                    safe_folder_rename_execute_action(self.config, raw_folder_id, raw_new_name),
                    HTTPStatus.OK,
                )
                return



            if path == "/api/settings/runtime/ui-locale":
                raw_locale = self._request_text_value(
                    parsed=parsed,
                    name="ui_locale",
                )
                try:
                    new_config = save_runtime_ui_locale(self.config, raw_locale)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.locale.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = runtime_ui_locale_status(new_config)
                payload["action"] = "runtime-ui-locale-saved"
                payload["writes"] = {
                    "settings_json": True,
                    "config_json": False,
                    "catalog_db": False,
                    "cache": False,
                    "source_data": False,
                }
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/ui-theme":
                raw_theme = self._request_text_value(
                    parsed=parsed,
                    name="ui_theme",
                )
                try:
                    new_config = save_runtime_ui_theme(self.config, raw_theme)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.theme.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = runtime_ui_locale_status(new_config)
                payload["action"] = "runtime-ui-theme-saved"
                payload["writes"] = {
                    "settings_json": True,
                    "config_json": False,
                    "catalog_db": False,
                    "cache": False,
                    "source_data": False,
                }
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/catalog-title":
                raw_title = self._request_text_value(
                    parsed=parsed,
                    name="catalog_title",
                )
                try:
                    new_config = save_runtime_catalog_title(self.config, raw_title)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.catalog_title.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = runtime_ui_locale_status(new_config)
                payload["action"] = "runtime-catalog-title-saved"
                payload["writes"] = {
                    "settings_json": True,
                    "config_json": False,
                    "catalog_db": False,
                    "cache": False,
                    "source_data": False,
                }
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/data-root/verify":
                raw_data_root = self._request_text_value(
                    parsed=parsed,
                    name="data_root",
                )
                self._send_json(source_root_setting_verify(self.config, raw_data_root), HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/data-root":
                raw_data_root = self._request_text_value(
                    parsed=parsed,
                    name="data_root",
                )
                verification = source_root_setting_verify(self.config, raw_data_root)
                if not verification.get("can_save"):
                    raise api_message_error(
                        400,
                        "settings.source_root.cannot_save",
                        params={"path": raw_data_root},
                    )
                try:
                    new_config = save_runtime_data_root(self.config, raw_data_root)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.source_root.invalid",
                        params={"path": raw_data_root},
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = thumbnail_cache_status(new_config)
                payload["action"] = "runtime-data-root-saved"
                payload["source_root_verification"] = verification
                payload["result_messages"] = [
                    build_backend_message(
                        "settings.source_root.saved",
                        severity="success",
                        params={"path": str(new_config.data_root)},
                    )
                ]
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/cache-limit":
                raw_limit = self._request_text_value(
                    parsed=parsed,
                    name="thumbnail_cache_limit_gb",
                )
                if raw_limit is None or not str(raw_limit).strip():
                    raise api_message_error(
                        400,
                        "request.parameter.missing",
                        params={"name": "thumbnail_cache_limit_gb"},
                    )
                try:
                    new_config = save_runtime_thumbnail_cache_limit(self.config, raw_limit)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.cache_limit.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = thumbnail_cache_status(new_config)
                payload["action"] = "runtime-cache-limit-saved"
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/page-sizes":
                raw_page_sizes = {
                    "photo_page_size": self._request_text_value(parsed=parsed, name="photo_page_size"),
                    "video_page_size": self._request_text_value(parsed=parsed, name="video_page_size"),
                    "gif_page_size": self._request_text_value(parsed=parsed, name="gif_page_size"),
                    "other_page_size": self._request_text_value(parsed=parsed, name="other_page_size"),
                    "folder_page_size": self._request_text_value(parsed=parsed, name="folder_page_size"),
                    "gallery_density": self._request_text_value(parsed=parsed, name="gallery_density"),
                }
                try:
                    new_config = save_runtime_page_sizes(self.config, raw_page_sizes)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.page_sizes.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = thumbnail_cache_status(new_config)
                payload["action"] = "runtime-page-sizes-saved"
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/thumbnail-video-params":
                raw_values = {
                    "image_thumb_width": self._request_text_value(parsed=parsed, name="image_thumb_width"),
                    "image_thumb_height": self._request_text_value(parsed=parsed, name="image_thumb_height"),
                    "gif_thumb_width": self._request_text_value(parsed=parsed, name="gif_thumb_width"),
                    "gif_thumb_height": self._request_text_value(parsed=parsed, name="gif_thumb_height"),
                    "video_preview_width": self._request_text_value(parsed=parsed, name="video_preview_width"),
                    "ffmpeg_timeout_seconds": self._request_text_value(parsed=parsed, name="ffmpeg_timeout_seconds"),
                    "ffmpeg_threads_per_job": self._request_text_value(parsed=parsed, name="ffmpeg_threads_per_job"),
                }
                try:
                    new_config = save_runtime_thumbnail_video_params(self.config, raw_values)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.preview_parameters.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = thumbnail_cache_status(new_config)
                payload["action"] = "runtime-thumbnail-video-params-saved"
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/settings/runtime/thumbnail-video-params/reset":
                try:
                    new_config = reset_runtime_thumbnail_video_params(self.config)
                except ConfigError as exc:
                    raise api_message_error(
                        400,
                        "settings.preview_parameters.reset_failed",
                        technical_detail=str(exc),
                    ) from exc
                self.server.config = new_config  # type: ignore[attr-defined]
                payload = thumbnail_cache_status(new_config)
                payload["action"] = "runtime-thumbnail-video-params-reset"
                self._send_json(payload, HTTPStatus.OK)
                return

            if path == "/api/thumbnail-cache/protected-orphan-cleanup-execute":
                raw_confirm = self._request_text_value(parsed=parsed, name="confirm")
                if raw_confirm != "protected-orphan-cleanup":
                    raise api_message_error(
                        400,
                        "cache.protected.confirmation.invalid",
                    )
                candidate_plan = getattr(self.server, "protected_cache_cleanup_candidate_plan", None)
                self.server.protected_cache_cleanup_candidate_plan = None  # type: ignore[attr-defined]
                self._send_json(
                    protected_cache_orphan_cleanup_execute_action(
                        self.config,
                        candidate_plan=candidate_plan,
                    ),
                    HTTPStatus.OK,
                )
                return

            if path == "/api/jobs/thumbnail-cache/protected-orphan-check":
                try:
                    job = self.job_manager.start_protected_cache_orphan_check(self.config)
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "protected-cache-orphan-check-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/thumbnail-cache/protected-orphan-clean":
                raw_confirm = self._request_text_value(parsed=parsed, name="confirm")
                if raw_confirm != "protected-orphan-cleanup":
                    raise api_message_error(
                        400,
                        "cache.protected.confirmation.invalid",
                    )
                try:
                    job = self.job_manager.start_protected_cache_orphan_clean(self.config)
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                except ValueError as exc:
                    raise api_message_error(
                        400,
                        "jobs.request.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "protected-cache-orphan-clean-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/thumbnail-cache/dynamic-cleanup":
                try:
                    job = self.job_manager.start_dynamic_cache_cleanup(self.config)
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "dynamic-cache-cleanup-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/scan-preview":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_scan_preview(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "scan-preview-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/catalog-update":
                try:
                    job = self.job_manager.start_catalog_update(self.config)
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "catalog-update-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/scan-stage":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_scan_stage(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "scan-stage-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/scan-activate":
                scan_id, expected_branch, missing_decision = self._request_scan_activate_values(parsed=parsed)
                try:
                    job = self.job_manager.start_scan_activate(
                        self.config,
                        scan_id=scan_id,
                        expected_branch=expected_branch,
                        missing_action=missing_decision,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                except ValueError as exc:
                    raise api_message_error(
                        400,
                        "jobs.request.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "scan-activate-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/thumbnails/gif-preview":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_gif_preview(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "gif-preview-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/thumbnails/video-poster":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_video_poster(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "video-poster-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/thumbnails/video-frames":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_video_frames(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "video-frames-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/folder-preview/build-tree":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_folder_preview_build_tree(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "folder-preview-build-tree-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/prepare-previews":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_prepare_previews(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "prepare-previews-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/jobs/update-branch":
                raw_branch = self._request_branch_value(parsed=parsed)
                try:
                    job = self.job_manager.start_update_branch(
                        self.config,
                        branch_rel_path=raw_branch,
                    )
                except JobBusyError as exc:
                    raise api_message_error(
                        409,
                        exc.code,
                        params=exc.params,
                    ) from exc
                except ValueError as exc:
                    raise api_message_error(
                        400,
                        "jobs.request.invalid",
                        technical_detail=str(exc),
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "action": "update-branch-started",
                        "job": job.to_dict(),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return

            raise api_message_error(
                404,
                "server.endpoint.unknown",
                params={"path": path},
            )
        except ApiError as exc:
            self._send_json(exc.to_payload(), exc.status_code)
        except ScannerError as exc:
            self._send_json(
                api_message_payload(
                    "scan.scope.unavailable",
                    technical_detail=str(exc),
                ),
                HTTPStatus.CONFLICT,
            )
        except Exception as exc:
            if is_client_disconnect(exc):
                return
            self._send_json(
                api_message_payload(
                    "server.internal_error",
                    technical_detail=f"{exc.__class__.__name__}: {exc}",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def send_response(self, code: int, message: str | None = None) -> None:
        self._diagnostic_status_code = int(code)
        super().send_response(code, message)

    def send_header(self, keyword: str, value: str) -> None:
        if keyword.lower() == "content-length":
            try:
                self._diagnostic_response_bytes = max(0, int(value))
            except (TypeError, ValueError):
                self._diagnostic_response_bytes = 0
        super().send_header(keyword, value)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the development server log compact."""
        print(f"{self.address_string()} - {format % args}")

    @property
    def config(self) -> Config:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def job_manager(self) -> JobManager:
        return self.server.job_manager  # type: ignore[attr-defined]

    def _handle_get(self, *, parsed: Any | None = None) -> tuple[dict[str, Any], int]:
        if parsed is None:
            parsed = urlparse(self.path)

        query = parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/instance":
            return {
                "ok": True,
                "application": "Catalog 2.0",
                "protocol_version": INSTANCE_PROTOCOL_VERSION,
                "instance_id": str(getattr(self.server, "instance_id", "")),
                "pid": int(getattr(self.server, "process_id", os.getpid())),
                "host": str(getattr(self.server, "bound_host", "127.0.0.1")),
                "port": int(getattr(self.server, "bound_port", self.server.server_address[1])),
            }, HTTPStatus.OK

        if path == "/api/status":
            return catalog_status(self.config), HTTPStatus.OK

        if path == "/api/folder":
            return folder_detail(
                self.config,
                _single_query_value(query, "path", default=""),
            ), HTTPStatus.OK

        if path == "/api/folder-preview/build-tree/plan":
            return folder_preview_build_tree_plan_page(
                self.config,
                _single_query_value(
                    query,
                    "folder",
                    default=_single_query_value(query, "branch", default=""),
                ),
                variant=_optional_int_query_value(query, "variant") or 0,
                requested_count=_optional_int_query_value(query, "preview_count") or 6,
            ), HTTPStatus.OK

        if path == "/api/folder/missing-delete-plan":
            return missing_folder_delete_plan_page(
                self.config,
                _single_query_value(query, "path", default=""),
            ), HTTPStatus.OK

        if path == "/api/folders":
            return child_folders(
                self.config,
                _single_query_value(query, "parent", default=""),
                raw_page=_optional_query_value(query, "page"),
                raw_page_size=_optional_query_value(query, "page_size"),
                raw_include_previews=_optional_query_value(query, "include_previews"),
            ), HTTPStatus.OK

        if path == "/api/media":
            return media_page(
                self.config,
                raw_folder=_single_query_value(query, "folder", default=""),
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_page=_optional_query_value(query, "page"),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/media/anchor":
            return media_page_anchor(
                self.config,
                raw_folder=_single_query_value(query, "folder", default=""),
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_path=_single_query_value(query, "path", default=""),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/favorites/media":
            return favorites_media_page(
                self.config,
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_page=_optional_query_value(query, "page"),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/favorites/anchor":
            return favorites_page_anchor(
                self.config,
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_path=_single_query_value(query, "path", default=""),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/favorites":
            return favorites_page(
                self.config,
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_page=_optional_query_value(query, "page"),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/search/media":
            return search_media_page(
                self.config,
                raw_query=_single_query_value(query, "q", default=""),
                raw_folder=_single_query_value(query, "folder", default=""),
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_page=_optional_query_value(query, "page"),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/search/anchor":
            return search_page_anchor(
                self.config,
                raw_query=_single_query_value(query, "q", default=""),
                raw_folder=_single_query_value(query, "folder", default=""),
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_path=_single_query_value(query, "path", default=""),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/search":
            return search_page(
                self.config,
                raw_query=_single_query_value(query, "q", default=""),
                raw_folder=_single_query_value(query, "folder", default=""),
                raw_media_type=_single_query_value(query, "type", default="all"),
                raw_page=_optional_query_value(query, "page"),
                raw_page_size=_optional_query_value(query, "page_size"),
            ), HTTPStatus.OK

        if path == "/api/jobs/status":
            return job_status(self.config, runtime_job=self.job_manager.snapshot()), HTTPStatus.OK


        if path == "/api/settings/runtime/ui-locale":
            return runtime_ui_locale_status(self.config), HTTPStatus.OK

        if path == "/api/settings/runtime/ui-theme":
            return runtime_ui_locale_status(self.config), HTTPStatus.OK

        if path == "/api/settings/runtime/catalog-title":
            return runtime_ui_locale_status(self.config), HTTPStatus.OK

        if path == "/api/thumbnail-cache/status":
            return thumbnail_cache_status(self.config), HTTPStatus.OK

        if path == "/api/thumbnail-cache/protected-orphan-audit":
            return protected_cache_orphan_audit_status(self.config), HTTPStatus.OK

        if path == "/api/thumbnail-cache/protected-orphan-cleanup-plan":
            payload, candidate_plan = protected_cache_orphan_cleanup_plan_status_with_candidate_plan(self.config)
            self.server.protected_cache_cleanup_candidate_plan = candidate_plan  # type: ignore[attr-defined]
            return payload, HTTPStatus.OK

        if path == "/api/video-tools/status":
            return video_tools_status_page(self.config), HTTPStatus.OK

        raise api_message_error(
                404,
                "server.endpoint.unknown",
                params={"path": path},
            )



    def _request_path_value(self, *, parsed: Any, allow_missing: bool) -> str:
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_path = _single_query_value(query, "path", default="")
        body_path = self._body_text_value("path")
        raw_path = body_path if body_path is not None else query_path

        if raw_path == "" and not allow_missing:
            raise api_message_error(
                400,
                "request.parameter.missing",
                params={"name": "path"},
            )

        return raw_path

    def _request_text_value(self, *, parsed: Any, name: str, default: str = "") -> str:
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_value = _single_query_value(query, name, default=default)
        body_value = self._body_text_value(name)
        return body_value if body_value is not None else query_value

    def _request_branch_value(self, *, parsed: Any) -> str:
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_branch = _single_query_value(query, "branch", default="")
        body_branch = self._body_text_value("branch")
        return body_branch if body_branch is not None else query_branch

    def _request_missing_decision_value(self, *, parsed: Any) -> str:
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_decision = _single_query_value(query, "missing_decision", default="require_decision")
        body_decision = self._body_text_value("missing_decision")
        return body_decision if body_decision is not None else query_decision

    def _request_media_rename_plan_values(self, *, parsed: Any) -> tuple[str, str]:
        query = parse_qs(parsed.query, keep_blank_values=True)
        values = {
            "media_id": _single_query_value(query, "media_id", default=""),
            "new_name": _single_query_value(query, "new_name", default=""),
        }

        body_values = self._body_text_values({"media_id", "new_name"})
        values.update({key: value for key, value in body_values.items() if value is not None})

        if not values["media_id"].strip():
            raise api_message_error(
                400,
                "request.parameter.missing",
                params={"name": "media_id"},
            )
        if not values["new_name"].strip():
            raise api_message_error(
                400,
                "request.parameter.missing",
                params={"name": "new_name"},
            )

        return values["media_id"], values["new_name"]

    def _request_folder_rename_plan_values(self, *, parsed: Any) -> tuple[str, str]:
        query = parse_qs(parsed.query, keep_blank_values=True)
        values = {
            "folder_id": _single_query_value(query, "folder_id", default=""),
            "new_name": _single_query_value(query, "new_name", default=""),
        }

        body_values = self._body_text_values({"folder_id", "new_name"})
        values.update({key: value for key, value in body_values.items() if value is not None})

        if not values["folder_id"].strip():
            raise api_message_error(
                400,
                "request.parameter.missing",
                params={"name": "folder_id"},
            )
        if not values["new_name"].strip():
            raise api_message_error(
                400,
                "request.parameter.missing",
                params={"name": "new_name"},
            )

        return values["folder_id"], values["new_name"]

    def _read_raw_body(self, *, max_bytes: int) -> bytes:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise api_message_error(400, "request.content_length.invalid") from exc
        if length < 0 or length > max_bytes:
            raise api_message_error(400, "request.body.too_large")
        return self.rfile.read(length) if length else b""

    def _body_text_values(self, names: set[str]) -> dict[str, str | None]:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise api_message_error(
                400,
                "request.content_length.invalid",
            ) from exc

        if length <= 0:
            return {name: None for name in names}
        if length > 32 * 1024:
            raise api_message_error(
                400,
                "request.body.too_large",
            )

        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        result: dict[str, str | None] = {name: None for name in names}

        if content_type == "application/json":
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise api_message_error(
                    400,
                    "request.json.invalid",
                ) from exc

            if not isinstance(payload, dict):
                raise api_message_error(
                    400,
                    "request.json.object_required",
                )

            for name in names:
                value = payload.get(name)
                if value is None:
                    continue
                if not isinstance(value, (str, int)):
                    raise api_message_error(
                        400,
                        "request.json.text_or_integer_required",
                        params={"name": name},
                    )
                result[name] = str(value)
            return result

        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        for name in names:
            result[name] = _optional_query_value(form, name)
        return result

    def _request_scan_activate_values(self, *, parsed: Any) -> tuple[int, str | None, str]:
        query = parse_qs(parsed.query, keep_blank_values=True)
        raw_scan_id = _single_query_value(query, "scan_id", default="").strip()
        if not raw_scan_id:
            raise api_message_error(
                400,
                "request.parameter.missing",
                params={"name": "scan_id"},
            )
        try:
            scan_id = int(raw_scan_id)
        except ValueError as exc:
            raise api_message_error(
                400,
                "request.parameter.integer_required",
                params={"name": "scan_id"},
            ) from exc
        if scan_id <= 0:
            raise api_message_error(
                400,
                "request.parameter.positive_integer_required",
                params={"name": "scan_id"},
            )

        expected_branch_raw = _optional_query_value(query, "branch")
        expected_branch = expected_branch_raw if expected_branch_raw is not None else None
        missing_decision = _single_query_value(query, "missing_decision", default="require_decision")
        return scan_id, expected_branch, missing_decision

    def _body_text_value(self, name: str) -> str | None:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise api_message_error(
                400,
                "request.content_length.invalid",
            ) from exc

        if length <= 0:
            return None
        if length > 32 * 1024:
            raise api_message_error(
                400,
                "request.body.too_large",
            )

        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()

        if content_type == "application/json":
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise api_message_error(
                    400,
                    "request.json.invalid",
                ) from exc

            if not isinstance(payload, dict):
                raise api_message_error(
                    400,
                    "request.json.object_required",
                )

            value = payload.get(name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise api_message_error(
                    400,
                    "request.json.text_required",
                    params={"name": name},
                )
            return value

        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return _optional_query_value(form, name)

    def _send_index_html(self) -> None:
        static_root = Path(__file__).resolve().parent / "static"
        template_path = static_root / "index.html"
        template = template_path.read_text(encoding="utf-8")
        runtime = startup_runtime_settings(self.config)
        body = render_index_html(template, self.config).encode("utf-8")

        diagnostic_record(
            "backend.startup.runtime_settings_embedded",
            source="initial_html",
            ui_locale=runtime["ui_locale"],
            ui_locale_source=runtime["ui_locale_source"],
            ui_theme=runtime["ui_theme"],
            ui_theme_source=runtime["ui_theme_source"],
            gallery_density=runtime["gallery_density"],
            gallery_density_source=runtime["gallery_density_source"],
            catalog_title_source=runtime["catalog_title_source"],
        )

        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static_asset(self, asset_name: str) -> None:
        if not asset_name or asset_name.startswith("/") or ".." in Path(asset_name).parts:
            raise api_message_error(
                404,
                "server.static.not_found",
            )

        mime_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
        self._send_static_file(asset_name, mime_type)

    def _send_static_file(self, asset_name: str, content_type: str) -> None:
        static_root = Path(__file__).resolve().parent / "static"
        file_path = (static_root / asset_name).resolve()

        try:
            file_path.relative_to(static_root.resolve())
        except ValueError as exc:
            raise api_message_error(
                404,
                "server.static.not_found",
            ) from exc

        if not file_path.exists() or not file_path.is_file():
            raise api_message_error(
                404,
                "server.static.not_found",
            )

        body = file_path.read_bytes()
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_thumbnail_media(
        self,
        raw_path: str,
        raw_variant: str,
        *,
        existing_only: bool = False,
        frame_index: int | None = None,
    ) -> None:
        resource = thumbnail_media_resource(
            self.config,
            raw_path,
            raw_variant,
            existing_only=existing_only,
            frame_index=frame_index,
        )
        safe_name = quote(resource.file_name)

        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", resource.mime_type)
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{safe_name}")
        self.send_header("Content-Length", str(resource.size_bytes))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Catalog-Thumbnail-Type", resource.thumbnail_type)
        self.send_header("X-Catalog-Thumbnail-Variant", resource.variant_key)
        self.send_header("X-Catalog-Thumbnail-Cache-Class", resource.cache_class)
        self.send_header("X-Catalog-Thumbnail-Generated", "1" if resource.generated else "0")
        self.end_headers()

        with resource.filesystem_path.open("rb") as file_handle:
            shutil.copyfileobj(file_handle, self.wfile, length=256 * 1024)

    def _send_folder_preview_media(self, raw_cache_path: str) -> None:
        resource = folder_preview_cache_resource(self.config, raw_cache_path)
        safe_name = quote(resource.file_name)

        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", "image/webp")
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{safe_name}")
        self.send_header("Content-Length", str(resource.size_bytes))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        with resource.filesystem_path.open("rb") as file_handle:
            shutil.copyfileobj(file_handle, self.wfile, length=256 * 1024)

    def _send_original_media(self, raw_path: str) -> None:
        resource = original_media_resource(self.config, raw_path)
        mime_type = mimetypes.guess_type(resource.file_name)[0] or "application/octet-stream"
        safe_name = quote(resource.file_name)
        common_headers = {
            "Content-Type": mime_type,
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        }

        range_header = self.headers.get("Range")
        if not range_header:
            self._send_full_file(resource, common_headers)
            return

        byte_range = _parse_range_header(range_header, resource.size_bytes)
        if byte_range is None:
            self._send_range_not_satisfiable(resource.size_bytes, common_headers)
            return

        self._send_file_range(resource, byte_range, common_headers)

    def _send_full_file(self, resource: Any, headers: dict[str, str]) -> None:
        self.send_response(int(HTTPStatus.OK))
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(resource.size_bytes))
        self.end_headers()

        with resource.filesystem_path.open("rb") as file_handle:
            shutil.copyfileobj(file_handle, self.wfile, length=1024 * 1024)

    def _send_file_range(
        self,
        resource: Any,
        byte_range: ByteRange,
        headers: dict[str, str],
    ) -> None:
        self.send_response(int(HTTPStatus.PARTIAL_CONTENT))
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(byte_range.length))
        self.send_header(
            "Content-Range",
            f"bytes {byte_range.start}-{byte_range.end}/{resource.size_bytes}",
        )
        self.end_headers()

        with resource.filesystem_path.open("rb") as file_handle:
            file_handle.seek(byte_range.start)
            remaining = byte_range.length
            chunk_size = 1024 * 1024

            while remaining > 0:
                chunk = file_handle.read(min(chunk_size, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_range_not_satisfiable(
        self,
        size_bytes: int,
        headers: dict[str, str],
    ) -> None:
        self.send_response(int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE))
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Range", f"bytes */{size_bytes}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, payload: dict[str, Any], status_code: int) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(int(status_code))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            if is_client_disconnect(exc):
                return
            raise


def create_http_server(
    config: Config,
    *,
    host: str,
    port: int,
    instance_id: str,
) -> CatalogHTTPServer:
    """Bind one Catalog 2.0 server without starting its request loop."""
    diagnostic_record(
        "backend.server.create.start",
        host=host,
        requested_port=port,
        db_path=str(config.db_path),
        db_size_bytes=config.db_path.stat().st_size if config.db_path.exists() else 0,
        wal_size_bytes=config.db_path.with_name(config.db_path.name + "-wal").stat().st_size
        if config.db_path.with_name(config.db_path.name + "-wal").exists()
        else 0,
    )
    db_check_started = time.perf_counter()
    database_status = validate_database_runtime(config.db_path)
    diagnostic_record(
        "backend.server.database_checked",
        duration_ms=round((time.perf_counter() - db_check_started) * 1000.0, 3),
        validation_mode="runtime",
        application_id=database_status.application_id,
        schema_version=database_status.schema_version,
        journal_mode=database_status.journal_mode,
        foreign_keys_enabled=database_status.foreign_keys_enabled,
        integrity_check="not_run",
    )
    bind_started = time.perf_counter()
    server = CatalogHTTPServer((host, port), CatalogRequestHandler)
    diagnostic_record(
        "backend.server.socket_bound",
        duration_ms=round((time.perf_counter() - bind_started) * 1000.0, 3),
        bound_port=int(server.server_address[1]),
    )
    server.config = config  # type: ignore[attr-defined]
    server.job_manager = JobManager()  # type: ignore[attr-defined]
    server.protected_cache_cleanup_candidate_plan = None  # type: ignore[attr-defined]
    server.instance_id = instance_id  # type: ignore[attr-defined]
    server.process_id = os.getpid()  # type: ignore[attr-defined]
    server.bound_host = host  # type: ignore[attr-defined]
    server.bound_port = int(server.server_address[1])  # type: ignore[attr-defined]
    return server


def print_server_banner(
    server: CatalogHTTPServer,
    *,
    host: str,
    port: int,
    launch_mode: bool,
) -> None:
    """Print the resolved server identity and URL before serving requests."""
    mode_label = "safe launcher" if launch_mode else "strict serve"
    print("Catalog 2.0 – local server")
    print("=" * 70)
    print(f"UI:       http://{host}:{port}/")
    print(f"instance: {str(getattr(server, 'instance_id', ''))[:16]}")
    print(f"start:    {mode_label}")
    print(f"status:   http://{host}:{port}/api/status")
    print("mode: catalog read + search + favorites + job status + scan-preview/scan-stage/scan-activate + thumbnail-cache status + read-only cleanup plan + protected-cache orphan audit + protected-cache orphan cleanup plan + persistent protected-cache maintenance jobs + safe dynamic cleanup execute + runtime data-root settings + runtime cache-limit settings + runtime ui-locale settings + thumbnail params reset + DB evidence + one-photo thumbnail + photo thumbnails in gallery + dynamic thumbnail cleanup + GIF preview job + video tools status + video poster job + existing video posters in gallery + video frame job + existing video frames in gallery + update-branch job + prepare-previews job")
    print("stop: Ctrl+C")
    for line in diagnostics_output_lines():
        print(line)


def run_server(config: Config, *, host: str = "127.0.0.1") -> None:
    """Start the strict service command on the configured port."""
    instance_id = instance_id_for_config(config.config_path)
    lock = InstanceProcessLock(instance_lock_path(config))
    if not lock.acquire():
        raise ServerError("This catalog instance is already running or starting in another process.")

    server: CatalogHTTPServer | None = None
    runtime_info = None
    finish_reason = "strict_serve_finished"
    try:
        try:
            server = create_http_server(
                config,
                host=host,
                port=config.server_port,
                instance_id=instance_id,
            )
        except OSError as exc:
            raise ServerError(
                f"Server cannot bind exclusively to {host}:{config.server_port}: {exc}"
            ) from exc

        runtime_info = build_runtime_info(
            instance_id=instance_id,
            host=host,
            port=int(server.server_address[1]),
        )
        write_runtime_info(runtime_info_path(config), runtime_info)
        print_server_banner(
            server,
            host=host,
            port=int(server.server_address[1]),
            launch_mode=False,
        )
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            finish_reason = "stopped_by_user"
            print("\nServer stopped by user.")
    except Exception:
        finish_reason = "strict_serve_error"
        raise
    finally:
        if server is not None:
            server.server_close()
        if runtime_info is not None:
            remove_runtime_info(runtime_info_path(config), expected=runtime_info)
        lock.release()
        diagnostic_record("backend.server.cleanup_complete", reason=finish_reason)
        finish_diagnostics_session(finish_reason)


def _parse_range_header(range_header: str, size_bytes: int) -> ByteRange | None:
    """Parse one HTTP Range header for a single byte range."""
    if size_bytes < 0:
        return None

    value = range_header.strip()
    if not value.startswith("bytes="):
        return None

    range_spec = value[len("bytes=") :].strip()
    if not range_spec or "," in range_spec or "-" not in range_spec:
        return None

    start_text, end_text = range_spec.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()

    try:
        if start_text == "":
            # Suffix range: bytes=-500 means last 500 bytes.
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(size_bytes - suffix_length, 0)
            end = size_bytes - 1
        else:
            start = int(start_text)
            if end_text == "":
                end = size_bytes - 1
            else:
                end = int(end_text)
    except ValueError:
        return None

    if size_bytes == 0:
        return None

    if start < 0 or end < start or start >= size_bytes:
        return None

    end = min(end, size_bytes - 1)
    return ByteRange(start=start, end=end)


def _single_query_value(
    query: dict[str, list[str]],
    name: str,
    *,
    default: str,
) -> str:
    values = query.get(name)
    if not values:
        return default
    return values[-1]


def _optional_query_value(
    query: dict[str, list[str]],
    name: str,
) -> str | None:
    values = query.get(name)
    if not values:
        return None
    return values[-1]


def _optional_int_query_value(
    query: dict[str, list[str]],
    name: str,
) -> int | None:
    value = _optional_query_value(query, name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise api_message_error(
            400,
            "request.parameter.integer_required",
            params={"name": name},
        ) from exc


def _query_flag_value(
    query: dict[str, list[str]],
    name: str,
) -> bool:
    value = _optional_query_value(query, name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}

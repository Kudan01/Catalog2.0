from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .i18n_registry import load_locale_registry
from .paths import is_path_within, same_path


SUPPORTED_CONFIG_VERSION = 1
SUPPORTED_RUNTIME_SETTINGS_VERSION = 1
PAGE_SIZE_KEYS = (
    "all_page_size",
    "photo_page_size",
    "video_page_size",
    "gif_page_size",
    "other_page_size",
    "folder_page_size",
)
THUMBNAIL_VIDEO_PARAM_KEYS = (
    "image_thumb_size",
    "gif_thumb_size",
    "video_preview_width",
    "ffmpeg_timeout_seconds",
    "ffmpeg_threads_per_job",
)
_LOCALE_REGISTRY = load_locale_registry()
SUPPORTED_UI_LOCALES = _LOCALE_REGISTRY.codes
SUPPORTED_UI_THEMES = ("original", "serious-light", "serious-dark", "vivid-content", "dark-cinema")
SUPPORTED_GALLERY_DENSITIES = ("compact", "comfortable", "large", "extra-large")
DEFAULT_GALLERY_DENSITY = "comfortable"
DEFAULT_TRANSITION_UI_LOCALE = _LOCALE_REGISTRY.default
DEFAULT_UI_THEME = "original"
DEFAULT_CATALOG_TITLE = "Catalog 2.0"
MAX_CATALOG_TITLE_LENGTH = 80
RUNTIME_SETTING_KEYS = (
    "data_root",
    "thumbnail_cache_limit_gb",
    *PAGE_SIZE_KEYS,
    *THUMBNAIL_VIDEO_PARAM_KEYS,
    "ui_locale",
    "ui_theme",
    "catalog_title",
    "gallery_density",
)


class ConfigError(RuntimeError):
    """Raised when config.json is missing or invalid."""


@dataclass(frozen=True)
class Config:
    config_path: Path
    config_version: int

    data_root: Path
    data_root_source: str
    output_root: Path

    server_port: int
    thumbnail_cache_limit_gb: float
    thumbnail_cache_limit_source: str
    runtime_settings_active: bool
    runtime_settings_error: str
    page_size_sources: dict[str, str]
    thumbnail_video_param_sources: dict[str, str]
    ui_locale: str
    ui_locale_source: str
    ui_theme: str
    ui_theme_source: str
    catalog_title: str
    catalog_title_source: str
    gallery_density: str
    gallery_density_source: str

    all_page_size: int
    photo_page_size: int
    video_page_size: int
    gif_page_size: int
    other_page_size: int
    folder_page_size: int

    image_thumb_size: tuple[int, int]
    gif_thumb_size: tuple[int, int]

    video_preview_width: int
    ffmpeg_timeout_seconds: int
    ffmpeg_threads_per_job: int

    @property
    def db_path(self) -> Path:
        return self.output_root / "catalog.db"

    @property
    def state_dir(self) -> Path:
        return self.output_root / "_state"

    @property
    def favorites_json(self) -> Path:
        return self.state_dir / "favorites.json"

    @property
    def settings_json(self) -> Path:
        return self.state_dir / "settings.json"

    @property
    def cache_dir(self) -> Path:
        return self.output_root / "_cache"

    @property
    def thumbnail_cache_dir(self) -> Path:
        return self.cache_dir / "thumbnails" / "v1"

    @property
    def thumbnail_dynamic_cache_dir(self) -> Path:
        return self.thumbnail_cache_dir / "dynamic"

    @property
    def thumbnail_protected_cache_dir(self) -> Path:
        return self.thumbnail_cache_dir / "protected"

    @property
    def photo_tile_cache_dir(self) -> Path:
        return self.thumbnail_dynamic_cache_dir / "photo_tiles"

    @property
    def protected_photo_tile_cache_dir(self) -> Path:
        return self.thumbnail_protected_cache_dir / "photo_tiles"

    @property
    def gif_preview_cache_dir(self) -> Path:
        return self.thumbnail_protected_cache_dir / "gif_previews"

    @property
    def video_poster_cache_dir(self) -> Path:
        return self.thumbnail_protected_cache_dir / "video_posters"

    @property
    def video_frame_cache_dir(self) -> Path:
        return self.thumbnail_protected_cache_dir / "video_frames"

    @property
    def photo_cache_limit_gb(self) -> float:
        # Backward-compatible internal alias. New code should use
        # thumbnail_cache_limit_gb.
        return self.thumbnail_cache_limit_gb


def load_config(config_path: Path) -> Config:
    """Read and validate config.json without writing anything to disk."""
    config_path = config_path.expanduser()

    if not config_path.exists():
        raise ConfigError(f"Missing config.json: {config_path}")

    if not config_path.is_file():
        raise ConfigError(f"Configuration path is not a file: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"config.json is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config.json: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("The config.json root must be a JSON object.")

    config_version = _required_int(raw, "config_version")

    if config_version != SUPPORTED_CONFIG_VERSION:
        raise ConfigError(
            f"Unsupported config_version: {config_version}. "
            f"Supported version is {SUPPORTED_CONFIG_VERSION}."
        )

    config_base_dir = config_path.resolve().parent
    data_root = _resolve_config_path(_required_path(raw, "data_root"), config_base_dir)
    data_root_source = "config.json:data_root"
    output_root = _resolve_config_path(_required_path(raw, "output_root"), config_base_dir)
    runtime_settings_path = output_root / "_state" / "settings.json"
    runtime_settings = _read_runtime_settings(runtime_settings_path)

    if "data_root" in runtime_settings:
        data_root = _resolve_config_path(_path_value(runtime_settings["data_root"], "data_root"), config_base_dir)
        data_root_source = "settings.json:data_root"

    thumbnail_cache_limit_gb = _thumbnail_cache_limit_gb(raw, 20)
    thumbnail_cache_limit_source = _thumbnail_cache_limit_source(raw)

    all_page_size_configured = "all_page_size" in raw
    page_sizes = {
        "photo_page_size": _positive_int(raw, "photo_page_size", 24),
        "video_page_size": _positive_int(raw, "video_page_size", 48),
        "gif_page_size": _positive_int(raw, "gif_page_size", 48),
        "other_page_size": _positive_int(raw, "other_page_size", 100),
        "folder_page_size": _positive_int(raw, "folder_page_size", 60),
    }
    page_sizes["all_page_size"] = _positive_int(
        raw,
        "all_page_size",
        page_sizes["photo_page_size"],
    )
    page_size_sources = {key: "config.json" for key in PAGE_SIZE_KEYS}

    image_thumb_size = _size_pair(raw, "image_thumb_size", (300, 400))
    gif_thumb_size = _size_pair(raw, "gif_thumb_size", (300, 300))
    video_preview_width = _positive_int(raw, "video_preview_width", 640)
    ffmpeg_timeout_seconds = _positive_int(raw, "ffmpeg_timeout_seconds", 180)
    ffmpeg_threads_per_job = _positive_int(raw, "ffmpeg_threads_per_job", 1)
    thumbnail_video_param_sources = {key: "config.json" for key in THUMBNAIL_VIDEO_PARAM_KEYS}

    ui_locale = DEFAULT_TRANSITION_UI_LOCALE
    ui_locale_source = "application-default"
    ui_theme = DEFAULT_UI_THEME
    ui_theme_source = "application-default"
    catalog_title = DEFAULT_CATALOG_TITLE
    catalog_title_source = "application-default"
    gallery_density = DEFAULT_GALLERY_DENSITY
    gallery_density_source = "application-default"

    runtime_settings_active = any(key in runtime_settings for key in RUNTIME_SETTING_KEYS)

    if "ui_locale" in runtime_settings:
        ui_locale = _runtime_ui_locale_value(runtime_settings["ui_locale"])
        ui_locale_source = "settings.json:ui_locale"

    if "ui_theme" in runtime_settings:
        ui_theme = _runtime_ui_theme_value(runtime_settings["ui_theme"])
        ui_theme_source = "settings.json:ui_theme"

    if "catalog_title" in runtime_settings:
        catalog_title = _runtime_catalog_title_value(runtime_settings["catalog_title"])
        catalog_title_source = "settings.json:catalog_title"

    if "gallery_density" in runtime_settings:
        gallery_density = _runtime_gallery_density_value(runtime_settings["gallery_density"])
        gallery_density_source = "settings.json:gallery_density"

    if "thumbnail_cache_limit_gb" in runtime_settings:
        thumbnail_cache_limit_gb = _positive_number(
            runtime_settings,
            "thumbnail_cache_limit_gb",
            thumbnail_cache_limit_gb,
        )
        thumbnail_cache_limit_source = "settings.json:thumbnail_cache_limit_gb"

    for key in PAGE_SIZE_KEYS:
        if key in runtime_settings:
            page_sizes[key] = _positive_int(runtime_settings, key, page_sizes[key])
            page_size_sources[key] = f"settings.json:{key}"

    if not all_page_size_configured and "all_page_size" not in runtime_settings:
        page_sizes["all_page_size"] = page_sizes["photo_page_size"]
        page_size_sources["all_page_size"] = page_size_sources["photo_page_size"]

    if "image_thumb_size" in runtime_settings:
        image_thumb_size = _size_pair(runtime_settings, "image_thumb_size", image_thumb_size)
        thumbnail_video_param_sources["image_thumb_size"] = "settings.json:image_thumb_size"

    if "gif_thumb_size" in runtime_settings:
        gif_thumb_size = _size_pair(runtime_settings, "gif_thumb_size", gif_thumb_size)
        thumbnail_video_param_sources["gif_thumb_size"] = "settings.json:gif_thumb_size"

    if "video_preview_width" in runtime_settings:
        video_preview_width = _positive_int(runtime_settings, "video_preview_width", video_preview_width)
        thumbnail_video_param_sources["video_preview_width"] = "settings.json:video_preview_width"

    if "ffmpeg_timeout_seconds" in runtime_settings:
        ffmpeg_timeout_seconds = _positive_int(runtime_settings, "ffmpeg_timeout_seconds", ffmpeg_timeout_seconds)
        thumbnail_video_param_sources["ffmpeg_timeout_seconds"] = "settings.json:ffmpeg_timeout_seconds"

    if "ffmpeg_threads_per_job" in runtime_settings:
        ffmpeg_threads_per_job = _positive_int(runtime_settings, "ffmpeg_threads_per_job", ffmpeg_threads_per_job)
        thumbnail_video_param_sources["ffmpeg_threads_per_job"] = "settings.json:ffmpeg_threads_per_job"

    config = Config(
        config_path=config_path,
        config_version=config_version,
        data_root=data_root,
        data_root_source=data_root_source,
        output_root=output_root,
        server_port=_port_number(raw, "server_port", 8765),
        thumbnail_cache_limit_gb=thumbnail_cache_limit_gb,
        thumbnail_cache_limit_source=thumbnail_cache_limit_source,
        runtime_settings_active=runtime_settings_active,
        runtime_settings_error="",
        page_size_sources=page_size_sources,
        thumbnail_video_param_sources=thumbnail_video_param_sources,
        ui_locale=ui_locale,
        ui_locale_source=ui_locale_source,
        ui_theme=ui_theme,
        ui_theme_source=ui_theme_source,
        catalog_title=catalog_title,
        catalog_title_source=catalog_title_source,
        gallery_density=gallery_density,
        gallery_density_source=gallery_density_source,
        all_page_size=page_sizes["all_page_size"],
        photo_page_size=page_sizes["photo_page_size"],
        video_page_size=page_sizes["video_page_size"],
        gif_page_size=page_sizes["gif_page_size"],
        other_page_size=page_sizes["other_page_size"],
        folder_page_size=page_sizes["folder_page_size"],
        image_thumb_size=image_thumb_size,
        gif_thumb_size=gif_thumb_size,
        video_preview_width=video_preview_width,
        ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
        ffmpeg_threads_per_job=ffmpeg_threads_per_job,
    )

    validate_config_paths(config)
    return config


def validate_config_paths(config: Config) -> None:
    """Validate configured and derived paths without creating them."""
    # data_root may be temporarily unavailable, for example when the source
    # archive is on an external or delayed disk. That must not prevent the
    # existing catalog from being opened. Scan/update entrypoints perform a
    # stricter runtime availability guard before doing any work.
    if config.data_root.exists() and not config.data_root.is_dir():
        raise ConfigError(f"data_root is not a folder: {config.data_root}")

    if same_path(config.output_root, config.data_root):
        raise ConfigError("output_root must not be the same as data_root.")

    if is_path_within(config.output_root, config.data_root):
        raise ConfigError("output_root must not be inside data_root.")

    derived_paths = {
        "db_path": config.db_path,
        "favorites_json": config.favorites_json,
        "settings_json": config.settings_json,
        "cache_dir": config.cache_dir,
        "thumbnail_cache_dir": config.thumbnail_cache_dir,
        "thumbnail_dynamic_cache_dir": config.thumbnail_dynamic_cache_dir,
        "thumbnail_protected_cache_dir": config.thumbnail_protected_cache_dir,
        "protected_photo_tile_cache_dir": config.protected_photo_tile_cache_dir,
        "photo_tile_cache_dir": config.photo_tile_cache_dir,
        "gif_preview_cache_dir": config.gif_preview_cache_dir,
        "video_poster_cache_dir": config.video_poster_cache_dir,
        "video_frame_cache_dir": config.video_frame_cache_dir,
    }

    for name, path in derived_paths.items():
        if is_path_within(path, config.data_root):
            raise ConfigError(
                f"Derived path {name} must not be inside data_root: {path}"
            )


def config_summary_lines(config: Config) -> list[str]:
    """Return a read-only summary for the command-line check."""
    cache_limit = _format_number(config.thumbnail_cache_limit_gb)

    return [
        "Catalog 2.0 – configuration check",
        "=" * 70,
        f"config.json:    {config.config_path}",
        f"config_version: {config.config_version}",
        f"data_root:      {config.data_root}",
        f"data_root_source: {config.data_root_source}",
        f"output_root:    {config.output_root}",
        f"data_root available: {'yes' if config.data_root.exists() and config.data_root.is_dir() else 'no'}",
        "",
        "Server and cache:",
        f"- server_port: {config.server_port}",
        f"- thumbnail_cache_limit_gb: {cache_limit}",
        f"- thumbnail_cache_limit_source: {config.thumbnail_cache_limit_source}",
        f"- runtime_settings_active: {'yes' if config.runtime_settings_active else 'no'}",
        f"- settings_json: {config.settings_json}",
        f"- ui_locale: {config.ui_locale}",
        f"- ui_locale_source: {config.ui_locale_source}",
        f"- ui_theme: {config.ui_theme}",
        f"- ui_theme_source: {config.ui_theme_source}",
        f"- catalog_title: {config.catalog_title}",
        f"- catalog_title_source: {config.catalog_title_source}",
        f"- gallery_density: {config.gallery_density}",
        f"- gallery_density_source: {config.gallery_density_source}",
        "",
        "Derived paths (not created by this check):",
        f"- db_path: {config.db_path}",
        f"- favorites_json: {config.favorites_json}",
        f"- settings_json: {config.settings_json}",
        f"- cache_dir: {config.cache_dir}",
        f"- thumbnail_cache_dir: {config.thumbnail_cache_dir}",
        f"- thumbnail_dynamic_cache_dir: {config.thumbnail_dynamic_cache_dir}",
        f"- thumbnail_protected_cache_dir: {config.thumbnail_protected_cache_dir}",
        f"- protected_photo_tile_cache_dir: {config.protected_photo_tile_cache_dir}",
        f"- photo_tile_cache_dir: {config.photo_tile_cache_dir}",
        f"- gif_preview_cache_dir: {config.gif_preview_cache_dir}",
        f"- video_poster_cache_dir: {config.video_poster_cache_dir}",
        f"- video_frame_cache_dir: {config.video_frame_cache_dir}",
        "",
        "Paging:",
        f"- all_page_size: {config.all_page_size}",
        f"- photo_page_size: {config.photo_page_size}",
        f"- video_page_size: {config.video_page_size}",
        f"- gif_page_size: {config.gif_page_size}",
        f"- other_page_size: {config.other_page_size}",
        f"- folder_page_size: {config.folder_page_size}",
        "",
        "Thumbnails:",
        f"- image_thumb_size: {config.image_thumb_size[0]} x {config.image_thumb_size[1]}",
        f"- image_thumb_size_source: {config.thumbnail_video_param_sources.get('image_thumb_size', 'config.json')}",
        f"- gif_thumb_size: {config.gif_thumb_size[0]} x {config.gif_thumb_size[1]}",
        f"- gif_thumb_size_source: {config.thumbnail_video_param_sources.get('gif_thumb_size', 'config.json')}",
        "",
        "Video:",
        f"- video_preview_width: {config.video_preview_width}",
        f"- video_preview_width_source: {config.thumbnail_video_param_sources.get('video_preview_width', 'config.json')}",
        f"- ffmpeg_timeout_seconds: {config.ffmpeg_timeout_seconds}",
        f"- ffmpeg_timeout_seconds_source: {config.thumbnail_video_param_sources.get('ffmpeg_timeout_seconds', 'config.json')}",
        f"- ffmpeg_threads_per_job: {config.ffmpeg_threads_per_job}",
        f"- ffmpeg_threads_per_job_source: {config.thumbnail_video_param_sources.get('ffmpeg_threads_per_job', 'config.json')}",
        "",
        "Configuration is valid.",
        "Nothing was written or changed.",
    ]



def save_runtime_ui_locale(
    config: Config,
    raw_locale: Any,
) -> Config:
    """Persist the frontend UI locale to settings.json and return reloaded config.

    This only updates settings.json. It does not edit config.json, scan the
    catalog, touch catalog.db, clean cache or modify source data.
    """
    locale = _runtime_ui_locale_value(raw_locale)
    settings_path = config.settings_json
    settings = _read_runtime_settings(settings_path) if settings_path.exists() else {}

    settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
    settings["ui_locale"] = locale
    settings["updated_at"] = _utc_now_iso()

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(settings_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, settings_path)

    return load_config(config.config_path)


def save_runtime_ui_theme(
    config: Config,
    raw_theme: Any,
) -> Config:
    """Persist the frontend UI theme to settings.json and return reloaded config.

    This only updates settings.json. It does not edit config.json, scan the
    catalog, touch catalog.db, clean cache or modify source data.
    """
    theme = _runtime_ui_theme_value(raw_theme)
    settings_path = config.settings_json
    settings = _read_runtime_settings(settings_path) if settings_path.exists() else {}

    settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
    settings["ui_theme"] = theme
    settings["updated_at"] = _utc_now_iso()

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(settings_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, settings_path)

    return load_config(config.config_path)


def save_runtime_catalog_title(
    config: Config,
    raw_title: Any,
) -> Config:
    """Persist the per-instance catalog title to settings.json and return reloaded config.

    This only updates settings.json. It does not edit config.json, scan the
    catalog, touch catalog.db, clean cache or modify source data.
    """
    title = _runtime_catalog_title_value(raw_title)
    settings_path = config.settings_json
    settings = _read_runtime_settings(settings_path) if settings_path.exists() else {}

    settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
    settings["catalog_title"] = title
    settings["updated_at"] = _utc_now_iso()

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(settings_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, settings_path)

    return load_config(config.config_path)


def save_runtime_data_root(
    config: Config,
    raw_data_root: Any,
) -> Config:
    """Persist data_root runtime override and return reloaded config.

    This only updates settings.json. It does not edit config.json, scan the
    catalog, touch catalog.db, clean cache or modify source data.
    Callers must verify the path and dataset match before saving.
    """
    data_root = _path_value(raw_data_root, "data_root")
    settings_path = config.settings_json
    settings = _read_runtime_settings(settings_path) if settings_path.exists() else {}

    settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
    settings["data_root"] = str(data_root)
    settings["updated_at"] = _utc_now_iso()

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(settings_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, settings_path)

    return load_config(config.config_path)


def save_runtime_thumbnail_cache_limit(
    config: Config,
    raw_limit_gb: Any,
) -> Config:
    """Persist the dynamic cache limit to settings.json and return reloaded config.

    This is intentionally limited to one runtime value in step 8.5c. It does
    not clean cache, rebuild thumbnails, edit config.json or touch catalog.db.
    """
    limit_gb = _runtime_thumbnail_cache_limit_value(raw_limit_gb)
    settings_path = config.settings_json
    settings = _read_runtime_settings(settings_path) if settings_path.exists() else {}

    settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
    settings["thumbnail_cache_limit_gb"] = limit_gb
    settings["updated_at"] = _utc_now_iso()

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(settings_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, settings_path)

    return load_config(config.config_path)


def save_runtime_page_sizes(
    config: Config,
    raw_page_sizes: dict[str, Any],
) -> Config:
    """Persist page-size runtime settings and return reloaded config.

    This only updates settings.json. It does not edit config.json, rebuild
    pages, regenerate thumbnails, clean cache or touch catalog.db.
    """
    page_sizes = _runtime_page_size_values(raw_page_sizes)
    settings_path = config.settings_json
    settings = _read_runtime_settings(settings_path) if settings_path.exists() else {}

    settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
    for key, value in page_sizes.items():
        settings[key] = value
    if "gallery_density" in raw_page_sizes:
        settings["gallery_density"] = _runtime_gallery_density_value(raw_page_sizes["gallery_density"])
    settings["updated_at"] = _utc_now_iso()

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(settings_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, settings_path)

    return load_config(config.config_path)



def save_runtime_thumbnail_video_params(
    config: Config,
    raw_values: dict[str, Any],
) -> Config:
    """Persist thumbnail/video runtime settings and return reloaded config.

    This only updates settings.json. It does not edit config.json, rebuild
    thumbnails, regenerate video previews, clean cache or touch catalog.db.
    """
    values = _runtime_thumbnail_video_param_values(raw_values)
    settings_path = config.settings_json
    settings = _read_runtime_settings(settings_path) if settings_path.exists() else {}

    settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
    for key, value in values.items():
        settings[key] = value
    settings["updated_at"] = _utc_now_iso()

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(settings_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, settings_path)

    return load_config(config.config_path)



def reset_runtime_thumbnail_video_params(config: Config) -> Config:
    """Remove thumbnail/video runtime overrides and return reloaded config.

    This restores the effective values from config.json or application defaults.
    It does not edit config.json, rebuild thumbnails, clean cache or touch DB.
    """
    settings_path = config.settings_json

    if not settings_path.exists():
        return load_config(config.config_path)

    settings = _read_runtime_settings(settings_path)
    changed = False

    for key in THUMBNAIL_VIDEO_PARAM_KEYS:
        if key in settings:
            del settings[key]
            changed = True

    if changed:
        settings["settings_version"] = SUPPORTED_RUNTIME_SETTINGS_VERSION
        settings["updated_at"] = _utc_now_iso()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = settings_path.with_name(settings_path.name + ".tmp")
        temp_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, settings_path)

    return load_config(config.config_path)


def _runtime_thumbnail_video_param_values(raw_values: dict[str, Any]) -> dict[str, Any]:
    image_width = _coerce_positive_int_value(raw_values.get("image_thumb_width"), "image_thumb_width")
    image_height = _coerce_positive_int_value(raw_values.get("image_thumb_height"), "image_thumb_height")
    gif_width = _coerce_positive_int_value(raw_values.get("gif_thumb_width"), "gif_thumb_width")
    gif_height = _coerce_positive_int_value(raw_values.get("gif_thumb_height"), "gif_thumb_height")

    return {
        "image_thumb_size": [image_width, image_height],
        "gif_thumb_size": [gif_width, gif_height],
        "video_preview_width": _coerce_positive_int_value(
            raw_values.get("video_preview_width"),
            "video_preview_width",
        ),
        "ffmpeg_timeout_seconds": _coerce_positive_int_value(
            raw_values.get("ffmpeg_timeout_seconds"),
            "ffmpeg_timeout_seconds",
        ),
        "ffmpeg_threads_per_job": _coerce_positive_int_value(
            raw_values.get("ffmpeg_threads_per_job"),
            "ffmpeg_threads_per_job",
        ),
    }


def _runtime_page_size_values(raw_values: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}

    for key in PAGE_SIZE_KEYS:
        if key not in raw_values:
            raise ConfigError(f"Missing value for {key}.")
        result[key] = _coerce_positive_int_value(raw_values[key], key)

    return result


def _coerce_positive_int_value(value: Any, key: str) -> int:
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ConfigError(f"{key} must not be empty.")
        try:
            value = int(normalized)
        except ValueError as exc:
            raise ConfigError(f"{key} must be an integer greater than 0.") from exc

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be an integer greater than 0.")

    return value


def _runtime_ui_locale_value(value: Any) -> str:
    locale = str(value or "").strip().lower()
    if locale not in SUPPORTED_UI_LOCALES:
        allowed = ", ".join(SUPPORTED_UI_LOCALES)
        raise ConfigError(f"ui_locale must be one of: {allowed}.")
    return locale


def _runtime_ui_theme_value(value: Any) -> str:
    theme = str(value or "").strip()
    if theme not in SUPPORTED_UI_THEMES:
        allowed = ", ".join(SUPPORTED_UI_THEMES)
        raise ConfigError(f"ui_theme must be one of: {allowed}.")
    return theme


def _runtime_gallery_density_value(value: Any) -> str:
    density = str(value or "").strip().lower()
    if density not in SUPPORTED_GALLERY_DENSITIES:
        allowed = ", ".join(SUPPORTED_GALLERY_DENSITIES)
        raise ConfigError(f"gallery_density must be one of: {allowed}.")
    return density


def _runtime_catalog_title_value(value: Any) -> str:
    title = str(value or "").strip()
    if not title:
        raise ConfigError("catalog_title must not be empty.")
    if len(title) > MAX_CATALOG_TITLE_LENGTH:
        raise ConfigError(f"catalog_title must be at most {MAX_CATALOG_TITLE_LENGTH} characters.")
    if any(ord(char) < 32 for char in title):
        raise ConfigError("catalog_title must not contain control characters.")
    return title


def _runtime_thumbnail_cache_limit_value(value: Any) -> float:
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        if not normalized:
            raise ConfigError("Dynamic cache limit must not be empty.")
        try:
            value = float(normalized)
        except ValueError as exc:
            raise ConfigError("Dynamic cache limit must be a positive number in GB.") from exc

    return _positive_number({"thumbnail_cache_limit_gb": value}, "thumbnail_cache_limit_gb", 20)


def _read_runtime_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    if not path.is_file():
        raise ConfigError(f"settings.json is not a file: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"settings.json is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read settings.json: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("The settings.json root must be a JSON object.")

    version = raw.get("settings_version", SUPPORTED_RUNTIME_SETTINGS_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError("settings_version in settings.json must be an integer.")
    if version != SUPPORTED_RUNTIME_SETTINGS_VERSION:
        raise ConfigError(
            f"Unsupported settings_version: {version}. "
            f"Supported version is {SUPPORTED_RUNTIME_SETTINGS_VERSION}."
        )

    if "data_root" in raw:
        _path_value(raw["data_root"], "data_root")

    if "thumbnail_cache_limit_gb" in raw:
        _positive_number(raw, "thumbnail_cache_limit_gb", 20)

    for key in PAGE_SIZE_KEYS:
        if key in raw:
            _positive_int(raw, key, 1)

    if "image_thumb_size" in raw:
        _size_pair(raw, "image_thumb_size", (300, 400))
    if "gif_thumb_size" in raw:
        _size_pair(raw, "gif_thumb_size", (300, 300))
    for key in ("video_preview_width", "ffmpeg_timeout_seconds", "ffmpeg_threads_per_job"):
        if key in raw:
            _positive_int(raw, key, 1)

    if "ui_locale" in raw:
        _runtime_ui_locale_value(raw["ui_locale"])
    if "ui_theme" in raw:
        _runtime_ui_theme_value(raw["ui_theme"])
    if "catalog_title" in raw:
        _runtime_catalog_title_value(raw["catalog_title"])
    if "gallery_density" in raw:
        _runtime_gallery_density_value(raw["gallery_density"])

    return dict(raw)



def _resolve_config_path(path: Path, base_dir: Path) -> Path:
    """Resolve config paths relative to the config.json directory.

    Absolute paths keep the current behavior. Relative paths make installed
    catalog instances portable when data_root and Catalog_Output keep the same
    relative layout on another drive letter or mirror.
    """
    path = path.expanduser()
    text = str(path)
    if path.is_absolute() or (len(text) >= 3 and text[1] == ":" and text[2] in {"\\", "/"}):
        return path
    return (base_dir / path).resolve()


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def _required_int(raw: dict[str, Any], key: str) -> int:
    if key not in raw:
        raise ConfigError(f"Missing required key in config.json: {key}")

    value = raw[key]

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Key {key} must be an integer.")

    return value


def _required_path(raw: dict[str, Any], key: str) -> Path:
    if key not in raw:
        raise ConfigError(f"Missing required key in config.json: {key}")

    return _path_value(raw[key], key)


def _path_value(value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Key {key} must be a non-empty path string.")

    return Path(value).expanduser()


def _thumbnail_cache_limit_gb(raw: dict[str, Any], default: float) -> float:
    if "thumbnail_cache_limit_gb" in raw:
        return _positive_number(raw, "thumbnail_cache_limit_gb", default)

    if "photo_cache_limit_gb" in raw:
        return _positive_number(raw, "photo_cache_limit_gb", default)

    return float(default)


def _thumbnail_cache_limit_source(raw: dict[str, Any]) -> str:
    if "thumbnail_cache_limit_gb" in raw:
        return "thumbnail_cache_limit_gb"

    if "photo_cache_limit_gb" in raw:
        return "photo_cache_limit_gb (legacy)"

    return "default"


def _positive_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"Key {key} must be an integer greater than 0.")

    return value


def _positive_number(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ConfigError(f"Key {key} must be a positive finite number.")

    return float(value)


def _port_number(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Key {key} must be an integer.")

    if not 1 <= value <= 65535:
        raise ConfigError(f"Key {key} must be in the range 1 to 65535.")

    return value


def _size_pair(
    raw: dict[str, Any],
    key: str,
    default: tuple[int, int],
) -> tuple[int, int]:
    value = raw.get(key, list(default))

    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"Key {key} must contain two positive integers.")

    width, height = value

    for item in (width, height):
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ConfigError(f"Key {key} must contain two positive integers.")

    return width, height



def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)

from __future__ import annotations

import dataclasses
import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from catalog_app.api import _media_dict, thumbnail_media_resource
from catalog_app.config import load_config
from catalog_app.database import initialize_database, open_database
from catalog_app.server import (
    THUMBNAIL_BROWSER_CACHE_CONTROL,
    CatalogRequestHandler,
    render_index_html,
)
from catalog_app.setup_instance import _instance_config_text
from catalog_app.thumbnail_cache import (
    GIF_PREVIEW_ALGORITHM_VERSION,
    PHOTO_TILE_ALGORITHM_VERSION,
    media_thumbnail_version,
    thumbnail_browser_cache_key,
)


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "catalog_app" / "static" / "app.js"
INDEX_HTML = ROOT / "catalog_app" / "static" / "index.html"

INSTANCE_ID = "0123456789abcdef" * 4


class _Fixture:
    def _instance(self, root: Path):
        data_root = root / "data"
        output_root = root / "Catalog_Output"
        data_root.mkdir()
        output_root.mkdir()
        config_path = output_root / "config.json"
        config_path.write_text(
            _instance_config_text(config_data_root=str(data_root)),
            encoding="utf-8",
        )
        config = load_config(config_path)
        initialize_database(config.db_path)
        return config

    def _media_with_thumbnail(
        self,
        config,
        *,
        file_name: str,
        media_type: str,
        thumbnail_type: str,
        algorithm_version: str,
    ) -> int:
        with open_database(config.db_path, read_only=False) as connection:
            scan_row = connection.execute("SELECT id FROM scan_sessions LIMIT 1").fetchone()
            if scan_row is None:
                scan_id = int(connection.execute(
                    """
                    INSERT INTO scan_sessions (
                        scan_type, scope_rel_path, scope_path_key, started_at,
                        finished_at, status, folder_count, media_count, error_count
                    ) VALUES ('full', '', '', 1, 2, 'completed', 1, 1, 0)
                    """
                ).lastrowid)
                folder_id = int(connection.execute(
                    """
                    INSERT INTO folders (
                        rel_path, path_key, parent_id, name, depth, sort_key,
                        last_successful_scan_id, is_available
                    ) VALUES ('', '', NULL, 'Root', 0, 'root', ?, 1)
                    """,
                    (scan_id,),
                ).lastrowid)
            else:
                scan_id = int(scan_row[0])
                folder_id = int(connection.execute(
                    "SELECT id FROM folders WHERE rel_path = ''"
                ).fetchone()[0])
            media_id = int(connection.execute(
                """
                INSERT INTO media_files (
                    rel_path, path_key, folder_id, file_name, extension, media_type,
                    size_bytes, modified_time, sort_key, last_successful_scan_id, is_available
                ) VALUES (?, ?, ?, ?, ?, ?, 10, 1.5, ?, ?, 1)
                """,
                (
                    file_name,
                    file_name,
                    folder_id,
                    file_name,
                    Path(file_name).suffix,
                    media_type,
                    file_name,
                    scan_id,
                ),
            ).lastrowid)
            path = config.thumbnail_cache_dir / "test" / f"{thumbnail_type}_{media_id}.webp"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"webp")
            connection.execute(
                """
                INSERT INTO thumbnails (
                    media_id, thumbnail_type, cache_class, variant_key, output_rel_path,
                    width, height, file_size_bytes, source_size_bytes,
                    source_modified_time, algorithm_version, status, created_at, updated_at
                ) VALUES (?, ?, 'dynamic', 'default', ?, 1, 1, 4, 10, 1.5, ?, 'ready', 1, 1)
                """,
                (
                    media_id,
                    thumbnail_type,
                    path.relative_to(config.output_root).as_posix(),
                    algorithm_version,
                ),
            )
            connection.commit()
        return media_id


class FingerprintTests(_Fixture, unittest.TestCase):
    def test_cache_key_depends_on_instance_and_preview_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            key = thumbnail_browser_cache_key(config, INSTANCE_ID)
            self.assertRegex(key, r"^[0-9a-f]{16}$")
            self.assertEqual(key, thumbnail_browser_cache_key(config, INSTANCE_ID))
            self.assertNotEqual(key, thumbnail_browser_cache_key(config, "f" * 64))
            larger = dataclasses.replace(
                config,
                image_thumb_size=(config.image_thumb_size[0] + 1, config.image_thumb_size[1]),
            )
            self.assertNotEqual(key, thumbnail_browser_cache_key(larger, INSTANCE_ID))

    def test_media_version_follows_catalog_source_identity(self) -> None:
        self.assertEqual("7-10-1.500000", media_thumbnail_version(7, 10, 1.5))
        self.assertNotEqual(media_thumbnail_version(7, 10, 1.5), media_thumbnail_version(7, 11, 1.5))
        self.assertNotEqual(media_thumbnail_version(7, 10, 1.5), media_thumbnail_version(7, 10, 2.5))

    def test_media_payload_carries_thumbnail_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            media_id = self._media_with_thumbnail(
                config,
                file_name="a.jpg",
                media_type="image",
                thumbnail_type="photo_tile",
                algorithm_version=PHOTO_TILE_ALGORITHM_VERSION,
            )
            connection = sqlite3.connect(config.db_path)
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute("SELECT * FROM media_files WHERE id = ?", (media_id,)).fetchone()
            finally:
                connection.close()
            self.assertEqual(f"{media_id}-10-1.500000", _media_dict(row)["thumbnail_version"])


class ThumbnailResponseCacheabilityTests(_Fixture, unittest.TestCase):
    def test_only_current_fingerprint_is_browser_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            media_id = self._media_with_thumbnail(
                config,
                file_name="a.jpg",
                media_type="image",
                thumbnail_type="photo_tile",
                algorithm_version=PHOTO_TILE_ALGORITHM_VERSION,
            )
            key = thumbnail_browser_cache_key(config, INSTANCE_ID)
            current = f"{key}.{media_thumbnail_version(media_id, 10, 1.5)}"
            cases = (
                (key, current, True),
                (key, "", False),
                (key, f"{key}.{media_thumbnail_version(media_id, 11, 1.5)}", False),
                (key, f"{'0' * 16}.{media_thumbnail_version(media_id, 10, 1.5)}", False),
                ("", current, False),
            )
            for browser_cache_key, requested, expected in cases:
                with self.subTest(requested=requested, key=browser_cache_key):
                    resource = thumbnail_media_resource(
                        config,
                        "a.jpg",
                        "photo_tile",
                        browser_cache_key=browser_cache_key,
                        requested_version=requested,
                    )
                    self.assertEqual(expected, resource.browser_cacheable)

    def test_outdated_preview_served_until_regenerated_is_not_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            key = thumbnail_browser_cache_key(config, INSTANCE_ID)
            for version, expected in (
                (GIF_PREVIEW_ALGORITHM_VERSION, True),
                ("gif_preview_v1_old", False),
            ):
                with self.subTest(version=version):
                    file_name = f"{len(version)}.gif"
                    media_id = self._media_with_thumbnail(
                        config,
                        file_name=file_name,
                        media_type="gif",
                        thumbnail_type="gif_preview",
                        algorithm_version=version,
                    )
                    resource = thumbnail_media_resource(
                        config,
                        file_name,
                        "gif_preview",
                        existing_only=True,
                        browser_cache_key=key,
                        requested_version=f"{key}.{media_thumbnail_version(media_id, 10, 1.5)}",
                    )
                    self.assertEqual(expected, resource.browser_cacheable)

    def _handler(self, config, key: str) -> Mock:
        handler = Mock()
        handler.config = config
        handler.wfile = io.BytesIO()
        handler._thumbnail_browser_cache_key = Mock(return_value=key)
        return handler

    def test_thumbnail_response_header_follows_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            media_id = self._media_with_thumbnail(
                config,
                file_name="a.jpg",
                media_type="image",
                thumbnail_type="photo_tile",
                algorithm_version=PHOTO_TILE_ALGORITHM_VERSION,
            )
            key = thumbnail_browser_cache_key(config, INSTANCE_ID)
            current = f"{key}.{media_thumbnail_version(media_id, 10, 1.5)}"
            for requested, expected in ((current, THUMBNAIL_BROWSER_CACHE_CONTROL), ("", "no-store")):
                with self.subTest(requested=requested):
                    handler = self._handler(config, key)
                    CatalogRequestHandler._send_thumbnail_media(
                        handler,
                        "a.jpg",
                        "photo_tile",
                        requested_version=requested,
                    )
                    handler.send_header.assert_any_call("Cache-Control", expected)

    def test_folder_preview_header_follows_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            cache_file = config.thumbnail_cache_dir / "dynamic" / "photo_tiles" / "example.webp"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_bytes(b"webp")
            cache_rel_path = cache_file.relative_to(config.output_root).as_posix()
            key = thumbnail_browser_cache_key(config, INSTANCE_ID)
            for requested, expected in (
                (key, THUMBNAIL_BROWSER_CACHE_CONTROL),
                ("", "no-store"),
                ("0" * 16, "no-store"),
            ):
                with self.subTest(requested=requested):
                    handler = self._handler(config, key)
                    CatalogRequestHandler._send_folder_preview_media(
                        handler,
                        cache_rel_path,
                        requested_version=requested,
                    )
                    handler.send_header.assert_any_call("Cache-Control", expected)

    def test_immutable_cache_control_has_long_max_age(self) -> None:
        self.assertIn("immutable", THUMBNAIL_BROWSER_CACHE_CONTROL)
        self.assertIn("max-age=31536000", THUMBNAIL_BROWSER_CACHE_CONTROL)


class FrontendFingerprintTests(_Fixture, unittest.TestCase):
    def test_index_bootstrap_embeds_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            rendered = render_index_html(
                INDEX_HTML.read_text(encoding="utf-8"),
                config,
                instance_id=INSTANCE_ID,
            )
            match = re.search(
                r'<script id="catalogRuntimeBootstrap" type="application/json">(.*?)</script>',
                rendered,
                re.S,
            )
            self.assertIsNotNone(match)
            bootstrap = json.loads(match.group(1))
            self.assertEqual(
                thumbnail_browser_cache_key(config, INSTANCE_ID),
                bootstrap["thumbnail_cache_key"],
            )

    def test_thumbnail_urls_carry_fingerprint(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("STARTUP_RUNTIME_SETTINGS?.thumbnail_cache_key", source)
        self.assertEqual(4, source.count("v: mediaThumbnailVersion(media),"))
        self.assertIn("v: THUMBNAIL_CACHE_KEY || undefined,", source)


if __name__ == "__main__":
    unittest.main()

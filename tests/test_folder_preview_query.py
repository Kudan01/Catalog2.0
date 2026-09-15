from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from catalog_app.api import (
    ApiError,
    _folder_preview_items_by_folder,
    child_folders,
    folder_preview_cache_resource,
    thumbnail_media_resource,
)
from catalog_app.config import load_config
from catalog_app.database import initialize_database
from catalog_app.diagnostics import (
    begin_http_request,
    finish_diagnostics_session,
    finish_http_request,
    start_diagnostics_session,
)
from catalog_app.schema import SCHEMA_STATEMENTS
from catalog_app.server import CatalogRequestHandler
from catalog_app.setup_instance import _instance_config_text


class FolderPreviewQueryTests(unittest.TestCase):
    def tearDown(self) -> None:
        finish_diagnostics_session("test_cleanup")

    def test_multiple_folders_preserve_auto_parent_order_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            try:
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)
                scan_id = self._insert_scan(connection)
                root_id = self._insert_folder(connection, scan_id, "", None, 0, 0, 0)
                first_id = self._insert_folder(connection, scan_id, "First", root_id, 1, 2, 4)
                second_id = self._insert_folder(connection, scan_id, "Second", root_id, 1, 1, 2)

                self._add_preview(connection, output_root, scan_id, first_id, "auto", 2, "auto-2.jpg")
                self._add_preview(connection, output_root, scan_id, first_id, "auto", 1, "auto-1.jpg")
                self._add_preview(
                    connection, output_root, scan_id, first_id, "auto_parent", 2, "parent-2.mp4", media_type="video"
                )
                self._add_preview(
                    connection, output_root, scan_id, first_id, "auto_parent", 1, "parent-1.gif", media_type="gif"
                )
                self._add_preview(connection, output_root, scan_id, second_id, "auto", 1, "second.jpg")
                self._add_preview(
                    connection, output_root, scan_id, second_id, "auto_parent", 1, "second-parent.jpg"
                )

                self._add_preview(
                    connection, output_root, scan_id, first_id, "auto", 3, "unavailable.jpg", available=False
                )
                self._add_preview(
                    connection, output_root, scan_id, first_id, "auto", 4, "stale.jpg", thumbnail_status="stale"
                )
                self._add_preview(
                    connection, output_root, scan_id, first_id, "auto", 5, "wrong-kind.jpg", thumbnail_type="video_poster"
                )
                self._add_preview(
                    connection, output_root, scan_id, first_id, "auto", 6, "missing-cache.jpg", create_cache=False
                )
                self._add_preview(
                    connection, output_root, scan_id, first_id, "manual", 1, "manual.jpg"
                )
                connection.commit()

                statements: list[str] = []
                connection.set_trace_callback(statements.append)
                with patch.object(Path, "exists", wraps=Path.exists) as exists_mock, patch.object(
                    Path, "is_file", wraps=Path.is_file
                ) as is_file_mock:
                    result = _folder_preview_items_by_folder(
                        SimpleNamespace(output_root=output_root),
                        connection,
                        [first_id, second_id],
                    )
                self.assertEqual(0, exists_mock.call_count)
                self.assertEqual(0, is_file_mock.call_count)
            finally:
                connection.close()

        self.assertEqual({first_id, second_id}, set(result))
        self.assertEqual(
            [
                "auto-1.jpg.webp",
                "auto-2.jpg.webp",
                "wrong-kind.jpg.webp",
                "missing-cache.jpg.webp",
                "parent-1.gif.webp",
                "parent-2.mp4.webp",
            ],
            [Path(item["thumbnail_cache_path"]).name for item in result[first_id]],
        )
        self.assertEqual(
            ["second.jpg.webp", "second-parent.jpg.webp"],
            [Path(item["thumbnail_cache_path"]).name for item in result[second_id]],
        )
        returned_paths = {
            Path(item["thumbnail_cache_path"]).name for item in result[first_id]
        }
        self.assertNotIn("stale.jpg.webp", returned_paths)
        self.assertNotIn("manual.jpg.webp", returned_paths)
        self.assertTrue(
            all(item["thumbnail_cache_path"].startswith("_cache/thumbnails/v1/") for item in result[first_id])
        )
        preview_queries = [
            statement
            for statement in statements
            if "FROM folder_preview_items AS fpi" in statement
        ]
        self.assertEqual(1, len(preview_queries))
        self.assertNotIn("media_files", preview_queries[0])

    def test_folder_preview_cache_resource_serves_only_safe_existing_webp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
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
            cache_file = config.thumbnail_cache_dir / "dynamic" / "photo_tiles" / "example.webp"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_bytes(b"webp-data")
            cache_rel_path = cache_file.relative_to(output_root).as_posix()

            with patch("catalog_app.api.open_database") as database_mock, patch(
                "catalog_app.api.photo_tile_resource"
            ) as generation_mock:
                resource = folder_preview_cache_resource(config, cache_rel_path)

            self.assertEqual(cache_file.resolve(), resource.filesystem_path)
            self.assertEqual(len(b"webp-data"), resource.size_bytes)
            database_mock.assert_not_called()
            generation_mock.assert_not_called()

            handler = Mock()
            handler.config = config
            handler.wfile = io.BytesIO()
            CatalogRequestHandler._send_folder_preview_media(handler, cache_rel_path)
            self.assertEqual(b"webp-data", handler.wfile.getvalue())
            handler.send_response.assert_called_once_with(200)
            handler.send_header.assert_any_call("Content-Type", "image/webp")

            with self.assertRaises(ApiError) as missing:
                folder_preview_cache_resource(
                    config,
                    (config.thumbnail_cache_dir / "missing.webp").relative_to(output_root).as_posix(),
                )
            self.assertEqual(404, missing.exception.status_code)

            for unsafe_path in ("../outside.webp", "_cache/outside.webp"):
                with self.subTest(path=unsafe_path), self.assertRaises(ApiError) as unsafe:
                    folder_preview_cache_resource(config, unsafe_path)
                self.assertEqual(400, unsafe.exception.status_code)

    def test_missing_ready_cache_is_deferred_to_thumbnail_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            output_root = root / "Catalog_Output"
            data_root.mkdir()
            (data_root / "folder_a").mkdir()
            output_root.mkdir()
            config_path = output_root / "config.json"
            config_path.write_text(
                _instance_config_text(config_data_root=str(data_root)),
                encoding="utf-8",
            )
            initialize_database(output_root / "catalog.db")
            config = load_config(config_path)

            connection = sqlite3.connect(config.db_path)
            connection.row_factory = sqlite3.Row
            try:
                scan_id = self._insert_scan(connection)
                root_id = self._insert_folder(connection, scan_id, "", None, 0, 0, 0)
                folder_id = self._insert_folder(
                    connection, scan_id, "folder_a", root_id, 1, 1, 1
                )
                self._add_preview(
                    connection,
                    output_root,
                    scan_id,
                    folder_id,
                    "auto",
                    1,
                    "example.jpg",
                    create_cache=False,
                )
                connection.commit()
            finally:
                connection.close()

            response = child_folders(
                config,
                "",
                raw_page="1",
                raw_page_size="20",
                raw_include_previews="1",
            )
            folder = next(item for item in response["folders"] if item["rel_path"] == "folder_a")
            self.assertEqual(1, len(folder["folder_previews"]))
            self.assertEqual(
                "_cache/thumbnails/v1/dynamic/photo_tiles/example.jpg.webp",
                folder["folder_previews"][0]["thumbnail_cache_path"],
            )
            with self.assertRaises(ApiError) as raised:
                thumbnail_media_resource(
                    config,
                    "media/example.jpg",
                    "photo_tile",
                    existing_only=True,
                )

            self.assertEqual(404, raised.exception.status_code)
            self.assertEqual("thumbnail.cache.missing", raised.exception.message_object["code"])

    def test_thumbnail_request_diagnostics_separate_existing_only_and_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            output_root = root / "Catalog_Output"
            data_root.mkdir()
            output_root.mkdir()
            config_path = output_root / "config.json"
            config_path.write_text(
                _instance_config_text(config_data_root=str(data_root)),
                encoding="utf-8",
            )
            initialize_database(output_root / "catalog.db")
            config = load_config(config_path)

            connection = sqlite3.connect(config.db_path)
            try:
                scan_id = self._insert_scan(connection)
                root_id = self._insert_folder(connection, scan_id, "", None, 0, 1, 1)
                self._add_preview(
                    connection,
                    output_root,
                    scan_id,
                    root_id,
                    "auto",
                    1,
                    "example.jpg",
                )
                connection.commit()
            finally:
                connection.close()

            session = start_diagnostics_session(
                config_path=config.config_path,
                output_root=config.output_root,
                command="test",
                cli_elapsed_ms=0.0,
                config_load_ms=0.0,
                process_elapsed_ms=0.0,
            )
            try:
                for existing_only in (True, False):
                    trace = begin_http_request(
                        "GET",
                        "/media/thumbnail?path=media/example.jpg"
                        f"&variant=photo_tile&existing_only={int(existing_only)}",
                    )
                    resource = thumbnail_media_resource(
                        config,
                        "media/example.jpg",
                        "photo_tile",
                        existing_only=existing_only,
                    )
                    self.assertFalse(resource.generated)
                    finish_http_request(trace, status_code=200, response_bytes=resource.size_bytes)
            finally:
                finish_diagnostics_session("test_complete")

            events = [
                json.loads(line)
                for line in session.log_path.read_text(encoding="utf-8").splitlines()
            ]
            requests = [
                event
                for event in events
                if event.get("event") == "backend.http.request"
                and event.get("path") == "/media/thumbnail"
            ]
            self.assertEqual(2, len(requests))
            self.assertTrue(requests[0]["thumbnail"]["existing_only"])
            self.assertFalse(requests[1]["thumbnail"]["existing_only"])
            for request in requests:
                detail = request["thumbnail"]
                self.assertEqual("photo_tile", detail["thumbnail_type"])
                self.assertEqual("existing", detail["result"])
                self.assertEqual(200, detail["http_status"])
                for field in (
                    "total_ms",
                    "media_lookup_ms",
                    "thumbnail_lookup_ms",
                    "cache_file_check_ms",
                    "other_ms",
                ):
                    self.assertGreaterEqual(detail[field], 0.0)

    @staticmethod
    def _insert_scan(connection: sqlite3.Connection) -> int:
        cursor = connection.execute(
            """
            INSERT INTO scan_sessions (
                scan_type, scope_rel_path, scope_path_key, started_at,
                finished_at, status, folder_count, media_count, error_count
            ) VALUES ('full', '', '', 1, 2, 'completed', 3, 20, 0)
            """
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_folder(
        connection: sqlite3.Connection,
        scan_id: int,
        rel_path: str,
        parent_id: int | None,
        depth: int,
        direct_visual_count: int,
        recursive_visual_count: int,
    ) -> int:
        name = rel_path or "Root"
        cursor = connection.execute(
            """
            INSERT INTO folders (
                rel_path, path_key, parent_id, name, depth, sort_key,
                last_successful_scan_id, is_available,
                direct_image_count, recursive_image_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                rel_path,
                rel_path.casefold(),
                parent_id,
                name,
                depth,
                name.casefold(),
                scan_id,
                direct_visual_count,
                recursive_visual_count,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _add_preview(
        connection: sqlite3.Connection,
        output_root: Path,
        scan_id: int,
        folder_id: int,
        selection_type: str,
        position: int,
        file_name: str,
        *,
        media_type: str = "image",
        available: bool = True,
        thumbnail_status: str = "ready",
        thumbnail_type: str | None = None,
        create_cache: bool = True,
    ) -> None:
        extension = Path(file_name).suffix.lower()
        media_cursor = connection.execute(
            """
            INSERT INTO media_files (
                rel_path, path_key, folder_id, file_name, extension, media_type,
                size_bytes, modified_time, sort_key, last_successful_scan_id, is_available
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
            """,
            (
                f"media/{file_name}",
                f"media/{file_name}".casefold(),
                folder_id,
                file_name,
                extension,
                media_type,
                file_name.casefold(),
                scan_id,
                int(available),
            ),
        )
        media_id = int(media_cursor.lastrowid)
        expected_type = {
            "image": "photo_tile",
            "gif": "gif_preview",
            "video": "video_poster",
        }[media_type]
        thumbnail_type = thumbnail_type or expected_type
        output_rel_path = f"_cache/thumbnails/v1/dynamic/photo_tiles/{file_name}.webp"
        connection.execute(
            """
            INSERT INTO thumbnails (
                media_id, thumbnail_type, cache_class, variant_key, output_rel_path,
                width, height, file_size_bytes, source_size_bytes,
                source_modified_time, algorithm_version, status, created_at, updated_at
            ) VALUES (?, ?, 'dynamic', 'default', ?, 1, 1, 1, 1, 1, 'test', ?, 1, 1)
            """,
            (media_id, thumbnail_type, output_rel_path, thumbnail_status),
        )
        connection.execute(
            """
            INSERT INTO folder_preview_items (folder_id, selection_type, position, media_id)
            VALUES (?, ?, ?, ?)
            """,
            (folder_id, selection_type, position, media_id),
        )
        if create_cache:
            cache_path = output_root / output_rel_path
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b"preview")


if __name__ == "__main__":
    unittest.main()

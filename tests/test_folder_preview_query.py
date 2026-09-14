from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from catalog_app.api import _folder_preview_items_by_folder
from catalog_app.schema import SCHEMA_STATEMENTS


class FolderPreviewQueryTests(unittest.TestCase):
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

                result = _folder_preview_items_by_folder(
                    SimpleNamespace(output_root=output_root),
                    connection,
                    [first_id, second_id],
                )
            finally:
                connection.close()

        self.assertEqual({first_id, second_id}, set(result))
        self.assertEqual(
            ["auto-1.jpg", "auto-2.jpg", "parent-1.gif", "parent-2.mp4"],
            [item["file_name"] for item in result[first_id]],
        )
        self.assertEqual(
            ["second.jpg", "second-parent.jpg"],
            [item["file_name"] for item in result[second_id]],
        )
        rejected = {
            "unavailable.jpg",
            "stale.jpg",
            "wrong-kind.jpg",
            "missing-cache.jpg",
            "manual.jpg",
        }
        self.assertTrue(rejected.isdisjoint(item["file_name"] for item in result[first_id]))

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
        output_rel_path = f"_cache/{file_name}.thumb"
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

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from catalog_app.api import search_page
from catalog_app.database import initialize_database


class SearchMediaTypeFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.db_path = root / "catalog.db"
        self.config = SimpleNamespace(
            db_path=self.db_path,
            favorites_json=root / "favorites.json",
        )
        initialize_database(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            cursor = connection.execute(
                """
                INSERT INTO scan_sessions (
                    scan_type, scope_rel_path, scope_path_key, started_at,
                    finished_at, status, folder_count, media_count, error_count
                ) VALUES ('full', '', '', 1, 2, 'completed', 2, 59, 0)
                """
            )
            scan_id = int(cursor.lastrowid)
            cursor = connection.execute(
                """
                INSERT INTO folders (
                    rel_path, path_key, parent_id, name, depth, sort_key,
                    last_successful_scan_id, is_available
                ) VALUES ('', '', NULL, '', 0, '', ?, 1)
                """,
                (scan_id,),
            )
            root_id = int(cursor.lastrowid)
            cursor = connection.execute(
                """
                INSERT INTO folders (
                    rel_path, path_key, parent_id, name, depth, sort_key,
                    last_successful_scan_id, is_available
                ) VALUES ('matching_folder', 'matching_folder', ?, 'matching_folder', 1,
                          'matching_folder', ?, 1)
                """,
                (root_id, scan_id),
            )
            folder_id = int(cursor.lastrowid)

            for index in range(55):
                self._insert_media(
                    connection,
                    scan_id=scan_id,
                    folder_id=folder_id,
                    name=f"matching_image_{index:02d}.jpg",
                    media_type="image",
                )
            for media_type, extension in (("gif", "gif"), ("video", "mp4"), ("other", "bin")):
                self._insert_media(
                    connection,
                    scan_id=scan_id,
                    folder_id=folder_id,
                    name=f"matching_{media_type}.{extension}",
                    media_type=media_type,
                )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _insert_media(
        self,
        connection: sqlite3.Connection,
        *,
        scan_id: int,
        folder_id: int,
        name: str,
        media_type: str,
    ) -> None:
        rel_path = f"matching_folder/{name}"
        connection.execute(
            """
            INSERT INTO media_files (
                rel_path, path_key, folder_id, file_name, extension, media_type,
                size_bytes, modified_time, sort_key, last_successful_scan_id,
                is_available
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, 1)
            """,
            (
                rel_path,
                rel_path,
                folder_id,
                name,
                Path(name).suffix.lstrip("."),
                media_type,
                name,
                scan_id,
            ),
        )

    def _search(self, content_filter: str, *, page: int = 1) -> dict[str, object]:
        return search_page(
            self.config,
            raw_query="matching",
            raw_folder="",
            raw_content_filter=content_filter,
            raw_page=str(page),
            raw_page_size=None,
        )

    def test_all_keeps_folders_and_media_in_combined_results(self) -> None:
        result = self._search("all")
        kinds = {item["kind"] for item in result["results"]}
        self.assertEqual({"folder", "media"}, kinds)
        self.assertEqual(1, result["counts"]["folders"])
        self.assertEqual(59, result["total"])
        self.assertEqual(50, result["page_size"])

    def test_typed_searches_return_only_matching_media(self) -> None:
        expected_totals = {"image": 55, "gif": 1, "video": 1, "other": 1}
        for media_type, expected_total in expected_totals.items():
            with self.subTest(media_type=media_type):
                result = self._search(media_type)
                self.assertEqual(0, result["counts"]["folders"])
                self.assertEqual(expected_total, result["total"])
                self.assertTrue(result["results"])
                self.assertTrue(all(item["kind"] == "media" for item in result["results"]))
                self.assertTrue(all(item["media_type"] == media_type for item in result["results"]))

    def test_folders_search_returns_only_folders(self) -> None:
        result = self._search("folders")
        self.assertEqual("folders", result["type"])
        self.assertEqual(1, result["counts"]["folders"])
        self.assertEqual(0, result["counts"]["media"])
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["pages"])
        self.assertEqual(["folder"], [item["kind"] for item in result["results"]])

    def test_typed_search_paginates_media_only_with_fixed_page_size(self) -> None:
        result = self._search("image", page=2)
        self.assertEqual(50, result["page_size"])
        self.assertEqual(55, result["total"])
        self.assertEqual(2, result["pages"])
        self.assertEqual(0, result["counts"]["folders"])
        self.assertEqual(5, len(result["results"]))
        self.assertTrue(all(item["kind"] == "media" for item in result["results"]))


if __name__ == "__main__":
    unittest.main()

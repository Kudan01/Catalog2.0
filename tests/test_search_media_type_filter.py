from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

            for index in range(54):
                connection.execute(
                    """
                    INSERT INTO folders (
                        rel_path, path_key, parent_id, name, depth, sort_key,
                        last_successful_scan_id, is_available
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, 1)
                    """,
                    (
                        f"matching_folder_{index:02d}",
                        f"matching_folder_{index:02d}",
                        root_id,
                        f"matching_folder_{index:02d}",
                        f"matching_folder_{index:02d}",
                        scan_id,
                    ),
                )

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

    def _search(
        self,
        content_filter: str,
        *,
        folder_page: int = 1,
        media_page: int = 1,
    ) -> dict[str, object]:
        return search_page(
            self.config,
            raw_query="matching",
            raw_folder="",
            raw_content_filter=content_filter,
            raw_folder_page=str(folder_page),
            raw_media_page=str(media_page),
            raw_page_size=None,
        )

    def test_all_returns_independent_folder_and_media_pages(self) -> None:
        result = self._search("all")
        self.assertEqual(55, result["counts"]["folders"])
        self.assertEqual(58, result["counts"]["media"])
        self.assertEqual(113, result["total"])
        self.assertEqual(50, result["folders"]["page_size"])
        self.assertEqual(50, result["media"]["page_size"])
        self.assertEqual(50, len(result["folders"]["items"]))
        self.assertEqual(50, len(result["media"]["items"]))

    def test_folder_and_media_pages_change_independently(self) -> None:
        baseline = self._search("all")
        folder_page = self._search("all", folder_page=2)
        media_page = self._search("all", media_page=2)
        self.assertEqual(baseline["media"]["items"], folder_page["media"]["items"])
        self.assertNotEqual(baseline["folders"]["items"], folder_page["folders"]["items"])
        self.assertEqual(baseline["folders"]["items"], media_page["folders"]["items"])
        self.assertNotEqual(baseline["media"]["items"], media_page["media"]["items"])

    def test_folder_previews_are_loaded_once_for_current_page_folder_ids(self) -> None:
        with patch(
            "catalog_app.api._folder_preview_items_by_folder",
            return_value={},
        ) as preview_lookup:
            result = self._search("all", folder_page=2)

        folders = result["folders"]["items"]
        self.assertTrue(all(item["folder_previews"] == [] for item in folders))
        preview_lookup.assert_called_once()
        self.assertEqual(5, len(preview_lookup.call_args.args[2]))
        self.assertEqual([item["id"] for item in folders], preview_lookup.call_args.args[2])

    def test_typed_searches_return_only_matching_media(self) -> None:
        expected_totals = {"image": 55, "gif": 1, "video": 1, "other": 1}
        for media_type, expected_total in expected_totals.items():
            with self.subTest(media_type=media_type):
                result = self._search(media_type)
                self.assertEqual(0, result["counts"]["folders"])
                self.assertEqual(expected_total, result["media"]["total"])
                self.assertEqual([], result["folders"]["items"])
                self.assertTrue(result["media"]["items"])
                self.assertTrue(all(item["media_type"] == media_type for item in result["media"]["items"]))

    def test_media_only_search_skips_folder_preview_lookup(self) -> None:
        with patch("catalog_app.api._folder_preview_items_by_folder") as preview_lookup:
            result = self._search("image")

        self.assertTrue(result["media"]["items"])
        preview_lookup.assert_not_called()

    def test_folders_search_returns_only_folders(self) -> None:
        result = self._search("folders")
        self.assertEqual("folders", result["type"])
        self.assertEqual(55, result["counts"]["folders"])
        self.assertEqual(0, result["counts"]["media"])
        self.assertEqual(55, result["total"])
        self.assertEqual(2, result["folders"]["pages"])
        self.assertEqual([], result["media"]["items"])
        self.assertTrue(all(item["kind"] == "folder" for item in result["folders"]["items"]))

    def test_typed_search_paginates_media_only_with_fixed_page_size(self) -> None:
        result = self._search("image", media_page=2)
        self.assertEqual(50, result["media"]["page_size"])
        self.assertEqual(55, result["media"]["total"])
        self.assertEqual(2, result["media"]["pages"])
        self.assertEqual(0, result["counts"]["folders"])
        self.assertEqual(5, len(result["media"]["items"]))
        self.assertEqual([], result["folders"]["items"])

    def test_search_page_size_is_fixed_for_both_sections(self) -> None:
        result = search_page(
            self.config,
            raw_query="matching",
            raw_folder="",
            raw_content_filter="all",
            raw_folder_page="1",
            raw_media_page="1",
            raw_page_size="10",
        )
        self.assertEqual(50, result["folders"]["page_size"])
        self.assertEqual(50, result["media"]["page_size"])


if __name__ == "__main__":
    unittest.main()

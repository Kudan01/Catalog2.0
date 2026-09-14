from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalog_app.api import child_folders
from catalog_app.config import load_config
from catalog_app.database import initialize_database
from catalog_app.diagnostics import (
    begin_http_request,
    finish_diagnostics_session,
    finish_http_request,
    start_diagnostics_session,
)
from catalog_app.setup_instance import _instance_config_text
from catalog_app.sorting import catalog_path_key
from tools.summarize_diagnostics import folder_browse_performance_lines


class FolderBrowseDiagnosticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        finish_diagnostics_session("test_cleanup")

    def test_diagnostics_preserve_results_and_distinguish_root_and_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._make_catalog(Path(temp))
            expected_root = child_folders(
                config,
                "",
                raw_page="1",
                raw_page_size="20",
                raw_include_previews="0",
            )
            expected_child = child_folders(
                config,
                "Parent",
                raw_page="1",
                raw_page_size="20",
                raw_include_previews="1",
            )

            session = start_diagnostics_session(
                config_path=config.config_path,
                output_root=config.output_root,
                command="test",
                cli_elapsed_ms=0.0,
                config_load_ms=0.0,
                process_elapsed_ms=0.0,
            )
            try:
                actual_root = self._diagnosed_request(
                    config,
                    "/api/folders?parent=&page=1&page_size=20&include_previews=0",
                    parent="",
                    include_previews="0",
                )
                actual_child = self._diagnosed_request(
                    config,
                    "/api/folders?parent=Parent&page=1&page_size=20&include_previews=1",
                    parent="Parent",
                    include_previews="1",
                )
            finally:
                finish_diagnostics_session("test_complete")

            self.assertEqual(expected_root, actual_root)
            self.assertEqual(expected_child, actual_child)

            events = self._read_events(session.log_path)
            requests = [
                event
                for event in events
                if event.get("event") == "backend.http.request"
                and event.get("path") == "/api/folders"
            ]
            self.assertEqual(2, len(requests))
            root_metrics = requests[0]["folder_browse"]
            child_metrics = requests[1]["folder_browse"]

            self.assertTrue(root_metrics["is_root"])
            self.assertEqual("<root>", root_metrics["folder"])
            self.assertFalse(child_metrics["is_root"])
            self.assertEqual("Parent", child_metrics["folder"])
            for metrics in (root_metrics, child_metrics):
                self.assertEqual(1, metrics["page"])
                self.assertEqual(20, metrics["page_size"])
                self.assertIn("returned_folders", metrics)
                self.assertIn("total_ms", metrics)
                self.assertIn("sql_ms", metrics)
                self.assertIn("source_root_status_ms", metrics)
                self.assertIn("folder_fs_status_ms", metrics)
                self.assertIn("root_enumeration_ms", metrics)
                self.assertIn("preview_metadata_ms", metrics)
                self.assertIn("preview_query_ms", metrics)
                self.assertIn("preview_cache_file_checks_ms", metrics)
                self.assertIn("preview_count_maps_ms", metrics)
                self.assertIn("preview_composition_ms", metrics)
                self.assertIn("preview_other_ms", metrics)
                self.assertIn("other_ms", metrics)
                self.assertIn("folder_fs_checks", metrics)
                for field, value in metrics.items():
                    if field.endswith("_ms"):
                        self.assertGreaterEqual(value, 0.0)

            self.assertGreaterEqual(root_metrics["source_root_status_checks"], 1)
            self.assertEqual(0.0, child_metrics["root_enumeration_ms"])
            self.assertEqual(1, child_metrics["preview_query_row_count"])
            self.assertEqual(0, child_metrics["preview_cache_file_check_count"])
            self.assertEqual(0.0, child_metrics["preview_cache_file_checks_ms"])
            preview_parts = sum(
                child_metrics[field]
                for field in (
                    "preview_query_ms",
                    "preview_cache_file_checks_ms",
                    "preview_count_maps_ms",
                    "preview_composition_ms",
                    "preview_other_ms",
                )
            )
            self.assertLessEqual(
                abs(child_metrics["preview_metadata_ms"] - preview_parts),
                0.01,
            )

    def test_folder_browse_summary_formats_requests_in_order(self) -> None:
        events = [
            self._folder_event("<root>", True, page=1),
            self._folder_event("Parent", False, page=2),
        ]

        output = "\n".join(folder_browse_performance_lines(events))

        self.assertIn("Folder browse performance", output)
        self.assertLess(output.index("folder:              <root>"), output.index("folder:              Parent"))
        self.assertIn("page:                2", output)
        self.assertIn("include_previews:    true", output)
        self.assertIn("folder_fs_checks:    3", output)
        self.assertIn("preview_query:       0.250 ms (7 rows)", output)
        self.assertIn("cache_file_checks:   0.500 ms (7 checks)", output)
        self.assertIn("other_preview:       0.050 ms", output)
        self.assertIn("other:               1.000 ms", output)

    @staticmethod
    def _diagnosed_request(config, raw_path: str, *, parent: str, include_previews: str):
        trace = begin_http_request("GET", raw_path)
        try:
            return child_folders(
                config,
                parent,
                raw_page="1",
                raw_page_size="20",
                raw_include_previews=include_previews,
            )
        finally:
            finish_http_request(trace, status_code=200, response_bytes=100)

    @staticmethod
    def _read_events(path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _folder_event(folder: str, is_root: bool, *, page: int) -> dict[str, object]:
        return {
            "event": "backend.http.request",
            "path": "/api/folders",
            "folder_browse": {
                "folder": folder,
                "is_root": is_root,
                "page": page,
                "page_size": 20,
                "include_previews": True,
                "returned_folders": 3,
                "total_ms": 12.0,
                "sql_ms": 2.0,
                "source_root_status_ms": 1.0,
                "folder_fs_status_ms": 3.0,
                "root_enumeration_ms": 4.0 if is_root else 0.0,
                "preview_metadata_ms": 1.0,
                "preview_query_ms": 0.25,
                "preview_query_row_count": 7,
                "preview_cache_file_checks_ms": 0.5,
                "preview_cache_file_check_count": 7,
                "preview_count_maps_ms": 0.1,
                "preview_composition_ms": 0.1,
                "preview_other_ms": 0.05,
                "other_ms": 1.0,
                "source_root_status_checks": 4,
                "folder_fs_checks": 3,
            },
        }

    @staticmethod
    def _make_catalog(root: Path):
        data_root = root / "media"
        output_root = root / "Catalog_Output"
        (data_root / "Parent" / "Child").mkdir(parents=True)
        output_root.mkdir()
        config_path = output_root / "config.json"
        config_path.write_text(
            _instance_config_text(config_data_root=str(data_root)),
            encoding="utf-8",
        )
        initialize_database(output_root / "catalog.db")
        connection = sqlite3.connect(output_root / "catalog.db")
        try:
            scan_id = connection.execute(
                """
                INSERT INTO scan_sessions (
                    scan_type, scope_rel_path, scope_path_key, started_at,
                    finished_at, status, folder_count, media_count, error_count
                ) VALUES ('full', '', '', 1, 2, 'completed', 3, 0, 0)
                """
            ).lastrowid
            root_id = FolderBrowseDiagnosticsTests._insert_folder(
                connection, "", None, "Root", 0, scan_id
            )
            parent_id = FolderBrowseDiagnosticsTests._insert_folder(
                connection, "Parent", root_id, "Parent", 1, scan_id
            )
            child_id = FolderBrowseDiagnosticsTests._insert_folder(
                connection, "Parent/Child", parent_id, "Child", 2, scan_id
            )
            media_id = connection.execute(
                """
                INSERT INTO media_files (
                    rel_path, path_key, folder_id, file_name, extension, media_type,
                    size_bytes, modified_time, sort_key, last_successful_scan_id, is_available
                ) VALUES ('Parent/Child/preview.jpg', 'parent/child/preview.jpg', ?,
                          'preview.jpg', '.jpg', 'image', 1, 1, 'preview.jpg', ?, 1)
                """,
                (child_id, scan_id),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO thumbnails (
                    media_id, thumbnail_type, cache_class, variant_key, output_rel_path,
                    width, height, file_size_bytes, source_size_bytes, source_modified_time,
                    algorithm_version, status, created_at, updated_at
                ) VALUES (?, 'photo_tile', 'dynamic', 'default', '_cache/preview.thumb',
                          1, 1, 1, 1, 1, 'test', 'ready', 1, 1)
                """,
                (media_id,),
            )
            connection.execute(
                """
                INSERT INTO folder_preview_items (folder_id, selection_type, position, media_id)
                VALUES (?, 'auto', 1, ?)
                """,
                (child_id, media_id),
            )
            connection.commit()
        finally:
            connection.close()
        cache_path = output_root / "_cache" / "preview.thumb"
        cache_path.parent.mkdir()
        cache_path.write_bytes(b"preview")
        return load_config(config_path)

    @staticmethod
    def _insert_folder(
        connection: sqlite3.Connection,
        rel_path: str,
        parent_id: int | None,
        name: str,
        depth: int,
        scan_id: int,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO folders (
                rel_path, path_key, parent_id, name, depth, sort_key,
                last_successful_scan_id, is_available
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (rel_path, catalog_path_key(rel_path), parent_id, name, depth, name.casefold(), scan_id),
        )
        return int(cursor.lastrowid)


if __name__ == "__main__":
    unittest.main()

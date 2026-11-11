from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from catalog_app.api import catalog_status
from catalog_app.config import load_config
from catalog_app.database import check_database, initialize_database, open_database
from catalog_app.diagnostics import (
    finish_diagnostics_session,
    start_diagnostics_session,
)
from catalog_app.scan_activate import activate_scan_by_id
from catalog_app.scan_plan import build_scan_plan
from catalog_app.scan_store import (
    active_table_counts,
    catalog_summary_counts,
    stage_scan_plan,
)
from catalog_app.setup_instance import _instance_config_text
from catalog_app.thumbnail_cache import (
    _cache_size_totals,
    cleanup_dynamic_thumbnail_cache,
    dynamic_cache_cleanup_target_bytes,
    execute_dynamic_thumbnail_cache_cleanup,
    recompute_thumbnail_cache_totals,
)


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "catalog_app" / "static" / "app.js"

# 1024 bytes expressed in GB; a power of two keeps the byte limit exact.
LIMIT_1024_BYTES_GB = 1024 / (1024 * 1024 * 1024)


def _instance(root: Path):
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


def _exact_cache_totals(connection) -> dict[str, int]:
    totals = {"dynamic": 0, "protected": 0}
    for row in connection.execute(
        "SELECT cache_class, SUM(file_size_bytes) FROM thumbnails GROUP BY cache_class"
    ):
        totals[str(row[0])] = int(row[1])
    return totals


class _ThumbnailRows:
    def _media(self, connection, name: str) -> int:
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
            connection.execute(
                """
                INSERT INTO folders (
                    rel_path, path_key, parent_id, name, depth, sort_key,
                    last_successful_scan_id, is_available
                ) VALUES ('', '', NULL, 'Root', 0, 'root', ?, 1)
                """,
                (scan_id,),
            )
        else:
            scan_id = int(scan_row[0])
        folder_id = int(connection.execute("SELECT id FROM folders WHERE path_key = ''").fetchone()[0])
        return int(connection.execute(
            """
            INSERT INTO media_files (
                rel_path, path_key, folder_id, file_name, extension, media_type,
                size_bytes, modified_time, sort_key, last_successful_scan_id, is_available
            ) VALUES (?, ?, ?, ?, '.jpg', 'image', 10, 1.0, ?, ?, 1)
            """,
            (name, name, folder_id, name, name, scan_id),
        ).lastrowid)

    def _thumbnail(
        self,
        config,
        connection,
        name: str,
        *,
        cache_class: str,
        size: int,
        last_used_at: float = 1.0,
    ) -> int:
        media_id = self._media(connection, name)
        cache_dir = (
            config.thumbnail_dynamic_cache_dir
            if cache_class == "dynamic"
            else config.thumbnail_protected_cache_dir
        )
        path = cache_dir / "photo_tiles" / f"{name}.webp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        connection.execute(
            """
            INSERT INTO thumbnails (
                media_id, thumbnail_type, cache_class, variant_key, output_rel_path,
                width, height, file_size_bytes, source_size_bytes,
                source_modified_time, algorithm_version, status, created_at,
                updated_at, last_used_at
            ) VALUES (?, 'photo_tile', ?, 'default', ?, 1, 1, ?, 10, 1.0, 'v', 'ready', 1, 1, ?)
            """,
            (
                media_id,
                cache_class,
                path.relative_to(config.output_root).as_posix(),
                size,
                last_used_at,
            ),
        )
        return media_id


class ThumbnailCacheTotalsTriggerTests(_ThumbnailRows, unittest.TestCase):
    def test_new_database_has_triggers_and_zero_totals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            check_database(config.db_path)
            with open_database(config.db_path, read_only=True) as connection:
                self.assertEqual(
                    {"total": 0, "dynamic": 0, "protected": 0},
                    _cache_size_totals(connection),
                )

    def test_totals_follow_insert_update_class_change_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            with open_database(config.db_path, read_only=False) as connection:
                first = self._thumbnail(config, connection, "a", cache_class="dynamic", size=100)
                self._thumbnail(config, connection, "b", cache_class="dynamic", size=50)
                self._thumbnail(config, connection, "c", cache_class="protected", size=30)
                connection.commit()
                self.assertEqual(
                    {"total": 180, "dynamic": 150, "protected": 30},
                    _cache_size_totals(connection),
                )

                # Regeneration with a new file size.
                connection.execute(
                    "UPDATE thumbnails SET file_size_bytes = 120 WHERE media_id = ?",
                    (first,),
                )
                self.assertEqual(_exact_cache_totals(connection), self._by_class(connection))

                # Move between dynamic and protected cache, then back.
                connection.execute(
                    "UPDATE thumbnails SET cache_class = 'protected' WHERE media_id = ?",
                    (first,),
                )
                self.assertEqual({"dynamic": 50, "protected": 150}, self._by_class(connection))
                connection.execute(
                    "UPDATE thumbnails SET cache_class = 'dynamic', file_size_bytes = 90 WHERE media_id = ?",
                    (first,),
                )
                self.assertEqual({"dynamic": 140, "protected": 30}, self._by_class(connection))

                # An update that changes neither column leaves totals alone.
                connection.execute(
                    "UPDATE thumbnails SET status = 'stale', file_size_bytes = file_size_bytes"
                )
                self.assertEqual({"dynamic": 140, "protected": 30}, self._by_class(connection))

                connection.execute("DELETE FROM thumbnails WHERE media_id = ?", (first,))
                self.assertEqual({"dynamic": 50, "protected": 30}, self._by_class(connection))

                # Foreign-key cascade from media_files also updates totals.
                connection.execute("DELETE FROM media_files")
                self.assertEqual({"dynamic": 0, "protected": 0}, self._by_class(connection))
                connection.commit()

    def test_rolled_back_change_leaves_totals_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            with open_database(config.db_path, read_only=False) as connection:
                self._thumbnail(config, connection, "a", cache_class="dynamic", size=100)
                connection.rollback()
                self.assertEqual({"dynamic": 0, "protected": 0}, self._by_class(connection))

    def test_recompute_corrects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            with open_database(config.db_path, read_only=False) as connection:
                self._thumbnail(config, connection, "a", cache_class="dynamic", size=100)
                self._thumbnail(config, connection, "b", cache_class="protected", size=40)
                connection.execute("UPDATE thumbnail_cache_totals SET size_bytes = 7")
                connection.commit()

                result = recompute_thumbnail_cache_totals(connection)
                connection.commit()

                self.assertEqual({"total": 140, "dynamic": 100, "protected": 40}, result)
                self.assertEqual(result, _cache_size_totals(connection))

    @staticmethod
    def _by_class(connection) -> dict[str, int]:
        totals = _cache_size_totals(connection)
        return {"dynamic": totals["dynamic"], "protected": totals["protected"]}


class DynamicCacheCleanupTargetTests(_ThumbnailRows, unittest.TestCase):
    def _over_limit_cache(self, config) -> None:
        # Ten dynamic entries of 110 bytes (1100 bytes) against a 1024-byte limit,
        # oldest first, plus one protected entry that is never deleted.
        with open_database(config.db_path, read_only=False) as connection:
            for index in range(10):
                self._thumbnail(
                    config,
                    connection,
                    f"d{index}",
                    cache_class="dynamic",
                    size=110,
                    last_used_at=float(index + 1),
                )
            self._thumbnail(config, connection, "p", cache_class="protected", size=500)
            connection.commit()

    def test_target_is_ninety_percent_of_limit(self) -> None:
        self.assertEqual(921, dynamic_cache_cleanup_target_bytes(1024))
        self.assertEqual(0, dynamic_cache_cleanup_target_bytes(0))

    def test_automatic_cleanup_removes_oldest_down_to_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = dataclasses.replace(
                _instance(Path(temp)),
                thumbnail_cache_limit_gb=LIMIT_1024_BYTES_GB,
            )
            self._over_limit_cache(config)

            result = cleanup_dynamic_thumbnail_cache(config)

            # Cleaning only to the limit would remove one entry (990 bytes); the
            # target of 921 bytes removes two, so the next previews fit again.
            self.assertEqual(2, result.deleted_entries)
            self.assertEqual(880, result.dynamic_size_after_bytes)
            self.assertEqual(500, result.protected_size_after_bytes)
            with open_database(config.db_path, read_only=True) as connection:
                remaining = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT m.file_name FROM thumbnails t JOIN media_files m ON m.id = t.media_id"
                    )
                }
                self.assertNotIn("d0", remaining)
                self.assertNotIn("d1", remaining)
                self.assertIn("d2", remaining)
                self.assertIn("p", remaining)
                self.assertEqual(
                    _exact_cache_totals(connection),
                    {
                        key: value
                        for key, value in _cache_size_totals(connection).items()
                        if key != "total"
                    },
                )

            # Under the limit again: the next check deletes nothing.
            self.assertEqual(0, cleanup_dynamic_thumbnail_cache(config).deleted_entries)

    def test_explicit_cleanup_recomputes_totals_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = dataclasses.replace(
                _instance(Path(temp)),
                thumbnail_cache_limit_gb=LIMIT_1024_BYTES_GB,
            )
            self._over_limit_cache(config)
            with open_database(config.db_path, read_only=False) as connection:
                connection.execute("UPDATE thumbnail_cache_totals SET size_bytes = 0")
                connection.commit()

            # With drifted totals the automatic check sees no work to do.
            self.assertEqual(0, cleanup_dynamic_thumbnail_cache(config).deleted_entries)

            result = execute_dynamic_thumbnail_cache_cleanup(config)

            self.assertEqual(1100, result.dynamic_size_before_bytes)
            self.assertEqual(2, result.deleted_entries)
            self.assertEqual(880, result.dynamic_size_after_bytes)
            with open_database(config.db_path, read_only=True) as connection:
                self.assertEqual(
                    {"total": 1380, "dynamic": 880, "protected": 500},
                    _cache_size_totals(connection),
                )


class CatalogSummaryTests(unittest.TestCase):
    def _stage_and_activate(self, config, **activate_kwargs):
        stage = stage_scan_plan(config.db_path, build_scan_plan(config))
        return activate_scan_by_id(
            config.db_path,
            scan_id=stage.scan_id,
            output_func=lambda _line: None,
            **activate_kwargs,
        )

    def _assert_summary_matches_tables(self, config) -> tuple[int, int, int, int]:
        with open_database(config.db_path, read_only=True) as connection:
            stored = catalog_summary_counts(connection)
            exact = (
                *active_table_counts(connection, "folders"),
                *active_table_counts(connection, "media_files"),
            )
        self.assertEqual(exact, stored)
        return stored

    def test_scan_activation_stores_exact_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            (config.data_root / "A" / "B").mkdir(parents=True)
            (config.data_root / "C").mkdir()
            (config.data_root / "A" / "one.jpg").write_bytes(b"1")
            (config.data_root / "A" / "B" / "two.jpg").write_bytes(b"2")
            (config.data_root / "C" / "three.gif").write_bytes(b"3")

            self._stage_and_activate(config)
            self.assertEqual((4, 4, 3, 3), self._assert_summary_matches_tables(config))

            status = catalog_status(config)
            self.assertEqual(4, status["available_folders"])
            self.assertEqual(3, status["available_media"])
            self.assertEqual(0, status["unavailable_folders"])
            self.assertEqual(0, status["unavailable_media"])

            shutil.rmtree(config.data_root / "C")
            self._stage_and_activate(config, missing_action="keep")
            self.assertEqual((4, 3, 3, 2), self._assert_summary_matches_tables(config))

            status = catalog_status(config)
            self.assertEqual(1, status["unavailable_folders"])
            self.assertEqual(1, status["unavailable_media"])

    def test_status_reads_stored_counts_without_counting_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            with open_database(config.db_path, read_only=False) as connection:
                connection.execute(
                    """
                    UPDATE catalog_summary
                    SET folder_count = 9, available_folder_count = 7,
                        media_count = 5, available_media_count = 2
                    """
                )
                connection.commit()

            status = catalog_status(config)

            self.assertEqual(7, status["available_folders"])
            self.assertEqual(2, status["unavailable_folders"])
            self.assertEqual(2, status["available_media"])
            self.assertEqual(3, status["unavailable_media"])


class FrontendEventFieldTests(unittest.TestCase):
    def test_frontend_field_named_like_backend_key_is_prefixed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            session = start_diagnostics_session(
                config_path=config.config_path,
                output_root=config.output_root,
                command="test",
                cli_elapsed_ms=0.0,
                config_load_ms=0.0,
                process_elapsed_ms=0.0,
            )
            body = json.dumps({
                "events": [
                    {"event": "frontend.startup.runtime_settings", "source": "bootstrap", "elapsed_ms": 3},
                    {"event": "frontend.after", "value": 1},
                ]
            }).encode("utf-8")

            try:
                accepted = session.accept_frontend_events(token=session.session_id, raw_body=body)
                events = [
                    json.loads(line)
                    for line in session.log_path.read_text(encoding="utf-8").splitlines()
                ]
            finally:
                # Finish while the temporary output folder still exists; the
                # session writes its summary there.
                finish_diagnostics_session("test_complete")

            self.assertEqual(2, accepted)
            startup = next(e for e in events if e["event"] == "frontend.startup.runtime_settings")
            self.assertEqual("frontend", startup["source"])
            self.assertEqual("bootstrap", startup["frontend_source"])
            self.assertEqual(3, startup["frontend_elapsed_ms"])
            self.assertTrue(any(e["event"] == "frontend.after" for e in events))


class FrontendStartupOrderTests(unittest.TestCase):
    def test_initial_folder_view_does_not_wait_for_status(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        start = source.index("(async function main()")
        body = source[start:source.index("})();", start)]
        self.assertNotIn("await loadStatus()", body)
        self.assertNotIn("await loadJobStatus()", body)
        self.assertLess(body.index("loadStatus()"), body.index("await openInitialFolderView()"))
        self.assertLess(body.index("loadJobStatus()"), body.index("await openInitialFolderView()"))


if __name__ == "__main__":
    unittest.main()

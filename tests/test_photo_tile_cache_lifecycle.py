from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_app.config import load_config
from catalog_app.database import initialize_database, open_database
from catalog_app.folder_preview_candidates import apply_folder_preview_candidates
from catalog_app.setup_instance import _instance_config_text
from catalog_app.thumbnail_cache import (
    PHOTO_TILE_VARIANT_KEY,
    _photo_tile_destination,
    photo_tile_resource,
    reconcile_photo_tile_cache_lifecycle,
    thumbnail_cache_cleanup_plan,
    thumbnail_cache_database_summary,
    thumbnail_cache_layout,
)


class PhotoTileCacheLifecycleTests(unittest.TestCase):
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

    def _photo_tile(self, config, *, referenced: bool, cache_class: str = "dynamic") -> tuple[int, Path]:
        with open_database(config.db_path, read_only=False) as connection:
            token = int(connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]) + 1
            folder_rel = f"folder_{token}"
            media_rel = f"{folder_rel}/example_{token}.jpg"
            scan_row = connection.execute("SELECT id FROM scan_sessions LIMIT 1").fetchone()
            if scan_row is None:
                scan_id = int(connection.execute(
                    """
                    INSERT INTO scan_sessions (
                        scan_type, scope_rel_path, scope_path_key, started_at,
                        finished_at, status, folder_count, media_count, error_count
                    ) VALUES ('full', '', '', 1, 2, 'completed', 2, 1, 0)
                    """
                ).lastrowid)
                root_id = int(connection.execute(
                    """
                    INSERT INTO folders (
                        rel_path, path_key, parent_id, name, depth, sort_key,
                        last_successful_scan_id, is_available,
                        direct_image_count, recursive_image_count
                    ) VALUES ('', '', NULL, 'Root', 0, 'root', ?, 1, 0, 1)
                    """,
                    (scan_id,),
                ).lastrowid)
            else:
                scan_id = int(scan_row["id"])
                root_id = int(connection.execute(
                    "SELECT id FROM folders WHERE rel_path = ''"
                ).fetchone()["id"])
            folder_id = int(connection.execute(
                """
                INSERT INTO folders (
                    rel_path, path_key, parent_id, name, depth, sort_key,
                    last_successful_scan_id, is_available,
                    direct_image_count, recursive_image_count
                ) VALUES (?, ?, ?, ?, 1, ?, ?, 1, 1, 1)
                """,
                (folder_rel, folder_rel, root_id, folder_rel, folder_rel, scan_id),
            ).lastrowid)
            media_id = int(connection.execute(
                """
                INSERT INTO media_files (
                    rel_path, path_key, folder_id, file_name, extension, media_type,
                    size_bytes, modified_time, sort_key, last_successful_scan_id, is_available
                ) VALUES (?, ?, ?, ?, '.jpg', 'image', 10, 1, ?, ?, 1)
                """,
                (media_rel, media_rel, folder_id, f"example_{token}.jpg", media_rel, scan_id),
            ).lastrowid)
            base = (
                config.protected_photo_tile_cache_dir
                if cache_class == "protected"
                else config.photo_tile_cache_dir
            )
            path = base / "aa" / "bb" / f"tile_{token}.webp"
            rel_path = path.relative_to(config.output_root).as_posix()
            connection.execute(
                """
                INSERT INTO thumbnails (
                    media_id, thumbnail_type, cache_class, variant_key, output_rel_path,
                    width, height, file_size_bytes, source_size_bytes,
                    source_modified_time, algorithm_version, status, created_at, updated_at
                ) VALUES (?, 'photo_tile', ?, ?, ?, 1, 1, 4, 10, 1, 'test', 'ready', 1, 1)
                """,
                (media_id, cache_class, PHOTO_TILE_VARIANT_KEY, rel_path),
            )
            if referenced:
                connection.execute(
                    """
                    INSERT INTO folder_preview_items (folder_id, selection_type, position, media_id)
                    VALUES (?, 'auto', 1, ?)
                    """,
                    (folder_id, media_id),
                )
            connection.commit()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"tile")
        return media_id, path

    def _row(self, config, media_id: int) -> sqlite3.Row:
        with open_database(config.db_path, read_only=True) as connection:
            return connection.execute(
                "SELECT * FROM thumbnails WHERE media_id = ? AND thumbnail_type = 'photo_tile'",
                (media_id,),
            ).fetchone()

    def test_dynamic_without_reference_and_protected_with_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            dynamic_id, dynamic_path = self._photo_tile(config, referenced=False)
            protected_id, old_path = self._photo_tile(config, referenced=True)

            self.assertEqual(1, reconcile_photo_tile_cache_lifecycle(
                config, media_ids=[dynamic_id, protected_id]
            ))
            self.assertTrue(dynamic_path.is_file())
            self.assertEqual("dynamic", self._row(config, dynamic_id)["cache_class"])
            protected_row = self._row(config, protected_id)
            self.assertEqual("protected", protected_row["cache_class"])
            self.assertFalse(old_path.exists())
            self.assertTrue((config.output_root / protected_row["output_rel_path"]).is_file())

    def test_last_reference_removal_demotes_but_multiple_references_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            media_id, _ = self._photo_tile(config, referenced=True)
            with open_database(config.db_path, read_only=False) as connection:
                folder_id = int(connection.execute("SELECT id FROM folders LIMIT 1").fetchone()[0])
                connection.execute(
                    "INSERT INTO folder_preview_items (folder_id, selection_type, position, media_id) VALUES (?, 'auto_parent', 2, ?)",
                    (folder_id, media_id),
                )
                connection.commit()
            reconcile_photo_tile_cache_lifecycle(config, media_ids=[media_id])
            self.assertEqual("protected", self._row(config, media_id)["cache_class"])

            with open_database(config.db_path, read_only=False) as connection:
                connection.execute("DELETE FROM folder_preview_items WHERE selection_type = 'auto'")
                connection.commit()
            self.assertEqual(0, reconcile_photo_tile_cache_lifecycle(config, media_ids=[media_id]))
            self.assertEqual("protected", self._row(config, media_id)["cache_class"])

            with open_database(config.db_path, read_only=False) as connection:
                connection.execute("DELETE FROM folder_preview_items WHERE media_id = ?", (media_id,))
                connection.commit()
            self.assertEqual(1, reconcile_photo_tile_cache_lifecycle(config, media_ids=[media_id]))
            row = self._row(config, media_id)
            self.assertEqual("dynamic", row["cache_class"])
            self.assertTrue((config.output_root / row["output_rel_path"]).is_file())

    def test_interrupted_move_and_duplicate_are_reconciled_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            media_id, source = self._photo_tile(config, referenced=True)
            target = config.protected_photo_tile_cache_dir / "aa" / "bb" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)

            self.assertEqual(1, reconcile_photo_tile_cache_lifecycle(config, media_ids=[media_id]))
            self.assertEqual(0, reconcile_photo_tile_cache_lifecycle(config, media_ids=[media_id]))
            self.assertFalse(source.exists())
            self.assertTrue(target.is_file())

            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"duplicate")
            self.assertEqual(1, reconcile_photo_tile_cache_lifecycle(config, media_ids=[media_id]))
            self.assertFalse(source.exists())
            self.assertTrue(target.is_file())

    def test_explicit_reconciliation_does_not_touch_unrelated_photo_tile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            selected_id, _ = self._photo_tile(config, referenced=True)
            unrelated_id, unrelated_path = self._photo_tile(config, referenced=True)

            self.assertEqual(1, reconcile_photo_tile_cache_lifecycle(
                config, media_ids=[selected_id]
            ))
            self.assertEqual("protected", self._row(config, selected_id)["cache_class"])
            self.assertEqual("dynamic", self._row(config, unrelated_id)["cache_class"])
            self.assertTrue(unrelated_path.is_file())

    def test_new_photo_tile_uses_reference_derived_cache_class(self) -> None:
        for referenced, expected_class in ((False, "dynamic"), (True, "protected")):
            with self.subTest(referenced=referenced), tempfile.TemporaryDirectory() as temp:
                config = self._instance(Path(temp))
                media_id, old_path = self._photo_tile(config, referenced=referenced)
                old_path.unlink()
                with open_database(config.db_path, read_only=False) as connection:
                    connection.execute("DELETE FROM thumbnails WHERE media_id = ?", (media_id,))
                    connection.commit()

                sentinel = object()
                with patch("catalog_app.thumbnail_cache._generate_photo_tile", return_value=sentinel) as generate:
                    result = photo_tile_resource(
                        config,
                        media_id=media_id,
                        rel_path="folder_a/example.jpg",
                        source_path=config.data_root / "folder_a" / "example.jpg",
                        source_size_bytes=10,
                        source_modified_time=1,
                    )
                self.assertIs(sentinel, result)
                self.assertEqual(expected_class, generate.call_args.kwargs["cache_class"])
                expected_root = (
                    config.protected_photo_tile_cache_dir
                    if referenced
                    else config.photo_tile_cache_dir
                )
                self.assertTrue(generate.call_args.kwargs["destination"].is_relative_to(expected_root))

    def test_cleanup_statistics_and_layout_follow_cache_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            dynamic_id, _ = self._photo_tile(config, referenced=False)
            protected_id, _ = self._photo_tile(config, referenced=True)
            reconcile_photo_tile_cache_lifecycle(config, media_ids=[dynamic_id, protected_id])

            with open_database(config.db_path, read_only=True) as connection:
                summary = thumbnail_cache_database_summary(connection, limit_bytes=0)
                plan = thumbnail_cache_cleanup_plan(connection, limit_bytes=0)
            self.assertEqual(1, summary["by_cache_class"]["dynamic"]["entries"])
            self.assertEqual(1, summary["by_cache_class"]["protected"]["entries"])
            self.assertEqual(1, plan["candidate_entries"])
            self.assertNotIn("skipped_referenced_entries", plan)
            self.assertEqual("protected", self._row(config, protected_id)["cache_class"])

            layout = thumbnail_cache_layout(config)
            kinds = {(item.kind, item.cache_class, item.path.name) for item in layout.directories}
            self.assertIn(("photo_tile", "dynamic", "photo_tiles"), kinds)
            self.assertIn(("photo_tile", "protected", "photo_tiles"), kinds)
            self.assertNotIn("folder_preview", {item.kind for item in layout.directories})

    def test_preview_replacement_reconciles_old_and_new_but_not_unrelated_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            old_id, _ = self._photo_tile(config, referenced=True)
            new_id, _ = self._photo_tile(config, referenced=False)
            unrelated_id, _ = self._photo_tile(config, referenced=False)
            with open_database(config.db_path, read_only=True) as connection:
                target_folder_id = int(connection.execute(
                    "SELECT folder_id FROM media_files WHERE id = ?", (old_id,)
                ).fetchone()["folder_id"])
            report = SimpleNamespace(
                folder_id=target_folder_id,
                candidates=(SimpleNamespace(position=1, media_id=new_id),),
            )

            with patch(
                "catalog_app.folder_preview_candidates.folder_preview_candidate_report",
                return_value=report,
            ), patch(
                "catalog_app.folder_preview_candidates.reconcile_photo_tile_cache_lifecycle"
            ) as reconcile:
                apply_folder_preview_candidates(config, folder_rel_path="folder_1")

            reconciled_ids = set(reconcile.call_args.kwargs["media_ids"])
            self.assertEqual({old_id, new_id}, reconciled_ids)
            self.assertNotIn(unrelated_id, reconciled_ids)

    def test_destination_has_one_class_specific_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            dynamic = _photo_tile_destination(
                config, rel_path="folder_a/example.jpg", source_size_bytes=10, source_modified_time=1,
                cache_class="dynamic",
            )
            protected = _photo_tile_destination(
                config, rel_path="folder_a/example.jpg", source_size_bytes=10, source_modified_time=1,
                cache_class="protected",
            )
            self.assertEqual(dynamic.name, protected.name)
            self.assertNotEqual(dynamic, protected)


if __name__ == "__main__":
    unittest.main()

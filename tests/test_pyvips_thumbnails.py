from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_app.config import load_config
from catalog_app.database import initialize_database, open_database
from catalog_app.media_types import classify_media
from catalog_app.scan_activate import _invalidate_changed_derived_rows
from catalog_app.setup_instance import _instance_config_text
from catalog_app.thumbnail_cache import (
    GIF_PREVIEW_ALGORITHM_VERSION,
    PHOTO_TILE_ALGORITHM_VERSION,
    PHOTO_TILE_QUALITY,
    THUMBNAIL_WEBP_EFFORT,
    VIDEO_POSTER_ALGORITHM_VERSION,
    _delete_replaced_thumbnail_file,
    _ffmpeg_frame_scale_filter,
    _ready_existing_gif_preview,
    _ready_existing_photo_tile,
    _webp_compatible_image,
    _write_webp_thumbnail,
    gif_preview_existing_resource,
    ready_cached_thumbnail_resource,
    video_poster_existing_resource,
)


ROOT = Path(__file__).resolve().parents[1]
THUMBNAIL_CACHE_PY = ROOT / "catalog_app" / "thumbnail_cache.py"
REQUIREMENTS = ROOT / "requirements.txt"


class _FakeVipsImage:
    def __init__(self, *, width=600, height=400, interpretation="srgb", format="uchar"):
        self.width = width
        self.height = height
        self.interpretation = interpretation
        self.format = format
        self.calls: list[tuple[str, tuple, dict]] = []

    def colourspace(self, space):
        self.calls.append(("colourspace", (space,), {}))
        self.interpretation = space
        self.format = "uchar"
        return self

    def cast(self, format):
        self.calls.append(("cast", (format,), {}))
        self.format = format
        return self

    def webpsave(self, path, **kwargs):
        self.calls.append(("webpsave", (path,), kwargs))


def _fake_pyvips(image: _FakeVipsImage, *, at_least_8_15: bool = True):
    module = types.ModuleType("pyvips")
    thumbnail_calls: list[tuple[tuple, dict]] = []

    class Image:
        @staticmethod
        def thumbnail(*args, **kwargs):
            thumbnail_calls.append((args, kwargs))
            return image

    module.Image = Image
    module.at_least_libvips = lambda major, minor: at_least_8_15
    module.thumbnail_calls = thumbnail_calls
    return module


class PyvipsWriterTests(unittest.TestCase):
    def test_writer_fits_box_without_upscaling_and_strips_metadata(self) -> None:
        image = _FakeVipsImage(width=600, height=400)
        fake = _fake_pyvips(image)
        with patch.dict(sys.modules, {"pyvips": fake}):
            size = _write_webp_thumbnail(
                Path("source.jpg"),
                Path(".tile.webp.tmp"),
                box_width=600,
                box_height=800,
                quality=PHOTO_TILE_QUALITY,
            )

        self.assertEqual((600, 400), size)
        args, kwargs = fake.thumbnail_calls[0]
        self.assertEqual(("source.jpg", 600), args)
        self.assertEqual({"height": 800, "size": "down"}, kwargs)
        name, save_args, save_kwargs = image.calls[-1]
        self.assertEqual("webpsave", name)
        self.assertEqual((".tile.webp.tmp",), save_args)
        self.assertEqual(
            {"Q": 82, "effort": THUMBNAIL_WEBP_EFFORT, "keep": 0},
            save_kwargs,
        )

    def test_writer_uses_strip_on_older_libvips(self) -> None:
        image = _FakeVipsImage()
        with patch.dict(sys.modules, {"pyvips": _fake_pyvips(image, at_least_8_15=False)}):
            _write_webp_thumbnail(
                Path("frame.png"),
                Path("out.tmp"),
                box_width=640,
                box_height=640,
                quality=82,
            )
        self.assertTrue(image.calls[-1][2]["strip"])
        self.assertNotIn("keep", image.calls[-1][2])

    def test_compatible_image_converts_to_8_bit(self) -> None:
        cases = (
            ("rgb16", "ushort", "srgb"),
            ("grey16", "ushort", "b-w"),
            ("cmyk", "uchar", "srgb"),
        )
        for interpretation, pixel_format, expected_space in cases:
            with self.subTest(interpretation=interpretation):
                image = _webp_compatible_image(
                    _FakeVipsImage(interpretation=interpretation, format=pixel_format)
                )
                self.assertEqual(("colourspace", (expected_space,), {}), image.calls[0])
                self.assertEqual("uchar", image.format)

        plain = _webp_compatible_image(_FakeVipsImage())
        self.assertEqual([], plain.calls)

    def test_ffmpeg_scale_filter_uses_twice_preview_width_without_upscaling(self) -> None:
        config = types.SimpleNamespace(video_preview_width=640)
        self.assertEqual(
            "scale=w='min(iw,1280)':h='min(ih,1280)':force_original_aspect_ratio=decrease",
            _ffmpeg_frame_scale_filter(config),
        )

    def test_both_video_generators_pass_the_scale_filter(self) -> None:
        source = THUMBNAIL_CACHE_PY.read_text(encoding="utf-8")
        self.assertEqual(2, source.count('"-vf",\n            _ffmpeg_frame_scale_filter(config),'))

    def test_pillow_is_not_used(self) -> None:
        source = THUMBNAIL_CACHE_PY.read_text(encoding="utf-8")
        self.assertNotIn("from PIL", source)
        requirements = REQUIREMENTS.read_text(encoding="utf-8")
        self.assertNotIn("Pillow", requirements)
        self.assertIn("pyvips[binary]", requirements)

    def test_replaced_file_is_deleted_only_when_path_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "old.webp"
            new = root / "new.webp"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            _delete_replaced_thumbnail_file(new, new)
            self.assertTrue(new.is_file())
            _delete_replaced_thumbnail_file(old, new)
            self.assertFalse(old.exists())
            _delete_replaced_thumbnail_file(None, new)
            self.assertTrue(new.is_file())


class MediaTypeTests(unittest.TestCase):
    def test_bmp_is_other(self) -> None:
        self.assertEqual("other", classify_media(".bmp"))
        for extension in (".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp"):
            with self.subTest(extension=extension):
                self.assertEqual("image", classify_media(extension))
        self.assertEqual("gif", classify_media(".gif"))


class _CatalogFixture:
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

    def _media(self, connection: sqlite3.Connection, *, media_type: str, extension: str) -> tuple[int, int]:
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
            connection.execute(
                """
                INSERT INTO scan_folders (
                    scan_id, rel_path, path_key, parent_path_key, name, depth, sort_key
                ) VALUES (?, '', '', NULL, 'Root', 0, 'root')
                """,
                (scan_id,),
            )
        else:
            scan_id = int(scan_row[0])
            folder_id = int(connection.execute(
                "SELECT id FROM folders WHERE rel_path = ''"
            ).fetchone()[0])
        file_name = f"example{extension}"
        media_id = int(connection.execute(
            """
            INSERT INTO media_files (
                rel_path, path_key, folder_id, file_name, extension, media_type,
                size_bytes, modified_time, sort_key, last_successful_scan_id, is_available
            ) VALUES (?, ?, ?, ?, ?, ?, 10, 1, ?, ?, 1)
            """,
            (file_name, file_name, folder_id, file_name, extension, media_type, file_name, scan_id),
        ).lastrowid)
        return scan_id, media_id

    def _thumbnail(
        self,
        connection: sqlite3.Connection,
        config,
        *,
        media_id: int,
        thumbnail_type: str,
        algorithm_version: str,
        cache_class: str = "dynamic",
    ) -> Path:
        path = config.thumbnail_cache_dir / "test" / f"{thumbnail_type}_{media_id}.webp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"webp")
        connection.execute(
            """
            INSERT INTO thumbnails (
                media_id, thumbnail_type, cache_class, variant_key, output_rel_path,
                width, height, file_size_bytes, source_size_bytes,
                source_modified_time, algorithm_version, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'default', ?, 1, 1, 4, 10, 1, ?, 'ready', 1, 1)
            """,
            (
                media_id,
                thumbnail_type,
                cache_class,
                path.relative_to(config.output_root).as_posix(),
                algorithm_version,
            ),
        )
        return path


class AlgorithmVersionReadyCheckTests(_CatalogFixture, unittest.TestCase):
    def test_outdated_photo_tile_is_not_ready(self) -> None:
        for version, expected_ready in (
            (PHOTO_TILE_ALGORITHM_VERSION, True),
            ("photo_tile_v1_webp_fit", False),
        ):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                config = self._instance(Path(temp))
                with open_database(config.db_path, read_only=False) as connection:
                    _, media_id = self._media(connection, media_type="image", extension=".jpg")
                    self._thumbnail(
                        connection,
                        config,
                        media_id=media_id,
                        thumbnail_type="photo_tile",
                        algorithm_version=version,
                    )
                    connection.commit()

                fast = ready_cached_thumbnail_resource(
                    config,
                    media_id=media_id,
                    thumbnail_type="photo_tile",
                    variant_key="default",
                )
                existing = _ready_existing_photo_tile(
                    config,
                    media_id=media_id,
                    source_size_bytes=10,
                    source_modified_time=1,
                )
                self.assertEqual(expected_ready, fast is not None)
                self.assertEqual(expected_ready, existing is not None)

    def test_outdated_gif_and_video_previews_stay_served_until_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            with open_database(config.db_path, read_only=False) as connection:
                _, gif_id = self._media(connection, media_type="gif", extension=".gif")
                self._thumbnail(
                    connection,
                    config,
                    media_id=gif_id,
                    thumbnail_type="gif_preview",
                    algorithm_version="gif_preview_v1_webp_first_frame_fit",
                    cache_class="protected",
                )
                connection.commit()
            with open_database(config.db_path, read_only=False) as connection:
                _, video_id = self._media(connection, media_type="video", extension=".mp4")
                self._thumbnail(
                    connection,
                    config,
                    media_id=video_id,
                    thumbnail_type="video_poster",
                    algorithm_version="video_poster_v2_webp_ffmpeg_20_fit",
                    cache_class="protected",
                )
                connection.commit()

            self.assertNotEqual("gif_preview_v1_webp_first_frame_fit", GIF_PREVIEW_ALGORITHM_VERSION)
            self.assertNotEqual("video_poster_v2_webp_ffmpeg_20_fit", VIDEO_POSTER_ALGORITHM_VERSION)

            self.assertIsNotNone(ready_cached_thumbnail_resource(
                config, media_id=gif_id, thumbnail_type="gif_preview", variant_key="default",
            ))
            self.assertIsNotNone(gif_preview_existing_resource(
                config, media_id=gif_id, source_size_bytes=10, source_modified_time=1,
            ))
            self.assertIsNotNone(video_poster_existing_resource(
                config, media_id=video_id, source_size_bytes=10, source_modified_time=1,
            ))
            # Generating paths (folder preview candidates) regenerate outdated previews.
            self.assertIsNone(_ready_existing_gif_preview(
                config, media_id=gif_id, source_size_bytes=10, source_modified_time=1,
            ))


class MediaTypeChangeInvalidationTests(_CatalogFixture, unittest.TestCase):
    def test_media_type_change_marks_thumbnails_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._instance(Path(temp))
            with open_database(config.db_path, read_only=False) as connection:
                scan_id, media_id = self._media(connection, media_type="image", extension=".bmp")
                self._thumbnail(
                    connection,
                    config,
                    media_id=media_id,
                    thumbnail_type="photo_tile",
                    algorithm_version=PHOTO_TILE_ALGORITHM_VERSION,
                )
                connection.execute(
                    """
                    INSERT INTO scan_media_files (
                        scan_id, rel_path, path_key, folder_path_key,
                        file_name, extension, media_type, size_bytes, modified_time, sort_key
                    )
                    SELECT ?, rel_path, path_key, '', file_name, extension, 'other',
                           size_bytes, modified_time, sort_key
                    FROM media_files
                    WHERE id = ?
                    """,
                    (scan_id, media_id),
                )
                _invalidate_changed_derived_rows(connection, scan_id)
                status = connection.execute(
                    "SELECT status FROM thumbnails WHERE media_id = ?",
                    (media_id,),
                ).fetchone()[0]
                connection.rollback()

            self.assertEqual("stale", status)


if __name__ == "__main__":
    unittest.main()

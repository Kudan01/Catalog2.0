from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_app.api import runtime_settings_status
from catalog_app.config import load_config
from catalog_app.database import initialize_database, open_database
from catalog_app.setup_instance import DEFAULT_INSTANCE_CONFIG, _instance_config_text
from catalog_app.thumbnail_cache import (
    VIDEO_FRAME_ALGORITHM_VERSION,
    VIDEO_POSTER_ALGORITHM_VERSION,
    _ffmpeg_frame_scale_filter,
    _VideoImageRequest,
    _extract_video_images,
    _generate_video_poster,
    _video_job_worker_count,
    generate_video_frames_for_scope,
    generate_video_posters_for_scope,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "catalog_app" / "static"
MODULE = "catalog_app.thumbnail_cache"


def _instance(root: Path, *, extra_config: dict | None = None):
    data_root = root / "data"
    output_root = root / "Catalog_Output"
    data_root.mkdir()
    output_root.mkdir()
    raw = json.loads(_instance_config_text(config_data_root=str(data_root)))
    raw.update(extra_config or {})
    config_path = output_root / "config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(config_path)


def _source_files(config, count: int) -> list[dict[str, object]]:
    rows = []
    for index in range(1, count + 1):
        rel_path = f"video_{index}.mp4"
        path = config.data_root / rel_path
        path.write_bytes(b"x" * index)
        stat_result = path.stat()
        rows.append({
            "id": index,
            "rel_path": rel_path,
            "size_bytes": int(stat_result.st_size),
            "modified_time": float(stat_result.st_mtime),
        })
    return rows


class VideoExtractionCommandTests(unittest.TestCase):
    def _video_media(self, config) -> tuple[int, Path]:
        initialize_database(config.db_path)
        source = config.data_root / "clip.mp4"
        source.write_bytes(b"video")
        with open_database(config.db_path, read_only=False) as connection:
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
            media_id = int(connection.execute(
                """
                INSERT INTO media_files (
                    rel_path, path_key, folder_id, file_name, extension, media_type,
                    size_bytes, modified_time, sort_key, last_successful_scan_id, is_available
                ) VALUES ('clip.mp4', 'clip.mp4', ?, 'clip.mp4', '.mp4', 'video', 5, 1, 'clip.mp4', ?, 1)
                """,
                (folder_id, scan_id),
            ).lastrowid)
            connection.commit()
        return media_id, source

    def _run_with_fake_tools(self, generate) -> list[list[str]]:
        commands: list[list[str]] = []

        def fake_run(command, **kwargs):
            commands.append(list(command))
            Path(command[-1]).write_bytes(b"png")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        def fake_write(source_path, temp_path, **kwargs):
            Path(temp_path).write_bytes(b"webp")
            return 640, 360

        with patch(f"{MODULE}._require_pyvips"), patch(
            f"{MODULE}.require_video_tools",
            return_value=SimpleNamespace(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"),
        ), patch(f"{MODULE}._probe_video_duration", return_value=10.0), patch(
            f"{MODULE}.subprocess.run", side_effect=fake_run
        ), patch(f"{MODULE}._write_webp_thumbnail", side_effect=fake_write):
            generate()
        return commands

    def _assert_keyframe_command(self, config, command: list[str]) -> None:
        input_index = command.index("-i")
        before_input = command[:input_index]
        self.assertIn("-noaccurate_seek", before_input)
        skip_index = command.index("-skip_frame")
        self.assertLess(skip_index, input_index)
        self.assertEqual("nokey", command[skip_index + 1])
        threads_index = command.index("-threads")
        self.assertLess(threads_index, input_index)
        self.assertEqual("1", command[threads_index + 1])
        self.assertLess(command.index("-ss"), input_index)
        vf_index = command.index("-vf")
        self.assertEqual(_ffmpeg_frame_scale_filter(config), command[vf_index + 1])

    def _algorithm_version(self, config, media_id: int, thumbnail_type: str) -> str:
        with open_database(config.db_path, read_only=True) as connection:
            return str(connection.execute(
                "SELECT algorithm_version FROM thumbnails WHERE media_id = ? AND thumbnail_type = ?",
                (media_id, thumbnail_type),
            ).fetchone()[0])

    def test_poster_uses_keyframe_extraction_and_single_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            media_id, source = self._video_media(config)
            commands = self._run_with_fake_tools(lambda: _generate_video_poster(
                config,
                media_id=media_id,
                rel_path="clip.mp4",
                source_path=source,
                source_size_bytes=5,
                source_modified_time=1,
            ))
            self.assertEqual(1, len(commands))
            self._assert_keyframe_command(config, commands[0])
            self.assertEqual(
                VIDEO_POSTER_ALGORITHM_VERSION,
                self._algorithm_version(config, media_id, "video_poster"),
            )

    def test_frame_uses_keyframe_extraction_and_single_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            media_id, source = self._video_media(config)
            commands = self._run_with_fake_tools(lambda: _extract_video_images(
                config,
                "ffmpeg",
                media_id=media_id,
                rel_path="clip.mp4",
                source_path=source,
                source_size_bytes=5,
                source_modified_time=1,
                requests=[_VideoImageRequest("video_frame", "frame_1", 3.5)],
            ))
            self.assertEqual(1, len(commands))
            self._assert_keyframe_command(config, commands[0])
            self.assertEqual(
                VIDEO_FRAME_ALGORITHM_VERSION,
                self._algorithm_version(config, media_id, "video_frame"),
            )


class VideoJobWorkerCountTests(unittest.TestCase):
    def test_worker_count_follows_cores_and_is_capped(self) -> None:
        cases = ((None, 1), (2, 1), (4, 1), (8, 2), (16, 4), (32, 4), (64, 4))
        for cpu_count, expected in cases:
            with self.subTest(cpu_count=cpu_count), patch(
                f"{MODULE}.os.cpu_count", return_value=cpu_count
            ):
                self.assertEqual(expected, _video_job_worker_count())


class VideoScopeConcurrencyTests(unittest.TestCase):
    """Run with 4 workers; the first 4 items meet at a barrier to prove overlap.

    Only the first 4 items wait at the barrier, so the remaining items can never
    form an incomplete group. The barrier timeout fails the test instead of
    hanging it.
    """

    WORKERS = 4

    def test_posters_run_concurrently_and_keep_payload_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            rows = _source_files(config, 6)
            barrier = threading.Barrier(self.WORKERS, timeout=5)

            def fake_generate(config, *, media_id, **kwargs):
                if media_id <= self.WORKERS:
                    barrier.wait()
                if media_id == 5:
                    raise RuntimeError("poster failed")

            with patch(f"{MODULE}._video_job_worker_count", return_value=self.WORKERS), patch(
                f"{MODULE}._video_poster_work_rows", return_value=rows
            ), patch(f"{MODULE}._generate_video_poster", side_effect=fake_generate), patch(
                f"{MODULE}._record_video_poster_error"
            ) as record_error:
                result = generate_video_posters_for_scope(config)

            self.assertEqual(6, result["processed"])
            self.assertEqual(5, result["created"])
            self.assertEqual(0, result["reused"])
            self.assertEqual(1, result["errors"])
            self.assertEqual(
                [{"path": "video_5.mp4", "technical_detail": "poster failed"}],
                result["error_samples"],
            )
            self.assertEqual(
                {
                    "thumbnail_job", "thumbnail_type", "cache_class", "scope", "branch",
                    "processed", "created", "reused", "errors", "error_samples",
                    "duration_seconds", "writes",
                },
                set(result),
            )
            record_error.assert_called_once()
            self.assertEqual(5, record_error.call_args.kwargs["media_id"])

    def test_frames_run_concurrently_per_video_and_keep_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            frame_rows = [
                {**row, "variant_key": variant_key}
                for row in _source_files(config, 6)
                for variant_key in ("frame_1", "frame_2")
            ]
            barrier = threading.Barrier(self.WORKERS, timeout=5)
            extract_calls: list[tuple[int, list[str]]] = []
            calls_lock = threading.Lock()

            def fake_extract(config, ffmpeg_path, *, media_id, requests, **kwargs):
                with calls_lock:
                    extract_calls.append((media_id, [request.variant_key for request in requests]))
                if media_id <= self.WORKERS:
                    barrier.wait()
                if media_id == 5:
                    raise RuntimeError("video failed")
                return [object() for _ in requests]

            with patch(f"{MODULE}._video_job_worker_count", return_value=self.WORKERS), patch(
                f"{MODULE}._video_frame_work_rows", return_value=frame_rows
            ), patch(
                f"{MODULE}.require_video_tools",
                return_value=SimpleNamespace(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"),
            ), patch(f"{MODULE}._probe_video_duration", return_value=10.0) as probe, patch(
                f"{MODULE}._extract_video_images", side_effect=fake_extract
            ):
                result = generate_video_frames_for_scope(config)

            # One extraction call and one ffprobe per video, with all missing frames.
            self.assertEqual(
                sorted((media_id, ["frame_1", "frame_2"]) for media_id in range(1, 7)),
                sorted(extract_calls),
            )
            self.assertEqual(6, probe.call_count)
            self.assertEqual(6, result["processed"])
            self.assertEqual(12, result["frames_processed"])
            self.assertEqual(10, result["created"])
            self.assertEqual(2, result["errors"])
            self.assertEqual(
                [{"path": "video_5.mp4", "technical_detail": "video failed"}],
                result["error_samples"],
            )
            self.assertEqual("video_frame", result["thumbnail_type"])
            self.assertEqual([35, 50, 65, 80], result["frame_positions_percent"])
            self.assertEqual(
                {
                    "thumbnail_job", "thumbnail_type", "cache_class", "scope", "branch",
                    "processed", "frames_processed", "created", "reused", "errors",
                    "error_samples", "frame_positions_percent", "duration_seconds", "writes",
                },
                set(result),
            )


class FfmpegThreadsSettingRemovedTests(unittest.TestCase):
    def test_instance_with_old_key_in_config_json_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp), extra_config={"ffmpeg_threads_per_job": 3})
            self.assertFalse(hasattr(config, "ffmpeg_threads_per_job"))
            self.assertNotIn("ffmpeg_threads_per_job", runtime_settings_status(config)["video"])
            self.assertNotIn("ffmpeg_threads_per_job", config.thumbnail_video_param_sources)

    def test_instance_with_old_key_in_settings_json_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            config.settings_json.parent.mkdir(parents=True, exist_ok=True)
            config.settings_json.write_text(
                json.dumps({"settings_version": 1, "ffmpeg_threads_per_job": 3}),
                encoding="utf-8",
            )
            config = load_config(config.config_path)
            self.assertFalse(hasattr(config, "ffmpeg_threads_per_job"))
            self.assertNotIn("ffmpeg_threads_per_job", runtime_settings_status(config)["video"])

    def test_setting_is_not_offered_anywhere(self) -> None:
        self.assertNotIn("ffmpeg_threads_per_job", DEFAULT_INSTANCE_CONFIG)
        for path in (
            STATIC / "index.html",
            STATIC / "app.js",
            STATIC / "i18n" / "en.json",
            STATIC / "i18n" / "cs.json",
        ):
            with self.subTest(path=path.name):
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("ffmpegThreads", content)
                self.assertNotIn("FfmpegThreads", content)
                self.assertNotIn("ffmpeg_threads_per_job", content)


if __name__ == "__main__":
    unittest.main()

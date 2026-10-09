from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_app.config import load_config
from catalog_app.database import initialize_database, open_database
from catalog_app.media_preview_workflow import (
    build_media_previews_for_scope,
    media_preview_workflow_result_lines,
)
from catalog_app.setup_instance import _instance_config_text
from catalog_app.thumbnail_cache import (
    VIDEO_FRAME_ALGORITHM_VERSION,
    VIDEO_FRAME_VARIANT_KEYS,
    VIDEO_POSTER_ALGORITHM_VERSION,
    VIDEO_POSTER_VARIANT_KEY,
    ThumbnailResource,
    _VideoImageRequest,
    _extract_video_images,
    _ffmpeg_frame_scale_filter,
    _video_frame_seek_time,
    _video_poster_seek_time,
    generate_video_previews_for_scope,
    video_poster_resource,
)


MODULE = "catalog_app.thumbnail_cache"
DURATION = 100.0


def _instance(root: Path):
    data_root = root / "data"
    output_root = root / "Catalog_Output"
    data_root.mkdir()
    output_root.mkdir()
    config_path = output_root / "config.json"
    config_path.write_text(_instance_config_text(config_data_root=str(data_root)), encoding="utf-8")
    return load_config(config_path)


def _video_media(config) -> tuple[int, Path]:
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


def _all_requests() -> list[_VideoImageRequest]:
    requests = [
        _VideoImageRequest("video_poster", VIDEO_POSTER_VARIANT_KEY, _video_poster_seek_time(DURATION)),
    ]
    for index, variant_key in enumerate(VIDEO_FRAME_VARIANT_KEYS, start=1):
        requests.append(_VideoImageRequest("video_frame", variant_key, _video_frame_seek_time(DURATION, index)))
    return requests


def _output_paths(command: list[str]) -> list[str]:
    return [command[index + 2] for index, arg in enumerate(command) if arg == "-frames:v"]


class _ExtractionHarness:
    """Run _extract_video_images with fake ffmpeg and WebP writing."""

    def _extract(self, config, media_id, source, requests, fake_run):
        commands: list[list[str]] = []

        def recording_run(command, **kwargs):
            commands.append(list(command))
            return fake_run(list(command))

        def fake_write(source_path, temp_path, **kwargs):
            Path(temp_path).write_bytes(b"webp")
            return 640, 360

        with patch(f"{MODULE}._require_pyvips"), patch(
            f"{MODULE}.subprocess.run", side_effect=recording_run
        ), patch(f"{MODULE}._write_webp_thumbnail", side_effect=fake_write):
            outcomes = _extract_video_images(
                config,
                "ffmpeg",
                media_id=media_id,
                rel_path="clip.mp4",
                source_path=source,
                source_size_bytes=5,
                source_modified_time=1,
                requests=requests,
            )
        return outcomes, commands

    def _rows(self, config, media_id) -> dict[str, tuple[str, str]]:
        with open_database(config.db_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT variant_key, thumbnail_type, status, algorithm_version FROM thumbnails WHERE media_id = ?",
                (media_id,),
            ).fetchall()
        return {
            f"{row['thumbnail_type']}:{row['variant_key']}": (str(row["status"]), str(row["algorithm_version"]))
            for row in rows
        }


def _writes_all_outputs(command):
    for path in _output_paths(command):
        Path(path).write_bytes(b"png")
    return SimpleNamespace(returncode=0, stderr="", stdout="")


class SingleCallExtractionTests(_ExtractionHarness, unittest.TestCase):
    def test_algorithm_versions_are_unchanged_from_step_1(self) -> None:
        self.assertEqual("video_poster_v4_webp_ffmpeg_keyframe_scale_vips_20_fit", VIDEO_POSTER_ALGORITHM_VERSION)
        self.assertEqual(
            "video_frame_v4_webp_ffmpeg_keyframe_scale_vips_35_50_65_80_fit",
            VIDEO_FRAME_ALGORITHM_VERSION,
        )

    def test_poster_and_frames_use_one_ffmpeg_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            media_id, source = _video_media(config)
            requests = _all_requests()
            outcomes, commands = self._extract(config, media_id, source, requests, _writes_all_outputs)

            self.assertEqual(1, len(commands))
            command = commands[0]
            self.assertEqual(5, command.count("-i"))
            input_indexes = [index for index, arg in enumerate(command) if arg == "-i"]
            previous_end = 0
            for request, input_index in zip(requests, input_indexes):
                options = command[previous_end:input_index]
                self.assertEqual(f"{request.seek_time:.3f}", options[options.index("-ss") + 1])
                self.assertIn("-noaccurate_seek", options)
                self.assertEqual("nokey", options[options.index("-skip_frame") + 1])
                self.assertEqual("1", options[options.index("-threads") + 1])
                self.assertEqual(str(source), command[input_index + 1])
                previous_end = input_index + 2
            for index in range(5):
                map_index = command.index(f"{index}:V:0")
                self.assertEqual("-map", command[map_index - 1])
                self.assertEqual("-vf", command[map_index + 1])
                self.assertEqual(_ffmpeg_frame_scale_filter(config), command[map_index + 2])
                self.assertEqual(["-frames:v", "1"], command[map_index + 3:map_index + 5])
            self.assertEqual(5, len(_output_paths(command)))

            self.assertTrue(all(isinstance(outcome, ThumbnailResource) for outcome in outcomes))
            rows = self._rows(config, media_id)
            self.assertEqual(
                ("ready", VIDEO_POSTER_ALGORITHM_VERSION),
                rows[f"video_poster:{VIDEO_POSTER_VARIANT_KEY}"],
            )
            for variant_key in VIDEO_FRAME_VARIANT_KEYS:
                self.assertEqual(("ready", VIDEO_FRAME_ALGORITHM_VERSION), rows[f"video_frame:{variant_key}"])

    def test_missing_image_is_retried_alone_and_only_its_error_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            media_id, source = _video_media(config)
            requests = _all_requests()

            def fake_run(command):
                if command.count("-i") > 1:
                    # The shared call produces every image except frame_3.
                    for path in _output_paths(command)[:3] + _output_paths(command)[4:]:
                        Path(path).write_bytes(b"png")
                    return SimpleNamespace(returncode=0, stderr="", stdout="")
                return SimpleNamespace(returncode=1, stderr="seek failed", stdout="")

            outcomes, commands = self._extract(config, media_id, source, requests, fake_run)

            self.assertEqual(2, len(commands))
            retry = commands[1]
            self.assertEqual(1, retry.count("-i"))
            self.assertEqual(f"{requests[3].seek_time:.3f}", retry[retry.index("-ss") + 1])
            self.assertEqual(
                "ffmpeg did not create video frame frame_3 for clip.mp4: seek failed",
                outcomes[3],
            )
            for index in (0, 1, 2, 4):
                self.assertIsInstance(outcomes[index], ThumbnailResource)
            rows = self._rows(config, media_id)
            self.assertEqual("error", rows["video_frame:frame_3"][0])
            self.assertEqual("ready", rows[f"video_poster:{VIDEO_POSTER_VARIANT_KEY}"][0])
            for variant_key in ("frame_1", "frame_2", "frame_4"):
                self.assertEqual("ready", rows[f"video_frame:{variant_key}"][0])

    def test_timeout_records_every_missing_image_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            media_id, source = _video_media(config)
            requests = _all_requests()

            def fake_run(command):
                raise subprocess.TimeoutExpired(command, config.ffmpeg_timeout_seconds)

            outcomes, commands = self._extract(config, media_id, source, requests, fake_run)

            self.assertEqual(1, len(commands))
            self.assertEqual(
                f"ffmpeg exceeded the {config.ffmpeg_timeout_seconds} s timeout while creating a poster: clip.mp4",
                outcomes[0],
            )
            for outcome, variant_key in zip(outcomes[1:], VIDEO_FRAME_VARIANT_KEYS):
                self.assertEqual(
                    f"ffmpeg exceeded the {config.ffmpeg_timeout_seconds} s timeout while creating video frame "
                    f"{variant_key}: clip.mp4",
                    outcome,
                )
            self.assertTrue(all(status == "error" for status, _ in self._rows(config, media_id).values()))

    def test_poster_resource_uses_one_single_input_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            media_id, source = _video_media(config)
            commands: list[list[str]] = []

            def fake_run(command, **kwargs):
                commands.append(list(command))
                return _writes_all_outputs(list(command))

            def fake_write(source_path, temp_path, **kwargs):
                Path(temp_path).write_bytes(b"webp")
                return 640, 360

            with patch(f"{MODULE}._require_pyvips"), patch(
                f"{MODULE}.require_video_tools",
                return_value=SimpleNamespace(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"),
            ), patch(f"{MODULE}._probe_video_duration", return_value=DURATION), patch(
                f"{MODULE}.subprocess.run", side_effect=fake_run
            ), patch(f"{MODULE}._write_webp_thumbnail", side_effect=fake_write):
                resource = video_poster_resource(
                    config,
                    media_id=media_id,
                    rel_path="clip.mp4",
                    source_path=source,
                    source_size_bytes=5,
                    source_modified_time=1,
                )

            self.assertEqual("video_poster", resource.thumbnail_type)
            self.assertTrue(resource.generated)
            self.assertEqual(1, len(commands))
            self.assertEqual(1, commands[0].count("-i"))
            self.assertEqual(
                f"{_video_poster_seek_time(DURATION):.3f}",
                commands[0][commands[0].index("-ss") + 1],
            )


def _source_rows(config, count: int) -> list[dict[str, object]]:
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


class VideoPreviewsScopeTests(unittest.TestCase):
    WORKERS = 4

    def test_one_pass_per_video_with_union_of_work_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            rows = _source_rows(config, 6)
            # Videos 1-5 need a poster; videos 1-4 need two frames; video 6 needs
            # only its four frames.
            poster_rows = rows[:5]
            frame_rows = [
                {**row, "variant_key": variant_key}
                for row in rows[:4]
                for variant_key in ("frame_1", "frame_2")
            ] + [{**rows[5], "variant_key": variant_key} for variant_key in VIDEO_FRAME_VARIANT_KEYS]
            barrier = threading.Barrier(self.WORKERS, timeout=5)
            extract_calls: dict[int, list[tuple[str, str]]] = {}
            calls_lock = threading.Lock()

            def fake_extract(config, ffmpeg_path, *, media_id, requests, **kwargs):
                with calls_lock:
                    extract_calls[media_id] = [
                        (request.thumbnail_type, request.variant_key) for request in requests
                    ]
                # Only the first 4 videos meet at the barrier.
                if media_id <= self.WORKERS:
                    barrier.wait()
                if media_id == 5:
                    raise RuntimeError("video failed")
                return [object() for _ in requests]

            with patch(f"{MODULE}._video_job_worker_count", return_value=self.WORKERS), patch(
                f"{MODULE}._video_poster_work_rows", return_value=poster_rows
            ) as poster_work, patch(
                f"{MODULE}._video_frame_work_rows", return_value=frame_rows
            ) as frame_work, patch(
                f"{MODULE}.require_video_tools",
                return_value=SimpleNamespace(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"),
            ), patch(f"{MODULE}._probe_video_duration", return_value=DURATION) as probe, patch(
                f"{MODULE}._extract_video_images", side_effect=fake_extract
            ), patch(f"{MODULE}._record_video_poster_error") as record_poster_error:
                result = generate_video_previews_for_scope(config, branch_rel_path="Branch/Videos")

            poster = ("video_poster", VIDEO_POSTER_VARIANT_KEY)
            two_frames = [("video_frame", "frame_1"), ("video_frame", "frame_2")]
            self.assertEqual([poster, *two_frames], extract_calls[1])
            self.assertEqual([poster], extract_calls[5])
            self.assertEqual(
                [("video_frame", variant_key) for variant_key in VIDEO_FRAME_VARIANT_KEYS],
                extract_calls[6],
            )
            self.assertEqual(6, len(extract_calls))
            self.assertEqual(6, probe.call_count)
            poster_work.assert_called_once_with(config, branch_rel_path="Branch/Videos")
            frame_work.assert_called_once_with(config, branch_rel_path="Branch/Videos")

            self.assertEqual("video_previews", result["thumbnail_type"])
            self.assertEqual("branch", result["scope"])
            self.assertEqual(6, result["processed"])
            self.assertEqual(12, result["frames_processed"])
            self.assertEqual(16, result["created"])
            self.assertEqual(0, result["reused"])
            self.assertEqual(1, result["errors"])
            self.assertEqual(
                [{"path": "video_5.mp4", "technical_detail": "video failed"}],
                result["error_samples"],
            )
            record_poster_error.assert_called_once()
            self.assertEqual(5, record_poster_error.call_args.kwargs["media_id"])

    def test_unavailable_source_counts_every_missing_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _instance(Path(temp))
            missing = {"id": 1, "rel_path": "missing.mp4", "size_bytes": 1, "modified_time": 1.0}
            frame_rows = [{**missing, "variant_key": variant_key} for variant_key in ("frame_1", "frame_2")]

            with patch(f"{MODULE}._video_poster_work_rows", return_value=[missing]), patch(
                f"{MODULE}._video_frame_work_rows", return_value=frame_rows
            ), patch(f"{MODULE}._extract_video_images") as extract, patch(
                f"{MODULE}._record_video_poster_error"
            ) as record_poster_error:
                result = generate_video_previews_for_scope(config)

            extract.assert_not_called()
            self.assertEqual(1, result["processed"])
            self.assertEqual(2, result["frames_processed"])
            self.assertEqual(0, result["created"])
            self.assertEqual(3, result["errors"])
            self.assertEqual(1, len(result["error_samples"]))
            record_poster_error.assert_called_once()


class MediaPreviewWorkflowTests(unittest.TestCase):
    def test_workflow_has_gif_and_single_video_phase(self) -> None:
        gif_phase = {
            "thumbnail_type": "gif_preview",
            "processed": 3,
            "created": 2,
            "reused": 0,
            "errors": 1,
            "error_samples": [],
            "duration_seconds": 1.0,
        }
        video_phase = {
            "thumbnail_type": "video_previews",
            "processed": 4,
            "frames_processed": 16,
            "created": 20,
            "reused": 0,
            "errors": 0,
            "error_samples": [],
            "duration_seconds": 2.5,
        }
        with patch("catalog_app.media_preview_workflow.validate_database_runtime"), patch(
            "catalog_app.media_preview_workflow.generate_gif_previews_for_scope", return_value=gif_phase
        ) as gif, patch(
            "catalog_app.media_preview_workflow.generate_video_previews_for_scope", return_value=video_phase
        ) as video:
            result = build_media_previews_for_scope(
                SimpleNamespace(db_path=Path("catalog.db")),
                branch_rel_path="Branch",
            )

        gif.assert_called_once()
        video.assert_called_once()
        self.assertEqual("Branch", video.call_args.kwargs["branch_rel_path"])
        self.assertEqual(["gif_preview", "video_previews"], [phase["thumbnail_type"] for phase in result["phases"]])
        self.assertEqual(
            {
                "processed_media": 7,
                "processed_frames": 16,
                "created": 22,
                "reused": 0,
                "errors": 1,
                "duration_seconds": 3.5,
            },
            result["totals"],
        )
        lines = media_preview_workflow_result_lines(result)
        self.assertIn("2. video previews (poster and hover frames)", lines)


if __name__ == "__main__":
    unittest.main()

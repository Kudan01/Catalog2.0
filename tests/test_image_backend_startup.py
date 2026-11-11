from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_app import launcher, server
from catalog_app.config import load_config
from catalog_app.setup_instance import _instance_config_text
from catalog_app.thumbnail_cache import ThumbnailCacheError, warm_up_image_backend


class _StopStartup(Exception):
    pass


def _fake_pyvips(calls: list[str]):
    module = types.ModuleType("pyvips")

    class _Image:
        def webpsave_buffer(self, **kwargs):
            calls.append("webpsave_buffer")
            return b"webp"

    class Image:
        @staticmethod
        def black(width, height, **kwargs):
            calls.append("black")
            return _Image()

        @staticmethod
        def thumbnail_buffer(data, width, **kwargs):
            calls.append("thumbnail_buffer")
            return _Image()

    module.Image = Image
    return module


def _config(root: Path):
    data_root = root / "data"
    output_root = root / "Catalog_Output"
    data_root.mkdir()
    output_root.mkdir()
    config_path = output_root / "config.json"
    config_path.write_text(
        _instance_config_text(config_data_root=str(data_root)),
        encoding="utf-8",
    )
    return load_config(config_path)


class WarmUpImageBackendTests(unittest.TestCase):
    def test_warm_up_encodes_and_thumbnails_in_memory(self) -> None:
        calls: list[str] = []
        with patch.dict(sys.modules, {"pyvips": _fake_pyvips(calls)}):
            warm_up_image_backend()
        self.assertEqual(["black", "webpsave_buffer", "thumbnail_buffer"], calls)

    def test_missing_pyvips_is_a_cache_error(self) -> None:
        with patch.dict(sys.modules, {"pyvips": None}):
            with self.assertRaises(ThumbnailCacheError):
                warm_up_image_backend()

    def test_prepare_reports_failure_without_raising(self) -> None:
        with patch.object(server, "warm_up_image_backend", side_effect=ThumbnailCacheError("missing")):
            self.assertFalse(server.prepare_image_backend())
        with patch.object(server, "warm_up_image_backend"):
            self.assertTrue(server.prepare_image_backend())


class StartupOrderTests(unittest.TestCase):
    def test_launcher_prepares_image_backend_before_binding(self) -> None:
        order: list[str] = []

        def _bind(*args, **kwargs):
            order.append("bind")
            raise _StopStartup()

        with tempfile.TemporaryDirectory() as temp:
            config = _config(Path(temp))
            with patch.object(
                launcher, "prepare_image_backend", side_effect=lambda: order.append("prepare")
            ), patch.object(launcher, "_bind_available_server", side_effect=_bind):
                with self.assertRaises(_StopStartup):
                    launcher.launch_catalog(config)
        self.assertEqual(["prepare", "bind"], order)

    def test_strict_server_prepares_image_backend_before_binding(self) -> None:
        order: list[str] = []

        def _bind(*args, **kwargs):
            order.append("bind")
            raise _StopStartup()

        with tempfile.TemporaryDirectory() as temp:
            config = _config(Path(temp))
            with patch.object(
                server, "prepare_image_backend", side_effect=lambda: order.append("prepare")
            ), patch.object(server, "create_http_server", side_effect=_bind):
                with self.assertRaises(_StopStartup):
                    server.run_server(config)
        self.assertEqual(["prepare", "bind"], order)


if __name__ == "__main__":
    unittest.main()

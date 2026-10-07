from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_app.api import runtime_settings_status, thumbnail_cache_status
from catalog_app.config import load_config
from catalog_app.database import initialize_database
from catalog_app.setup_instance import _instance_config_text


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "catalog_app" / "static" / "app.js"
SERVER_PY = ROOT / "catalog_app" / "server.py"


class RuntimeSettingsStatusTests(unittest.TestCase):
    def _config(self, root: Path):
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

    def test_runtime_settings_status_does_not_open_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir))
            with patch("catalog_app.api.open_database", side_effect=AssertionError("database opened")):
                settings = runtime_settings_status(config)

            self.assertEqual(config.all_page_size, settings["page_sizes"]["all_page_size"])
            self.assertEqual(config.folder_page_size, settings["page_sizes"]["folder_page_size"])
            self.assertEqual(list(config.image_thumb_size), settings["thumbnail_sizes"]["image_thumb_size"])
            self.assertEqual(config.ffmpeg_threads_per_job, settings["video"]["ffmpeg_threads_per_job"])
            self.assertEqual(config.thumbnail_cache_limit_gb, settings["thumbnail_cache_limit_gb"])
            self.assertEqual(config.ui_locale, settings["ui_locale"])
            self.assertIn("source_root", settings)

    def test_cache_status_keeps_the_same_settings_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir))
            initialize_database(config.db_path)

            payload = thumbnail_cache_status(config)

            self.assertEqual(runtime_settings_status(config), payload["settings"])
            self.assertIn("database", payload)
            self.assertIn("cleanup_plan", payload)


class SettingsFormLoadingFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.server = SERVER_PY.read_text(encoding="utf-8")

    def _function_body(self, name: str) -> str:
        start = self.app.index(f"function {name}(")
        end = self.app.index("\n}\n", start)
        return self.app[start:end]

    def test_settings_status_route_is_served(self) -> None:
        self.assertIn('if path == "/api/settings/runtime/status":', self.server)
        self.assertIn('"settings": runtime_settings_status(self.config)', self.server)

    def test_form_is_filled_from_settings_without_waiting_for_cache_status(self) -> None:
        body = self._function_body("loadCacheSettingsStatus")
        settings_request = body.index('fetchJson("/api/settings/runtime/status")')
        cache_request = body.index('fetchJson("/api/thumbnail-cache/status")')
        self.assertLess(settings_request, cache_request)
        self.assertIn("renderSettingsFormValues(settingsPayload?.settings || {})", body)
        self.assertIn("renderCacheSettingsStatus(payload, { formValues: !(await formFilled) })", body)

    def test_cache_status_render_can_skip_form_values(self) -> None:
        body = self._function_body("renderCacheSettingsStatus")
        self.assertIn("if (options.formValues !== false) {", body)
        self.assertIn("renderSettingsFormValues(payload?.settings || {}, options);", body)

        form_body = self._function_body("renderSettingsFormValues")
        self.assertIn("[els.allPageSizeInput, pageSizes.all_page_size]", form_body)
        self.assertIn("[els.ffmpegThreadsInput, video.ffmpeg_threads_per_job]", form_body)
        self.assertIn("settings.thumbnail_cache_limit_gb", form_body)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from catalog_app.api import _default_page_size, _search_page_params
from catalog_app.config import load_config, save_runtime_page_sizes


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "catalog_app" / "static" / "app.js"
INDEX_HTML = ROOT / "catalog_app" / "static" / "index.html"


class AllPageSizeConfigTests(unittest.TestCase):
    def _instance(self, root: Path, *, photo_page_size: int = 24) -> Path:
        data_root = root / "data_root"
        output_root = root / "catalog_output"
        data_root.mkdir()
        output_root.mkdir()
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "data_root": str(data_root),
                    "output_root": str(output_root),
                    "photo_page_size": photo_page_size,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def test_legacy_config_uses_effective_photo_page_size_for_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._instance(Path(temp_dir), photo_page_size=31)
            config = load_config(config_path)
            self.assertEqual(31, config.all_page_size)

            config.settings_json.parent.mkdir(parents=True)
            config.settings_json.write_text(
                json.dumps({"settings_version": 1, "photo_page_size": 37}),
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(37, config.photo_page_size)
            self.assertEqual(37, config.all_page_size)

    def test_saved_all_page_size_is_explicit_and_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(self._instance(Path(temp_dir), photo_page_size=31))
            config = save_runtime_page_sizes(
                config,
                {
                    "all_page_size": 17,
                    "photo_page_size": 29,
                    "video_page_size": 41,
                    "gif_page_size": 43,
                    "other_page_size": 47,
                    "folder_page_size": 53,
                },
            )

            self.assertEqual(17, config.all_page_size)
            self.assertEqual(29, config.photo_page_size)
            saved = json.loads(config.settings_json.read_text(encoding="utf-8"))
            self.assertEqual(17, saved["all_page_size"])
            self.assertEqual(29, saved["photo_page_size"])

    def test_media_type_resolver_keeps_independent_values(self) -> None:
        config = SimpleNamespace(
            all_page_size=11,
            photo_page_size=13,
            video_page_size=17,
            gif_page_size=19,
            other_page_size=23,
        )
        self.assertEqual(11, _default_page_size(config, "all"))
        self.assertEqual(13, _default_page_size(config, "image"))
        self.assertEqual(17, _default_page_size(config, "video"))
        self.assertEqual(19, _default_page_size(config, "gif"))
        self.assertEqual(23, _default_page_size(config, "other"))

    def test_search_default_page_size_remains_fifty(self) -> None:
        params = _search_page_params(
            raw_query="example",
            raw_folder="",
            raw_media_type="all",
            raw_page=None,
            raw_page_size=None,
        )
        self.assertEqual(50, params.page_size)


class AllPageSizeFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_settings_load_validate_and_save_all_page_size(self) -> None:
        self.assertIn('allPageSizeInput: document.getElementById("allPageSizeInput")', self.app)
        self.assertIn("[els.allPageSizeInput, pageSizes.all_page_size]", self.app)
        self.assertIn("all_page_size: positiveIntegerInputValue(els.allPageSizeInput)", self.app)
        self.assertIn("values.all_page_size", self.app)

    def test_settings_layout_has_requested_two_columns(self) -> None:
        left_start = self.html.index('<div class="settings-page-size-column">')
        right_start = self.html.index('<div class="settings-page-size-column">', left_start + 1)
        left = self.html[left_start:right_start]
        right = self.html[right_start:self.html.index("</div>\n            </div>", right_start)]
        self.assertLess(left.index("allPageSizeInput"), left.index("photoPageSizeInput"))
        self.assertLess(left.index("photoPageSizeInput"), left.index("videoPageSizeInput"))
        self.assertLess(right.index("gifPageSizeInput"), right.index("otherPageSizeInput"))
        self.assertLess(right.index("otherPageSizeInput"), right.index("folderPageSizeInput"))

    def test_save_reloads_non_search_view_from_safe_first_pages(self) -> None:
        self.assertIn('if (state.view !== "search") {', self.app)
        self.assertIn("state.mediaPage = 1;", self.app)
        self.assertIn("state.childPage = 1;", self.app)
        self.assertIn("await reloadSafely(loadCurrentFolder);", self.app)


if __name__ == "__main__":
    unittest.main()

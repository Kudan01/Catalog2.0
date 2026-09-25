from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "catalog_app" / "static" / "app.js"


class FolderPreviewReadinessDiagnosticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS.read_text(encoding="utf-8")

    def test_navigation_measurement_spans_load_and_rendered_images(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        load_current = self._function_body("async function loadCurrentFolder(options = {})")

        self.assertIn("beginFolderPreviewNavigationMeasurement", open_folder)
        self.assertIn("folderPreviewMeasurement", open_folder)
        self.assertIn("renderChildFolders(childrenData)", load_current)
        self.assertIn(
            "scheduleFolderPreviewNavigationSnapshot(options.folderPreviewMeasurement, childrenData)",
            load_current,
        )

    def test_snapshot_uses_fixed_visible_images_and_load_or_error_completion(self) -> None:
        snapshot = self._function_body(
            "function finishFolderPreviewSnapshot(slot, measurement, fields, images, zeroWhenComplete = false)"
        )

        self.assertIn("const pending = images.filter(image => !image.complete)", snapshot)
        self.assertIn('image.addEventListener("load", completeOne, { once: true })', snapshot)
        self.assertIn('image.addEventListener("error", completeOne, { once: true })', snapshot)
        self.assertNotIn("querySelectorAll", snapshot)
        self.assertIn('performance.getEntriesByType("resource")', snapshot)
        self.assertIn('entry.initiatorType === "img"', snapshot)
        self.assertIn("resourceUrls.has(entry.name)", snapshot)
        self.assertIn("entry.requestStart - entry.fetchStart", snapshot)
        self.assertIn("entry.responseStart - entry.requestStart", snapshot)
        self.assertIn("entry.responseEnd - entry.responseStart", snapshot)
        self.assertIn('"/media/thumbnail", "/media/folder-preview"', snapshot)

    def test_folder_cards_use_direct_cache_endpoint_only(self) -> None:
        folder_preview_url = self._function_body("function folderPreviewUrl(preview)")

        self.assertIn('apiUrl("/media/folder-preview"', folder_preview_url)
        self.assertIn("preview.thumbnail_cache_path", folder_preview_url)
        self.assertNotIn('apiUrl("/media/thumbnail"', folder_preview_url)
        self.assertNotIn("existing_only", folder_preview_url)
        self.assertIn('apiUrl("/media/thumbnail"', self.source)

    def test_child_page_measurement_tracks_stages_and_existing_api_calls(self) -> None:
        page_change = self._function_body("async function goToChildPage(page)")
        load_current = self._function_body("async function loadCurrentFolder(options = {})")
        finish = self._function_body(
            "function finishChildPageMeasurement(measurement, visiblePreviewsReadyMs)"
        )

        self.assertIn("beginChildPageMeasurement", page_change)
        self.assertIn("childPageMeasurement", page_change)
        self.assertIn('recordChildPageApiRequest(options.childPageMeasurement, "/api/folders")', load_current)
        self.assertIn('recordChildPageApiRequest(options.childPageMeasurement, "/api/folder")', load_current)
        self.assertIn('recordChildPageApiRequest(options.childPageMeasurement, "/api/media")', load_current)
        self.assertIn("foldersResponseMs", load_current)
        self.assertIn("cardsRenderedMs", load_current)
        self.assertIn("visible_previews_ready_ms", finish)

    def test_stale_measurements_are_ended_and_ignored(self) -> None:
        begin = self._function_body(
            "function beginFolderPreviewNavigationMeasurement(folder, page, trigger)"
        )
        finish = self._function_body(
            "function finishFolderPreviewSnapshot(slot, measurement, fields, images, zeroWhenComplete = false)"
        )

        self.assertIn('cancelFolderPreviewMeasurement("folderPreviewNavigation")', begin)
        self.assertIn('cancelFolderPreviewMeasurement("folderPreviewScroll")', begin)
        self.assertIn(
            'diagnosticOperationEnd(measurement.event, measurement.operation, { result: "stale" })',
            self.source,
        )
        self.assertIn("diagnosticState[slot] !== measurement", finish)

    def test_scroll_diagnostics_are_gated_and_debounced(self) -> None:
        scroll = self._function_body("function scheduleFolderPreviewScrollSnapshot()")

        self.assertIn("if (!DIAGNOSTICS_ENABLED) return", scroll)
        self.assertIn("folderPreviewScrollTimer", scroll)
        self.assertIn("}, 200)", scroll)
        self.assertIn("zeroWhenComplete", self.source)

    def test_diagnostics_do_not_change_image_loading_attributes(self) -> None:
        navigation = self._function_body("function scheduleFolderPreviewNavigationSnapshot(measurement, childrenData)")
        scroll = self._function_body("function scheduleFolderPreviewScrollSnapshot()")
        diagnostic_code = navigation + scroll

        self.assertNotIn(".src =", diagnostic_code)
        self.assertNotIn("setAttribute", diagnostic_code)
        self.assertNotIn("fetch(", diagnostic_code)

    def _function_body(self, signature: str) -> str:
        start = self.source.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ")
            if (index := self.source.find(marker, start + len(signature))) >= 0
        ]
        end = min(boundaries) if boundaries else len(self.source)
        return self.source[start:end]


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "catalog_app" / "static" / "app.js"
INDEX_HTML = ROOT / "catalog_app" / "static" / "index.html"
I18N_ROOT = ROOT / "catalog_app" / "static" / "i18n"


class SearchBusyStateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_search_form_has_bound_submit_button(self) -> None:
        self.assertIn('id="searchForm"', self.html)
        self.assertIn('id="searchSubmit"', self.html)
        self.assertIn('searchSubmit: document.getElementById("searchSubmit")', self.app)

    def test_busy_state_updates_label_and_aria(self) -> None:
        busy = self._function_body("function setSearchBusy(requestId)")
        clear = self._function_body("function clearSearchBusy(requestId = null)")
        self.assertIn('setAttribute("aria-busy", "true")', busy)
        self.assertIn('setAttribute("data-text", "search.searching")', busy)
        self.assertIn('text("search.searching")', busy)
        self.assertIn('removeAttribute("aria-busy")', clear)
        self.assertIn('setAttribute("data-text", "search.submit")', clear)
        self.assertIn('text("search.submit")', clear)

    def test_request_owns_cleanup_and_stale_request_cannot_clear_new_busy_state(self) -> None:
        loader = self._function_body("async function loadSearchView({ requestId, snapshot })")
        clear = self._function_body("function clearSearchBusy(requestId = null)")
        begin = self._function_body("function beginViewLoadRequest()")
        self.assertIn("setSearchBusy(requestId)", loader)
        self.assertIn("finally", loader)
        self.assertIn("clearSearchBusy(requestId)", loader)
        self.assertIn("state.searchBusyRequestId !== requestId", clear)
        self.assertIn('state.view !== "search"', begin)
        self.assertIn("clearSearchBusy()", begin)

    def test_busy_labels_exist_in_both_locales(self) -> None:
        cs = json.loads((I18N_ROOT / "cs.json").read_text(encoding="utf-8"))
        en = json.loads((I18N_ROOT / "en.json").read_text(encoding="utf-8"))
        self.assertEqual("Vyhledávám…", cs["search.searching"])
        self.assertEqual("Searching…", en["search.searching"])

    @classmethod
    def _function_body(cls, signature: str) -> str:
        start = cls.app.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ")
            if (index := cls.app.find(marker, start + len(signature))) >= 0
        ]
        end = min(boundaries) if boundaries else len(cls.app)
        return cls.app[start:end]


if __name__ == "__main__":
    unittest.main()

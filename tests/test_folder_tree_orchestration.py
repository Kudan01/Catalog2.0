from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "catalog_app" / "static" / "app.js"


class FolderTreeOrchestrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS.read_text(encoding="utf-8")

    def test_current_folder_load_owns_children_request_and_hydrates_both_views(self) -> None:
        body = self._function_body("async function loadCurrentFolder(options = {})")

        self.assertEqual(1, body.count('fetchJson("/api/folders"'))
        self.assertIn("state.currentFolderChildrenRequest = childrenRequest", body)
        self.assertIn("hydrateTreeBranchFromChildrenData(snapshot.folder, childrenData)", body)
        self.assertIn("renderChildFolders(childrenData)", body)

    def test_startup_navigation_waits_for_main_data_before_tree_sync(self) -> None:
        body = self._function_body("async function openFolder(path)")

        self.assertLess(
            body.index("await loadCurrentFolder({ requestId })"),
            body.index("await loadRootFolders({ requestId })"),
        )
        self.assertNotIn("Promise.all", body)

    def test_current_parent_replaces_page_and_strips_previews(self) -> None:
        remember = self._function_body("function rememberTreeChildren(parentPath, data, options = {})")
        hydrate = self._function_body("function hydrateTreeBranchFromChildrenData(parentPath, data)")

        self.assertIn("if (!entry || replace)", remember)
        self.assertIn("folder_previews: _folderPreviews", remember)
        self.assertIn("entry.pageSize = Number(data?.page_size || 0)", remember)
        self.assertIn("{ replace: isCurrentParent }", hydrate)
        self.assertIn("replace: isCurrentParent", hydrate)

    def test_non_current_branch_keeps_preview_free_lazy_request(self) -> None:
        body = self._function_body("async function ensureTreeNodeChildren(node)")
        current_guard = "sameCatalogPath(folder.rel_path, state.folder)"
        lazy_request = 'fetchJson("/api/folders", {'

        self.assertIn("await pending.promise", body)
        self.assertLess(body.index(current_guard), body.index(lazy_request))
        self.assertIn("page_size: 200", body)
        self.assertIn("include_previews: 0", body)

    def test_root_tree_can_be_created_from_current_children_response(self) -> None:
        hydrate = self._function_body("function hydrateTreeBranchFromChildrenData(parentPath, data)")
        render_root = self._function_body("function renderRootTreePage(data)")

        self.assertIn('normalizedParent === "" && isCurrentParent', hydrate)
        self.assertIn("renderRootTreePage(data)", hydrate)
        self.assertIn("folder_previews: _folderPreviews", render_root)
        self.assertIn("state.treeLoaded = true", render_root)

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

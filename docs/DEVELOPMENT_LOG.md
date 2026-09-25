# Development log

This technical log records completed and approved development steps: what changed, why it changed, which significant files were affected, and how the resulting state was validated or tested. It records final outcomes rather than intermediate attempts or experiments. After each future approved development step, add a concise entry describing its result and validation.

## 2026-09-14 — Development documentation structure

### Changes

- Moved the development command reference from the repository root into `docs/`.
- Added this ongoing technical development log without creating empty screenshot directories.

### Reason

- Keep developer-facing documentation together and provide a durable record of completed development work and its validation.

### Files

- `docs/DEVELOPMENT.md`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Confirmed that the moved command reference retains its existing content.
- Searched the repository for references to the former `DEVELOPMENT.md` path and found none requiring updates.
- Reviewed the resulting Git diff and checked it for formatting errors.

## 2026-09-14 — Folder browse performance diagnostics

### Changes

- Added phase-level timing and operation counts to existing `/api/folders` HTTP diagnostic events.
- Extended the diagnostic summary tool with readable per-request folder browse breakdowns.
- Added targeted coverage for root and non-root diagnostics, unchanged API results, and summary formatting.

### Reason

- Identify where folder browsing spends time during startup, normal navigation, and pagination without assuming or applying an optimization.

### Files

- `catalog_app/api.py`
- `catalog_app/diagnostics.py`
- `tools/summarize_diagnostics.py`
- `tests/test_folder_browse_diagnostics.py`
- `docs/DEVELOPMENT.md`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Added targeted assertions that diagnostics-disabled and diagnostics-enabled folder responses are identical.
- Added assertions for the required timing fields, root/non-root context, operation counts, and readable summary formatting.
- Attempted `python -m unittest discover -s tests -v`; this workspace has no runnable Linux `python`, so the suite remains to be run in the supported Windows development environment.
- Functional `/api/folders` behavior was intentionally left unchanged.

## 2026-09-14 — Folder preview query join order

### Changes

- Constrained the folder preview metadata query to start from requested `folder_preview_items`, then look up media and thumbnails through existing selective indexes.
- Added regression coverage for multiple folders, `auto` and `auto_parent` ordering, unavailable media, invalid thumbnail states/types, missing cache files, and manual rows.

### Reason

- Prevent SQLite from starting the small-scope folder preview query with a broad scan of all ready thumbnails on large catalogs.

### Files

- `catalog_app/api.py`
- `tests/test_folder_preview_query.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Added exact-result assertions covering the existing folder preview selection and ordering contract without time-based thresholds.
- Attempted `python -m unittest discover -s tests -v`; this workspace has no runnable Linux `python`, so the suite remains to be run in the supported Windows development environment.
- Folder preview selection behavior and the `/api/folders` response contract were intentionally left unchanged.

## 2026-09-14 — Folder preview metadata phase diagnostics

### Changes

- Split the existing folder browse `preview_metadata` timing into query, cache-file checks, count-map loading, composition, and an unallocated preview remainder.
- Added aggregate row/check counts and readable nested output to the diagnostic summary.
- Extended diagnostics tests with a real preview fixture and sub-phase assertions.

### Reason

- Identify the remaining non-SQL source of folder preview latency after correcting the query plan, without changing preview behavior.

### Files

- `catalog_app/api.py`
- `tools/summarize_diagnostics.py`
- `tests/test_folder_browse_diagnostics.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Added assertions for unchanged preview results, non-negative timings, exact cache-check counts, non-negative remainder accounting, and summary formatting.
- Attempted `python -m unittest discover -s tests -v`; this workspace has no runnable Linux `python`, so the suite remains to be run in the supported Windows development environment.
- No preview selection, cache, pagination, frontend, or API response behavior was intentionally changed.

## 2026-09-14 — Deferred folder preview cache-file validation

### Changes

- Removed per-preview thumbnail cache-file probes from folder browsing while retaining the existing zero-valued diagnostic timing and check-count fields.
- Kept individual cache-file validation at the thumbnail endpoint, where a missing file produces the existing controlled missing-thumbnail response.
- Added regression coverage for ready metadata with both present and missing cache files, invalid thumbnail states, preview source mixing and ordering, and the absence of bulk filesystem checks.

### Reason

- Avoid multi-second folder browse latency caused by repeated filesystem metadata reads when thumbnail directories are not present in the operating system filesystem cache.

### Files

- `catalog_app/api.py`
- `tests/test_folder_browse_diagnostics.py`
- `tests/test_folder_preview_query.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Confirmed that healthy ready thumbnails retain the same preview metadata, source mixing, and ordering.
- Confirmed that stale thumbnails and unavailable media remain excluded.
- Confirmed that ready metadata with a missing cache file remains in the folder response and is rejected with the existing controlled response when that specific thumbnail is requested.
- Attempted `python -m unittest discover -s tests -v`; this workspace has no runnable Linux `python`, so the suite remains to be run in the supported Windows development environment.

## 2026-09-14 — 7C.1 Current-parent folder data shared by cards and tree

### Changes

- Changed current folder cards and the current-parent tree to consume one shared `/api/folders` response.
- Removed the duplicate tree request for the current parent, including the duplicate root request during startup.
- Changed the current-parent tree to show exactly the active folder-card page instead of accumulating previously visited pages.
- Preserved lazy loading and caching for other, non-current tree branches.

### Reason

- Remove redundant folder browsing work and give the current-parent tree one clear data source. This is an architectural correction; a user-visible performance improvement was not demonstrated in this step.

### Files

- `catalog_app/static/app.js`
- `catalog_app/static/i18n/cs.json`
- `catalog_app/static/i18n/en.json`
- `tests/test_folder_tree_orchestration.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Automated tests, the manual Windows workflow, and runtime diagnostics passed.
- Runtime diagnostics confirmed that duplicate current-parent `/api/folders` requests no longer occur.
- The `/api/folders` backend and folder filesystem-status logic were not changed in this step.
## 2026-09-14 — 7C.2/7C.2a Runtime filesystem status optimization for `/api/folders`

### Changes

- A request-scoped `source_root_status` removed redundant repeated source-root checks.
- Root browsing continues to use one shared filesystem snapshot for both active database folders and disk-only candidates.
- Cold validation showed that the original non-root batch `scandir` was unsuitable for directories containing many media entries. 7C.2a therefore checks only database folders returned on the current non-root page and does not enumerate the entire parent directory.
- A non-root page with no child folders performs no child-folder filesystem work.
- All existing filesystem statuses and path, link, and junction safety checks remain preserved. The frontend and 7C.1 orchestration were unchanged.

### Reason

- Reduce redundant runtime filesystem work without making non-root browse cost depend on every physical entry in a media-heavy parent directory.

### Files

- `catalog_app/api.py`
- `tools/summarize_diagnostics.py`
- `tests/test_folder_browse_diagnostics.py`
- `tests/test_folder_filesystem_batch.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The full automated test suite passed: 20 tests, OK.
- In cold validation, a representative problematic `/api/folders` request improved from approximately 2603 ms to 1056 ms total, while `folder_fs_status` improved from approximately 1728 ms to 395 ms and batch enumerations changed from 1 to 0.
- For a non-root request with zero child folders, total time improved from approximately 126 ms to 21 ms and child-folder filesystem work from approximately 94 ms to 0 ms.
- This does not establish that overall Catalog performance is solved. Startup still takes approximately 10.8 seconds, with separate diagnosed bottlenecks in `/api/status`, `/api/jobs/status`, and the thumbnail pipeline.

## 2026-09-14 — Startup SQL count aggregation

### Changes

- `/api/status` and `/api/jobs/status` previously performed multiple separate `COUNT` passes over `media_files`.
- Introduced a shared aggregate query: `SELECT COUNT(*), COALESCE(SUM(is_available), 0) FROM media_files`.
- Each endpoint now obtains total and available counts in one pass and derives unavailable as total minus available. The same approach is used for `folders`.
- Public response contracts were unchanged. The endpoints remain independent, so startup still performs two aggregated `media_files` passes: one for `/api/status` and one for `/api/jobs/status`.

### Reason

- Remove redundant cold database passes with a small local change and without adding persistent counters or changing the startup contract.

### Files

- `catalog_app/api.py`
- `catalog_app/scan_store.py`
- `tests/test_status_counts.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Targeted automated tests passed: 3/3, OK. The full automated test suite passed: 23/23, OK.
- Before the change, cold measurements were approximately 10824 ms for frontend initial load, 7837 ms for `/api/status`, and 2054 ms for `/api/jobs/status`.
- After the change, cold measurements were approximately 9047 ms for frontend initial load, 7877 ms for `/api/status`, and 120 ms for `/api/jobs/status`.
- Cold frontend initial load improved by approximately 1.8 seconds (about 16%), and removing redundant count passes substantially improved `/api/jobs/status`.
- The dominant `/api/status` cold cost remained approximately 7.9 seconds. Further substantial cold-start improvement would require a different design than this local aggregation change; startup and overall Catalog performance are not considered solved.

## 2026-09-15 — Folder preview and child-pagination performance diagnostics

### Changes

- Added end-to-end folder preview readiness diagnostics for navigation/page changes and debounced scroll snapshots.
- Added timing for the `existing_only=1` thumbnail hot path: backend total, media lookup, thumbnail lookup, cache-file check, and other time.
- Extended frontend Resource Timing metrics to separate browser queue/scheduling, TTFB, and download time.
- Added child-folder pagination timings for total completion, `/api/folders` response, rendered cards, and visible previews readiness, together with request counts for `/api/folder`, `/api/folders`, and `/api/media`.
- The instrumentation does not change browse, lazy-loading, or thumbnail runtime behavior.

### Reason

- Distinguish backend thumbnail work, browser scheduling, resource transfer, and repeated page-loading stages without applying an optimization.

### Files

- `catalog_app/api.py`
- `catalog_app/diagnostics.py`
- `catalog_app/static/app.js`
- `catalog_app/thumbnail_cache.py`
- `tools/summarize_diagnostics.py`
- `tests/test_folder_browse_diagnostics.py`
- `tests/test_folder_preview_query.py`
- `tests/test_folder_preview_readiness_diagnostics.py`
- `tests/test_folder_tree_orchestration.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The automated test suite passed.
- Runtime measurements showed that multi-second waits for existing folder previews are dominated by browser request queue/scheduling rather than downloading the small image responses.
- Child-folder page changes request `/api/folder`, `/api/folders`, and `/api/media` again, with `/api/folders` accounting for a significant part of the measured delay.

## 2026-09-15 — Step 7 folder-browse performance completion

### Changes

- Folder previews are now served directly from the existing thumbnail cache through `/media/folder-preview`, without repeated `media_files` or `thumbnails` database lookups.
- Normal non-root `/api/folders` browsing no longer performs per-folder filesystem probing.
- The folder-preview browse query was simplified and no longer uses `media_files`.
- Root filesystem behavior remains unchanged.
- The database schema, preview selection semantics, Prepare/Update/Reroll workflows, and the limit of six previews per card remain unchanged.

### Reason

- Remove redundant work from the user-visible child-folder page path while preserving the established preview and root-browse contracts.

### Files

- `catalog_app/api.py`
- `catalog_app/server.py`
- `catalog_app/static/app.js`
- `catalog_app/thumbnail_cache.py`
- `tests/test_folder_browse_diagnostics.py`
- `tests/test_folder_filesystem_batch.py`
- `tests/test_folder_preview_query.py`
- `tests/test_folder_preview_readiness_diagnostics.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The automated test suite passed.
- Real validation measured `/api/folders` at approximately 36–66 ms and cards rendered at approximately 54–81 ms on the validated child-folder pages.
- During rapid cold scrolling, previews can still wait in the browser request queue for several seconds; the resulting UX was product-accepted as sufficient for this phase.

## 2026-09-15 — Folder-preview production contract

### Changes

- Unified the production folder-preview contract on `requested_count=6` and `variant=0`.
- Removed the `--preview-count` and `--variant` CLI options.
- Browser, jobs/server workflows, and preview builds now use the same fixed contract.
- The existing `auto` plus `auto_parent` model, square-root weighting, hierarchical sampling, and final maximum of six previews remain unchanged.
- The database schema and thumbnail/cache data are unchanged, so existing previews do not require a rebuild. Reroll was not implemented.

### Reason

- Prevent production workflows from creating folder-preview states with different count or variant settings.

### Files

- `catalog_app/api.py`
- `catalog_app/cli.py`
- `catalog_app/folder_preview_candidates.py`
- `catalog_app/jobs.py`
- `catalog_app/server.py`
- `tests/test_folder_preview_contract.py`
- `tests/test_folder_preview_query.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The automated test suite passed.

## 2026-09-15 — Photo tile cache lifecycle

### Changes

- Dynamic cache now contains only cache entries that are safe to delete, while protected cache contains thumbnails that Catalog intentionally preserves.
- A `photo_tile` referenced by at least one `folder_preview_items` row is stored under `protected/photo_tiles`; an unreferenced `photo_tile` is stored under `dynamic/photo_tiles`.
- Creating or removing preview references moves only the affected photo tiles. Removing the final reference moves a tile back to dynamic cache.
- Newly generated photo tiles are created directly in the cache class implied by their current preview references.
- Lifecycle operations require explicit affected `media_ids` and do not perform a global scan.
- Cleanup and cache statistics now use `cache_class` directly; the previous special case for dynamic-but-referenced tiles is no longer needed.
- Removed the unused `protected/folder_previews` cache kind and layout.

### Reason

- Align the physical cache layout with the actual dynamic/protected lifecycle and remove the hidden exception where a physically dynamic file was not safe to delete.

### Files

- `catalog_app/config.py`
- `catalog_app/folder_preview_candidates.py`
- `catalog_app/thumbnail_cache.py`
- `tests/test_photo_tile_cache_lifecycle.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Targeted photo tile lifecycle tests passed.
- The full unittest suite passed.
- The transition was successfully validated on an existing development instance, and Catalog was functionally validated after the final implementation.
- `git diff --check` passed.
- The final runtime does not contain the temporary startup/global migration scan.

## 2026-09-15 — Child-folder pagination controls above and below cards

### Changes

- Kept the existing child-folder pager in the section header and added the same controls below the child-folder cards.
- Both pager instances use `state.childPage`, `state.childPages`, `state.childPageSize`, and the existing `goToChildPage()` navigation path.
- Shared DOM roles synchronize first, previous, jump, next, and last controls without duplicate element IDs.
- The bottom pager contains controls only and follows child-folder availability and the section's collapsed state.

### Reason

- Keep child-folder navigation accessible after browsing a page of cards without introducing a second pagination state or navigation implementation.

### Files

- `catalog_app/static/index.html`
- `catalog_app/static/app.js`
- `catalog_app/static/style.css`
- `tests/test_child_folder_pagers.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Added a targeted static contract test for shared state, event binding, synchronized controls, visibility, and unique HTML IDs.
- The Docker environment does not provide a Python interpreter, so the unittest could not be executed here.
- Static duplicate-ID inspection and `git diff --check` passed.

## 2026-09-16 — Independent All gallery pagination

### Changes

- Added a separate `all_page_size` setting for the All gallery.
- Existing instances without this value retain a compatible fallback to the effective `photo_page_size`; once saved, `all_page_size` is persisted independently.
- Search keeps its separate fixed pagination of 50 results.
- Settings → Browsing now presents six page-size values in a two-column desktop layout that collapses safely to one column on narrow viewports.
- Saving page-size settings applies the change immediately and safely returns the relevant gallery to its first page.

### Reason

- Allow the mixed All gallery to use an independent page size while preserving existing instance behavior and the Search contract.

### Files

- `catalog_app/api.py`
- `catalog_app/config.py`
- `catalog_app/server.py`
- `catalog_app/setup_instance.py`
- `catalog_app/static/app.js`
- `catalog_app/static/index.html`
- `catalog_app/static/style.css`
- `catalog_app/static/i18n/cs.json`
- `catalog_app/static/i18n/en.json`
- `tests/test_all_page_size.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- Focused `tests.test_all_page_size` tests passed.
- The full automated test suite passed.
- A real Windows instance validated independent All and Photos values, immediate application after Save, safe return to the first page, persistence after restart, and the final Settings layout.

## 2026-09-16 — Search rendering diagnostics fix

### Changes

- `renderSearchResults()` no longer fails with `Operation is not defined` after rendering results.
- The diagnostic operation is now created and completed correctly for both populated and empty media-result branches.
- The rendered-media count uses `mediaResults.length` instead of the nonexistent `data.media` value.
- Added a regression test for the Search rendering diagnostic contract.

### Reason

- Prevent diagnostic bookkeeping from sending an otherwise successful Search render into the general error handler.

### Files

- `catalog_app/static/app.js`
- `tests/test_search_render_diagnostics.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused test passed.
- The full automated test suite passed.
- A real Windows instance validated Search with media results and without media results; the technical error is no longer displayed.

## 2026-09-16 — Search media-type filtering

### Changes

- Search type `all` continues to return folders and media in the combined result set.
- Search types `image`, `gif`, `video`, and `other` now return only media of the selected type.
- Typed Search excludes folders from `results`, `counts`, `total`, and pagination.
- Search page size remains fixed at 50, and no frontend change was required.

### Reason

- Make the existing Search media filters control the complete result type rather than filtering only the media portion of a mixed folder/media result set.

### Files

- `catalog_app/api.py`
- `tests/test_search_media_type_filter.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused test passed.
- The full automated test suite passed.
- A real Windows instance validated combined folder/media results for All, media-only results for Photos, GIFs, Videos, and Other, error-free filter switching, and the correct empty state for typed Search.

## 2026-09-18 — Unified content filter

### Changes

- The main content filter now consistently represents All, Folders, Photos, GIFs, Videos, and Other.
- The frontend uses a dedicated `contentFilter` state; `folders` is not passed or represented as a media type.
- In normal folder browsing, All shows child folders and all media, Folders shows only child folders, and the remaining filters show only matching media.
- Search uses the same filter semantics. Its Folders mode returns and paginates folders only, while Search page size remains fixed at 50.
- Favorites still supports media only. Entering Favorites from the Folders filter safely selects All, and the unsupported Folders option is hidden in that view.
- Added Czech and English Folders labels and updated the filter's ARIA description.

### Reason

- Give the shared top-level filter one consistent content-kind meaning without extending the media API or Favorites data model with a synthetic folder media type.

### Files

- `catalog_app/api.py`
- `catalog_app/server.py`
- `catalog_app/static/app.js`
- `catalog_app/static/index.html`
- `catalog_app/static/i18n/cs.json`
- `catalog_app/static/i18n/en.json`
- `tests/test_all_page_size.py`
- `tests/test_content_filter.py`
- `tests/test_search_media_type_filter.py`
- `tests/test_search_render_diagnostics.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The relevant focused tests passed.
- The full automated test suite passed.
- A real Windows instance validated filter switching in normal folder browsing and Search, correct hiding of unneeded sections, and safe Favorites behavior.

## 2026-09-18 — Search folder UX and request feedback

### Changes

- Search folder results now use the existing stored folder previews. Preview metadata is loaded in one batch only for folder results on the current Search page; media-only Search performs no folder-preview lookup.
- Search folder cards preserve the folder name and path while reusing `folderPreviewMarkup()` and `folderInsightMarkup()`.
- In the All filter, the Search folder section can be collapsed and expanded using a dedicated temporary state. Each new search starts expanded, and the toggle remains hidden in the Folders-only filter.
- While a Search request is running, the search form shows `Vyhledávám…` / `Searching…` and exposes `aria-busy`.
- Busy-state ownership is tied to the active request, so stale completion cannot clear a newer request's state. The state is also cleared after success, failure, or leaving Search.

### Reason

- Bring Search folder cards to parity with normal folder cards and provide immediate, race-safe feedback while Search results are loading.

### Files

- `catalog_app/api.py`
- `catalog_app/static/app.js`
- `catalog_app/static/index.html`
- `catalog_app/static/i18n/cs.json`
- `catalog_app/static/i18n/en.json`
- `tests/test_content_filter.py`
- `tests/test_search_busy_state.py`
- `tests/test_search_media_type_filter.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The relevant focused tests passed.
- The full automated test suite passed.
- A real Windows instance validated folder previews, statistical insight, Search collapse and its reset on a new search, plus busy feedback during filter/page changes and when leaving Search.

## 2026-09-18 — Folder Favorites state and controls

### Changes

- `favorites.json` now uses version 2 with an explicit `kind: media|folder`. Legacy version 1 entries without `kind` are read as media and retain their original `added_at`; favorite identity is defined by item kind and path.
- Folders can be added to or removed from Favorites from normal folder cards, Search folder cards, and the header of the currently open non-root folder. All three surfaces share one folder-favorite mechanism and the existing media-favorite interaction pattern.
- Folder favorite state is persistent and synchronized across browse, Search, and the open-folder header. The current media-only Favorites view and media modal intentionally ignore folder entries until the follow-up folder Favorites view change.
- Media rename updates only media favorites. Folder branch rename and missing-branch delete include the exact folder favorite, descendant folder favorites, and media favorites while preserving `kind` and `added_at`; scan purge does not remove valid folder favorites.
- Open Folder moved into the existing More menu on normal folder cards and in the open-folder header. Folder-card favorite controls retain stable compact sizing.
- The media-modal favorite control retains standard modal button styling and uses both localized labels for intrinsic sizing, preventing layout movement when its state changes.

### Reason

- Establish kind-safe persistent folder favorite state and consistent controls before folder entries are introduced into the Favorites view itself.

### Files

- `catalog_app/api.py`
- `catalog_app/scan_activate.py`
- `catalog_app/server.py`
- `catalog_app/static/app.js`
- `catalog_app/static/index.html`
- `catalog_app/static/style.css`
- `catalog_app/static/i18n/cs.json`
- `catalog_app/static/i18n/en.json`
- `tests/test_folder_favorites.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused folder Favorites tests passed.
- The full automated test suite passed.
- A real Windows instance validated add/remove behavior, shared state across browse, Search, and the open folder, persistence after restart, the More menus, and the resulting favorite UI.
- The final media-modal correction was functionally and visually validated.

## 2026-09-18 — Independent Search folder and media pagination

### Changes

- Search All now returns two independently paginated sections: folders with the folder pager and media with the media pager.
- The frontend uses `state.childPage` and `state.mediaPage` independently, so changing either page preserves the other section's current page.
- Search Folders renders only the folder section, while Photos, GIFs, Videos, and Other render only the matching media section.
- Search page size remains fixed at 50 for both folder and media pagination.
- In Search All, the folder section retains its temporary collapse state and uses the same top/bottom pager visibility rules as normal folder browsing.
- A new search resets both page states to page 1 and starts with folders expanded.
- Search media cards and modal navigation use pagination metadata from the media section, independently of folder pagination.

### Reason

- Align Search with the normal catalog layout and prevent folder and media results from competing for one mixed result page.

### Files

- `catalog_app/api.py`
- `catalog_app/server.py`
- `catalog_app/static/app.js`
- `tests/test_all_page_size.py`
- `tests/test_content_filter.py`
- `tests/test_folder_favorites.py`
- `tests/test_search_media_type_filter.py`
- `tests/test_search_render_diagnostics.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The relevant focused tests passed.
- The full automated test suite passed.
- A real Windows instance validated independent folder and media paging, folder collapse, each content filter, reset behavior for a new search, and media-modal navigation.

## 2026-09-25 — Folder Favorites view

### Changes

- Favorites now uses the shared All, Folders, Photos, GIFs, Videos, and Other content-filter contract.
- All renders favorite folders above favorite media. The sections use independent `childPage` and `mediaPage` pagination; folders use `folder_page_size`, while media uses the page size configured for the active media filter.
- Folders renders only favorite folders without a collapse toggle, while media filters render only matching favorite media.
- In All, the folder section has a temporary collapse state that resets to expanded on each new entry into Favorites.
- Active favorite folders reuse the existing card previews, insight, path, and favorite action. Unavailable favorite folders remain removable but cannot be opened and expose no previews or unavailable statistics.
- Removing a folder favorite reloads the folder section, removes the stale card, and corrects an invalid final folder page without resetting media pagination.
- Entering Favorites now resets the content scroll to the top, matching normal folder navigation. Existing media Favorites and media-modal behavior remain unchanged.

### Reason

- Complete folder Favorites presentation while keeping folder and media navigation independent and preserving unavailable favorites for explicit user removal.

### Files

- `catalog_app/api.py`
- `catalog_app/server.py`
- `catalog_app/static/app.js`
- `tests/test_content_filter.py`
- `tests/test_folder_favorites.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused regression tests and full automated test suite passed.
- A real Windows instance validated independent pagination, collapse behavior, every content filter, folder cards, folder-favorite removal, the media modal, and scroll reset when entering Favorites.

## 2026-09-25 — Folder navigation History API

### Changes

- Added the versioned `catalog2.folder-view` history-state contract, version 1, containing the target folder and an optional `returnAnchor`.
- The initial folder view establishes its browser entry with `replaceState`; normal child-folder navigation uses `pushState` and stores the opened child's `rel_path` as the return anchor in the parent entry.
- `popstate` restores folder views without creating another history entry. Search, Favorites, and breadcrumb-specific history behavior remain outside this step.
- `/api/folders/anchor` resolves an anchor's current page from the current ordering and current `folder_page_size`, rather than treating a historical page number as authoritative.
- Root anchor resolution uses the same combined ordering of active folders and disk candidates as the normal root listing.
- A successful return expands the folder section and centers the anchor card with `scrollIntoView()`; a missing anchor safely falls back to the first folder page.

### Reason

- Support native browser Back/Forward through folder-card navigation while remaining correct after folder ordering or page-size changes.

### Files

- `catalog_app/api.py`
- `catalog_app/server.py`
- `catalog_app/static/app.js`
- `tests/test_folder_history.py`
- `tests/test_folder_preview_readiness_diagnostics.py`
- `tests/test_folder_tree_orchestration.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused tests and full automated test suite passed on Windows.
- A real instance validated A → B → C → Back → Back → Forward without a history loop.
- Return to an anchor outside the first page, recalculation after changing `folder_page_size`, and top-level return to the root with the correct centered card were validated.

## 2026-09-25 — Breadcrumb folder anchors

### Changes

- Hierarchical breadcrumb navigation now uses the same current-page anchor resolution as the folder History API.
- Clicking an ancestor uses the immediately following breadcrumb item as the target anchor. `sourceEntryAnchor` remains responsible for updating the parent entry during child-card navigation, while `targetEntryAnchor` is stored in the newly pushed breadcrumb target entry.
- The target anchor is resolved through the existing `/api/folders/anchor` endpoint before rendering. Its current page is loaded and the card is centered afterward; an unavailable anchor safely falls back to the first folder page.
- Breadcrumb navigation creates a normal history step. Back restores the original folder, Forward restores the breadcrumb target and its anchor, and `popstate` continues without creating another `pushState`.
- A breadcrumb jump from a media-only filter opens the target in All so the folder anchor is visible. Search and Favorites history remain outside this step.

### Reason

- Make ancestor and root breadcrumb jumps retain spatial context while preserving the source-entry semantics already used by child-folder cards.

### Files

- `catalog_app/static/app.js`
- `tests/test_folder_history.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused tests and full automated test suite passed on Windows.
- A real instance validated ancestor and root breadcrumb jumps, centering of the immediate child anchor, and Back/Forward restoration.
- The original child-card History API behavior from the preceding step was regression-tested successfully.

## 2026-09-25 — Folder view history snapshots

### Changes

- The version 1 `catalog2.folder-view` state now also stores `contentFilter`, `mediaPage`, `childPage`, `childFoldersCollapsed`, and `scrollTop`; older entries use safe defaults when these fields are absent or invalid.
- Changes within the current folder view update its matching history entry with `replaceState`, never `pushState`. Synchronization follows content-filter, media-page, folder-page, collapse, and debounced scroll changes; Search and Favorites do not overwrite folder snapshots.
- A restore without an active anchor reapplies the filter, both page states, folder collapse, and content scroll position. Pages outside the current valid range are corrected, reloaded, and persisted back to the entry.
- An active `returnAnchor` retains priority over stored child-page, collapse, and scroll state: the folder section is expanded and the current anchor card is centered. Programmatic centering is excluded from scroll synchronization.
- The first subsequent user snapshot change clears the active anchor, so later restoration uses the newer user state. Child-card and breadcrumb anchor behavior from the preceding History API steps remains unchanged.

### Reason

- Make each folder history entry represent the user's latest in-folder working state without adding browser history steps for filters, pagination, collapse, or scrolling.

### Files

- `catalog_app/static/app.js`
- `tests/test_folder_history.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused tests and full automated test suite passed on Windows.
- A real instance validated folder snapshot restoration, anchor priority over an older page/scroll snapshot, and anchor removal after subsequent user interaction.
- A → B → C → Back → Back → Forward remained functional without a history loop.

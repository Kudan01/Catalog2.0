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

## 2026-09-25 — Search and Favorites History API

### Changes

- Added separate version 1 history contracts for `catalog2.search-view` and `catalog2.favorites-view` while preserving the existing folder history contract and anchor behavior.
- Search history stores its folder context, query, search folder and scope, the "search in current folder" state, content filter, independent child and media pages, folder-collapse state, and content scroll position.
- Favorites history stores its folder context, content filter, independent child and media pages, folder-collapse state, and content scroll position.
- Each successful new Search and the first entry into Favorites from another view creates a normal browser step with `pushState`. Reopening active Favorites does not create a duplicate entry.
- Filter, page, collapse, and debounced scroll changes within Search or Favorites update only the matching current entry through `replaceState`.
- `popstate` now dispatches folder, Search, and Favorites restoration. Search and Favorites restores create no new history entry and restore the complete snapshot, including controls, folder context, corrected valid pages, and scroll position.
- Opening a folder from Search or Favorites first preserves the current view snapshot and then creates a normal folder entry without using folder `sourceEntryAnchor` semantics.
- One shared debounced mechanism records scroll snapshots for all three views. Folder History API behavior from the preceding steps remains unchanged.
- This completes the History API sequence: 3A added folder Back/Forward with intelligent anchors, 3B added breadcrumb anchor navigation, 3C added folder-view snapshots, and 3D added Search and Favorites history views.

### Reason

- Make browser Back/Forward preserve the user's complete navigation context across folder, Search, and Favorites views without introducing history loops or duplicate entries.

### Files

- `catalog_app/static/app.js`
- `tests/test_folder_history.py`
- `tests/test_search_favorites_history.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused tests and full automated test suite passed on Windows.
- A real instance validated Favorites → folder → Back/Forward with preserved state.
- Search → folder → Back/Forward restored the query, scope, content filter, both pages, collapse state, and scroll position.
- Starting a new Search from Search and returning to the preceding Search with Back was validated.
- Reopening active Favorites created no duplicate history entry.
- Combined navigation among Search, folder, and Favorites completed without history loops.

## 2026-10-07 — Settings form values without waiting for cache statistics

### Changes

- Added `runtime_settings_status(config)`, which returns the effective Settings values from the loaded config without database access. `thumbnail_cache_status` now uses it, so the `settings` part of `/api/thumbnail-cache/status` and of the Settings save responses is unchanged.
- Added the read-only `GET /api/settings/runtime/status` endpoint returning `{"ok": true, "settings": ...}`.
- Split frontend Settings rendering: `renderSettingsFormValues` fills the form (title, language, theme, density, page sizes, thumbnail/video parameters, cache limit, and source root), while `renderCacheSettingsStatus` renders the cache summary and fills the form only when not told to skip it.
- On opening Settings, `loadCacheSettingsStatus` requests the runtime settings in parallel with the cache and job status. The form is filled as soon as the runtime settings arrive. A later cache status response renders only the cache summary, so it cannot overwrite values the user has meanwhile edited. If the runtime settings request fails, the form is filled from the cache status payload as before.
- Removed unused local variables from the previous combined render function.

### Reason

- Task 20: the Pagination and thumbnail/video values loaded slowly on the first Settings open because the form waited for aggregate statistics over the whole `thumbnails` table, although its values do not depend on them.
- The cost of the cache statistics themselves is unchanged; they no longer block the form.

### Files

- `catalog_app/api.py`
- `catalog_app/server.py`
- `catalog_app/static/app.js`
- `tests/test_settings_form_loading.py`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The focused tests and full automated test suite passed on Windows.
- A real instance validated that Settings opened immediately after catalog startup shows complete form values while the cache summary loads independently.
- Save, language switching, and cache-limit changes worked as before.

## 2026-10-07 — Task 10 thumbnail benchmark and decision

### Results

The benchmark was run on Windows with standalone scripts on a representative sample of test media outside the repository.

Photos (large JPEG and BMP), tile 600×800:

- Pillow (current pipeline): median 96.4 ms; 26.2 photos/s with 4 parallel threads.
- pyvips: median 48.7 ms; 69.6 photos/s in parallel; same output size. libvips cannot load BMP.
- Pillow with `Image.draft`: median 68.8 ms; 32.5 photos/s in parallel.
- Small photos (equal to or smaller than the target tile) occur in the tested media.

Videos (4K and 1080p), 5 images per video, width 1024:

- Current pipeline: 61.8 s.
- ffmpeg scale + Pillow: 51.7 s.
- Full-resolution frame + pyvips: 56.6 s.
- ffmpeg scale + pyvips: 46.8 s (−24 %).
- Temporary data dropped by about 44 %; output size unchanged.

GIFs, 300×300:

- Pillow and pyvips were equally fast (about 10 ms) with the same output size; transparency was preserved for all files.

Licenses:

- pyvips is MIT-licensed; `pyvips-binary` is LGPL-3.0-or-later. No GPL component is included (see THIRD-PARTY-NOTICES in `kleisauke/libvips-packaging`).

### Decision

- All thumbnail types (photo tile, GIF preview, video poster, video frames) move from Pillow to pyvips. Pillow is removed and `pyvips[binary]` is added; no parallel Pillow path is kept.
- ffmpeg downscales video poster and frame images directly (box of at most 2× `video_preview_width`, no upscaling, aspect ratio preserved); pyvips performs the final resize and WebP encoding.
- BMP moves from images to Other (no thumbnail).
- Reuse of small photos as the tile source is not introduced, in favor of one uniform generation logic.
- The algorithm versions of all four thumbnail types are increased so that all thumbnails are regenerated once.
- Output is preserved: WebP quality 82, the same target sizes, EXIF orientation for photos, transparency for PNG/GIF, and no metadata.
- libvips distribution for end users is an open question in task 23.

### Implementation

- `requirements.txt` now requires `pyvips[binary]>=3.2.0` instead of Pillow.
- All four thumbnail generators use one shared pyvips writer, `_write_webp_thumbnail`. It calls `Image.thumbnail` with `size="down"`, which applies EXIF orientation, never upscales, and loads only the first GIF frame. It then normalizes 16-bit, CMYK, and greyscale input to 8-bit sRGB or greyscale while keeping alpha, and writes WebP with quality 82, effort 4, and no metadata (`keep=0` on libvips 8.15+, `strip` otherwise).
- ffmpeg extracts video poster and frame images with `scale=w='min(iw,2W)':h='min(ih,2W)':force_original_aspect_ratio=decrease`, where W is `video_preview_width`.
- All four algorithm versions were increased (`photo_tile_v2_webp_vips_fit`, `gif_preview_v2_webp_vips_first_frame_fit`, `video_poster_v3_webp_ffmpeg_scale_vips_20_fit`, `video_frame_v3_webp_ffmpeg_scale_vips_35_50_65_80_fit`).
- `.bmp` was removed from image extensions, so BMP files are classified as Other; README no longer lists BMP as a supported image.
- Cache contract fixes:
  - Ready checks compare `algorithm_version`. An outdated photo tile is not ready and is regenerated on demand. Existing-only GIF and video lookups still serve an outdated file until preview preparation or folder-preview generation regenerates it.
  - GIF preview and video poster/frame generators delete the previous output file after a successful regeneration, as photo tiles already did.
  - Scan activation marks thumbnails stale when `media_type` changes, even if size and modified time are unchanged.

### Files

- `requirements.txt`
- `README.md`
- `catalog_app/media_types.py`
- `catalog_app/scan_activate.py`
- `catalog_app/thumbnail_cache.py`
- `tests/test_pyvips_thumbnails.py`
- `tests/test_folder_preview_query.py`
- `tests/test_instance_runtime_environment.py`
- `docs/WORK_PLAN.md`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The benchmark was performed on Windows outside the repository; no benchmark tooling is added to the repository.
- This is a pre-1.0 change without a transition layer for existing instances; it was validated on a newly created instance.
- Installed and validated with pyvips 3.2.0 and pyvips-binary 8.18.7 (libvips 8.18.7).
- The full automated test suite passed on Windows.
- A new test instance created with `setup-instance` on test media validated:
  - photo thumbnails, including correct EXIF rotation (a test photo with orientation 6 was displayed in portrait);
  - GIF static previews and hover playback;
  - video posters and hover frames with the correct aspect ratio;
  - BMP classified as Other.

## 2026-10-08 — Task 24 step 1: keyframe extraction and concurrent video previews

### Changes

- Video poster and hover-frame extraction adds `-noaccurate_seek -skip_frame nokey` before `-i` and runs ffmpeg with `-threads 1`. Both generators (`_generate_video_poster`, `_generate_video_frame`) are the only ffmpeg extraction paths, so preview preparation and folder-preview builds through `video_poster_resource` use the same command.
- `VIDEO_POSTER_ALGORITHM_VERSION` and `VIDEO_FRAME_ALGORITHM_VERSION` were increased to `video_poster_v4_webp_ffmpeg_keyframe_scale_vips_20_fit` and `video_frame_v4_webp_ffmpeg_keyframe_scale_vips_35_50_65_80_fit`.
- `generate_video_posters_for_scope` and `generate_video_frames_for_scope` process several videos at once with a thread pool of `min(4, max(1, cpu_count // 4))` workers (`_video_job_worker_count`). The cap of 4 comes from measured disk throughput. The value is computed when the phase starts, is not stored, and is not a setting. The frames of one video are generated sequentially by one worker.
- Results are aggregated in row order in the calling thread, so job payloads, counts, error-sample order, and per-thumbnail error recording are unchanged. Each worker uses its own short SQLite connections (WAL, 5 s busy timeout); temporary file names stay unique per process, thread, and call.
- `ffmpeg_threads_per_job` was removed from config, runtime settings, the Settings API and UI, translations, instance setup defaults, and video-tool diagnostics. An old key in `config.json` or `settings.json` is ignored.
- The Performance section of `CLAUDE.md` now states that concurrency defaults are computed automatically, are not stored, and are not user settings; a documented disk-based cap is allowed.
- GIF preview generation and the output contract from task 10 are unchanged.

### Reason

- Task 24: video preview generation was slow, especially with source media on slow disks. The external benchmark showed large gains from keyframe extraction and from processing several videos at once; see `docs/WORK_PLAN.md`, task 24.

### Files

- `CLAUDE.md`
- `catalog_app/thumbnail_cache.py`
- `catalog_app/config.py`
- `catalog_app/api.py`
- `catalog_app/server.py`
- `catalog_app/setup_instance.py`
- `catalog_app/video_tools.py`
- `catalog_app/static/index.html`
- `catalog_app/static/app.js`
- `catalog_app/static/i18n/en.json`
- `catalog_app/static/i18n/cs.json`
- `tests/test_video_thumbnail_concurrency.py`
- `tests/test_settings_form_loading.py`
- `docs/WORK_PLAN.md`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The full automated test suite and `tools/check_i18n_translations.py` passed on Windows.
- After updating an existing instance, the "FFmpeg threads/job" field no longer appears in Settings.
- Preview preparation regenerated existing video previews with the new method and created previews for newly added videos; posters and hover frames display correctly.
- Catalog browsing remained usable while preview preparation was running.

## 2026-10-09 — Task 24 step 2: one ffmpeg call per video

### Changes

- Added `_extract_video_images`, which extracts any number of posters and hover frames of one video with one ffmpeg call: one `-ss … -noaccurate_seek -skip_frame nokey -threads 1 -i` input per image and one output per image via `-map i:V:0` (a video stream that is not cover art), with the same scale filter and pyvips/WebP encoding as step 1.
- Each image is stored or fails on its own. An image the shared call did not produce is retried with its own single-input call; if it still fails, the error is recorded only for that image and the other images are stored. A timeout is not retried; every image not yet produced gets the timeout error.
- Poster and frame rows are written by one shared `_store_video_image` with the unchanged upsert SQL.
- `_generate_video_poster` (also used by `video_poster_resource` for folder previews) extracts its single image through `_extract_video_images`; its interface and errors are unchanged.
- The video-frames job runs ffprobe once per video and extracts all missing frames of a video in one call; its payload is unchanged. `_generate_video_frame` was removed.
- Added `generate_video_previews_for_scope` for preview preparation. It groups poster and frame work rows by video and extracts the poster and missing frames of each video together with one ffprobe and one ffmpeg call. Videos are processed concurrently with `_video_job_worker_count` and aggregated in work-row order.
- Preview preparation (`build_media_previews_for_scope`) now has a GIF phase and one `video_previews` phase instead of separate video-poster and video-frame phases. The new phase keeps the same keys: `processed` counts videos, `frames_processed` counts hover frames, `created`/`reused`/`errors` count images, and `duration_seconds` is the real pass time. As a result, `processed_media` in preview-preparation totals counts each video once instead of twice. The CLI result lines label the new phase.
- `VIDEO_POSTER_ALGORITHM_VERSION` and `VIDEO_FRAME_ALGORITHM_VERSION` are unchanged because the images match step 1, so existing previews are not regenerated.
- The separate video-poster and video-frames jobs (service buttons), video concurrency, the frontend, and the output contract from task 10 are unchanged.

### Reason

- Task 24 step 2: reduce per-image ffmpeg process and input-open overhead by extracting all images of a video with one call.

### Files

- `catalog_app/thumbnail_cache.py`
- `catalog_app/media_preview_workflow.py`
- `tests/test_video_single_ffmpeg_call.py`
- `tests/test_video_thumbnail_concurrency.py`
- `tests/test_pyvips_thumbnails.py`
- `docs/WORK_PLAN.md`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The full automated test suite passed on Windows.
- `VIDEO_POSTER_ALGORITHM_VERSION` and `VIDEO_FRAME_ALGORITHM_VERSION` were confirmed unchanged.
- Preview preparation on an already prepared part of the catalog created no video previews; existing previews were not regenerated.
- For newly added videos, catalog update followed by preview preparation created posters and hover frames, which display correctly.
- The video-posters service job completed. Its error samples were expected: very short videos without a decodable frame, not a regression.
- The video-frames service job and poster creation through folder-preview builds (`video_poster_resource`) were not validated manually; they are covered by the automated tests.

## 2026-10-09 — Folder page simplification and top media pager

### Changes

- Media pagination was converted to the child-folder pager pattern: two copies (`data-media-pager`, top and bottom) without fixed IDs, controlled by `mediaPagerControls`, `syncMediaPagerControls`, and `setMediaPagerVisible`, sharing `state.mediaPage`/`state.mediaPages` and the single `goToMediaPage` navigation path. The new top copy sits centered directly above the media cards. Both copies carry `data-aria-label="sections.media"`.
- All pagers (child folders and media, top and bottom) use the symbols « ‹ › » with `actions.first`/`previous`/`next`/`last` as title and aria-label. The "Page" label and the "Go" button were removed; the jump field shows the current page followed by "/ N", submits with Enter, has `pagination.jumpLabel` as its aria-label, and hides the number spinner arrows. Child-folder pagers stay compact and right-aligned; media pagers keep full size and are centered.
- All pagers are hidden when there is at most one page. The other child-folder visibility conditions (collapsed section, no cards) still apply to the bottom copy only. Pagination logic and state are unchanged.
- `pagination.folders` and `pagination.items` show only the count. The count sits next to its section title on the left (`.section-title-group` for subfolders, `.media-head` for media). This also applies to the folder count in the folder-tree header.
- A separator and a larger gap precede the Media section when a visible Subfolders section is above it.
- The open-folder header shows one line with the non-zero recursive media counts and the recursive folder count (`folderPrimarySummary`) instead of the "Directly" and "Total" lines; the folder problem notice is kept.
- Folder cards and search-result folder cards no longer show text counts (`folder-card-primary-meta`, "Directly", "Total"); the count graphic (`folderInsightMarkup`) remains, and "Unavailable" is kept for unavailable search results. `directCountText` and the unused keys `count.directLabel` and `count.directSuffix` were removed; `recursiveCountText` remains for the folder tree.
- The breadcrumb is plain text separated by "›" (`appendBreadcrumbItem`); the current item is highlighted, disabled, and marked with `aria-current="page"` (`markBreadcrumbCurrent`) in folder, Search, and Favorites views. Clicking the current folder in the breadcrumb no longer reloads it.
- Content filters are underlined tabs without frame or background; the active filter has an accent underline and stronger text.
- Themes: the new rules use only theme variables; `.tab.active` was removed from the four theme-specific active-button rules, and the breadcrumb and tab rules use higher specificity than theme button rules.

### Reason

- Task 28: allow paging media without scrolling to the bottom, and reduce visual noise on the folder page while keeping the same functions, with a distinct look for each kind of element in all themes.

### Files

- `catalog_app/static/index.html`
- `catalog_app/static/app.js`
- `catalog_app/static/style.css`
- `catalog_app/static/i18n/cs.json`
- `catalog_app/static/i18n/en.json`
- `tests/test_media_pagers.py`
- `tests/test_folder_page_simplification.py`
- `tests/test_child_folder_pagers.py`
- `tests/test_folder_history.py`
- `tests/test_search_favorites_history.py`
- `docs/WORK_PLAN.md`
- `docs/DEVELOPMENT_LOG.md`

### Validation

- The full automated test suite and `tools/check_i18n_translations.py` passed on Windows.
- Validated manually on Windows in Firefox in all themes.
- Chrome and Edge were not validated (they have known issues tracked in tasks 5 and 6).

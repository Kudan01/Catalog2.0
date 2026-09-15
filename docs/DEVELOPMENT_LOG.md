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

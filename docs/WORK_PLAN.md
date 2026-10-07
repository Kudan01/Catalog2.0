# Catalog 2.0 — Work plan

This file tracks planned and unresolved work for Catalog 2.0. Completed implementation details and validation history belong in `docs/DEVELOPMENT_LOG.md`.

A task is not automatically active because it appears first in this file. Work starts only when a task is explicitly selected.

Statuses:
- `COMPLETED` — implemented and functionally validated.
- `PENDING` — planned but not yet implemented or resolved.
- `DECISION REQUIRED` — a product/behavior contract must be agreed before implementation.
- `DEFERRED` — intentionally postponed beyond the 1.0 release.

## 1. Media pagination — COMPLETED

Independent page-size handling for All, Photos, Videos, GIFs, Other, and Folders was implemented and validated. Search remains on its separate fixed page size of 50.

See `docs/DEVELOPMENT_LOG.md` for implementation and validation details.

## 2. Child-folder pagers above and below cards — COMPLETED

The child-folder pager is available above and below folder cards; both instances share one pagination state and navigation path and were regression-tested.

See `docs/DEVELOPMENT_LOG.md` for implementation and validation details.

## 3. History API + intelligent folder return — COMPLETED

Browser Back/Forward and breadcrumb navigation use the implemented history model for folder navigation. Folder return uses an anchor folder rather than a stored page number, resolves the current page from the current list, restores the view safely, and avoids creating history loops during `popstate`.

Search and Favorites also have dedicated versioned history-state contracts. The completed change was functionally validated.

The later question about Back/Forward between media pages inside an already open album is separate and does not reopen this task; see task 19.

## 4. Startup / readiness — PENDING

### Problem

Catalog can visually appear to be running before the main content is actually ready. Global folder/media counts are not a readiness signal for the main gallery.

The main content currently waits for `/api/folder`, `/api/folders`, and `/api/media`; a slow request can therefore leave the main area empty.

### Target

- Remove any need for manual refresh.
- Either open the browser only after the main content is ready, or show an unambiguous initialization state until actual readiness.
- A loading indicator must not merely hide an unresolved performance problem.

## 5. Chrome / Edge — Settings UI — PENDING

### Problem / validation scope

- Some fields/selects have problematic backgrounds or readability in Chromium browsers.
- Verify all themes.
- Verify Chrome, Edge, and Firefox.
- Verify focus, disabled, and dropdown states.

## 6. Chrome / Edge — video hover preview — PENDING

### Target

- Investigate poster frames reappearing between hover-preview frames.
- The poster should appear only before preview starts and after preview ends.
- Preserve the current hover delay.

## 7. Performance / cold loading / folder browsing — COMPLETED

The accepted performance phase removed redundant non-root filesystem probing and repeated folder-preview database work from normal browsing while preserving root behavior and the established preview contract.

Real Windows measurements and automated validation were completed. Rapid cold scrolling can still queue preview requests briefly in the browser; that UX was accepted for this phase.

See `docs/DEVELOPMENT_LOG.md` for measurements and implementation details.

## 8. Folder and parent-folder preview contract — COMPLETED

Production folder previews were unified on `requested_count = 6` and `variant = 0`. The production CLI no longer creates alternate count/variant states. Existing selection semantics and stored preview/cache data remained compatible.

Reroll was not implemented for 1.0.

## 9. Cache management — COMPLETED

Dynamic cache is the safely deletable class; protected cache contains thumbnails Catalog intentionally preserves. Photo tiles move between those classes according to folder-preview references, and the unused `protected/folder_previews` layout was removed.

The resulting lifecycle was validated on the development instance.

## 10. Photo thumbnails — source reuse and generation performance — PENDING

### Investigation

- Determine whether static photos whose dimensions are equal to or smaller than the target `photo_tile` should reuse the original as the source instead of generating another tile.
- Benchmark pyvips/libvips against the current Pillow pipeline on a small representative sample.
- The benchmark must not require rebuilding the whole catalog.
- Source originals must not be modified.

### Decision rule

If pyvips/libvips shows a meaningful project-specific benefit, assess Windows runtime/dependency impact and supported-format compatibility before considering migration.

Do not change the thumbnail pipeline based only on generic performance claims; decide from the project benchmark.

## 11. Final version / release metadata — PENDING

- Set the final version to `1.0.0`.
- Remove obsolete development-version labels.
- No functional changes belong in this task.

## 12. Public GitHub / release hygiene — PENDING

- Remove personal paths, data, and test artifacts.
- Review `README.md`.
- Review `.gitignore`.
- Review repository description/topics.
- Add screenshots if needed.
- Produce a release ZIP without database, cache, config, `.venv`, or other local instance data.

## 13. Complete final regression — PENDING

Validate the final release across:

- new-instance setup and existing-instance update, including local `.venv`;
- multiple independent instances;
- Scan / Update;
- photos, GIFs, video, and other media;
- folder previews including `auto` / `auto_parent`;
- Favorites for media and folders;
- folder and media rename;
- cache management;
- `data_root` change;
- media pagination including independent All page size;
- child-folder pagers above and below cards;
- History API / browser Back / breadcrumb / anchor return;
- Search;
- all themes;
- Firefox, Chrome, and Edge;
- performance regression scenarios after task 7: startup, root, a large internal folder, return to an already visited page, and folder-preview loading.

## 14. Structural refactor of large modules — PENDING

- Target oversized modules such as `app.js`, `api.py`, and other similarly large files.
- Treat this as a separate phase.
- Do not mix it with functional fixes.
- Prefer stronger automated-test coverage before the refactor.
- Do not perform it as a last-minute risky change immediately before release.

## 15. Post-1.0 options — DEFERRED

Possible later work:

- bundled Python/runtime;
- second launcher / shortcut;
- broader theme and language extensibility;
- new product features.

## 16. Search — fixes and usability — COMPLETED

Search rendering errors, folder-result previews, folder collapse behavior, content filtering, and request feedback were implemented and validated. Search retains its separate fixed pagination of 50 results.

See `docs/DEVELOPMENT_LOG.md` for the completed implementation history.

## 17. Folder Favorites — COMPLETED

Folder Favorites were added with persistent kind-safe state and folder controls, then integrated into the Favorites experience. Relevant navigation/history behavior was included in the completed History work.

The completed behavior was functionally validated.

## 18. Launcher / runtime lifecycle — PENDING

### Target

- On startup, keep the launcher / PowerShell window visible and use it to communicate initialization progress briefly.
- After the main UI is actually ready, hide the launcher window automatically so only Catalog remains visible in the browser.
- When Catalog is closed, terminate the local server/runtime and launcher process cleanly so no launcher window or orphaned background process remains.

### Constraint

Choose the exact hide/shutdown mechanism only after inspecting the current launcher and server lifecycle.

This is not an immediate implementation priority.

### Coordination

The "hide after ready" part depends on the readiness contract established in task 4.

## 19. Browser Back through media pages — DECISION REQUIRED

### Context

This is a follow-up discovered after task 3 was completed. It is not part of the completed folder-return contract.

Current media-page navigation does not provide browser-history steps for previously viewed pages inside the same album.

### Product decision

Decide whether media-page navigation should behave like:

`page 1 -> page 2 -> page 3 -> Back -> page 2 -> Back -> page 1 -> Back -> previous view`

Before implementation, define the exact history behavior for media-page changes and its interaction with the already completed History API contracts.

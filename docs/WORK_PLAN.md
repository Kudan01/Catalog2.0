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

## 10. Thumbnail generation — migration from Pillow to pyvips — COMPLETED

### Context

The project benchmark was run on Windows outside the repository and the decision below was accepted. Benchmark results are recorded in `docs/DEVELOPMENT_LOG.md` (2026-10-07). No benchmark tooling is added to the repository. The decision was implemented and validated on a newly created Windows instance; see the same log entry.

### Decision

1. All thumbnail types move from Pillow to pyvips: photo tile, GIF preview (first frame), video poster, and video frames. Pillow is removed from the project (`requirements.txt`) and `pyvips[binary]` is added. There is no parallel Pillow path and no legacy branch.
2. Video poster and frames: ffmpeg downscales the extracted frame directly (box of at most 2× `video_preview_width`, no upscaling, aspect ratio preserved) instead of writing it at full resolution. pyvips performs the final resize and WebP encoding.
3. BMP moves from images to Other (no thumbnail).
4. Reuse of small photos (equal to or smaller than the target tile) as the tile source is **not** introduced. Small photos do occur, but the reuse was rejected in favor of one uniform generation logic.
5. The algorithm versions of all four thumbnail types (`PHOTO_TILE_`, `GIF_PREVIEW_`, `VIDEO_POSTER_`, `VIDEO_FRAME_ALGORITHM_VERSION`) are increased so that all thumbnails are regenerated once cleanly.
6. Output is preserved: WebP quality 82, the same target sizes from config, EXIF orientation for photos, transparency for PNG/GIF, and no metadata in the output.
7. How libvips is shipped in the end-user distribution is not decided here; it belongs to task 23.

### Cache contract fixes

These apply to any future algorithm or media-type change:

- Ready checks must treat a thumbnail with an outdated `algorithm_version` as not ready.
- GIF preview and video poster/frame generators must delete the previous output file after successful regeneration, as photo tiles already do.
- Thumbnails of media whose `media_type` changed must be invalidated during scan activation.

### Constraint

Source originals must not be modified.

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
- folder and media rename, including case-only rename (task 22);
- Settings loading and consistent apply behavior, including language switching (tasks 20 and 21);
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

- user-friendly Windows distribution — see task 23;
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

## 19. Folder URLs and browser Back through pages — PENDING

### Context

History entries are created with `history.pushState(state, "")` without a URL, so a folder has no address of its own and a page change inside a folder creates no history step. Both parts change the same history entries, so they share one design and are implemented in two steps, each validated separately.

### Decisions

1. URL content: folder, content filter, child-folder page, and media page. Default values are omitted, so the root stays `/` and a folder on default settings has only the folder parameter. Scroll position and collapsed state stay in the history state only, not in the URL. Encoding uses `URLSearchParams`. Existing query parameters (`catalog2_diagnostics`) are preserved.
2. Back steps through both media pages and child-folder pages: `page 3 -> Back -> page 2 -> Back -> page 1 -> Back -> previous view`. Changing the content filter does not create a history step.
3. URLs apply to folder views only. Search and Favorites keep their current history behavior.
4. Middle-click and Ctrl+click open a folder in a new tab from the folder tree, child-folder cards, and breadcrumb items (except the current folder).
5. A URL with a folder that does not exist opens the root and shows a message that the folder was not found.

### Step 1 — PENDING

History steps for page changes; every folder history entry carries its URL.

### Step 2 — PENDING

Loading the folder view from the URL at startup and after reload; links for middle-click/Ctrl+click.

### To assess during planning

- Interaction with the completed History API contracts from task 3 (anchors, snapshots, `popstate` without loops).
- Step 2 must not slow down startup (task 4 covers readiness separately).

## 20. Settings — cleanup and loading — PENDING

Required for the 1.0 release.

### Progress

- Completed: Settings form values are filled from a lightweight runtime-settings endpoint without waiting for cache statistics (see `docs/DEVELOPMENT_LOG.md`, 2026-10-07).
- Remaining: cleanup of testing/diagnostic leftovers; the cache summary itself still loads with the original cost.

### Problem

- The Settings section still contains leftovers from testing and diagnostics.
- When Settings is opened for the first time after startup, the values in the Pagination and Media sections take a long time to load, even though the catalog content is already loaded.

### Target

- The final Settings UI contains no testing or diagnostic leftovers.
- When the catalog is loaded, Settings opens with complete content instead of filling in sections progressively.

### Investigation

- Determine which requests Settings issues when it opens and why displaying the data takes so long.
- A loading indicator must not merely hide an unresolved performance problem (same principle as task 4).

### Coordination

- Task 5 covers Settings appearance in Chromium browsers.
- Task 4 covers the general startup/readiness contract.

## 21. Settings — consistent apply behavior — DECISION REQUIRED

Required for the 1.0 release.

### Problem

- Settings changes do not behave consistently: some take effect immediately, others only after Save.
- Switching the language updates part of the UI immediately, but some texts remain in the previous language until the page is reloaded or the user navigates elsewhere.

### Product decision

- Define one apply model for Settings: all changes on Save, all changes immediately, or an explicitly defined set of exceptions.
- Define exactly which parts of the UI must be re-rendered when the language changes.

### Target

After the decision, all Settings changes behave according to the agreed model and the whole UI reflects them without a manual page reload.

## 22. Folder and media rename — behavior and UI — PENDING

Required for the 1.0 release.

### Investigation

- Determine why rename does not allow changing only the letter case of a folder or media name. Verify on Windows, where the filesystem is case-insensitive.
- Determine whether renaming a folder or a media item renames the source data on disk or only the catalog record. Verify from the implementation, then confirm on Windows.

### Target

- Simplify the rename dialog from a UI/UX perspective.
- Remove diagnostic messages from the rename dialog.

### Constraint

Source-media changes are allowed only as explicit user-initiated operations. If the investigation shows that the intended behavior differs from the current one, the resulting contract is a product decision to be agreed before implementation.

### Coordination

Rename is part of the final regression in task 13.

## 23. Windows end-user distribution — DECISION REQUIRED

This is a requirement and planning topic, not an implementation task. Do not choose a packaging technology or implement any part of it until the decisions below are made.

### Problem

The current public workflow requires an installed Python, creating a `.venv`, and using the CLI for `setup-instance` and `update-instance`. A regular Windows user should not have to work with the CLI, Python, or a virtual environment.

### Target

- A user-friendly Windows distribution, ideally a downloadable ZIP/portable package with an EXE or an equivalent launcher.
- Without the CLI, the user can create a new Catalog instance, start it normally, and later update it to a newer version.

### Constraints

- Preserve the current model of separate instances and their persistent data.
- An update must not lose or unnecessarily replace config, database, cache, runtime state, or source media.
- "Update capability" does not mean an automatic online self-update.

### Open decisions

- Packaging technology. PyInstaller, embedded Python, an installer, and other options remain open.
- Update UX, for example running an updater from a newer release package versus updating from within the application.
- How FFmpeg/FFprobe is distributed. Bundling it is not automatically an approved solution; it is an open technical and licensing question.
- How libvips (required by pyvips since task 10) is distributed: bundled in the release package (for example via `pyvips[binary]`), or downloaded after first start similarly to FFmpeg. pyvips is MIT-licensed; `pyvips-binary` is LGPL-3.0-or-later (see its THIRD-PARTY-NOTICES). Both variants are subject to the license inventory below.
- Whether this belongs to the 1.0 release or post-1.0 work.

### License compliance (prerequisite)

- Before choosing a distribution approach, inventory all redistributed components and verify their license/distribution terms, especially the Python runtime, Python dependencies, libvips, FFmpeg/FFprobe, and any packaging tool.
- The resulting release must include the required licenses, notices, and attribution.
- Verify the license of Catalog itself for public distribution; packaging must not leave the project's licensing state unclear.

### Coordination

- Task 11 covers final version metadata.
- Task 12 covers the release ZIP and public release hygiene.
- Task 18 covers launcher/runtime lifecycle.

## 24. Video thumbnail generation performance — COMPLETED

### Context

The benchmark was run on Windows outside the repository. No benchmark tooling is added to the repository. Results are relative to the pipeline before this task.

Disk-bound run, cold reads:

- One ffmpeg call per video with keyframe extraction: 3.5× faster.
- The same with 2 videos at once: 4.0×; with 4 videos at once: 6.3×.

Run with faster storage:

- Keyframe extraction with one ffmpeg call per image: 2.5× (1 video at once) to 8.7× (4 videos at once).
- `-noaccurate_seek` alone gave no gain.
- `-threads 2` with keyframe extraction gave no gain.

### Step 1 — keyframes and concurrent videos — COMPLETED

Within the current structure; posters and hover frames remain separate phases. Implemented and validated on Windows; see `docs/DEVELOPMENT_LOG.md` (2026-10-08).

- Poster and frame extraction add `-noaccurate_seek -skip_frame nokey` before `-i` in every path that generates them (preview preparation and folder-preview builds through `video_poster_resource`). `VIDEO_POSTER_ALGORITHM_VERSION` and `VIDEO_FRAME_ALGORITHM_VERSION` are increased.
- Video poster and frame phases process `min(4, max(1, cpu_count // 4))` videos at once. The cap of 4 comes from measured disk throughput, not from the CPU. The value is computed when the phase starts, is not stored, and is not a setting (neither in the UI nor in `config.json`).
- `ffmpeg_threads_per_job` is removed completely (config, settings, API, UI, instance setup, tests); ffmpeg runs with `-threads 1`. No transition layer; instances are recreated.

### Step 2 — one ffmpeg call per video — COMPLETED

Implemented and validated on Windows; see `docs/DEVELOPMENT_LOG.md` (2026-10-09).

Decision:

- The poster and missing hover frames of one video are extracted with one ffmpeg call: one `-ss … -noaccurate_seek -skip_frame nokey -threads 1 -i` input per image and one output per image via `-map i:V:0` (a video stream that is not cover art), with the same scale filter and pyvips/WebP encoding as step 1.
- Images stay identical to step 1, so `VIDEO_POSTER_ALGORITHM_VERSION` and `VIDEO_FRAME_ALGORITHM_VERSION` are not changed and step-1 previews are not regenerated.
- An image the shared call did not produce is retried with its own single-input call; when one image fails, the others are stored and the error is recorded only for that image. A timeout is not retried.
- Users: `_generate_video_poster` (one image; also `video_poster_resource` for folder previews), the video-frames job (missing frames of a video at once), and preview preparation.
- Preview preparation replaces the separate video-poster and video-frame phases with one `video_previews` phase with the same keys: `processed` counts videos, `frames_processed` counts hover frames, `created`/`reused`/`errors` count images, and `duration_seconds` is the real pass time. ffprobe runs once per video. As a result, `processed_media` in preview-preparation totals counts each video once instead of twice. The GIF phase is unchanged.
- The separate video-poster and video-frames jobs (service buttons for the whole catalog and the current folder) and their results are unchanged. Video concurrency stays at `min(4, max(1, cpu_count // 4))`. The frontend is unchanged.

### Constraint

Preserve the output contract from task 10 (WebP quality 82, target sizes from config, correct aspect ratio, no upscaling), job results and payloads, and per-thumbnail error recording. Source originals must not be modified.

## 25. Photo browsing — thumbnail loading while scrolling — PENDING

### Problem

- Photo thumbnails are generated on demand while browsing; this is intentional, and thumbnails appearing progressively is expected.
- Generation became faster after the move to pyvips (task 10), but browsing feels almost unchanged. Something other than the generation itself is therefore probably the bottleneck.
- Follows up on task 7.

### Target

Noticeably smoother photo browsing.

### To investigate (not decisions)

- Where the waiting occurs between the browser request and the displayed thumbnail.
- Viewport handling.
- Browser caching of thumbnail responses (currently `Cache-Control: no-store`). A possible solution is a thumbnail fingerprint in the URL with `Cache-Control: immutable`; beware of collisions between instances served on the same port.
- Whether generating one thumbnail opens the database several times (reportedly three times), and whether that is unnecessary.
- Limiting the number of concurrent browser requests.
- Cancelling requests for thumbnails that are no longer on screen.
- Prioritizing visible thumbnails.
- Possible generation ahead of scrolling (must be measured on an HDD).

### Constraint

The dynamic-cache cleanup logic (removing the least recently used entries) is complete and does not change.

## 26. Folder URLs — open a folder in another tab

This task was merged into task 19.

## 27. Catalog update performance — PENDING

### Problem

Updating the whole catalog is slow for large catalogs, especially the first (cold) run; a repeated run is faster.

### To investigate (not decisions)

- `os.path.isjunction` called for every entry during the walk (`scanner.py`) instead of using data from `os.scandir`.
- Writing and deleting the whole catalog in the staging tables on every update.
- Recalculating statistics for all folders.

### Approach

First measure a cold run with a timing breakdown (the activation service output), and only then propose changes.

## 28. Folder page simplification — COMPLETED

Media pagination gained a top copy above the media cards using the child-folder pager pattern. The folder page was simplified: compact symbol pagers hidden for a single page, counts next to section titles, a separator before Media, a one-line folder header summary, folder cards without text counts, a plain-text breadcrumb with a non-clickable current item, and underlined filter tabs, adjusted for all themes.

Validated in Firefox; Chrome and Edge were not validated. See `docs/DEVELOPMENT_LOG.md` (2026-10-09).

## 29. Folder card layout — PENDING

### Problem

After the text counts were removed, folder cards keep an empty left column between the folder name and the buttons.

### Constraints

- A folder has 6 previews (`FOLDER_PREVIEW_REQUESTED_COUNT`).
- The card switches between the wide and compact layout (`folderCardWideRequiredWidth`).
- On the root page, the root name appears both in the breadcrumb and in the heading.

### Approach

Design and try the layout before implementation.

## 30. Photo modal — selection flash on navigation — PENDING

### Problem

When clicking the edge of the photo modal to navigate to the previous or next photo, the old photo briefly flashes blue like a browser selection. The modal code has not changed.

### Possible solution

`user-select: none` on the photo area.

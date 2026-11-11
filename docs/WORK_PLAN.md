# Catalog 2.0 — Work plan

This file tracks planned and unresolved work for Catalog 2.0. Completed implementation details and validation history belong in `docs/DEVELOPMENT_LOG.md`.

A task is not automatically active because it appears first in this file. Work starts only when a task is explicitly selected.

Statuses:
- `COMPLETED` — implemented and functionally validated.
- `PENDING` — planned but not yet implemented or resolved.
- `DECISION REQUIRED` — a product/behavior contract must be agreed before implementation.
- `DEFERRED` — intentionally postponed beyond the 1.0 release.

## Design reference

The 1.0 redesign and the related product decisions were agreed on 2026-11-06. They are stored outside version control in `design-reference/` (ignored by Git):

- `design-reference/catalog-redesign-rozhodnuti.md` — the full decision record (in Czech) with rationale. Section numbers in this plan such as "decisions §5" refer to it.
- `design-reference/artboards/*.dc.html` — the interactive design source for each screen (exact colors, sizes, texts, states); `design-reference/README.md` maps files to screens.
- `design-reference/screenshots/` — exported images of the artboards.

This plan states the requirements with concrete values. When this plan and the design reference disagree, stop and ask; do not choose silently. The design uses placeholder images and invented names; real behavior is validated on data.

Task steps use the form "Done when: …". A task is complete only when every step's condition holds and the Windows validation listed in the task has been reported by the user.

## Release 1.0 — order of work

All work below belongs to 1.0. Only support for non-Windows systems is deferred (task 15). Development and debugging use a small sample instance; the full collection is used only at the end as a performance check. Schema and tile changes are not migrated; instances are recreated (project rule: no transition layers before 1.0).

1. **Performance quick fixes:** task 4 step A, task 25 step A and the cache-size and catalog-count fix (completed); task 25 step B (anchor database connection).
2. **Frontend refactor before the redesign:** task 14 (modules, `style.css` cleanup, color tokens, browser tests), task 39 (UI texts).
3. **Redesign:** tasks 31 (with 27), 32, 33, 34, 35, 36 — in this order; each is separately verifiable.
4. **Settings, rename, modal fixes:** tasks 20, 21, 22, 30.
5. **Catalog resilience:** task 37.
6. **Enhancements:** task 38.
7. **Distribution, startup and shutdown:** tasks 23, 18, task 4 step B.
8. **Release:** tasks 5, 6, 11, 12, 13.

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

The main content waits for `/api/folder`, `/api/folders`, and `/api/media`; a slow request can therefore leave the main area empty.

### Cause of the slow start (found 2026-11-11)

The original assumption that loading pyvips delayed the first tiles by about a minute was wrong: pyvips loads in under a second. The measured causes were:

- After every generated photo tile, the automatic dynamic-cache check summed `thumbnails.file_size_bytes` over the whole table twice (total and per cache class). On a large catalog with a cold database this cost seconds per tile and made concurrent thumbnail requests queue on the disk.
- `/api/status` and the job status counted all rows of `folders` and `media_files` on every call, and the frontend waited for `/api/status` before opening the folder view (about 13 s at startup).

Both were fixed together with task 25 (see `docs/DEVELOPMENT_LOG.md`, 2026-11-11).

### Step A — load pyvips at server start (block 1) — COMPLETED

- pyvips/libvips is loaded and warmed up during server startup, before the server reports readiness.
- Validated on Windows; this alone did not shorten the start (see the cause above).

### Step B — startup page (block 7, after task 23)

- The server starts immediately and the browser opens a startup page at once: logo, a progress bar, "Catalog is starting…" and the current step (for example "Preparing previews"). Design: artboard "Spouštění" (decisions §7d).
- The page switches automatically when the main content is actually ready: one catalog → that catalog; several → the catalog chooser (task 23); none → the new-catalog wizard (task 23).
- No console window is visible to the user (replaces the original idea in task 18 of showing progress in the PowerShell window).
- Done when: no manual refresh is ever needed after start, and the startup page never shows a gallery that is not ready.

### Constraint

A loading indicator must not merely hide an unresolved performance problem.

## 5. Chrome / Edge — Settings UI and form controls — PENDING

### Problem / validation scope

- Some fields/selects have problematic backgrounds or readability in Chromium browsers. Cause found during design review: `style.css` has no rules for `option`; Firefox inherits the select colors, Chrome and Edge draw the open list with their own light background.
- The fix is part of task 14 (each theme sets `color-scheme` and option colors through the color tokens). This task validates it.
- Verify all themes (including the light theme "Warm Light"), Chrome, Edge, and Firefox; focus, disabled, and open-dropdown states.

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

Note: the preview count and selection change again in tasks 33–35 (nine places per folder).

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

### Versioning rules (decided 2026-11-06)

- **Program version** follows semantic versioning: `1.0.1` fixes only, `1.1.0` new features, `2.0.0` incompatible changes. Pre-releases use a suffix (`1.1.0-beta.1`) and are published as GitHub pre-releases; the in-app update check ignores them.
- **Catalog format version** is a separate integer stored in each catalog database. It increases only when the database format changes; only then does the catalog upgrade (with backup) run. A program refuses to open a catalog with a newer format version (task 23).
- The program version, the catalog format version, and the release checksum are available to the in-app update check.

## 12. Public GitHub / release hygiene — PENDING

- Remove personal paths, data, and test artifacts.
- Review `README.md`; the installation section is rewritten for the portable ZIP (task 23).
- Review `.gitignore` (`design-reference/` stays ignored).
- `LICENSE` (MIT) exists; verify that third-party notices required by task 23 are included in the release ZIP.
- Review repository description/topics.
- Add screenshots if needed.
- Publish the release ZIP as a GitHub Release asset together with its checksum; the ZIP contains no database, cache, config, `.venv`, or other local instance data.
- **Branches and releases:** `main` is always in a working state; each task is done in its own short-lived branch (for example `task-31-media-dimensions`) and merged into `main` only after validation on Windows; a release is a version tag on `main` with the ZIP attached. Users receive only published releases, never work in progress. Prepare the exact steps and, if useful, a GitHub Actions workflow that builds the ZIP and checksum from a tag.
- The author's own catalog runs only on released versions; development uses the sample instance.

## 13. Complete final regression — PENDING

Validate the final release across:

- portable ZIP: first start, new-catalog wizard, catalog chooser, several catalogs, desktop shortcuts, program update from the app (check, download, verify, switch, restart; failure leaves the current version running), manual update by unpacking a newer ZIP, automatic cleanup of old program versions, moving the whole folder to a USB drive with a different drive letter (task 23);
- before a release that changes the catalog format: upgrade of a catalog created by the previous released version, including the backup and the failure path;
- multiple catalogs running at the same time and a second start of a running catalog;
- automatic shutdown after the last Catalog tab is closed, the notification-area icon, and "Quit Catalog" (task 18);
- Scan / Update, including media dimensions and video duration (task 31);
- photos, GIFs, video (playable and non-playable in the browser), and other media;
- folder previews: nine places, replacement of obviously bad photos, smart crop, manual pins and crop focus (tasks 34, 35, 38);
- Favorites for media and folders;
- folder and media rename, including case-only rename (task 22);
- Settings: sections, apply model, language switching without reload (tasks 20, 21);
- cache management and cleanup;
- source folder change and unavailable source (tasks 20, 33, 37);
- one pagination with page sizes 24/48/96/192 (task 33);
- History API / browser Back / breadcrumb / anchor return;
- Search grouped by folder and "Show all" (task 36);
- page states: loading, empty states, unavailable source, missing folder, running task, narrow window (task 33);
- catalog resilience scenarios (task 37);
- all themes, including the light theme;
- Firefox, Chrome, and Edge;
- performance regression scenarios: startup, root, a large internal folder, return to an already visited page, and folder-preview loading.

## 14. Frontend refactor before the redesign — PENDING

Block 2. Behavior must not change in this task; the redesign follows in tasks 31–36.

### Step 1 — split `app.js`

- Split `catalog_app/static/app.js` (about 10,000 lines) into modules: API client, history/URLs, folder page, media grid, modal, settings, jobs.
- Done when: the automated suite and the browser tests from step 3 pass and the app behaves identically on Windows.

### Step 2 — clean up `style.css` and introduce color tokens

- Remove the 45 accumulated "Step/Phase" layers of overridden rules.
- Convert every color to a named token set (background, header, surfaces, inputs, two border levels, three text levels, accent, success, warning, error, info, modal overlay). Today `style.css` has only 7 color variables, 128 hard-coded colors, and 242 per-theme override rules. A theme is a different set of token values.
- Each theme also sets `color-scheme` (light/dark) and the colors of `option` lists (fixes task 5).
- An active input has exactly one focus ring in the accent color; the browser's default outline is not drawn on top of it.
- Done when: all five themes look as before in Firefox, Chrome, and Edge, no hard-coded colors remain outside the token definitions, and no per-theme override rules remain except token values.

### Step 3 — browser tests

- Add Playwright tests run on Windows: open a folder, number of tiles, Back, F5 (reload restores the view).
- They gradually replace tests that only search for text in `app.js`.
- Done when: the tests run with one documented command in `docs/DEVELOPMENT.md` and pass.

### Coordination

The backend modules (`api.py` and others) stay out of this task; split them only when a later task requires it.

## 15. Post-1.0 options — DEFERRED

- Support for systems other than Windows (separate packages per system; the code contains Windows-specific parts such as the launcher and "open in Windows").
- New product features not listed in this plan.

## 16. Search — fixes and usability — COMPLETED

Search rendering errors, folder-result previews, folder collapse behavior, content filtering, and request feedback were implemented and validated. Search retains its separate fixed pagination of 50 results.

See `docs/DEVELOPMENT_LOG.md` for the completed implementation history.

## 17. Folder Favorites — COMPLETED

Folder Favorites were added with persistent kind-safe state and folder controls, then integrated into the Favorites experience. Relevant navigation/history behavior was included in the completed History work.

The completed behavior was functionally validated.

## 18. Launcher / runtime lifecycle — PENDING

Block 7, together with task 23. Decisions §7d.

### Target

- No console window is visible to the user; startup progress is shown in the browser (task 4 step B).
- **Automatic shutdown:** every open Catalog page reports to the server periodically (heartbeat, for example every 30 s). When no Catalog page has reported for about 15 minutes, the server shuts down cleanly. If a job is running (catalog update, preview preparation), the server waits for it to finish first. Closing the browser tab is therefore enough.
- **Notification-area icon** (Windows tray) owned by the server process, with "Open Catalog" and "Quit".
- **"Quit Catalog"** in Settings.
- Shutdown leaves no orphaned background process or window.
- A second start of a running catalog only opens the browser (already implemented in `launcher.py`; keep it). Different catalogs take the configured port or the next free one (already implemented; keep it).

### Constraint

Choose the exact mechanisms (heartbeat endpoint, tray library) after inspecting the current launcher and server lifecycle. The development command `serve` (`run_server`) keeps its strict behavior.

## 19. Folder URLs and browser Back through pages — COMPLETED

Step 1 (COMPLETED): page changes inside a folder create history steps, and every folder history entry carries a URL (`folder`, `filter`, `folder_page`, `media_page`, default values omitted, other parameters preserved); Back/Forward between pages of the same folder uses a light restore without reloading the tree. Step 2 (COMPLETED): the folder view is restored from the address at startup and after reload, a missing folder opens the root with a message, and tree items, child-folder cards, and breadcrumb items are links for middle-click and Ctrl+click. Search and Favorites keep their history behavior.

Validated in Firefox; Chrome and Edge were not validated. See `docs/DEVELOPMENT_LOG.md` (2026-10-09, steps 1 and 2).

## 20. Settings — redesign, cleanup and loading — PENDING

Required for 1.0. Decisions §7b; artboard "Nastavení (interaktivní)".

### Structure

- Separate modal of fixed size; left column with four sections, one shown at a time: **General**, **Updates**, **Source folder**, **Thumbnails and cache**. The General section fits without scrolling in its default state.
- Primary action buttons in a section have one minimum width (260 px in the design) so they line up.
- Every block uses one pattern: heading, at most one short description sentence (as wide as the fields), control. Fields align: the catalog-name field is as wide as Language and Items per page side by side (464 px in the design).
- **General:** catalog name; theme (five themes and "Custom…" in a three-column grid; the custom-color editor is task 38); language; **items per page** — one value for all filters with the options 24 / 48 / 96 / 192, default 48 (multiples of 12 keep folder cards in full rows at 1–4 columns). This replaces today's per-filter page sizes, the fixed 48 for "All", and Search's fixed 50.
- **Updates:** "Catalog update" (description: finds new, changed, and deleted files; source files are not changed; time of the last update; primary button "Update catalog" in the accent color) and "Catalog previews" ("Recalculate catalog previews"), and "Program version" (current version, "Check for a new version"; behavior in task 23). The program-version block uses distinct wording ("new version", not "update") so it is not confused with the catalog update.
- **Source folder:** description "The folder with your photos and videos."; path, "Verify", "Save" (enabled only after a successful verification; editing the path requires verifying again); after verification "✓ The folder is available and matches the catalog." Nothing else runs after saving: `source_root_setting_verify` already compares a sample of catalog folders with the new path and refuses a low match. This section is the target of "Change in Settings" from the unavailable-source notice (task 33).
- **Thumbnails and cache:** usage of the regular cache with a bar and the protected thumbnails; regular-cache limit in GB with "Save" (description: how much space regular thumbnails may use; protected ones are not counted); "Cleanup" with "Clean unused previews", which runs the dynamic-cache cleanup and then the protected-orphan check and clean; it deletes only thumbnails that nothing in the catalog references.

### Remove from the UI

- Tile size (replaced by the media-size slider, task 32).
- Thumbnail parameters (photo, GIF, video sizes, FFmpeg timeout) — they stay only in `config.json`; changing them breaks the layout and requires regenerating the cache.
- Thumbnail service, Diagnostics, and the service output — testing leftovers. The same actions stay available in `cli.py` (`scan-preview`, `scan-stage`, `scan-activate`, `update-branch`, …). Recomputing previews of one folder is in the folder's ⋯ menu.

### Loading

- Cache statistics are computed only when "Thumbnails and cache" is opened; the other sections open immediately with complete values.
- Investigate which requests Settings issues and why the cache summary is slow; a loading indicator must not hide an unresolved performance problem.

### Done when

The modal matches the artboard, the removed items are gone from the UI (and their now-unused frontend code is removed), and every section opens with complete content.

## 21. Settings — consistent apply behavior — PENDING

Required for 1.0. The decision was made on 2026-11-06 (decisions §7b).

### Apply model

- Safe changes apply and save immediately: theme, language, items per page; the catalog name on leaving the field or Enter. A short "✓ Saved" appears next to the block heading for a few seconds.
- Changes with consequences require "Save": source folder (after verification) and cache limit.

### Language

- Switching the language re-renders the whole UI (header, tree panel, folder page, open modal, Settings, notices, task chip) without a reload or navigation.

### Done when

Every Settings control follows the model above and a language switch leaves no text in the previous language.

## 22. Folder and media rename — behavior and UI — PENDING

Required for 1.0. Decisions §7c; implemented in the interactive design (folder header, folder card, modal).

### Current state (verified)

A separate dialog with a developer workflow: "Verify change" → sections Impact, Paths, Technical checks, Blocking errors, Warnings → "Rename". The physical folder or file on disk is renamed, and the catalog updates the paths of the whole branch, Favorites, and previews.

### Target

- **Rename in place, no dialog:** the pencil next to the folder name in the folder header turns the heading into an input of the same size; the pencil on a folder card turns the name under the collage into an input (the card's star and pencil are hidden meanwhile); in the modal toolbar the file name becomes an input with the extension shown separately and not editable. Next to the input: ✓ (save) and ✕ (cancel) icons; Enter saves, Esc cancels.
- **Verification and execution run automatically:** keep the backend `plan` + `execute` steps; Enter runs `plan` and, if it passes, `execute`. The user never sees the technical sections.
- **Errors in one sentence under the input**, no technical details:
  - "A folder with this name already exists."
  - "The name must not contain the characters \ / : * ? " < > |" — checked while typing.
  - "The name must not end with a period or a space." — checked while typing.
  - "The name must not be empty."
  - "The folder cannot be renamed because it is open in another program."
  - "The source folder is not available."
- **Case-only rename must work** (foto → Foto): investigate why it is refused today (Windows treats such names as equal) and fix it.
- **No page reload:** the heading, breadcrumb, and URL change in place; "✓ Renamed" appears briefly next to the name. While the operation runs (it can take a while for large folders), the input shows "Renaming…" and is disabled.
- An unavailable folder (unavailable source, folder missing on disk) shows no pencil.

### Constraint

Source-media changes are allowed only as explicit user-initiated operations.

### Done when

Folder rename from the header and from a card and file rename from the modal work as above on Windows, including a case-only rename, and the old dialog and its diagnostic texts are removed.

## 23. Windows distribution, catalogs and first start — PENDING

Required for 1.0 (decided 2026-11-06). Decisions §7d; artboards "Spouštění", "Průvodce prvním spuštěním (interaktivní)", "Rozcestník – výběr katalogu (interaktivní)".

### Program and data are separate (replaces today's `setup-instance` model)

Today `setup-instance` creates `Catalog2_<name>` next to the source folder with its own copy of the program (`.venv`, `app`) and `Start Catalog.bat`; every catalog is updated separately. New model:

- **One program:** a portable ZIP unpacked anywhere the user can write (desktop, `D:\Programs`, a USB drive; not `Program Files`). It contains `Catalog.exe` and the program in a **versioned subfolder** (`program\<version>\`, for example `program\1.0.0\`) with Python and libvips. No CLI, no Python installation. `Catalog.exe` starts the newest valid version present.
- **Catalogs are data only** (database, thumbnails, settings) with a user-given name and no technical prefix. Default location **next to the program**: `Catalog 2.0\Catalogs\<name>`, so the whole folder can be carried on a USB drive; "Change…" chooses another location. The program keeps the list of catalogs next to itself. Names of folders and files that the program creates on disk (`program`, `Catalogs`, …) are English and fixed; they never follow the UI language. Only the user-given catalog name is used as entered.
- **Program update from the app (user-initiated, decided 2026-11-06):** Settings › Updates › "Program version" shows the current version and "Check for a new version". The check looks only at **published GitHub Releases** (never at commits; pre-releases are ignored). Results: "You have the latest version", or "Version {version} is available" with a short summary of changes and "Download and install". Installing: download the release ZIP over HTTPS from GitHub, verify it against the checksum published with the release, unpack the new `program\<version>\` **next to** the current one, and switch only after successful verification; a small separate updater process does the switch (Windows does not allow replacing files of a running program), then Catalog restarts. If anything fails, the current version stays untouched and keeps running. There is no automatic background check — Catalog contacts the internet only when the user clicks. The program version belongs to the whole application, so the same check is also at the bottom of the catalog chooser: "Catalog 2.0 · version 1.0.0 · Check for a new version" (users with one catalog never see the chooser and use Settings). Design: artboards "Nastavení (interaktivní)" (section Updates) and "Rozcestník – výběr katalogu (interaktivní)".
- **Manual update stays possible:** unpack a newer ZIP over the program folder; the new versioned subfolder is added and `Catalog.exe` uses it. Leftover files of older versions cannot interfere because each version lives in its own subfolder. `Catalogs` stays untouched.
- **Automatic cleanup of program versions:** at most two versions are kept — the current one and the previous one (fallback). After a new version has started successfully once, versions older than the previous one are deleted automatically. If the user deletes the newest version manually, `Catalog.exe` starts the newest remaining one; catalogs already converted to a newer format are shown as "Newer version" in the chooser and nothing is damaged.
- **FFmpeg is not bundled:** with an explanation and the user's consent it is downloaded from the official source and verified with a checksum (first start or first need). Without it photos work and videos have no thumbnails.
- **Licenses:** Python (bundle, include its license); libvips (`pyvips-binary`, LGPL: bundle as a separate replaceable library, include its license and a link to its source code); Catalog itself is MIT (`LICENSE`). Inventory all other redistributed components before release; the ZIP includes all required license texts and notices.
- **Packaging technology** (how `Catalog.exe` is built) is not decided here: propose it after the license inventory, against these requirements.
- The ZIP is published as a GitHub Release asset (task 12); the source stays in the repository.

### New-catalog wizard (in the browser)

Shown on the first start and from "+ New catalog". Four steps with a step indicator:

1. **New catalog:** name (empty, placeholder "For example: Family photos"; required to continue); location built automatically as `<program folder>\Catalogs\<name>` and updated while the name is typed; "Change…" picks another parent folder, after which the location follows the name under that folder; note "Regular previews are limited to 20 GB; you can change the limit later in Settings." (default `thumbnail_cache_limit_gb` = 20).
2. **Source folder:** "Browse…" opens the native Windows folder picker through the local server (the browser cannot reveal a real path); manual entry as a fallback; verification.
3. **Videos:** FFmpeg download with consent, or "Not now".
4. **First load:** file counter "Scanning files…", then a summary, the checkbox "Create a “Catalog 2.0” shortcut on the desktop", and "Open catalog".

### Catalog chooser

- Shown when there is more than one catalog; with one catalog it is skipped.
- List: small collage, name, counts, when last opened, source folder; clicking a row opens the catalog. Page buttons: "+ New catalog" and "Add existing…" (pick a catalog folder, for example from a USB drive or after moving it).
- ⋯ menu per catalog: Rename, Open catalog folder, **Create desktop shortcut** (the shortcut opens this catalog directly), Remove… → dialog with "Remove from list only" (data stays, can be added back) and "Delete catalog" (data and thumbnails; photos and videos are never deleted).

### Appearance and language of the startup screens

- Startup page, chooser, and wizard **always use the default theme** (Classic Orange); a catalog's own theme applies only after it is opened.
- **Default language: English.** A dropdown language selector in the top right of the chooser and the wizard (globe icon, current language name, chevron; the list shows each language in its own language). Afterwards the language chosen in the catalog applies.

### Drive letter change

When the source folder is on the same drive as the catalog (typically a USB drive), the catalog stores the source path relative to its own drive. On another computer it tries the same path on the drive where it now lives, verifies it with the existing catalog-match check, and continues without asking on success. Otherwise the unavailable-source notice with "Change in Settings" appears (task 33).

### Catalog format upgrade (after 1.0)

When a newer program opens a catalog created by an older version, it first backs up the catalog database, then upgrades it automatically (the CLI already has `db-upgrade`). The startup page shows "Updating catalog “{name}” to the new version…" with progress and "A backup was created before the update." (artboard "Spouštění – převod katalogu na novou verzi"). If the upgrade fails, the backup is restored and a page explains it: "The catalog could not be updated" + "Catalog “{name}” was restored from the backup; nothing was lost. Try running the update again." with "Back to catalog selection" and "Try again" (artboard "Převod katalogu se nepovedl"). A catalog created by a *newer* program version is not opened: in the chooser it shows the label "Newer version" and "This catalog was created by a newer version of Catalog. Update the program."; its ⋯ menu offers only "Remove from list only". Before 1.0 no upgrade code is written (instances are recreated); the version check and the chooser state are part of 1.0.

### Done when

A user with no Python installed unpacks the ZIP, creates two catalogs through the wizard, opens each from the chooser and from a catalog shortcut, updates the program from Settings and once by unpacking a newer ZIP, and carries the folder to another computer on a USB drive with a different drive letter — all validated on Windows.

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
- Generation became faster after the move to pyvips (task 10), but browsing felt almost unchanged. The main bottleneck was the cache-size check after every generated tile (see task 4, "Cause of the slow start"); it was fixed on 2026-11-11.
- Follows up on task 7.

### Step A — browser caching (block 1) — COMPLETED

- Thumbnail URLs carry a fingerprint (an instance-specific cache key plus the media identity) and are served with `Cache-Control: immutable` and a long max-age. Validated on Windows: returning to a visited folder loads tiles from the browser cache without requests to the server.

### Cache size and catalog counts without full-table scans (block 1) — COMPLETED

- The thumbnail cache size per class and the catalog folder/media counts are stored and kept up to date in the same transaction as the change; they are recomputed exactly only in explicit cache cleanup and at catalog update. Details in `docs/DEVELOPMENT_LOG.md` (2026-11-11).
- A covering index on `thumbnails` was considered for the folder-preview query and is not needed: after the fix the query takes a few milliseconds. The remaining per-thumbnail point lookups need almost all columns, so a covering index would duplicate most of the table.

### Step B — anchor database connection (block 1)

- Measured on 2026-11-11: occasional `/api/folders` requests spend 0.5–0.8 s opening the database connection while their SQL takes about 10 ms. The cost lands on the first statement of the new connection (`PRAGMA application_id` in the connection check). The likely cause is that the server opens a new connection per request and holds none permanently: when the last connection closes, SQLite checkpoints and deletes the WAL and shared-memory files, and a request opening a connection at that moment waits for the lock with growing back-off; recreating the files on the next open may add to it.
- Keep one idle read-only connection open for the lifetime of the server so that the last connection never closes during normal operation; automatic WAL checkpoints continue as before.
- Done when: a new diagnostic measurement after restarting Windows shows no `/api/folders` requests dominated by `db_connect`, and `PRAGMA application_id` is no longer among the most expensive statements.

### Step C — remaining investigation (not decisions)

- Where the waiting occurs between the browser request and the displayed thumbnail; folder previews wait mostly in the browser queue behind photo tiles (measured 2026-11-11).
- Viewport handling.
- Whether generating one thumbnail opens the database several times (reportedly three times), and whether that is unnecessary.
- Limiting the number of concurrent browser requests.
- Cancelling requests for thumbnails that are no longer on screen.
- Prioritizing visible thumbnails.
- Possible generation ahead of scrolling (must be measured on an HDD).

### Constraint

The dynamic-cache cleanup keeps its principle: it removes the least recently used entries. How the cleanup is carried out may change when that improves performance. Since 2026-11-11 it starts when the dynamic cache exceeds its limit and cleans down to 90 % of the limit, so the full selection does not repeat after every new tile.

### README

Recommend in `README.md` keeping the catalog folder (database and cache) on an SSD for large collections; media may stay on an HDD (already added on 2026-11-06).

## 26. Folder URLs — open a folder in another tab

This task was merged into task 19.

## 27. Catalog update performance — PENDING

Done together with task 31, because both change the file walk.

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

Note: the folder page is redesigned again in tasks 32 and 33.

## 29. Folder card layout — COMPLETED

Folder cards (including Search and Favorites results) have one header row with the name, a summary with media and subfolder counts and a media-type ratio bar, and the actions, followed by full-width previews about 160 px wide whose number follows the card width. The statistics panel and the wide/compact card switching were removed, the breadcrumb is hidden on the root page, and `FOLDER_PREVIEW_REQUESTED_COUNT` is 12 (existing instances need one full folder-preview tree build).

Validated in Firefox; Chrome and Edge were not validated. See `docs/DEVELOPMENT_LOG.md` (2026-10-09).

Note: replaced by the collage cards of task 33 (`FOLDER_PREVIEW_REQUESTED_COUNT` 12 → 9).

## 30. Photo modal — selection flash on navigation — PENDING

### Problem

When clicking the edge of the photo modal to navigate to the previous or next photo, the old photo briefly flashes blue like a browser selection. The modal code has not changed.

### Solution

`user-select: none` on the photo area. In the new modal (task 32) clicking the photo does nothing, which also removes the trigger.

## 31. Redesign step 1 — media dimensions, video duration, larger photo tiles — PENDING

Block 3, first redesign step. Decisions §1, §5, §7, §8. Includes task 27.

- **Read media dimensions** (width and height) during the catalog update: only the file header for images; `ffprobe` for videos. Store them in the database. Only new and changed files are read on later updates. The first run on a large collection on an HDD takes noticeable time (tens of minutes to hours); measure it.
- **Read video duration** in the same `ffprobe` call and store it; it is shown in the modal info line (task 32).
- **Photo tile height 560 px** with a width limit for panoramas (today `image_thumb_size` is at most 300 × 400 px, so a landscape photo has only 300 × 200 px). 560 px covers the largest media row (360 px) at 150 % display scaling (540 px). About a quarter more space per tile than 500 px; generation time is dominated by reading the original. Tiles are never upscaled (`size="down"` stays); a smaller original keeps its size. Increase the photo-tile algorithm version so tiles are regenerated once. Video posters stay as they are (`video_preview_width` 1024 → 1024 × 576).
- Estimate the cache size on the sample instance.
- Done when: dimensions and duration are in the database after an update, new photo tiles are 560 px high, and the measured update time on the full collection is reported.

## 32. Redesign step 2 — justified media rows and the new modal — PENDING

Block 3. Decisions §5, §7; interactive artboard "Stránka složky (interaktivní)".

### Media rows

- Every item in a row has the same height; the width follows the original's aspect ratio from the database (task 31); no crop. The row height adjusts slightly so the row fills the width; the last row is not stretched.
- **Extreme aspect ratios are clamped to 1:2.5 … 3:1**; only those items are cropped (centered) in the grid. Phone screenshots (~1:2.2) are not cropped; panoramas (4:1–8:1) become 3:1 (about 780 px at 260 px height). The modal always shows the whole photo. Layout rule only; generation is not affected.
- No frames and no text; on hover a star in the top right and the file name at the bottom. Video: a play icon in the center drawn by Catalog (all formats including AVI). GIF: a "GIF" label.
- **Media-size slider** in the Media section header; it changes media only (folders keep a fixed look). Row heights **180 / 220 / 260 / 310 / 360 px**, default 260 (approved). The chosen value is remembered.
- The row height is at most (content width − gap) / 2.5 so at least two items fit (narrow window, task 33).

### Modal

- Covers the whole window above the page; closing returns to the same place.
- **A photo opens fitted to the window.** Zoom: − [%] + (clicking the percentage fits to the window), "1:1" = original size.
- **Navigation only with the arrows** at the sides; click area over the full height of the edge (96 px; 56 px in narrow windows). Clicking the photo does nothing (task 30). ←/→ keys navigate.
- **Closing:** the ✕ button, Esc, and a click on the dark area around the photo (not on the photo, the arrows, the toolbar, or a video and its controls).
- **Toolbar of icons with tooltips**, from the left: favorite (star), pin (pin) | open the original in the default application (↗; for a video "Open in player"), open the source folder (folder icon) | − % + 1:1 | close. "Open original" opens the file in the system default application (`/api/open-original`), not in the browser.
- **Info line:** position / total, path, dimensions; video duration.
- **Rename in the toolbar:** task 22.
- The thumbnail strip is not used.

### Video in the modal

- Arrows navigate photos and videos in one sequence in grid order (today the video modal breaks navigation: `openVideoModal` clears `imageItems`). Leaving a video stops it.
- A video the browser can play (`is_html_playable`, `media_types.py`): the browser's built-in controls; it starts automatically with sound (the click is a user action); mute and volume are remembered.
- A video the browser cannot play (for example AVI): the modal opens too (today a click starts the system player directly), showing a large poster, the sentence "This format cannot be played in the browser.", and the primary button "Play in player" (`/api/open-original`).
- Keys: ←/→ always navigate, Space plays/pauses; the modal keeps focus so the arrows do not seek in the video.
- The toolbar has no zoom group for video; the video fits the window, full screen through the player controls.

### Done when

Media rows, the slider, the modal, and video behave as in the interactive artboard on Windows in Firefox.

## 33. Redesign step 3 — folder collage, one pagination, tree panel, header, page states — PENDING

Block 3. Decisions §2, §3, §6, §11; artboards "Stránka složky (interaktivní)", "Stavy – načítání", "Stavy – prázdné a nedostupné", "Stavy – běžící úloha", "Stavy – úzké okno 960 px", "Úzké okno pod 900 px – hlavička".

### Page layout

- **Content width:** at most 1600 px, centered. One CSS value.
- **Header** (fixed height, never wraps — wrapping would push the page down when the task chip appears):
  - left: ☰ (tree) and the logo "Catalog 2.0" — 40 px, bold, in the theme accent (renameable); under it a small GitHub link;
  - center: the search field as the main element; "Current folder only" as a folder-icon toggle inside the field (highlighted with the accent); search with the magnifier in the field or Enter; no separate Search button;
  - right: Favorites (navigates to the view) and Settings; header buttons are 40 px;
  - task status: hidden when nothing runs; see "Running task" below.
- **Accent color** only for the logo and active states (active filter, selected item, toggles), not for ordinary buttons.
- **Tree:** a drawer under ☰ over the content (the page does not move): "Find folder" field, sections Favorite folders, Recently visited, and the tree without icons with item counts.
- **Folder header:** breadcrumb; name (30 px) with borderless icons: pencil (rename, task 22), star (favorite), ⋯ (open in Windows, update, recompute previews). Under the name one line of non-zero counts.
- **Filter row:** All / Folders / Photos / GIFs / Videos / Other as underlined tabs; tabs with zero items are greyed out and disabled (not hidden), so the row never changes layout — `folder_detail` already returns per-type counts. On the right a compact pager "‹ 3 / 10 ›" (click the number → input to jump, Enter confirms, Esc cancels), hidden with a single page.

### Folder collage cards

- Grid `repeat(auto-fill, minmax(min(340px, 100%), 1fr))` (about 4 per row at 1920 px; 2 at 960 px; 1 below 720 px).
- Each card: 1 large preview + 4 small (grid `2fr 1fr 1fr` / 2 rows, card ratio 4:3), name and counts below; star and pencil appear on hover.
- All folder previews are cropped to one portrait ratio (uniform look, fixed height). Media are not cropped.
- **Nine previews per folder** (`FOLDER_PREVIEW_REQUESTED_COUNT` 12 → 9): place 1 main, 2–5 secondary (the 4 small ones), 6–9 only in rotation.
- **Rotation:** main preview only. Moving the mouse left to right over the large preview steps through all nine (not timed); a bar with nine segments at the bottom shows the position. Hovering a small preview shows it in the large frame. Small previews do not change. Places 6–9 load only on hover.
- **Parent folders:** places are still divided among subfolders by size (`_weighted_select_rows_by_source`, square root of the media count); within a subfolder's share its manually pinned photos have priority (task 35).

### One pagination

- One list: subfolders first, then media; one pager; one URL parameter `page` (replaces `folder_page` and `media_page`).
- Items per page from Settings (24 / 48 / 96 / 192, default 48; task 20).
- The button for collapsing subfolders is removed (the filters replace it).
- Compact pager in the filter row at the top, full pager at the bottom (« ‹ [3] / 10 › »). Keyboard shortcuts Page Up / Page Down for the previous/next page.

### Page states

Principle: content never jumps. Space is reserved in advance; notices that appear while the user is browsing float over the content.

- **Loading:** breadcrumb, name, counts, and filters render immediately from the API; tiles are skeletons with the final shape (folder cards 4:3, media rows with real widths from the database). A thin indeterminate bar under the header appears only after 300 ms. A loaded tile fades in over 150 ms; no animations with `prefers-reduced-motion`. A tile that fails to load shows a grey area with an icon and the file name, "Preview not available"; the item still opens. An API error replaces the grid with "The folder content could not be loaded." and "Try again". During a cold start the task area shows "Preparing previews…" until the server reports readiness (requires the readiness signal of task 4).
- **Empty states** (calm icon, one sentence, at most one action):
  - folder without subfolders and media: "The folder is empty" + a link back to the parent folder;
  - filter without results (only reachable from a URL or history, because zero tabs are disabled): "There are no GIFs in this folder" + "Show all";
  - empty catalog of a new instance: "The catalog is empty" + "Update catalog";
  - folder no longer in the catalog: the root opens with the message "The folder from the address was not found: …" (task 19, already implemented).
- **Unavailable source** (not an empty state): `source_root_status` (checked on every load, nothing cached) reports that `data_root` does not exist, is not a folder, or cannot be opened. Causes vary, so none is guessed. One application-wide notice above the breadcrumb, shown on page load only: "The source folder **{path}** is not available. Check that the drive or network location is connected." + "The content is shown from the catalog; originals cannot be opened now." Actions "Try again" (reload) and "Change in Settings" (task 20). The folder shows a "Unavailable" label.
- **Folder missing on disk while the source is available** (`missing`, `not_directory`, `link_or_junction`): a notice for that folder only, "The folder no longer exists on disk. It may have been deleted or moved outside Catalog." + "Update this folder"; label "Unavailable"; catalog content stays.
- **Running task (first version, no backend change):** data from `/api/jobs/status` (`current_job`: kind, scope, state, `started_at`, error, result), polled every second; only one job at a time (`_ensure_start_allowed`).
  - Chip "Catalog update running 4:12" (elapsed time computed from `started_at`) between the search field and the buttons; clicking it opens a panel with scope, start time, and elapsed time.
  - Conflicting actions are disabled with the explanation "Another job is already running: …" (the backend also refuses them with `JobBusyError`).
  - When finished: "Catalog update finished" for 4 s and a floating notice at the bottom center of the window, "The catalog has changed. The content of this folder may be different." with "Refresh" and ✕. This replaces today's automatic `refreshCurrentViewAndTree` after an update; the page never rearranges itself under the user.
  - Error: the chip stays in the error color until the user opens it; the panel shows the backend error text translated through its message key.
  - Screen readers hear only the start and the end (`aria-live="polite"`).
  - Optional in this step, without backend change: during the walk phase a file counter "Scanning files… 12,000" from `latest_scan.staged` (WAL mode, the scan commits in batches of 1000); poll it about every 5 s because the count scans the index. Percentages, estimates, and cancellation are task 38.
- **Narrow window** (the real case is a window snapped to half of a 1920 px monitor, 960 px):
  - below 1280 px: Favorites and Settings become icons with tooltips; the search field narrows;
  - below 900 px: search hides behind a magnifier icon and expands over the whole header (Esc or a back arrow closes it); the filter tabs scroll horizontally;
  - folder cards follow the grid rule above; media rows follow the row-height rule of task 32; in the modal the toolbar wraps and the arrow areas shrink to 56 px;
  - guaranteed support from 768 px; below that nothing may break.

### Done when

The folder page and all states match the artboards on Windows in Firefox, the URL contains only `page`, and no state makes the content jump.

## 34. Redesign step 4 — replace obviously bad photos in folder previews — PENDING

Block 3. Decisions §3 (variant C, approved 2026-11-06).

- The nine photos are chosen evenly across the folder, as today.
- From the tiles that are created for the card anyway, compute a fixed rule "obviously bad photo": nearly uniform (wall, ceiling, covered lens), nearly black, or nearly white. Videos are judged by their poster.
- An obviously bad photo is replaced by its neighbor in the same folder; if that one is bad too, by the next one, up to a fixed limit per folder.
- The rule is the same for every user, set conservatively to catch only obvious cases (it must not reject, for example, a snowy landscape or a night sky). The user sets nothing.
- Cost: 9 reads per folder plus replacements only.
- Rejected alternatives: choosing the best 9 of 27 candidates (about 3× longer first preparation); precomputing from embedded EXIF thumbnails (depends on whether files have them).
- Done when: on the sample instance, folder cards contain no black frames, walls, or blank areas, and normal photos such as snow scenes are kept.

## 35. Redesign step 5 — manual pinning of folder previews and crop focus — PENDING

Block 3. Decisions §4; interactive artboard (pin panel in the modal).

### Pinning

- In the modal the pin icon "Pin to folder preview" opens a drop-down panel anchored under the button: choose the level (the photo's own folder or one of its parents) and a plan of the nine places (main, 4 secondary, 4 in rotation). The rotation row (places 6–9) spans the full width of the collage; its tiles have the same shape as the small previews. Clicking a place pins the current item; clicking again releases it. A pinned place is marked everywhere with a dot in the accent color (also the main place). Text under the plan: "Click a place to pin the photo; click it again to release it. Drag to move the crop; double-click to center it again."
- Rules:
  1. automatic recomputation (catalog update, new albums, preview preparation) never changes pinned places; it fills only unpinned places, without duplicates;
  2. only the user releases pins: per place, or "Restore automatic selection…" for a folder — an unobtrusive link with a confirmation in the panel ("Release the pinned places (N) in folder X? The automatic selection will replace them.");
  3. when a pinned item disappears, the place returns to automatic selection (a preview never stays empty);
  4. pinning or releasing recomputes that folder and its parents immediately; the user never starts recomputation manually.
- The database already supports it (`folder_preview_items`, type `manual`, position). Verify: the link survives a file rename; manual places' tiles are protected in the protected cache (task 9); the final preview is composed from manual and automatic places.

### Crop focus (last part of this task)

- In the pin panel a pinned photo can be moved inside its place by dragging (the frame stays; the image moves); a double click returns it to the center. The focus is saved and the folder card changes immediately. Only in the panel, not on the cards (accidental drags).
- Technically: two columns with the crop focus of manual places in `folder_preview_items`, applied with CSS `object-position`; thumbnails and cache are not regenerated.

### Done when

Pinning, releasing, the reset confirmation, and crop focus work as in the interactive artboard, and an update never changes pinned places.

## 36. Search results grouped by folder; Favorites — PENDING

Block 3. Decisions §7a; artboard "Výsledky hledání".

### Current state (verified)

`search_page` matches the text against the whole path (`path_key LIKE`, folder and file names) and the media type; results are ordered by path; filters and folder scope exist; folders and media have separate pagination. Favorites (`favorites.json`, with the date added) have the same structure.

### Search

- Same template as the folder page: header, filters, one pagination, collage cards for folders.
- Instead of the breadcrumb: "Results for “{text}”" + a scope label ("in the whole catalog" / "in folder {name} ✕" — the ✕ widens the search to the whole catalog) + a count line.
- **Found folders** as collage cards; the match in the name is underlined in the accent color; the parent path below the name.
- **Found media grouped by folder:** each folder with a match is one group: a path line (link to the folder), the number of matches, "Show all →", and **one row of sample items** (clipped to the width). **Pagination is by groups** (for example 20 groups per page), not by items — necessary for large collections (one search can find 100,000 photos).
- **"Show all →"** opens the search limited to that folder (existing scope) with normal pagination over all its matches.
- **Match highlighting:** a match in the path is underlined in the path line; a match in a file name is underlined in the name shown on hover.
- Order by path (predictable; no relevance ranking).
- **The modal from results** navigates within the results; the info line shows the full path and "Show in folder" (anchors `/api/search/anchor` and `/api/favorites/anchor` exist).
- **Empty result:** "Nothing found for “{text}”"; with a folder scope also "Search the whole catalog".
- **Backend:** a new grouped query — folders with matches + counts + the first N samples per folder, paginated by groups (SQLite `GROUP BY` and window functions).

### Favorites

- No grouping; one list of folders and media (Favorites are usually small).
- Empty: "You have no favorites yet. Add them with the star on a folder or photo."
- An unavailable item stays in the list with the "Unavailable" label and can be removed with the star.

### Done when

A search with many matches shows every folder group within a few pages, "Show all" lists all matches of one folder, and both work on the full collection with acceptable response times.

## 37. Catalog resilience — PENDING

Block 5. Decisions §7d. Situations caused outside Catalog.

- **Catalog folder missing** (deleted or moved in Explorer): in the chooser (task 23) a dashed frame, the label "Not found", the text "The catalog folder was moved or deleted", the button "Find…", and in the ⋯ menu only "Remove from list only".
- **Damaged database** (for example after a power failure): an understandable error and "Recreate catalog" — the media are read again from the source. To keep Favorites, pins, crop focus, and settings, **back them up automatically** (for example at every start, keeping the last few copies) and restore them into the recreated catalog.
- **Full disk with the catalog:** missing thumbnails appear as "Preview not available" tiles; an update ends with the error "The drive with the catalog is out of space." **Verify** that the update's staging phase leaves the original catalog untouched in this case.
- Done when: each scenario is reproduced on Windows and behaves as described.

## 38. Enhancements — PENDING

Block 6, after the core works. Each item is independent; if one stalls, it must not delay the others.

1. **Job progress** (backend): extend `RuntimeJob` with phase, done / total, and the time of the last change; the job thread updates them and `snapshot()` reads them under the lock. Percentages only for phases with a known total (activation, previews); the file walk has only a counter. The frontend computes the remaining-time estimate from the rate and shows it only after the rate stabilizes (about 30 s of data). UI: chip "Catalog update 45%" with a thin progress line, "Scanning files… 12,400" for phases without a total, and a panel with phase, processed x / y, elapsed time, and remaining time. Design: artboard "Stavy – běžící úloha", right column.
2. **Safe cancellation of long jobs** ("Zastavit" button in the task panel): a job stops at a safe point and leaves the catalog consistent.
3. **Smart crop for automatic preview places:** when a preview tile is created, determine the crop center with pyvips `smartcrop` (`interest="attention"`) and use it instead of the center (`object-position`). A few milliseconds per photo; nothing extra is read.
4. **Custom color scheme** (draft; decide whether to keep it once task 14 step 2 is done; may be dropped): "Custom…" in Settings › Appearance opens an editor of four base colors (background, surfaces, text, accent) with a live preview, a readability warning when text/background contrast is below 4.5:1 ("The text is hard to read on this background."), and "Restore defaults". The other tokens (borders, muted text, …) are derived; status colors (success, warning, error, info) stay fixed. Design: artboard "Nastavení (interaktivní)".

## 39. UI texts — one voice, translation rules, human backend messages — PENDING

Block 2, together with task 14. Decisions §12.

- **Formal address (vykání) everywhere** in Czech. Today `cs.json` mostly uses the informal form (about 15 texts, for example "Zadej hledaný text.", "Připoj zdrojový disk…") — rewrite them. Buttons use the infinitive ("Zkusit znovu", "Obnovit", "Aktualizovat tuto složku").
- **Rules for every new or changed text:**
  1. one complete sentence per key, never composed from fragments (word order differs between languages);
  2. variables (path, name, count) only as placeholders inside the sentence;
  3. buttons and labels are designed to fit a longer translation.
- Counts use the existing `count.*.one/few/other` forms.
- Every key exists in `cs.json` and `en.json` (checked by `tools/check_i18n_translations.py`).
- **Backend messages shown to users are human:** no internal terms such as "dynamic cache items", "DB", "reference"; one sentence about what happened and, where useful, what to do.
- Done when: the translation check passes, no informal Czech text remains, and no user-visible message contains internal terms.

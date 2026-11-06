# Catalog 2.0

## Project

Catalog 2.0 is a local media catalog for Windows. A local Python application serves the browser UI; catalog state is kept separately from the user's source media.

Separate Catalog instances have their own database, settings, cache, runtime state, and launcher. Normal browsing, scanning, preview generation, and instance updates must not delete, move, or modify source media. Source-media changes are limited to explicit user-initiated operations such as rename. Task 23 in `docs/WORK_PLAN.md` replaces the per-instance program copy with one program and data-only catalogs; until it is implemented, the current model applies.

The supported product environment is Windows. Do not change product behavior merely to accommodate a non-Windows development environment.

Project documentation is written in English.

## Repository orientation

- `catalog2.py` — main CLI entry point for project/instance operations.
- `catalog_app/` — application implementation.
- `catalog_app/static/` — browser frontend, styles, and localization resources.
- `tests/` — automated regression tests.
- `tools/` — development and diagnostic utilities.
- `README.md` — user-facing product, setup, and supported-environment documentation.
- `LICENSE` — the project license (MIT).
- `docs/DEVELOPMENT.md` — authoritative development, test, validation, and diagnostic commands.
- `docs/DEVELOPMENT_LOG.md` — completed and validated development history.
- `docs/WORK_PLAN.md` — current task map, unresolved requirements, and deferred work.
- `design-reference/` — not tracked by Git. The agreed design and decision record for the 1.0 redesign; see "Design reference" below.

## Development environment

Claude Code runs in an isolated Linux Docker container. The project is mounted at `/workspace`; `.git` and `.venv` are read-only.

The container has no Python runtime, project dependencies, or FFmpeg. Do not try to install them.

Tests and the application run on the Windows host. When validation is needed, give the exact command from `docs/DEVELOPMENT.md` and wait for the user to report the result.

Communicate with the user in Czech. Keep project documentation, code comments, and commit messages in English.

## Working with the project

For planned work, read the relevant task in `docs/WORK_PLAN.md`, then inspect the current implementation and relevant tests before proposing or making changes. Treat the work plan as planning context, not as proof of the current implementation.

Use `docs/DEVELOPMENT_LOG.md` when previous implementation decisions or completed work are relevant. Do not load the full log by default when a focused lookup is sufficient.

If a task does not define enough product behavior to implement safely, stop at analysis and identify the unresolved decision instead of inventing a contract.

Work on one clearly bounded functional change at a time. Keep structural refactoring separate unless refactoring itself is the selected task.

Preserve established API, configuration, persistent-data, cache, and instance-update contracts unless the selected task requires changing them. Before the 1.0 release, there are no external users: do not add compatibility, migration, or transition layers for existing instances unless the task explicitly asks for them. Instances are recreated cleanly after such changes.

Plan only the selected task. Do not include outlooks on other tasks; mention another task only if it blocks the selected one.

One-off measurement, benchmark, or investigation scripts do not belong in the repository unless the task explicitly says so. Provide them as standalone scripts for the user to run outside the project.

If the same step fails twice, stop and report the problem instead of retrying.

## Working through a whole task

The user selects one task from `docs/WORK_PLAN.md` and asks for all of its steps at once. Work through the steps in order without waiting for approval after each step.

Stop and ask only when:

- the task or the design reference does not define the needed product behavior;
- the work plan and the design reference disagree, or the implementation contradicts an assumption the task relies on;
- a change would touch source media outside an explicit user operation;
- the same step fails twice.

`.git` is read-only and commits are made by the user on Windows. At the end of the task, report in Czech:

1. what changed in each step (files and behavior, briefly — no full diffs);
2. what was not done and why, and any open question;
3. the exact validation commands from `docs/DEVELOPMENT.md` and a short manual check list for Windows;
4. a proposed commit message (English) for each step, so the user can commit the steps separately.

Keep progress messages short during the work. Do not paste large code excerpts or diffs into the conversation unless the user asks.

After the user reports successful validation, record the result in `docs/DEVELOPMENT_LOG.md` and update the task status in `docs/WORK_PLAN.md`.

## Design reference

`design-reference/` holds the agreed design (not tracked by Git):

- `catalog-redesign-rozhodnuti.md` — the decision record in Czech with rationale; the work plan states the same requirements in English.
- `artboards/*.dc.html` — design source of each screen: exact colors, sizes, texts, and states. Read the source for values instead of guessing them.
- `screenshots/` — images of the artboards.
- `README.md` — which file is which screen.

The design uses placeholder images and invented names. The artboards are design sources, not production code: rebuild the UI in the application's own structure.

## UI rules

These rules apply to every UI change:

- **Content never jumps.** Space for content is reserved before it loads (sizes come from the database). Notices that appear while the user is browsing float over the content; they are never inserted into the layout. The header never wraps.
- **Colors only from the token set** (task 14). No hard-coded colors outside the token definitions. Each theme sets `color-scheme` and the colors of `option` lists. The accent color is used only for the logo and active states, not for ordinary buttons.
- **One focus ring:** an active input has a single border in the accent color; the browser's default outline is not drawn on top of it. Keyboard focus stays visible.
- **Accessible as built:** real `button`, `a`, `input` with `label`; `aria-label` on icon-only buttons; text contrast at least 4.5:1 in every theme; no animation with `prefers-reduced-motion`.
- **Every new UI element works in all themes**, including the light theme "Warm Light", and in Firefox, Chrome, and Edge.

## UI texts

- Czech texts use the formal address (vykání). Buttons use the infinitive ("Zkusit znovu", "Obnovit").
- Every text is one complete sentence in one key; never compose sentences from fragments. Variables appear only as placeholders inside the sentence. Counts use the `count.*.one/few/other` forms.
- Every key exists in `cs.json` and `en.json`; run the translation check from `docs/DEVELOPMENT.md`.
- Messages shown to users contain no internal terms (cache classes, "DB", reference counts, technical paths); one sentence about what happened and, where useful, what to do.
- Buttons and labels must fit a longer translation.

## Performance

Catalog must perform acceptably on typical consumer hardware, not only on the developer's machine. Do not tune defaults to one computer.

Concurrency defaults (worker counts, threads) must be derived from the CPU core count and stay conservative. They are computed automatically when the work starts, are not stored, and are not user settings. A cap based on measured disk limits (for example HDD throughput) is allowed when it is documented in the task.

Source media may be on HDDs. Any change that adds concurrent disk reads or background work must be measured with media on an HDD, not only on an SSD.

Benchmark notes describe variants and relative results, without details of the user's machine or data. Results from one machine are valid for comparing variants, not for choosing absolute defaults.

## Validation and completion

Use the established commands and checks in `docs/DEVELOPMENT.md`. Run validation appropriate to the changed surface, including focused regression coverage and the full automated suite when applicable.

Changes involving browser behavior, Windows runtime/launcher behavior, filesystem semantics, or installed instances require validation in the supported Windows environment before they are considered complete. If that validation cannot be performed in the current environment, report the limitation explicitly.

After a functional change has been validated and accepted, record the final result and validation in `docs/DEVELOPMENT_LOG.md` and update its status in `docs/WORK_PLAN.md`.

Do not commit or push unless explicitly requested.

## Repository hygiene

Do not add personal filesystem paths, secrets, private instance data, databases, caches, generated runtime state, `.venv`, or local test artifacts to tracked public content unless a task explicitly requires a safe fixture.

Keep public examples anonymized.

Do not record details of the user's media libraries in tracked content: no concrete file paths, folder or media names, collection sizes, sample file counts, or data volumes. Benchmark and validation notes keep only anonymized results.

Never copy files from `design-reference/` into tracked content.

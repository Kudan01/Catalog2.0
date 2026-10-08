# Catalog 2.0

## Project

Catalog 2.0 is a local media catalog for Windows. A local Python application serves the browser UI; catalog state is kept separately from the user's source media.

Separate Catalog instances have their own database, settings, cache, runtime state, and launcher. Normal browsing, scanning, preview generation, and instance updates must not delete, move, or modify source media. Source-media changes are limited to explicit user-initiated operations such as rename.

The supported product environment is Windows. Do not change product behavior merely to accommodate a non-Windows development environment.

Project documentation is written in English.

## Repository orientation

- `catalog2.py` — main CLI entry point for project/instance operations.
- `catalog_app/` — application implementation.
- `catalog_app/static/` — browser frontend, styles, and localization resources.
- `tests/` — automated regression tests.
- `tools/` — development and diagnostic utilities.
- `README.md` — user-facing product, setup, and supported-environment documentation.
- `docs/DEVELOPMENT.md` — authoritative development, test, validation, and diagnostic commands.
- `docs/DEVELOPMENT_LOG.md` — completed and validated development history.
- `docs/WORK_PLAN.md` — current task map, unresolved requirements, and deferred work.

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

## Performance

Catalog must perform acceptably on typical consumer hardware, not only on the developer's machine. Do not tune defaults to one computer.

Concurrency defaults (worker counts, threads) must be derived from the CPU core count, stay conservative, and remain configurable.

Source media may be on HDDs. Any change that adds concurrent disk reads or background work must be measured with media on an HDD, not only on an SSD.

Benchmarks state the CPU core count and disk type. Results from one machine are valid for comparing variants, not for choosing absolute defaults.

## Validation and completion

Use the established commands and checks in `docs/DEVELOPMENT.md`. Run validation appropriate to the changed surface, including focused regression coverage and the full automated suite when applicable.

Changes involving browser behavior, Windows runtime/launcher behavior, filesystem semantics, or installed instances require validation in the supported Windows environment before they are considered complete. If that validation cannot be performed in the current environment, report the limitation explicitly.

After a functional change has been validated and accepted, record the final result and validation in `docs/DEVELOPMENT_LOG.md` and update its status in `docs/WORK_PLAN.md`.

Do not commit or push unless explicitly requested.

## Repository hygiene

Do not add personal filesystem paths, secrets, private instance data, databases, caches, generated runtime state, `.venv`, or local test artifacts to tracked public content unless a task explicitly requires a safe fixture.

Keep public examples anonymized.

Do not record details of the user's media libraries in tracked content: no concrete file paths, folder or media names, collection sizes, sample file counts, or data volumes. Benchmark and validation notes keep only anonymized results.

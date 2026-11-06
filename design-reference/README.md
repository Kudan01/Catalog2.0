# Design reference — Catalog 2.0 (1.0 redesign)

Not tracked by Git (`design-reference/` is in `.gitignore`). Agreed on 2026-11-06.

- `catalog-redesign-rozhodnuti.md` — decision record (Czech) with rationale. `docs/WORK_PLAN.md` states the same requirements in English; when they disagree, stop and ask.
- `artboards/` — design source of each screen (`.dc.html`). Read them for exact colors, sizes, texts, and states. They are design sources, not production code; they rely on a design-tool runtime and do not open as standalone pages. Placeholder images and invented names are used.
- `screenshots/` — images of the artboards exported from the design tool.

## Artboards

| File | Screen | Work plan |
|---|---|---|
| `Main.dc.html` | Folder page (interactive): header, tree drawer, folder header, filters, pager, collage cards with rotation, media rows with the size slider, modal (photo, playable video, non-playable AVI), pin panel with crop focus, rename in the header, on cards, and in the modal | 22, 32, 33, 35 |
| `Stavy-nacitani.dc.html` | Loading: skeletons, bar under the header, a failed tile | 33 |
| `Stavy-prazdne.dc.html` | Empty folder, filter without results, empty catalog, folder no longer in the catalog, unavailable source (application-wide notice), folder missing on disk | 33 |
| `Stavy-uloha.dc.html` | Running task: first version (elapsed time, panel, disabled action, done + floating "Obnovit" notice, error) and target state (percentages, counter, estimate) | 33, 38 |
| `Uzke-okno-960.dc.html` | Window 960 px wide | 33 |
| `Uzke-okno-pod900-hledani.dc.html` | Header below 900 px; click the magnifier to expand search | 33 |
| `Velikosti-medii.dc.html` | Media-size slider scale 180–360 px compared at 1600 px | 32 |
| `Vysledky-hledani.dc.html` | Search results: found folders and media grouped by folder | 36 |
| `Nastaveni.dc.html` | Settings (interactive): four sections, "Uloženo" feedback, verify-then-save source folder, custom-color editor draft | 20, 21, 38 |
| `Spousteni.dc.html` | Startup page "Catalog se spouští…" | 4 |
| `Pruvodce.dc.html` | New-catalog wizard (interactive), four steps | 23 |
| `Rozcestnik.dc.html` | Catalog chooser (interactive): language dropdown, ⋯ menu, remove dialog, a catalog that was not found | 23, 37 |

The default accent color in the artboards is the "Classic Orange" theme (`#f06d32`) on a dark background.

# Step 11: "How It Works" Page

Date: 2026-11-21. Status: built and checked on branch `claude/explain-page-36c065`, not yet linked from the app. Plan: `docs/plans/explanation-page.md`. Page text and data shapes: `docs/plans/explanation-page-draft.md`.

## What Was Built

A new view `#explain` in new files only:

| File | Content |
|---|---|
| `web/explain.html` | The page text: outline, 10 sections (main idea first, "More detail" blocks closed), sources under each block, and the glossary of 49 terms |
| `web/explain.js` | Loads the text and data on first open and runs the interactive parts. One closure; only `window.renderExplain` is global |
| `web/explain.css` | Layout and styles under `#view-explain`, on the app's gruvbox tokens |
| `web/explain-samples.json` | Real schema, row and file counts, and one real row per pipeline stage, plus the feature rows of all 514 admin areas at one issue |
| `web/explain-membership.json` | IMERG cell weights of every area (290 KB, about 100 KB with gzip) |
| `experiments/08_explain_samples.py` | Writes the two JSON files from `DATA_ROOT` (read only) |

Interactive parts:
- **Grid cells.** The chosen area drawn over its IMERG cells, with weights and the rain of any archived half-hour. The browser reads the cell values from `/tiles/rain/{slot}.png` and computes the weighted mean next to the stored silver value.
- **Clickable pipeline.** Nine stages, each with a panel: what it does, the batch and speed layer roles, inputs and outputs, and the real data shape. A switch highlights the batch or the speed layer.
- **Follow one area.** Kota Malang by default, any of the 514 admin areas by choice: cells, silver rows, feature row, the three newest probabilities, the map colour class and the latest verified window.
- **Scores.** The card's metrics for the test year, 2026-03 to 10 and validation, a reliability chart and the top inputs by gain per lead, and the live daily scores with BMKG.
- **Issue-time timeline.** A slider over the issue times of the last 48 h. It shows the IMERG cut-off, the rain the model could not know, the cold cloud up to t, the three windows with their outcome, and the BMKG run in force.
- **Glossary terms.** Underlined terms show a short definition on hover, focus or tap.

Live values (latencies, processed values, storage and retention, the issue delay, model metrics, daily scores) come from the existing `/api/meta`, `/api/areas/{id}`, `/api/pipeline`, `/api/model` and `/api/scores`. No API change was needed.

## Hook-Up (After Pass 1)

`web/index.html`:
1. After `<link rel="stylesheet" href="/style.css">`: `<link rel="stylesheet" href="/explain.css">`
2. Before `<script defer src="/app.js"></script>`: `<script defer src="/explain.js"></script>` (it must run before `app.js`, so `renderExplain` exists when the first view renders)
3. Before the Settings tab: `<a href="#explain" data-view="explain">How it works</a>`
4. Before `<section class="view" id="view-settings" hidden>`: `<section class="view" id="view-explain" hidden></section>`

`web/app.js`:
1. `const views = [..., "pipeline", "explain", "settings"];`
2. In `renderView`, before `} else if (!meta) {`: `} else if (current === "explain") {` with `await renderExplain();`

`explain.js` reads these `app.js` globals at call time: `read`, `meta`, `areas`, `areaById`, `malangId`, `P`, `legend`, `rainClass`, `prefersLight`, `fmtTime`, `clock`, `clockWib`, `size`.

## Done When

Checked with `check_explain.py` (session scratchpad): a second API from this worktree on port 8001 with `DATA_ROOT` = the main checkout's `data/`. Playwright serves `index.html` and `app.js` with the hook-up lines above applied, so the check covers the hook-up too. It opens `#explain`, clicks a stage and a layer, taps a term, switches to Kabupaten Bogor and moves the timeline.

| Theme | Width | Console errors | Failed requests | Text below AA | Controls under 44 px | Page overflow |
|---|---:|---:|---:|---:|---:|---:|
| dark | 1440 | 0 | 0 | 0 | not checked | 0 px |
| dark | 390 | 0 | 0 | 0 | 0 | 0 px |
| light | 1440 | 0 | 0 | 0 | not checked | 0 px |
| light | 390 | 0 | 0 | 0 | 0 | 0 px |

The same check against pass 1's working copy of `index.html` and `app.js` (main checkout, 2026-11-21 after `3dc2f63`): the hook-up lines apply, with the same zero counts in all four rows. Inline glossary terms are not counted as controls: they sit inside sentences, and their hit area is widened by 10 px above and below.

Real-data checks:
- The snapshot's feature rows, rebuilt with the live scorer's code and run through the live models, give 1,542 of 1,542 stored probabilities exactly (issue 2026-11-21 03:30 UTC).
- Kabupaten Bogor, slot 2026-11-20 22:00 UTC: the weighted mean of 38 cells read from the rain image is 0.98 mm/h, and silver holds `rain_mean_x10 = 10`.
- Kota Malang has 4 cells with weights 0.583, 0.261, 0.057 and 0.001 (sum 0.902, which is 110 km² of about 122 km² per cell).

## Differences From the Plan

- "Follow one area" is its own section (6), because it runs through every step.
- The real schemas differ from `reference/schemas.md` in places, and the page shows the real ones: `map_frames` is partitioned by `date=`, `climatology` has `p1` to `p3` and `n` (no `years`), `scores_daily` stores `n` as int64 and the scores as double, and the cloud and gauge tables have one file per source.
- Feature rows are not stored, so step 3 of "Follow one area" shows the snapshot issue, labelled with its time. Every other step uses the newest issue.
- The sources table names access terms (login, rate limits, the required BMKG credit, CC BY 3.0 IGO) instead of licence names, because the repo documents only those.
- Section 8 reads the card live, so its numbers follow the model in `model.live_version`.

## Issues Met

- The shared pre-commit hook (`.git/hooks/pre-commit`, not in git) exits with status 1 in every worktree: under `set -euo pipefail` its `grep "$root/.env"` fails when a worktree has no `.env`. juan chose to fix the hook: line 6 needs `{ grep ... || true; }`. The fix runs outside this worktree, so juan applies it.
- The BMKG bronze folder `run=2026-11-20T12` holds bodies of the 00 UTC run as well (BMKG's cache, step 2). The generator takes a body whose `analysis_date` matches its folder.

## Open Points

- Regenerate the snapshot (`experiments/08_explain_samples.py`) after a model change, because the feature-row check uses the live model.
- The snapshot holds the feature rows of one issue. A live feature row per area would need the scorer to store its rows.
- Not checked: real touch devices and screen readers.

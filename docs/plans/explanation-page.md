# Plan: Explanation Page

Status: planned by juan on 2026-11-21, not built. It is pass 2. Pass 1 is the six planned UI changes in `docs/progress/step-09-ui-polish.md` (Open Points).

## Goal

A new web view, "How it works", explains the whole system to a human reader: the data sources, the pipeline, the prediction method, how it is scored, and the limits. It is detailed, but a reader who knows no meteorology or big data can follow it. Interactive parts show the real data at every step.

## Writing Rules

- Start with an outline. Each section opens with its main idea in one or two sentences, then gives the supporting detail.
- Plain words and short sentences (ASD-STE100 style). Full forms, no em dashes.
- Define each term at first use and link it to the glossary.
- Every number comes from the code, the config or a measured result (a model card, an experiment, a progress report), with the source named. No rounded claims without a source.
- One idea per paragraph. Lists only for real lists.
- The detail is in layers: the main text stays short, and "More detail" blocks and tooltips hold the depth.

## Proposed Outline

1. **The question.** Will it rain in my kab/kota in the next 1, 2 or 3 hours? Why that is hard in Indonesia (convective rain, no open radar archive).
2. **The answer in one picture.** A short flow: satellites and BMKG, then per-area numbers, then a model, then a probability, then the map. One sentence per box.
3. **Data sources.** IMERG Early (rain truth), GPM MERGIR (cloud-top history), Himawari-9 Band 13 (live clouds), BMKG forecasts (comparison), GHCNh and METAR gauges (check), geoBoundaries and wilayah.id (areas). For each source: what it measures, resolution, latency, licence, and why we use it.
4. **From grids to kab/kota.** How a satellite grid becomes one value per area (cell membership by area share, the 514 admin areas plus 5 non-admin polygons). Interactive: one area drawn over its grid cells, with the cell weights.
5. **Pipeline (big data view).** Bronze, silver, gold layers; batch layer (10-year backfill) and speed layer (sensors as data lands); Dagster jobs and sensors; Parquet, partitions and retention; volumes processed and stored. Map these to the course concepts (volume, velocity, variety, and the lambda layers).
6. **The prediction.** Target (rain at or above 0.5 mm/h at any time in the next N hours), what is known at issue time (IMERG lag, Himawari scan), the features in groups (cloud now, cloud motion, recent rain, neighbours, time of day and year, climatology), LightGBM per lead, the train, validation and test split by time.
7. **How good is it.** Brier skill score against climatology, AUC, CSI, reliability; the baselines (persistence, climatology, BMKG). Numbers from the model card in use.
8. **The live loop.** One issue cycle on a timeline: scan lands, features, score, frames, map, later verification when IMERG covers the window.
9. **Limits and honest caveats.** IMERG Early is a satellite estimate, not a gauge; latency; BMKG coverage gaps; the area-mean target hides local storms.
10. **Glossary.**

## Interactive Elements

- **Clickable pipeline diagram.** Each stage opens a side panel: what it does, when it runs, inputs and outputs, and the data shape.
- **Data shape at every step.** A tooltip or panel per stage with the real schema (columns, types, encodings such as `*_x10`) and one real sample row, from bronze grid to API JSON. Shapes come from `reference/schemas.md` and are checked against the real Parquet files at build time.
- **Follow one area.** Pick a kab/kota (default Kota Malang). The page shows its value at each step for the latest issue: grid cells, silver row, feature row, three probabilities, map colour.
- **Issue-time timeline.** A slider over the last hours that shows what data is known at an issue time (IMERG cutoff, Himawari scan, BMKG run) and the window each lead covers.
- **Glossary hover.** Underlined terms show a short definition, and the glossary section holds the full list.

## Glossary (First List)

Nowcast, lead time, issue time, slot, kab/kota, area mean, IMERG Early, MERGIR, Himawari Band 13, brightness temperature (Tb), cold cloud share, advection, climatology, persistence, bronze, silver, gold, batch layer, speed layer, sensor, partition, retention, Parquet, ZSTD, feature, LightGBM, probability of rain, Brier score, Brier skill score (BSS), AUC, CSI, POD, FAR, reliability, hindcast, verification, BMKG, ENSO, dekad, WIB, WITA, WIT.

## Inputs to Read in Pass 2

`reference/data-sources.md`, `reference/model.md`, `reference/schemas.md`, `reference/decisions.md`, `reference/course/` (course slides and the project brief), `docs/decision-log.md`, `docs/experiments/`, `docs/progress/`, the model card of the model in use, `config/settings.yaml`, and the real Parquet schemas.

## Build Notes

- A new view `#explain` with its own nav link. Most code in new files (`web/explain.js`, `web/explain.css`, or a section of `index.html` loaded on demand), so it does not touch the pass 1 code paths.
- Live values (current model numbers, volumes, latest issue) come from the existing `/api/meta`, `/api/model` and `/api/pipeline`. A new endpoint only if a sample row per stage needs one.
- Same themes, contrast and target-size rules as the rest of the app. Verify at 1440 px and 390 px, dark and light.

## Running in Parallel

The pass 1 changes and this page share `web/index.html`, `web/app.js` and `web/style.css`. To run them at the same time:

- **Research and draft (parallel with pass 1).** Read the inputs, check the facts, and write the full page text and data shapes in a draft file. Read-only on code.
- **Build (parallel with pass 1 if it stays in its own files).** Build the page in new files in its own worktree. After pass 1 lands, one small change adds the nav link and the view hook-up, then a final check of the whole app.

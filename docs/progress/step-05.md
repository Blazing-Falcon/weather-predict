# Step 5: Dagster Orchestration, Gauges and Retention

Date: 2026-11-19/20. Status: built and run once on dev; long-running checks pending (see below).

## What Was Built

- Gauges: `nowcast gauges stations | ghcnh | metar` into `silver.gauge_obs`. `is_rain` reads the METAR present-weather section only (RA, DZ, GR with SH/TS/FZ; not VC; nothing after TEMPO, BECMG, NOSIG or RMK). Station lists are fixed in `config/geo/` (212 GHCNh IDs, 106 airports).
- Retention: `nowcast retention [--dry-run]` and `nowcast disk [--budget-gb]`. The repo's own storage doc is `docs/storage.md`.
- Dagster: `orchestration/definitions.py` with ref, bronze and silver assets (monthly partitions from 2016-01 for rain and cloud), sensors for BMKG (15 min), IMERG (new CMR granule), Himawari (full scan on S3), schedules for METAR (hourly), retention (daily) and run cleanup (weekly), a `nasa` pool of 1 for the two backfills, and asset checks for the disk budget and 514 rows per slot. All sensors start stopped.

## Done When

| Check | Result |
|---|---|
| Every asset ran green once on dev | `bmkg_forecast`, `gauge_obs` (METAR job), IMERG live, Himawari live, retention (dry run), disk check: SUCCESS (Codex run). `imerg_backfill_job` and `mergir_backfill_job` for partition 2025-01 and `bmkg_job`: `RUN_SUCCESS` (run on 2026-11-20 from `dagster job execute`). |
| Restarting Dagster loses no data, sensors resume | Not yet run with the daemon. By design the cursors live in `state/*.json` (not in Dagster) and every sensor run has a run key, so a restart cannot double a run. **Pending**: needs a daemon left running, which needs juan's OK (see below). |
| Retention deletes a test partition older than its window | A planted `silver/rain_kabkota/year=2014/month=01` was removed by `nowcast retention` (10-year window); the 2025 and 2026 partitions stayed. Unit tests cover `run=`, `date=/hour=`, mtime and model-version cases. |
| Disk check fails when the budget is lowered | `nowcast disk --budget-gb 0.01` printed `TOTAL 0.259661 GB of 0.01 GB` / `FAIL disk budget exceeded`, `exit=1`. |

Gauges, January 2025: 67 of 212 GHCNh stations had data, 45,972 observations, 13.9% with rain, none with an amount (380 MB downloaded and deleted after parsing). Live METAR: 27 of 106 airports reporting.

## Differences From the Plan

- The systemd user timers (`ops/systemd/`, installer `ops/install-timers.sh`) exist for every live job, but installing them was blocked by the permission check, so on dev the jobs currently run in loops tied to the working session. Installing either the timers or a Dagster daemon is **juan's decision**.
- The lineage graph image for the presentation is not captured yet (it needs `dagster dev` running).

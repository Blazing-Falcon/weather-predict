# Data

All data lives under `DATA_ROOT` (default `./data`). It is never committed. Parquet files use ZSTD compression. Monthly datasets use hive folders `year=YYYY/month=MM`; daily datasets use `date=YYYY-MM-DD`. Every file is published atomically: the writer fills a hidden temp file in the target folder, syncs it, renames it over the target and syncs the folder, so a reader sees the old file or the new file, never a part.

Times are UTC. Integer columns with a suffix store scaled values: `_x10` is tenths, `_pct` is percent.

## Zones

| Zone | Holds |
|---|---|
| `ref/` | Reference tables built once: areas, grid memberships, neighbours, BMKG points, full-resolution boundaries |
| `bronze/` | Raw downloads, kept only as long as reprocessing needs them |
| `silver/` | One row per area and time step, per source |
| `gold/` | Products: climatology, predictions, verification, scores, map frames, tiles |
| `models/` | One folder per trained model version |
| `state/` | Cursors, locks, latency logs, health files, gap reports |
| `tmp/` | Scratch space (Spark, feature tables, screenshots); not counted in the disk budget |

## Reference Tables (`ref/`)

| File | Columns | Built by |
|---|---|---|
| `kabkota_full.geojson` | geoBoundaries gbOpen IDN ADM2, commit `9469f09`; `kabkota_full.source.json` records URL, bytes and sha256 | `nowcast registry boundaries` |
| `dim_area.parquet` | `area_id`, `geo_name`, Kemendagri code, name and province, `island`, `tz` (WIB, WITA, WIT), `is_admin`, centroid, `area_km2`, BMKG point (`bmkg_adm4`, `bmkg_lon`, `bmkg_lat`) | `registry dim-area`, then `registry bmkg-picks` fills the BMKG columns |
| `imerg_membership.parquet`, `mergir_membership.parquet` | `area_id`, `cell` (row-major index in the crop), `weight` (share of the cell inside the area) | `registry membership --grid all` |
| `neighbours.parquet` | `area_id`, `neighbour_id`, `kind` (`border` or `nearest`), `dist_km` | `registry neighbours` |
| `bmkg_candidates.parquet` | Candidate BMKG villages per kecamatan with location | `registry bmkg-picks` |

There are 519 areas. 514 are administrative kab/kota (`is_admin`). The other 5 are shapes in the boundary file that belong to no kab/kota: Danau, Danau Toba, Hutan, Waduk Cirata and Wadung Kedungombo. Only the 514 get forecasts.

## Bronze

| Dataset | Layout | Retention | Content |
|---|---|---|---|
| `bronze/imerg` | `year/month/<title>.nc4` | 7 days | OPeNDAP crop of IMERG Early precipitation, one file per 30-min granule |
| `bronze/mergir` | `year/month/<title>` | 7 days | OPeNDAP crop of MERGIR brightness temperature, two frames per hourly file |
| `bronze/himawari` | `date=YYYY-MM-DD/hour=HH/HS_H09_..._B13_FLDK_R20_S0510.DAT.bz2` | 6 hours | Himawari-9 band 13 segments 5 to 7 for each 10-min scan |
| `bronze/bmkg` | `run=YYYY-MM-DDTHH/sentinel=<stamp>.jsonl.gz`, `sweep=<stamp>-<n>.jsonl.gz` | 12 months | Every BMKG HTTP response as one JSON line (`area_id`, `adm4`, `fetched_at`, `http_status`, `body`), 50 lines per sweep file |
| `bronze/bmkg/_dlq` | `date=YYYY-MM-DD/run=HH.jsonl.gz` | 3 months | BMKG steps that failed validation, with the reason |
| `bronze/gauges` | `ghcnh/date=.../*.psv`, `metar/date=.../*.json` | 7 days | Raw GHCNh station-year files and METAR responses |

NASA backfills delete each month's bronze crops when the month is done. The Himawari tick deletes processed scans older than 6 hours.

## Silver

| Dataset | Files per month | Retention | Columns |
|---|---|---|---|
| `silver/rain_kabkota` | `part.parquet`, `_manifest.parquet` | 10 years | `slot`, `area_id`, `rain_mean_x10` (area-weighted mean, mm/h), `rain_max_x10`, `wet_pct` (share of the area at 0.1 mm/h or more), `product_version` (V07B, V07C). The manifest has one row per granule. |
| `silver/cloud_kabkota` | `part-mergir.parquet` (source 1), `part-himawari.parquet` (source 2), one manifest per source | 10 years | `slot`, `area_id`, `source`, `tb_min` (K), `tb_mean_x10`, `cold_pct` (share below 233 K), `adv1_pct` to `adv3_pct` (cold share advected 1, 2, 3 h ahead), `flow_kmh` |
| `silver/bmkg_forecast` | `part.parquet` | 3 years | `analysis_time`, `first_seen`, `area_id`, `adm4`, `valid_time`, `lead_h`, `weather` (BMKG code), `tp_x10`, `tcc_pct`, `t`, `hu_pct`, `ws_x10`, `wd_deg` |
| `silver/gauge_obs` | `part-ghcnh.parquet`, `part-metar.parquet` | 10 years | `station_id`, `source`, `obs_time`, `lat`, `lon`, `area_id`, `wx` (rain groups), `is_rain`, `precip_mm` |

Rain and cloud rows exist for each of the 514 admin areas and each 30-min slot. MERGIR covers 2016-01 to 2026-10 and is the training history; Himawari is the live cloud source from 2026-11.

## Gold

| Dataset | Layout | Retention | Columns |
|---|---|---|---|
| `gold/climatology` | `part.parquet` | 10 years | `area_id`, `month`, `hour_local`, `wet_pct`, `rain_mean_x10`, `p1` to `p3` (event probability for each lead, percent), `n` |
| `gold/predictions` | monthly `part.parquet` | 12 months | `issued_at`, `t` (issue slot), `area_id`, `lead_h`, `prob_pct`, `model_version`, `cloud_slot`, `imerg_slot` |
| `gold/hindcast` | monthly `part.parquet` | not set | Same columns as predictions, for past slots scored later; never mixed into predictions |
| `gold/verification` | monthly `part.parquet` | 12 months | Prediction columns plus `verified_at`, `observed`, `clim_pct`, `persist_pct`, `motion_pct`, `bmkg_rain`, `bmkg_analysis_time` |
| `gold/scores_daily` | monthly `part.parquet` | 10 years | `day`, `scope` (national, island, province), `scope_id`, `lead_h`, `source`, `n`, `brier`, `bss`, `csi`, `pod`, `far`, `base_rate` |
| `gold/map_frames` | `date=YYYY-MM-DD/part.parquet` | 31 days | `slot`, `area_id`, `layer`, `value` (long format, see below) |
| `gold/tiles` | `rain/<YYYYMMDDTHHMMZ>.png`, `cloud/<YYYYMMDDTHHMMZ>.webp`, `bounds.json` | 31 days | Rain in tenths of mm/h (0 to 25.5) as 8-bit grey; cloud coldness as 8-bit grey; the bounds and pixel sizes of both grids |

Map frame layers:

| Layer | Value |
|---|---|
| `obs` | IMERG area mean, tenths of mm/h |
| `cold` | Cold-cloud share, percent |
| `prob1`, `prob2`, `prob3` | Model probability for the next 1, 2, 3 h, percent (live predictions first, hindcasts only for slots without one) |
| `bmkg` | 100 when the BMKG step over the slot has a rain code, 0 when it has none; absent when no step of the newest run covers the slot |
| `bmkg_code`, `bmkg_step`, `bmkg_age` | BMKG weather code, minutes since the step began, hours since the run |
| `disagree` | 1 when the model (`prob3` at 50% or more) and BMKG disagree about rain in the next 3 h |

## Models

`models/<YYYYMMDDTHHMMZ>-<commit>/` holds `model_lead1.txt` to `model_lead3.txt` (LightGBM boosters), optional `isotonic_lead<n>.json`, `card.json` (periods, features, parameters, metrics, thresholds, importance, reliability) and the reliability and importance figures. Retention keeps the 5 newest folders and always the one named by `model.live_version`.

## State

| File | Written by | Content |
|---|---|---|
| `imerg_live.json`, `himawari_live.json`, `score_live.json` | the live ticks | Cursor: the newest slot or scan done |
| `imerg_latency.jsonl`, `himawari_latency.jsonl` | the live ticks | One line per new slot: when it appeared and how late |
| `bmkg.json` | `bmkg tick` | Per run: first seen, areas done, areas missing, complete |
| `imerg_backfill.json`, `mergir_backfill.json` | backfills | Per month: done, slot counts, missing slots, timings |
| `mergir_last_frame.npz`, `himawari_last_frame.npz`, `himawari_index.npz` | MERGIR backfill, Himawari tick | Last frame for motion across runs; Himawari pixel-to-grid index |
| `health/*.json` | each job | Last run time and counts; the API's pipeline page shows them |
| `imerg_gaps/`, `mergir_gaps/`, `gauge_check/` | the gap and gauge commands | Reports per month or date range |
| `*.lock` | each job | `flock` locks; see [operations](operations.md#locks) |

## Disk Budget

`config/retention.yaml` sets each window and an 8 GB budget for `DATA_ROOT` without `tmp/`. `nowcast retention` deletes whole partitions past their window (files by modification time where a dataset has no time partitions). `nowcast disk` prints the size of each dataset and exits with code 1 above the budget.

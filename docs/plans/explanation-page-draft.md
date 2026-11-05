# How It Works: Page Draft

Draft text for the `#explain` view (plan: `docs/plans/explanation-page.md`). Written on 2026-11-21 against the live model `20261120T1329Z-a2f52f2`.

Rules for the build:
- Each section shows its main idea first. Supporting paragraphs follow. "More detail" blocks start closed.
- A value marked **live** comes from an existing endpoint when the page opens. The draft shows the value at 04:00 UTC on 2026-11-21 in brackets.
- A value marked **snapshot** comes from `web/explain-samples.json`. `experiments/08_explain_samples.py` writes that file from the real Parquet files. Feature rows are not stored anywhere, so the script rebuilds them with the live scorer's code (`model/score.py`, `live_features`). It then runs the live models on them again: all 1,542 probabilities equal the stored ones.
- Every other number names its source in the "Source" line under its block.
- Terms in *italics* link to the glossary at first use.

Change from the plan: "Follow one area" becomes its own section (6), because it runs through every step.

## Outline

1. **The Question.** Will it rain in my kab/kota in the next 1, 2 or 3 hours?
2. **The Answer in One Picture.** Satellites and BMKG, then numbers per area, then a model, then a probability, then the map.
3. **Data Sources.** Six sources. Each one has a job.
4. **From Grids to Kab/Kota.** Each satellite grid becomes one row per area.
5. **The Pipeline.** Bronze, silver and gold storage, fed by a batch path and a speed path.
6. **Follow One Area.** One kab/kota through every step, with real values.
7. **The Prediction.** Three models read 25 numbers per area and give a probability.
8. **How Good Is It.** Scores on years the model never saw, against simple baselines.
9. **The Live Loop.** One forecast cycle on a timeline.
10. **Limits and Honest Caveats.** What the system cannot see or prove.
11. **Glossary.**

---

## 1. The Question

**Main idea.** For each of the 514 kab/kota in Indonesia, the app gives the chance of rain in the next 1, 2 and 3 hours. A new forecast comes every 30 minutes.

"Rain" has one exact meaning here. The *area mean* rain rate must reach 0.5 mm/h in at least one half-hour of the window. A light shower over one corner of a large kabupaten can stay below that value.

A short-range forecast like this is a *nowcast*. It is hard for two reasons. Many rain clouds form in place and grow within an hour or two, so a forecast cannot follow a cloud that does not exist yet. Indonesia also has no open weather radar archive, so the system uses satellites only.

Over 10 years, a 1 hour window had rain in 20.2% of cases, a 2 hour window in 25.6% and a 3 hour window in 30.0%.

More detail:
- The stored rain value is rounded to 0.1 mm/h, so in practice an area mean just above 0.45 mm/h counts as rain.
- In the 2-day smoke test, the scores fell at about 08:00 WIB, when new storms formed instead of moving in.

Source: `config/settings.yaml` (`target.threshold_mm_h: 0.5`, `leads_h: [1, 2, 3]`); `reference/decisions.md` (decisions 2 and 6); `docs/progress/step-10-full-run.md` (positive rates); `docs/rain-logic-audit.md` (rounding note); `reference/model.md` (smoke test).

## 2. The Answer in One Picture

**Main idea.** The system turns satellite images into a few numbers per area, gives these numbers to a model, and colours the map with the model's probability. About 5 hours later the satellite rain estimate arrives, and each forecast gets a score.

Five boxes, one sentence each:
1. **Satellites and BMKG.** Himawari-9 sees the cloud tops every 10 minutes, NASA IMERG estimates the rain every 30 minutes, and BMKG publishes its own forecast twice a day.
2. **Numbers per area.** Each grid becomes a few numbers per kab/kota, such as the share of very cold cloud.
3. **Model.** Three *LightGBM* models, one for each *lead time*, read 25 numbers per area.
4. **Probability.** Each area gets one chance of rain for the next 1 hour, 2 hours and 3 hours.
5. **Map and score.** The app colours each area, and later compares each forecast with the rain that IMERG observed.

Source: `reference/data-sources.md` (cadences); model card `20261120T1329Z-a2f52f2` (25 model columns).

## 3. Data Sources

**Main idea.** Six sources feed the system. IMERG gives the rain truth, MERGIR and Himawari-9 give the clouds, BMKG is the forecast to compare with, rain gauges check IMERG, and two registries define the areas.

| Source | What it measures | Grid or points | How often, how late | Access and terms | Why we use it |
|---|---|---|---|---|---|
| NASA *IMERG Early* (GPM_3IMERGHHE V07) | Rain rate in mm/h, estimated from satellites | 0.1° cells (about 11 km), 480 x 180 = 86,400 cells over the Indonesia box | Every 30 min. Lands about 5 h after the slot ends (live median [297 min]) | Earthdata login and the GES DISC agreement | The rain truth for training and scoring, and a lagged input |
| NASA *GPM_MERGIR* | *Brightness temperature* of the cloud tops (infrared), in K | About 4 km, 1,319 x 495 = 652,905 cells | Hourly files with 2 half-hour frames. About 47 h late | Earthdata login | 10 years of cloud history for training |
| NOAA *Himawari-9* AHI Band 13 | The same infrared brightness temperature, live | 2 km, averaged onto the MERGIR grid | A scan every 10 min. Lands about 12 to 15 min after the scan starts (live median [13.1 min]) | Public AWS bucket, no login | Live clouds and cloud motion |
| *BMKG* forecast API | BMKG's official forecast: weather code, rain amount, cloud cover, wind | 514 points, one village per kab/kota | 2 runs a day (00 and 12 UTC seen), 3 days ahead in 3 h steps | No login. 60 requests per minute per IP. The app must show BMKG as the source | The official forecast to compare with. No past runs are published |
| GHCNh and METAR gauges | Rain yes or no at airports, from the weather report text | 212 GHCNh stations, 106 airports (27 reported in a 24 h check) | GHCNh: files lag by days. METAR: about every 30 min | No login | A check of IMERG at real stations |
| geoBoundaries and wilayah.id | Kab/kota polygons (2020) and Kemendagri codes | 519 polygons, 514 admin kab/kota (416 kabupaten, 98 kota) | Static | geoBoundaries: CC BY 3.0 IGO. wilayah.id: community site, at most 5 requests a second | The areas themselves |

More detail:
- IMERG Early arrives in pairs: both half-hours of an hour appear together. The :00 slot is about 307 min old after its end, the :30 slot about 278 min.
- MERGIR is the merged infrared that IMERG itself uses. Over Indonesia its data comes from Himawari, so the training clouds and the live clouds measure the same thing.
- IMERG Early has run as "V07C" with a new calibration since 1 March 2026. The archive switches on 2026-03-04. A move to version 08 is planned, and the model must be trained again then.
- BMKG's cache can serve an old run for about an hour after a new run appears. The archiver credits each response to the run its own body shows.

Source: `reference/data-sources.md`; `docs/experiments/00-source-probe.md` (IMERG 292 min after slot end, MERGIR 46.9 h, Himawari 12.4 min); `docs/progress/step-02.md`, `step-03.md`, `step-05.md`, `step-10-full-run.md`; live `/api/pipeline` → `latency_minutes`.

## 4. From Grids to Kab/Kota

**Main idea.** The system turns each satellite grid into one row per kab/kota. A grid cell counts in proportion to the share of the cell that lies inside the area.

The areas are 519 polygons from geoBoundaries. 514 are admin kab/kota. Five are lakes, a reservoir and a forest (Danau, Danau Toba, Hutan, Waduk Cirata, Wadung Kedungombo). The map draws them, but the model does not forecast them, because they have no Kemendagri code and no BMKG point.

The *membership table* lists, for each area, the cells it touches and a *weight* from 0 to 1. The weight is the share of that cell's area inside the polygon. The area mean is the sum of weight times value, divided by the sum of the weights. Kota Malang (110 km²) touches 4 IMERG cells, with weights 0.583, 0.261, 0.057 and 0.001 [snapshot].

A simpler rule, "use the cells whose centre is inside", leaves 19 areas with no cell at all, among them 9 of the 10 smallest. The weighted rule gives every one of the 514 areas a value. Areas touch 1 to 441 IMERG cells (median 32.5) and 5 to 2,961 MERGIR cells (median 169).

From the rain grid, each area gets the mean rate, the highest cell and the *wet share* (weighted share of cells at 0.1 mm/h or more). From the cloud grid, each area gets the coldest cell, the mean brightness temperature, the *cold cloud share* (colder than 233 K) and three motion values (section 7).

Interactive: the selected area drawn over its IMERG cells. A slider picks a half-hour that has a rain image. Each cell shows its rain and its weight. Below, the page computes the weighted mean and shows it next to the stored silver value.

More detail:
- The weights use the full-resolution polygons. The map shows a simplified copy, so a drawn edge can differ a little from the edge used in the calculation.
- The weighted cell areas differ from the polygon areas by 0.44% to 0.45% for every area. The cell areas use a sphere and the polygon areas use the WGS84 ellipsoid. A mean does not change, because the same factor is in the top and the bottom.
- The rain image stores each cell as a whole number of 0.1 mm/h up to 25.5 mm/h, and a missing cell as 0. A cell above 25.5 mm/h or a missing cell can make the page value differ a little from the stored one.

Source: `docs/experiments/01-membership-methods.md`; `docs/progress/step-01.md`; `reference/decisions.md` (decision 27); `src/nowcast/gold/tiles.py` (rain image encoding); snapshot `ref_imerg_membership`.

## 5. The Pipeline

**Main idea.** Data moves through three storage zones. *Bronze* keeps raw files for hours to days, *silver* keeps small per-area tables for 10 years, and *gold* keeps the forecasts and scores that the app reads. A *batch layer* built 10 years of history once, and a *speed layer* adds new data as it lands.

### Stages (Clickable Diagram)

Each stage opens a panel with: what it does, when it runs, inputs, outputs and the real data shape (columns, types and one real row).

| Stage | What it does | When it runs | Output |
|---|---|---|---|
| Sources | NASA OPeNDAP, NOAA S3, BMKG API, gauge files | | |
| Bronze | Downloads raw files as they are | IMERG: check every 10 min. Himawari: every scan. BMKG: check every 15 min. Backfills: once | `bronze/imerg`, `bronze/himawari`, `bronze/bmkg`, `bronze/gauges` |
| Area registry | Polygons, codes, cell weights, neighbours, BMKG points | Once | `ref/*.parquet` |
| Silver | One row per area and half-hour (or per BMKG step) | After each new file | `silver/rain_kabkota`, `cloud_kabkota`, `bmkg_forecast`, `gauge_obs` |
| Features | 27 columns per area and issue time, from data known at that time | Training: once. Live: each issue | In memory live. The training table is deleted after training |
| Model | Three LightGBM models, one per lead | Training: once. Live: each issue | `models/<version>/` with a model card |
| Gold | Forecasts, verification, daily scores, map frames, climatology, images | Each issue, and when IMERG covers a window | `gold/predictions`, `verification`, `scores_daily`, `map_frames`, `climatology`, `tiles` |
| API | FastAPI with DuckDB reads gold and silver Parquet in place | On each request | JSON, images, GeoJSON |
| Web app | The map and the other pages | In the browser, checks for new data every 60 s | |

### Data Shape at Each Step [Snapshot, Kota Malang, Issue 2026-11-21 03:30 UTC]

Bronze, IMERG: one file per half-hour, `GPM_3IMERGHHE.07:3B-HHR-E.MS.MRG.3IMERG.20261120-S213000-E215959.1290.V07C.HDF5.nc4`, 58,198 bytes. Variable `/Grid/precipitation`, float32, `[time 1][lon 480][lat 180]`, mm/hr, fill -9999.9. Kota Malang's 4 cells: 0.0 mm/h each.

Bronze, MERGIR: one file per hour, `Tb` int16 in K, shape `[time 2][lat 495][lon 1319]` after the crop, about 0.83 to 0.94 MB. Deleted after each month is processed, so no sample is on disk.

Bronze, Himawari-9: three segment files per scan, `HS_H09_20261121_0340_B13_FLDK_R20_S0610.DAT.bz2` (3,217,097 bytes), segments 05 to 07. Deleted after 6 hours.

Bronze, BMKG: gzip JSON lines, one line per point: `area_id`, `adm4`, `fetched_at`, `http_status`, `body`. One step of the 2026-11-20 12 UTC run for Kota Malang: `datetime` 2026-11-20T14:00:00Z, `weather` 2 (Cerah Berawan, Partly Cloudy), `t` °C, `hu` %, `tcc` %, `tp`, `ws`, `wd`. The response has 17 steps.

Silver, `rain_kabkota` (97,701,120 rows in 131 monthly files, 166 MB):

| Column | Type | Sample |
|---|---|---|
| slot | timestamp[us, tz=UTC] | 2026-11-20T21:30:00Z |
| area_id | int16 | 219 |
| rain_mean_x10 | int16 | 0 |
| rain_max_x10 | int16 | 0 |
| wet_pct | uint8 | 0 |
| product_version | dictionary&lt;string&gt; | V07C |

Silver, `cloud_kabkota` (97,703,176 rows, 407 MB): `slot` 2026-11-21T03:30:00Z, `area_id` 219, `source` 2 (Himawari-9), `tb_min` 277 K, `tb_mean_x10` 2875 (287.5 K), `cold_pct` 0, `adv1_pct` 0, `adv2_pct` 0, `adv3_pct` 0, `flow_kmh` null (no cold cloud to track).

Silver, `bmkg_forecast` (22,427 rows): run `analysis_time` 2026-11-20T12:00:00Z, `first_seen` 13:28:09Z, `valid_time` 2026-11-21T05:00:00Z, `lead_h` 17, `weather` 63 (Hujan Sedang, Moderate Rain), `tp_x10` 62, `tcc_pct` 85, `t` 24, `hu_pct` 89, `ws_x10` 185, `wd_deg` 135.

Silver, `gauge_obs` (46,156 rows): `station_id` WAQQ, `source` metar, `obs_time` 2026-11-19T23:30:00Z, `area_id` 263, `wx` "-RA", `is_rain` true, `precip_mm` null.

Features (27 columns, the models use 25: `clim_p1` to `clim_p3` become one `clim_p` per lead): `cold_pct` 0, `tb_min` 277, `tb_mean` 287.5, `cold_trend_1h` 0, `nb_cold_pct` 0, `adv1_pct` 0, `adv2_pct` 0, `adv3_pct` 0, `adv_max_pct` 0, `flow_kmh` null, `rain_lag` 0, `wet_lag` 0, `rainmax_lag` 0, `rain_sum_3h` 0, `rain_sum_6h` 8.4, `rain_sum_24h` 10.5, `nb_rain_lag` 0, `hour_sin` 0.383, `hour_cos` -0.924, `doy_sin` -0.652, `doy_cos` 0.759, `area_km2` 109.95, `island` Jawa, `clim_wet_pct` 7.86, `clim_p1` 7, `clim_p2` 16, `clim_p3` 36.

Gold, `predictions` (3 rows per area and issue): `issued_at` 2026-11-21T03:44:16Z, `t` 03:30:00Z, `lead_h` 1, 2, 3, `prob_pct` 1, 7, 38, `model_version`, `cloud_slot` 03:30Z, `imerg_slot` 2026-11-20T21:30Z.

Gold, `verification`: the prediction row plus `verified_at`, `observed` (false), `clim_pct` 6, `persist_pct` 100, `motion_pct` 0, `bmkg_rain` false, `bmkg_analysis_time`.

Gold, `scores_daily`: `day`, `scope` (national, island, province), `scope_id`, `lead_h`, `source` (model, clim, persist, motion, bmkg), `n`, `brier`, `bss`, `csi`, `pod`, `far`, `base_rate`.

Gold, `map_frames` (partitioned by day): `slot`, `area_id`, `layer` (prob1, prob2, prob3, obs, cold, bmkg, disagree), `value` int16. Kota Malang at 03:30Z: prob1 1, prob2 7, prob3 38, cold 0, bmkg 100, disagree 1.

Gold, `climatology` (514 areas x 12 months x 24 local hours = 148,032 rows): Kota Malang, November, 10:00 WIB: `wet_pct` 7.9, `p1` 7, `p2` 16, `p3` 36, `n` 480.

API: `/api/areas/219` [live] returns the area row, 48 h of observed rain, forecasts, BMKG steps and cloud, plus scores and climatology.

### The Big Data View

The course describes big data with five Vs (Pertemuan 1). This system meets each one:
- **Volume.** The system has processed [16.4 billion] IMERG values and [124.0 billion] MERGIR values [live]. They downloaded 88.5 GB and deleted each file after use. Silver keeps [577 MB] [live] of per-area summaries.
- **Velocity.** A new Himawari scan comes every 10 minutes (about 9 MB for the three segments), a new IMERG slot every 30 minutes, and a new BMKG run twice a day.
- **Variety.** HDF5 and NetCDF4 grids, Himawari's binary format, JSON, pipe-separated text, METAR report text and GeoJSON.
- **Veracity.** IMERG is checked against airport reports. Each backfill month gets a gap report: 0 missing slots in 130 months. BMKG rows that fail a check go to a *dead-letter folder*.
- **Value.** A rain chance and an alert level per kab/kota, compared with BMKG's own forecast.

The design follows the *Lambda architecture* (Pertemuan 2). The batch layer is the 10-year backfill, the *Spark* feature build and the training. The speed layer is the ticks that react to each new IMERG slot, Himawari scan and BMKG run. The serving layer is gold Parquet, read by DuckDB behind FastAPI. A Kappa design (one stream path) does not fit, because the 10 years of history come from MERGIR, a different product from the live Himawari stream.

Storage follows the zones of Pertemuan 4. Tables are *Parquet* files with *ZSTD* compression. Small integer codes keep them small: `*_x10` stores a value times 10 as int16, and `*_pct` stores a share from 0 to 100 as uint8. Time tables are split into monthly *partitions* (`year=YYYY/month=MM`). The *retention* job deletes whole partitions older than their window, and a disk check fails above 8 GB.

More detail:
- Batch run times: IMERG backfill 3 h 50 min (189,936 slots), MERGIR backfill 6 h 22 min (189,936 frames), feature build 539 s (22,143,120 rows), training 1,910 s, peak memory 4.6 GB.
- Spark runs in local mode with 3 worker threads on one machine (8 cores, 7.7 GB RAM). The same code can run on a cluster.
- *Dagster* holds the job definitions: *sensors* for BMKG (every 15 min), IMERG (10 min) and Himawari (5 min), and schedules for METAR (hourly), retention (daily) and clean-up (weekly). On the dev machine the live jobs run as shell loops for now, because installing the timers or a Dagster daemon waits for juan's decision.
- Replication: a nightly copy of `bronze/bmkg`, silver and the models to a second disk is planned for the server move. It is not built yet.
- Retention windows per dataset [live from `/api/pipeline` → `storage`].

Source: `docs/progress/step-10-full-run.md`; `docs/progress/step-05.md`; `reference/HANDOFF.md` (5V and Lambda mapping); `reference/infra.md` (replication plan); `reference/course/PPT Big Data Pertemuan 1.pdf` slide 8, `Pertemuan 2.pdf` slides 16 to 18, `Pertemuan 4.pdf` slides 6 and 15 to 16; `src/nowcast/features/spark.py`; `orchestration/definitions.py`; `config/retention.yaml`; snapshot (all rows above); live `/api/pipeline` → `processed_vs_stored`.

## 6. Follow One Area

**Main idea.** Pick one kab/kota. The page follows it through every step for the newest forecast, with the real value at each step. Kota Malang is the default.

Steps:
1. **Grid cells.** The cells the area touches, with weights and the rain of the IMERG slot that the forecast used (live rain image).
2. **Silver rows.** The area's rain at that slot and its cloud at the issue time (live `/api/areas/{id}`).
3. **Feature row.** The 27 numbers the models read [snapshot, labelled with its issue time, because feature rows are not stored].
4. **Three probabilities.** The newest +1, +2 and +3 h chances (live).
5. **Map colour.** The colour class the map uses for the +3 h chance.
6. **Later check.** When IMERG covers a window, the page shows whether it rained, with the same rule as verification.

Source: live `/api/meta`, `/api/areas/{id}`, `/tiles/rain/{slot}.png`; snapshot `features`.

## 7. The Prediction

**Main idea.** Three LightGBM models, one per lead, read 25 numbers about an area at the *issue time* and give a probability. They learned from 8 years of history and were tested on later years they never saw.

### What the Model Knows at Issue Time

The model must see exactly what the live system sees. This is the main rule of the design.

| Input | Newest data at issue time t | Why |
|---|---|---|
| Clouds | The scan at t | Himawari lands about 12 to 15 min after the scan starts |
| IMERG rain | The slot that starts 360 min before t | IMERG lands about 5 h late. 360 min covers the 30 min slot plus the delay |
| BMKG | Not an input | No past runs exist, so 10-year training cannot include it. It is a baseline |
| Climatology | Training years only (2016 to 2023) | Later years must stay unseen |

### Features in Groups

- **Cloud now:** `cold_pct` (share colder than 233 K), `tb_min` (coldest cell), `tb_mean`, `cold_trend_1h` (change of `cold_pct` in the last hour), `nb_cold_pct` (mean `cold_pct` of the neighbours).
- **Cloud motion:** *optical flow* finds how the cold cloud moved in the last half-hour. The system moves today's cold cloud forward by 1, 2 and 3 hours: `adv1_pct`, `adv2_pct`, `adv3_pct` are the shares of the area that the moved cloud covers. Also `adv_max_pct` and `flow_kmh` (mean speed of the cold cloud).
- **Recent rain (6 h old):** `rain_lag`, `wet_lag`, `rainmax_lag` at the newest usable slot, rain totals in mm over 3, 6 and 24 h up to that slot, and `nb_rain_lag` (neighbours).
- **Time:** the local hour and the day of the year, each as a sine and a cosine, so 23:30 sits next to 00:00.
- **Place:** `area_km2` and `island`.
- **Climatology:** `clim_wet_pct` and `clim_p`, the share of past windows with rain for this area, month and local hour.

Neighbours are areas with a shared border, plus the 6 nearest areas for the 30 areas with no land border.

### How the Model Learns

LightGBM builds many small decision trees, one after another. Each new tree corrects part of the error that the trees before it left. Each model here has 2,000 trees of at most 15 leaves, learning rate 0.03.

The data is split by time, never at random, because two half-hours next to each other are almost the same row:

| Set | Period | Rows per lead | Use |
|---|---|---|---|
| Train | 2016-01 to 2023-12, 25% of the days (760 of 2,922) | 9,375,360 | Fit the trees |
| Validation | 2024 | 4,514,976 | Stop the training, choose thresholds |
| Test | 2025 | 4,502,640 | Headline scores |
| Test after the IMERG change | 2026-03 to 2026-10 | 3,022,320 | Shows drift after the new calibration |

Training uses MERGIR clouds. Live scoring uses Himawari-9 clouds on the same grid, because MERGIR is about 47 hours late.

More detail:
- Most of the skill comes from the clouds now. Share of the total gain at +1 h: `cold_pct` 46%, `tb_mean` 26%, `tb_min` 14%. At +3 h the motion values carry 17% (`adv_max_pct` 9%, `adv1_pct` 7%).
- In a one-month test, removing the cloud-now group cut the Brier skill score by 0.066, 0.058 and 0.053 at +1, +2 and +3 h. Removing the recent-rain group cut it by 0.003 or less.
- The motion settings (OpenCV DIS "fast", 33 km smoothing) won a test of 8 settings over one week. They cut the error of the moved cold cloud share by 14.4%, 8.7% and 5.1% against "no motion" at +1, +2 and +3 h.
- All three models reached the 2,000-tree cap. A test with 8,000 trees at +1 h added only 0.004 to the validation Brier skill score, so the cap stays.
- The 360 min IMERG lag cost 0.003 to 0.005 Brier skill score against a 300 min lag in the one-month test. 300 min would use a slot that is not yet available live.

Source: `reference/model.md`; `docs/decision-log.md` (lag); `src/nowcast/features/build.py`; `config/settings.yaml` (`model.lgbm`, `model.split`); model card (`rows_per_split`, `feature_importance`); `docs/progress/step-10-full-run.md`; `docs/experiments/03-flow-settings.md`; `docs/experiments/04-ablation.md`; `docs/progress/step-01.md` (neighbours).

## 8. How Good Is It

**Main idea.** On 2025, a year the model never saw, its *Brier score* is 48%, 46% and 42% better than *climatology* for +1, +2 and +3 h. The two simple rules, *persistence* and motion only, score worse than climatology.

A probability forecast needs probability scores. Accuracy alone does not work: most windows are dry, so "always dry" gets a high accuracy.
- The Brier score is the mean squared error of the probability (0 is perfect).
- The *Brier skill score* (BSS) compares it with climatology: 0 means "as good as the long-term rate", 1 means perfect, and below 0 means worse than climatology.
- *AUC* measures how well the probability ranks wet windows above dry ones (0.5 is chance, 1 is perfect).
- *CSI* counts hits divided by hits, misses and false alarms, at a yes/no cut of 50%. *POD* is the share of wet windows that the forecast caught. *FAR* is the share of "rain" forecasts that stayed dry.

Test 2025 [live from `/api/model` → card]:

| Lead | BSS | AUC | CSI | POD | FAR | Wet share |
|---|---|---|---|---|---|---|
| +1 h | 0.475 | 0.932 | 0.567 | 0.679 | 0.224 | 22.7% |
| +2 h | 0.456 | 0.920 | 0.585 | 0.696 | 0.214 | 29.0% |
| +3 h | 0.423 | 0.905 | 0.590 | 0.704 | 0.215 | 34.0% |

Baselines on the same rows, BSS at +1, +2, +3 h: climatology 0 by definition, persistence (the 6 h old IMERG rain) -0.758, -0.706, -0.728, motion only -0.244, -0.124, -0.103.

Interactive: a lead switch (+1, +2, +3 h) for the *reliability* chart (test 2025, 10 bins) and the top features by gain.

After the IMERG calibration change (2026-03 to 2026-10), the BSS is 0.438, 0.426 and 0.406. These months are drier (wet share 12.1% at +1 h), and the 2016 to 2023 climatology does not know the drier V07C estimates.

BMKG cannot be scored on history, because BMKG publishes no past runs. The live comparison started with the archive on 2026-11-20 [live from `/api/scores`]. The scores for 2026-11-20 include rows from the retired sample model up to 13:30 UTC. On a 48 h replay (2026-11-18 02:00 to 11-19 23:00 UTC) with Himawari input, the live model scored BSS 0.388, 0.340 and 0.284. That is below the test year, so the live record needs more days before it says much.

More detail:
- On 2025 the reliability points lie on the diagonal for every lead. For example, +1 h forecasts near 35% rained in 33% of cases, and forecasts near 85% in 86%.
- CSI favours frequent events, so it is lower on drier data. That does not mean the forecast got worse.
- No calibration step is applied: validation reliability was within tolerance for every lead.

Source: model card `20261120T1329Z-a2f52f2` (`leads.*.metrics`, `leads.*.reliability.test`, `calibration_used`); `docs/progress/step-10-full-run.md`; `docs/rain-logic-audit.md` (replay scores and the mixed 2026-11-20 scores); `reference/decisions.md` (decision 13).

## 9. The Live Loop

**Main idea.** Every 30 minutes a Himawari scan starts a forecast cycle. The forecast is ready about 15 minutes after the scan time, and its check comes about 5 hours after its window ends.

One cycle, for issue time t:
1. **t.** Himawari-9 starts a full-disk scan.
2. **About t + 13 min.** The three Band 13 segments over Indonesia land [live median: 13.1 min].
3. **Next minute.** The scorer builds the 514 feature rows and writes 1,542 probabilities (514 areas x 3 leads) [live: last delay 14.3 min].
4. **Within about 2 min.** The map frames are built again, and the web app sees the new stamp within 60 s.
5. **About 5 h after the window ends.** IMERG covers the window. Verification marks each forecast as wet or dry, and the daily scores update.

At issue time t, the newest rain the model knows is 6 hours old. The clouds are fresh. So the clouds carry the forecast, and the IMERG rain is mostly a check afterwards.

Interactive: a slider over the issue times of the last 24 hours. For the chosen t, a timeline shows the IMERG cut-off (t minus 360 min), the scan at t, the BMKG run in force, the three windows, the area's observed rain, and, where IMERG has arrived, whether each window turned wet.

More detail:
- An issue happens only for scans at :00 and :30 that also have a scan 1 hour earlier (for `cold_trend_1h`).
- The BMKG run in force is the newest run first seen at or before t. Its `first_seen` is accurate to about 1 hour, because of BMKG's cache.
- Training used one issue per hour, to keep the table small. Live, every half-hour is an issue.

Source: `src/nowcast/model/score.py`; `docs/progress/step-10-full-run.md` (scan 14:00 UTC scored 13.41 min later); `docs/progress/step-09-render-fixes.md` (frames about every 2 min); `docs/progress/step-09-ui-polish.md` (60 s check); `reference/data-sources.md` (BMKG cache); live `/api/pipeline` → `latency_minutes`, `health.score`.

## 10. Limits and Honest Caveats

**Main idea.** The truth itself is a satellite estimate, the newest rain is 6 hours old, and an area mean can hide a local storm. The live record is still short.

- **IMERG is an estimate, not a gauge.** It misses some heavy rain over mountains. The same estimate is the training truth and the score truth, so the scores measure agreement with IMERG. Against airport reports (January 2025, 53 stations), an hour counts as wet for IMERG when the airport's area mean reaches 0.5 mm/h in either half-hour. IMERG caught 71% of the reported rain hours, and 65% of its wet hours had no rain in the report (CSI 0.31). A point report and an area mean measure different things, so part of this gap is expected.
- **Latency.** IMERG lands about 5 hours late, so the model never sees the last 6 hours of rain.
- **New storms.** A satellite sees a cloud only after it forms. The smoke test lost skill at about 08:00 WIB, when new storms formed in place.
- **The area mean hides local storms.** A storm over one cell of a large kabupaten (up to 441 cells) barely moves the mean. A small kota such as Kota Malang has only 4 cells.
- **Two cloud sources.** Training used MERGIR and live uses Himawari-9. The check of their difference waits for MERGIR to cover the live days (about 47 h late).
- **BMKG comparison.** BMKG is read at one village per kab/kota. The time a 3 h BMKG step covers is assumed centred on its time stamp, and is not yet tested. Three areas in Papua Barat Daya (Raja Ampat, Sorong Selatan, Maybrat) use a point from another area inside their polygon, because BMKG rejects their own codes.
- **Separate models per lead.** The +2 h window contains the +1 h window, so p1 must not be above p2. In 1.4% of live cases it is (by at most 6 points).
- **One machine.** The live jobs run as shell loops on the dev machine, so a restart stops them until someone starts them again.
- **A future product change.** IMERG Early will move to version 08, and the model must be trained again then.

Source: `reference/data-sources.md`; `src/nowcast/gold/gauge_check.py`; `data/state/gauge_check/2025-01-01_2025-01-31.json` (`imerg_mean`: POD 0.711, FAR 0.645, CSI 0.310); `reference/model.md`; `docs/progress/step-01.md`, `step-04.md`, `step-05.md`; `config/settings.yaml` (`step_alignment: centred`, untested); `docs/rain-logic-audit.md` (finding 8).

## 11. Glossary

Short form for the hover text, then the full entry.

| Term | Short definition | Full entry |
|---|---|---|
| Nowcast | A forecast for the next few hours. | A short-range forecast made mostly from what is visible now. Here: the next 1, 2 and 3 hours. |
| Lead time | How far ahead a forecast looks. | The length of the forecast window from the issue time: 1, 2 or 3 hours here. Each lead has its own model. |
| Issue time | The time a forecast is made for. | The half-hour slot t at which a forecast starts. All windows begin at t. The forecast is written about 15 minutes later. |
| Slot | One half-hour. | A half-hour period named by its start time in UTC, for example 03:30 for 03:30 to 04:00. |
| Kab/kota | A regency or city. | Kabupaten (regency) or kota (city), the second level of Indonesian government. Indonesia has 514. |
| Area mean | The average over one kab/kota. | The weighted mean of all grid cells that touch the area, weighted by the share of each cell inside it. |
| IMERG Early | NASA's fast satellite rain estimate. | The "Early" run of NASA's IMERG product (GPM_3IMERGHHE): rain every 30 minutes on a 0.1° grid, about 5 hours after the fact. |
| MERGIR | NASA's archive of infrared cloud images. | GPM_MERGIR: merged geostationary infrared at about 4 km every 30 minutes, back to 1998. About 47 hours late. |
| Himawari Band 13 | Japan's weather satellite, infrared channel. | The infrared channel of the Advanced Himawari Imager on Himawari-9. It gives the brightness temperature of the cloud tops every 10 minutes. |
| Brightness temperature (Tb) | The temperature a satellite sees. | The temperature that matches the infrared energy the satellite receives. Cold tops (high clouds) give low values. |
| Cold cloud share | Share of the area under very cold cloud tops. | The weighted share of cells colder than 233 K (about -40 °C). It is the model's strongest input. |
| Advection | Moving a field with the wind. | Here: moving today's cold cloud along its measured motion to guess where it will be 1, 2 and 3 hours later. |
| Optical flow | Motion found by comparing two images. | A method that finds how each part of an image moved between two frames. The system uses OpenCV DIS. |
| Climatology | The usual rain rate for a place and time. | The share of past windows with rain for this area, month and local hour, from 2016 to 2023. It is the baseline for the Brier skill score. |
| Persistence | "Same as last seen". | A baseline that forecasts rain if the newest usable IMERG slot (6 hours old) had rain. |
| Bronze | Raw files, kept briefly. | The storage zone for files as they were downloaded. Windows from 6 hours to 12 months. |
| Silver | Clean per-area tables. | The storage zone for checked tables with one row per area and half-hour. Kept for 10 years. |
| Gold | Tables the app reads. | The storage zone for forecasts, verification, scores and map frames. |
| Batch layer | Processing of the full history. | The part of a Lambda design that processes all history at once: here the 10-year backfills, features and training. |
| Speed layer | Processing of new data as it lands. | The part of a Lambda design that handles each new file in minutes: here the IMERG, Himawari and BMKG ticks and the live scorer. |
| Lambda architecture | Batch path plus speed path. | A design with one batch path for history and one stream path for new data, joined in a serving layer. |
| Sensor | A job that waits for new data. | In Dagster, a check that starts a job when new data appears, for example a new IMERG granule. |
| Dagster | The job orchestrator. | A tool that defines the jobs, their inputs and outputs, and when they run. |
| Spark | A distributed data engine. | Apache Spark. It builds the feature table with window functions. Here it runs in local mode on one machine. |
| Partition | One folder of a table. | A part of a table stored in its own folder, for example `year=2026/month=11`. Old data is deleted folder by folder. |
| Retention | How long data is kept. | The window after which the retention job deletes a partition. Each dataset has one. |
| Dead-letter folder | Where bad records go. | A folder for records that fail validation, kept for 3 months so they can be checked and processed again. |
| Parquet | A columnar file format. | A compressed file format that stores each column together, so a query reads only the columns it needs. |
| ZSTD | A compression method. | Zstandard, the compression used in every Parquet file here. |
| Feature | One input number of the model. | One column the model reads, for example `cold_pct`. Each area and issue time has 25. |
| LightGBM | A fast tree-based learning library. | A library that builds gradient-boosted decision trees. Each new tree corrects part of the error of the trees before it. |
| Probability of rain | The chance of rain in a window. | The model's chance, from 0 to 100%, that the area mean reaches 0.5 mm/h in at least one half-hour of the window. |
| Base rate | How often it rained. | The share of windows in a data set that had rain. |
| Brier score | Error of a probability forecast. | The mean of (probability minus outcome) squared, with outcome 1 for rain and 0 for dry. 0 is perfect. |
| Brier skill score (BSS) | Brier score compared with climatology. | 1 minus the model's Brier score divided by climatology's. 0 is as good as climatology, 1 is perfect. |
| AUC | How well wet ranks above dry. | Area under the ROC curve: the chance that a random wet window gets a higher probability than a random dry one. |
| CSI | Hits over all hits and errors. | Critical success index: hits / (hits + misses + false alarms), for a yes/no cut. |
| POD | Share of rain events caught. | Probability of detection: hits / (hits + misses). |
| FAR | Share of false rain forecasts. | False alarm ratio: false alarms / (hits + false alarms). |
| Reliability | Do the probabilities mean what they say? | A forecast is reliable when, of all windows forecast at 30%, about 30% rain. The reliability chart shows this in 10 bins. |
| Hindcast | A forecast made later for a past time. | A forecast run for a past issue time with only the data that was available then. Used to fill the map before live scoring started. |
| Verification | Checking a forecast against the truth. | Comparing each forecast with IMERG once IMERG covers its whole window. |
| BMKG | Indonesia's weather agency. | Badan Meteorologi, Klimatologi, dan Geofisika. Its public API gives forecasts in 3 hour steps. |
| ENSO | El Niño and La Niña. | El Niño Southern Oscillation, a cycle of the tropical Pacific Ocean. The History page tags El Niño and La Niña years from NOAA's Relative Oceanic Niño Index. |
| Dekad | Ten days. | A period of 10 days, used for wet-season timing on the History page. |
| WIB, WITA, WIT | Indonesia's three time zones. | Western (UTC+7), Central (UTC+8) and Eastern (UTC+9) Indonesia Time. |

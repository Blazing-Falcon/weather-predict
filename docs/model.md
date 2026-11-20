# Model

## The Forecast

For each of the 514 admin kab/kota and each issue time `t` (every 30 min), the model gives the probability that the area-mean IMERG rain reaches 0.5 mm/h or more in at least one 30-min slot of the next 1, 2 and 3 hours (`target.threshold_mm_h`, `target.leads_h`). The target for lead `n` exists only when all `2n` slots of that window are in the archive.

## Features

Features are built with Spark in `features/rows.py` (`feature_rows`), the same code for training and live scoring.

| Group | Features | Source |
|---|---|---|
| Current cloud | `cold_pct`, `tb_min`, `tb_mean`, `cold_trend_1h` (change in cold share over 1 h), `nb_cold_pct` (mean of the neighbours) | MERGIR in training, Himawari live, at slot `t` |
| Cloud motion | `adv1_pct`, `adv2_pct`, `adv3_pct` (cold cloud moved 1, 2, 3 h ahead with optical flow), `adv_max_pct`, `flow_kmh` | Same |
| Lagged rain | `rain_lag`, `wet_lag`, `rainmax_lag`, `rain_sum_3h`, `rain_sum_6h`, `rain_sum_24h` (mm), `nb_rain_lag` | IMERG at the newest slot that is available live |
| Time and place | `hour_sin`, `hour_cos` (local time), `doy_sin`, `doy_cos`, `area_km2`, `island` | Registry and clock |
| Climatology | `clim_wet_pct`, `clim_p` (the event probability for this lead) | `gold/climatology`, per area, local month and hour, from 2016 to 2023 |

IMERG Early arrives late, so features use the newest slot that starts at least `model.imerg_lag_minutes` (360) before `t`. The lag covers the 30-min slot plus the measured delay after the slot ends (about 278 to 307 min).

## Training

`nowcast features build --start ... --end ...` writes the feature table to `tmp/features/`; `nowcast model train` reads it and trains one LightGBM classifier per lead.

| Setting | Value |
|---|---|
| Train | 2016-01-01 to 2023-12-31, 25% of the days of each month (seed 42) |
| Validation | 2024 (early stopping after 100 rounds without gain, cutoff tuning, calibration check) |
| Test | 2025, and 2026-03 to 2026-10 (after IMERG Early V07C, which starts on 2026-03-04) |
| Issues | Every 60 min in training (`model.issue_every_minutes`) |
| LightGBM | 2000 trees at most, learning rate 0.03, 15 leaves, 60 rows per leaf, row and column sampling 0.8, deterministic |
| Calibration | Isotonic regression on validation when a reliability bin with 1% of the rows or more is off by more than 0.05 |

Each model folder holds a `card.json` with the periods, features, parameters, metrics per split and source, the best-CSI cutoffs, feature importance and reliability. The version is `<UTC time>-<commit>`; the commit comes from `NOWCAST_GIT_COMMIT`, then git, then `unknown`.

## Baselines

| Source | Rule |
|---|---|
| `clim` | The climatology event probability for the area, local month and hour |
| `persist` | Rain if the lagged IMERG area mean is 0.5 mm/h or more |
| `motion` | Rain if `adv_max_pct` reaches a cutoff tuned for CSI on validation |
| `bmkg` | Rain if the newest BMKG run seen at issue time has a step with a rain code (60 to 99) over the window; no value if no step covers it. Steps are 3 h long and centred on their valid time (`bmkg.step_alignment`). Live only, because the BMKG archive starts in 2026-11. |

## Results

Live model `20261120T1329Z-a2f52f2` (from its card). Brier skill score (BSS) is against climatology. AUC is the area under the ROC curve.

| Split | Lead | BSS model | AUC model | AUC clim | BSS persist | BSS motion |
|---|---:|---:|---:|---:|---:|---:|
| Test 2025 | +1 h | 0.475 | 0.932 | 0.709 | -0.758 | -0.244 |
| Test 2025 | +2 h | 0.456 | 0.920 | 0.707 | -0.706 | -0.124 |
| Test 2025 | +3 h | 0.423 | 0.905 | 0.707 | -0.728 | -0.103 |
| Test 2026 | +1 h | 0.438 | 0.939 | 0.758 | -0.695 | -0.207 |
| Test 2026 | +2 h | 0.426 | 0.930 | 0.757 | -0.583 | -0.101 |
| Test 2026 | +3 h | 0.406 | 0.921 | 0.756 | -0.555 | -0.087 |

Every lead stopped at the 2000-tree cap, so more trees may help. Calibration was not needed.

## Live Scoring and Verification

- `nowcast live score` takes each new Himawari model slot of the last 3 hours (with the slot 1 hour earlier present for the trend), builds features, and writes `gold/predictions`. It skips a slot when IMERG truth for its window is already in silver, so no forecast is issued after the fact. If the lagged IMERG slot is missing, the rain features are empty and the model still runs.
- `nowcast live verify` adds a verification row when every IMERG slot of the window is in silver: the observed event and all baselines.
- `nowcast live daily` writes the scores of one UTC issue date for the nation, each island and each province. Model and climatology use the card's best-CSI cutoffs; persistence, motion and BMKG are 0 or 1.
- `nowcast live hindcast` scores past slots into `gold/hindcast`; the map uses them only for slots without a live forecast.

## Promoting a Model

Train, read the new card, then set `model.live_version` in `config/settings.yaml` to the new folder name. The scorer reads the setting on its next run. Restart the API, because it caches the settings.

## Choices That Still Hold

| Choice | Evidence |
|---|---|
| Area-weighted cell overlap for zonal means | The only tested method that gave every one of the 514 areas cells; area error below 0.45% for each area |
| Optical flow: DIS preset `fast`, 33 km smoothing | Mean area error of the advected cold share 13.021 to 12.699 percentage points (2.47% lower) than `medium`/66 km, and about 3 times faster |
| IMERG lag 360 min from the slot start | The slot length plus the live delay after the slot ends; a shorter lag uses a slot that does not exist yet at issue time |
| Climatology as the event probability per lead | The baseline must predict the same event as the model |
| Current cloud carries most of the skill | Removing the current-cloud features cut BSS by 0.0660, 0.0575 and 0.0532 at +1, +2, +3 h in a January 2025 test |

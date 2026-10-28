# Rain Logic Audit: Observed Rain, Predicted Rain and the Map

Date: 2026-11-20, data as of 19:20 UTC. Live model `20261120T1329Z-a2f52f2` (since the 14:00 UTC slot). Method: Codex read-only review (`gpt-6.2-astra`), then each claim checked in code and with DuckDB on `data/` (read-only). Only verified findings are kept. Severity: high (wrong or lost data now), medium (wrong numbers or misleading display), low (small or latent), note.

## Summary

The core chain is correct. IMERG crop, silver area mean and the map `obs` layer agree exactly, and verification compares every prediction with the right window exactly once (0 mismatches in 31,354 rows). The problems are in the edges: the IMERG live cursor loses late granules for good, the live scorer can pick up an unfinished or unreviewed model, verification freezes missing climatology, the persistence CSI is really "always rain", and three map displays compare or label windows wrongly.

## Findings

### 1. High: IMERG Live Cursor Skips Late Granules for Good

`sources/imerg.py:404-406` starts each tick at `last_slot + 1 s`. A granule that CMR indexes after a newer one is never fetched. Evidence: silver has no 2026-11-20 11:00 slot. The 16:39 tick returned only 11:30 (`imerg_tick_loop.log:172-173`, `new_slots=1`). CMR now lists 11:00 (updated 16:26:05Z). Effect: 4,626 live prediction keys (5 issue times, 08:30 to 11:00) can never be verified, and the 17:00 issue (`imerg_slot` 11:00, 1,542 rows) ran with all seven rain features set to NaN (`model/score.py:102`). Fix: each tick also looks back (for example 12 h) for slots missing in silver. `nowcast imerg fill` can recover 11:00 now.

### 2. High: Live Scorer Picks the Lexically Newest Model Directory

`model/score.py:107` uses `max(path.parent.name for path in models.glob("*/card.json"))`. Two consequences, both verified in code and on disk:

- `train.py:235-240` writes `card.json` into a `TemporaryDirectory` (now `data/models/tmpy67v2_55`, created 18:42 by the 8000-tree retrain) and renames it later. `"tmp..." > "2026..."`, so a score run in that window loads the temporary model and records `model_version = tmpy67v2_55`. After the rename, `verify.py` fails on `model_card(root, "tmpy67v2_55")`.
- When the retrain finishes, its version sorts after `20261120T1329Z` and goes live at once, with no review step.

Fix: an explicit live-model pointer (for example `data/models/live.txt` or a setting) that the scorer reads.

### 3. High: Verification Freezes Climatology at Verification Time

`model/verify.py:58,146` reads `gold/climatology` when a row is verified and never revisits it. 20,046 of 31,354 verification rows have `clim_pct` NULL: every row verified before 13:19 UTC, when the climatology was still the January sample. `scores_daily.py:82-87` then computes BSS on the 11,308 matched rows only, but reports `n` as all rows (`scores_daily.py:94`). Fix: refill `clim_pct` for these rows (it depends only on area and local time), and report the BSS sample size.

### 4. Medium: Persistence CSI Is "Always Rain"

Both model cards store `csi_thresholds.probability.persist = 0.0` for every lead (`best_csi_threshold` picks the cut with the highest CSI, and for a 0/1 forecast "always yes" wins). `scores_daily.py:73-81` applies it as `p >= 0`, so every row counts as a rain forecast and the persistence CSI equals the always-yes CSI. The cutoffs also differ across the product: daily scores use the card cutoff (model about 0.35), API live CSI uses 50% (`api/queries.py:441`), `disagree` uses `prob3 >= 50` (`gold/frames.py:106`), the overview tile uses 60%. Fix: score persistence at its own wet/dry rule (0.5), and state the cutoff next to each CSI.

### 5. Medium (UI Labelling): Future Map Frames Mislabel the Forecast Window

`web/app.js:206-214`: after the issue frame, the slider shows the fixed latest issue with leads 1, 1, 2, 2, 3, 3. At frame `issue + 1 h` the legend says "Chance of rain, next 1 h" (`app.js:278`) next to the frame clock, but the value is for `[issue, issue + 1 h)`, which ends at the frame time. At `issue + 2 h` and `issue + 3 h` the frame half-hour is outside the window shown. "Issued" (`app.js:317, 789, 839`) shows the Himawari scan slot `t`, not `issued_at`, which is about 15 min later. Suggested label: "Chance of rain from 14:00 to 16:00 WIB, issued 14:15". Route to the UI session.

### 6. Medium (UI Labelling): Overview Chart Compares a 3 h Window With a 30 min Slot

`web/app.js:797-800` ("Share of kab/kota with rain") plots the mean `prob3` (event in the next 3 h) against the share of areas with `obs >= 0.5 mm/h` in one half-hour. Even a perfect forecast sits far above the observed line: over 2026-11-18 to 11-20 the 3 h wet share is 0.381 against 0.203 for single slots. The area panel has the same mismatch (`app.js:670-718`, `prob3` at X beside IMERG at X). Fix: plot the observed 3 h event share (max over `[X, X+3 h)`), or label the lines as different quantities. Route to the UI session.

### 7. Medium: Map Hindcast Comes From the Retired Sample Model and Is Unlabelled

`gold/frames.py:27-44` fills slots without a live issue from `gold/hindcast`. All 138,780 hindcast rows (90 slots, 2026-11-18 02:00 to 11-19 23:00) and all live rows up to 13:30 are from `20261120T0004Z-3bf0e31` (trained on 21 January days). In November it is badly under-confident:

| Lead | Rows | Base Rate | Mean p | 0-10% Bin Observed | 10-20% Bin Observed |
|---|---|---|---|---|---|
| +1 h | 46,260 | 0.260 | 0.058 | 15.9% | 73.1% |
| +2 h | 46,260 | 0.336 | 0.186 | 13.9% | 40.9% |
| +3 h | 46,260 | 0.399 | 0.210 | 19.0% | 44.0% |

Hindcast BSS against its own base rate: 0.05, 0.26, 0.15. 55% of its +1 h values are exactly 0 (isotonic flat segment). The live old-model rows show the same pattern (0-10% bin observed 20.0%, 21.8%, 30.7%). The map does not say these are replays or which model made them. Fix: rerun the hindcast with the live model (frames already prefer the newest version per slot) and label hindcast slots (UI session).

### 8. Low: Non-Monotone Lead Triplets

The three leads are separate models, and the +2 h and +3 h windows contain the +1 h window, so `p1 <= p2 <= p3` must hold. Live data breaks it in 77 of 5,654 current-model triplets (1.4%, largest drop 6 points) and 90 of 13,364 old-model triplets. Fix: cumulative max over leads in `model/score.py:114-128` (changes calibration slightly; recheck on validation).

### 9. Low: Rolling Rain Sums Accept Gaps

`features/build.py:180-185` sums whatever slots exist in the 3, 6 and 24 h windows, with no completeness count. A missing slot lowers the sum silently. A missing anchor clears all rain features (finding 1). Fix: null the sum when coverage is incomplete, or add a coverage feature.

## Notes (Verified, No Action Needed Unless Wanted)

- Wet label uses the stored value: `rain_mean_x10 >= 5` after `np.rint` (half to even), so the effective threshold is a mean just above 0.45 mm/h (`np.rint(4.5) = 4`, `np.rint(4.5000001) = 5`). State it in the model description.
- `test_2026` starts 2026-03-01 (`settings.yaml:58`), but silver switches from V07B (last 03-03 23:30) to V07C at 03-04 00:00. Three V07B days are in the "after V07C" test.
- Split filters check only issue time (`train.py:29`). Validation targets at 2024-12-31 22:00 and 23:00 use 2025 truth (514 x 3 rows). Negligible.
- Codex flagged cell weights as overlap fraction without cell area (`zonal.py:10`). Within one area cell area varies at most 0.62%, so the effect on any mean is below that. Not a bug in practice.
- Daily scores pool model versions, the API takes the newest per `t`. No `(t, area, lead)` key has two versions today.
- Motion fallback to "dry" when no cloud row exists never happened (all 31,354 rows have a Himawari row). BMKG is NULL, not dry, when no run exists (3,084 rows). Frames take BMKG as of the slot, verification as of `issued_at`.

## Checked and Correct

- IMERG to silver to map, slot 2026-11-20 06:00: own zonal mean of the bronze crop equals `silver/rain_kabkota` for all 514 areas, and equals the `obs` frame for all 514. Spot areas 255, 363, 467: 8.19, 9.61, 11.31 mm/h by hand, 82, 96, 113 in silver and frames. Units mm/h x 10, legend says mm/h.
- Grid order: `read_crop` transposes `[lon][lat]` to row-major `(lat, lon)`, matching `cell // width` in `membership.py:82`. Fill and negative cells excluded.
- Slot keys are granule start times in UTC. Himawari issue slots are only :00 and :30.
- Target: max of exactly 2, 4 or 6 half-hours from `t` (inclusive) to `t + L` (exclusive), in training (`build.py:121-131`) and verification (`verify.py:93-99`). My own recomputation: 0 incomplete windows, 0 label mismatches, 0 wrong window bounds over 31,354 rows; 31,354 unique `(t, area, lead)` keys.
- Leakage guard: every live row has `imerg_slot = t - 360 min`; persistence uses that slot (0 mismatches). The latest IMERG slot lands about 23 min before issue.
- Live and hindcast use the same `predict`; live refuses to issue once truth is in silver (`score.py:193-196`). The current card has no calibration on any lead.
- Metrics: Brier, BSS against matched climatology, CSI = hits / (hits + misses + false alarms), AUC with tied scores grouped, reliability bins with empty bins as NULL (`metrics.py`).
- Frames: Codex matched all 63,222 `obs` and 194,292 `prob` frame values to their sources. `prob1..3` are keyed by issue slot `t`, `obs` by IMERG slot start.

## Open Question: Is the New Model Low Under Himawari?

The live model's mean probability (14:00 to 18:30 UTC) is below climatology: 16.8% against 25.3% at +1 h, 21.8% against 30.9% at +2 h, 26.4% against 35.3% at +3 h. No truth window for it exists yet (first one about 20:10 UTC). A sensor check finds no cold-cloud bias: the wet share by `cold_pct` bin is close for Himawari (Nov 2026) and MERGIR (Nov 2025), for example 37.3% and 39.8% at 10-29%, 74.7% and 79.5% at 60% or more. `flow_kmh` is higher under Himawari (31.3 against 21.1 km/h, 14-18 UTC, Nov 2016-2025 MERGIR), which may be weather or sensor (hypothesis). Check again when the new model has a day of verification.

## Fix Status (2026-11-20)

juan picked findings 1, 2, 3, 4 and 7. UI findings 5, 6 and the hindcast label in 7 stay with the UI session.

| Finding | Commit | Change |
|---|---|---|
| 2 | `ca535ac` | `model.live_version` in `settings.yaml` names the live model. Scorer and API read it. Promote a model by editing it. |
| 1 | `efe679a` | Each IMERG tick also searches the last 12 h and fetches any slot missing from the silver manifest. |
| 3 | `973d031` | Verification fills `clim_pct` for rows that have none, on the next verify run. |
| 4 | `cb39db5` | Persistence and motion (0 or 1) are scored at 0.5 in daily scores and in new model cards. |
| 7 | data only | Hindcast for 2026-11-18 02:00 to 11-19 23:00 rerun with `20261120T1329Z-a2f52f2` (90 slots, 138,780 rows, 4 min 46 s, 787 MB peak). |

Checked on live data after the loops ran the new code:

- Finding 1: the 19:31 tick fetched the 11:00 slot (514 rows in silver, tile drawn). The next verify run verified the 4,626 keys it had blocked.
- Finding 3: the 19:33 verify run filled `clim_pct` on 20,046 rows. All 35,980 rows now have it, and every value equals the climatology for its area, local month, local hour and lead. A recomputation of `observed` gives 0 mismatches and 0 incomplete windows; keys are unique.
- Finding 4: daily scores for 2026-11-20, computed into `data/tmp` (since deleted): persistence CSI is 0.103, 0.086, 0.076 for +1, +2, +3 h, against base rates 0.269, 0.374, 0.466 that the old cutoff would have given.
- Finding 2: the scorer and the API read `live_version`; tests cover both (`tests/test_live.py`, `tests/test_api.py`). Full suite: 153 passed.
- The same daily scores show that the retired sample model was below climatology for all of 2026-11-20 (model BSS -0.34, -0.25, -0.49). The live model has no verified window yet.
- Finding 7: the 19:49 frames build shows the new hindcast. All 138,780 `prob` frame values for those slots equal the live-model rows, and `/api/frames` serves them (mean +1 h chance at 2026-11-19 06:00 is 40.9%, against 6.7% from the old model). The old-model rows stay in `gold/hindcast` as history.

Hindcast scored against IMERG (complete windows, BSS against the same climatology):

| Model | Lead | Base Rate | Mean p | BSS |
|---|---|---|---|---|
| Old sample | +1 h | 0.260 | 0.058 | -0.115 |
| Old sample | +2 h | 0.336 | 0.186 | 0.116 |
| Old sample | +3 h | 0.399 | 0.210 | -0.008 |
| Live | +1 h | 0.260 | 0.243 | 0.388 |
| Live | +2 h | 0.336 | 0.313 | 0.340 |
| Live | +3 h | 0.399 | 0.366 | 0.284 |

The live model is well calibrated on Himawari input. Each 10% bin is within 3 points of its observed rate at +1 h, and slightly low in the 10-30% bins at +3 h (14.1% predicted against 21.8% observed). This answers the open question above: on these 48 h, the Himawari features do not make the live model too low. It is below the MERGIR test BSS (0.48, 0.46, 0.42), so keep checking as more days are verified. Non-monotone triplets in this hindcast: 929 of 46,260 (2.0%), finding 8.

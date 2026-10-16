# Step 4: Cloud Data (MERGIR History and Live Himawari-9)

Date: 2026-11-19/20. Status: done on the sample month and live; the Himawari vs MERGIR bias check is pending (MERGIR is about 47 h late).

## What Was Built

- `nowcast mergir backfill --start --end` and `nowcast mergir gaps`: DAP4 crops, per-frame `tb_min`, `tb_mean_x10`, `cold_pct`, and DIS optical-flow motion (`adv1..3_pct`, `flow_kmh`) per area; the last frame carries across months; bronze deleted per month.
- `nowcast himawari tick`: Band 13 segments 05 to 07 from the NOAA bucket, satpy `ahi_hsd` decode, block mean onto the MERGIR grid through a cached pixel-to-cell index, the same features as source 2 for HH:00 and HH:30 scans, and a cloud tile for every 10-minute scan.
- Experiment 03 (`docs/experiments/03-flow-settings.md`): 8 flow settings against persistence over one week. Every setting beats persistence; DIS `fast` with 33 km smoothing is best (2.5% lower area error than the starting `medium`/66 km, about 3 times faster) and was adopted.

## Done When

| Check | Result |
|---|---|
| Sample month processed, median cold-cloud speed plausible | 1,488/1,488 frames, 514 rows per frame. Median 32.5 km/h with the adopted settings (35.5 km/h with the starting ones; planning measured about 30) |
| Gap report and 10-year projection | No missing frames. 3.8 MB per month, so about 0.46 GB for 120 months, under the 0.6 GB cap |
| Live Himawari, several scans in a row | Yes: every 10-minute scan since 2026-11-19 23:00 UTC; median latency from scan start to all three segments 15.4 min at 5-minute polling (13.5 to 13.7 min when the tick runs right after the files land) |
| Himawari vs MERGIR bias | **Pending**: MERGIR for the live days arrives about 47 h later (first overlap about 2026-11-21 23:00 UTC). |

Runtime for the month: 226 s download, 107 s processing, 424 MiB peak RSS. Invalid MERGIR cells: 0.71%.

## Differences From the Plan

- First decode: segments 05 to 07 span 20.58° S to 10.03° N over 94 to 142° E, wider than the planned 11.5° S to 6.5° N. All three are still needed, and they cover the box with 0% empty cells.
- Cloud tiles are 97 KB on average (planned about 60 KB), so about 0.1 GB for the 7-day window.
- `state/himawari_index.npz` (35 MB) caches the regrid index.

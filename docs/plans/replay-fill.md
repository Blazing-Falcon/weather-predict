# Plan: Fill the 30-Day Replay

Status: run on 2026-11-21 with juan's OK. Results at the end.

## Goal

The 30-day replay (commit `1c0f692`) shows data only from 18 Nov 2026. After this fill, every layer except BMKG has data for the whole window, from 21 Oct 2026.

## What Is Missing

| Data | Now | Missing |
|---|---|---|
| Observed rain, silver | 2016-01-01 to now | 2026-10-31 23:30 to 2026-11-17 23:00 UTC |
| Rain tiles | from 2026-11-17 23:00 UTC | everything older |
| Cloud, silver | MERGIR to 2026-10-31 23:30, Himawari from 2026-11-18 01:00 UTC | the gap between them |
| Cloud tiles | from 2026-11-18 01:00 UTC | everything older |
| Forecasts (hindcast and live) | from 2026-11-18 02:00 UTC | everything older |
| BMKG | 3 runs from 2026-11-19 12:00 UTC | cannot be filled: BMKG does not publish past runs |

The "Model vs BMKG" layer needs BMKG, so it stays short too.

## Steps

Times are UTC. Each command takes the same locks as the live loops, so the loops keep running.

1. **IMERG fill**: `nowcast imerg fill --start 2026-10-21T00:00:00+00:00 --end 2026-11-17T23:30:00+00:00`. It fills the silver rain gap (the last 17 days of the window) and draws rain tiles for the whole window. That is about 1,340 slots of a small OPeNDAP crop. Expected time: minutes (the 10-year backfill did about 14 slots per second). It holds `imerg.lock` for its whole run, so the live IMERG tick waits until it ends.
2. **Himawari fill** (can run at the same time as step 1): `nowcast himawari fill --start 2026-10-21T00:00:00+00:00 --end 2026-11-18T00:30:00+00:00`. It writes cloud per area (source Himawari) and one cloud tile per half hour. That is about 1,345 scans × 3 segments × 3.1 MB, so about 12.4 GB downloaded, and each file is deleted after use. With the measured rate on 2026-11-20 (about 60 KB/s per connection, 8 connections), this takes about 7 h. It locks only while it merges each scan, so live Himawari ticks continue.
3. **Hindcast** (after steps 1 and 2): `nowcast live hindcast --start 2026-10-21T06:00:00+00:00 --end 2026-11-18T01:30:00+00:00`. The live model `20261120T1329Z-a2f52f2` scores about 1,340 slots into `gold/hindcast` (never into `gold/predictions`). The last run took 4 min 46 s for 90 slots with 0.8 GB of memory, so this takes about 72 min. The model never trained on these months: training ended on 2023-12-31, and test_2026 ends on 2026-10-31.
4. **Map frames** (after step 3): `nowcast gold frames --days 32`, run once. The 2-minute loop keeps rebuilding the newest 7 days, and 31-day retention keeps the older days.
5. **Check**: frame slots per layer over the window (expect about 1,440 for obs, cold, prob1 to prob3), tile counts, `experiments/06_web_check.py` with 0 errors, and 30-day replay screenshots at 1440 px and 390 px, dark and light.

## Cost and Risk

- Disk: about 150 MB kept (cloud tiles about 130 MB, rain tiles about 25 MB, silver, hindcast and frames a few MB). `DATA_ROOT` is 2.0 GB of the 8 GB budget, and the disk has 31 GB free.
- Network: S3 (public NOAA bucket) and NASA OPeNDAP, within the configured parallel limits.
- A stopped run can be started again, because fills merge by slot. After a power cut, remove a stale `part.parquet.tmp` first, as the 10-year backfill showed.
- Live loops: none is stopped or restarted. During step 1, IMERG ticks are late by the length of that step.
- Running steps 1 to 4 as one chained script in the background means one log to watch (`data/tmp/replay-fill.log`).

## Results (2026-11-21)

| Step | Time | Peak memory | Result |
|---|---|---|---|
| IMERG fill | 4 min 34 s | not measured | silver rain gap filled; 0 gaps since 21 Oct |
| Himawari fill | 1 h 3 min | 1.7 GB | 1,345 slots, cloud per area and cloud tiles |
| Hindcast, first run | 3 h 36 min | 2.3 GB, then killed for memory | written through 16 Nov 11:00 UTC |
| Hindcast, rest (after fix `71d4555`) | 15 s | 0.7 GB | 77 slots |
| Map frames, 32 days | 24 s | 1.9 GB | 4.0 million rows |

The first hindcast run was slow and ran out of memory because its write loop read and rewrote the whole month partition for every slot. Commit `71d4555` merges each month once. The kernel also stopped two other user processes (`hermes`) during the memory shortage at 09:21 UTC.

Coverage of the 30-day window (from 22 Oct 04:00 UTC): obs 1,440 slots, cold 1,446, prob1 to prob3 1,439 each, 1,448 rain tiles and 1,589 cloud tiles. The few single missing forecast slots are half-hours without a Himawari scan one hour earlier. BMKG and Model vs BMKG start on 20 Nov, as expected.

Two app fixes came out of the check (`8954eb6`): `/api/meta` listed only 7 days of tiles, so old frames said no cloud image was archived, and long windows did not load observed rain for the drawer. `/api/meta` is now 251 KB raw and 51 KB with gzip, polled every 60 s.

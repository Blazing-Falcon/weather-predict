# Step 2: BMKG Archiver

Date: 2026-11-20. Status: done, awaiting review.

## What Was Built

- `nowcast bmkg tick`: three sentinels (Kota Malang WIB, Kota Makassar WITA, Kota Jayapura WIT) detect a new run. The tick then sweeps the 514 picks at 55 requests a minute or less. Raw bodies go to `bronze/bmkg/run=.../sweep=...jsonl.gz` in 50-line chunks, and a point counts as done only once its chunk is on disk. A response is credited to the run its own body shows, so only the newest run is swept, and an older run closes after 6 hours with its missing points listed. State is in `state/bmkg.json` and health in `state/health/bmkg.json`.
- `nowcast bmkg parse`: whole monthly partitions of `silver.bmkg_forecast` rebuilt from bronze, with dead letters in `bronze/bmkg/_dlq/`.
- `ops/systemd/nowcast-bmkg.*` (not installed: juan runs the jobs manually on dev). The tick runs every 15 minutes in a loop instead.

## Done When

| Check | Result |
|---|---|
| At least one complete run | Run 2026-11-20 00 UTC: 514/514 points, `complete = true`, 0 missing |
| Each run has at least 510 of 514 points, missing ones listed | 00 UTC run: 514. The 2026-11-19 12 UTC run was already being replaced when the archiver started (01:06 UTC): 131 points captured, the 383 others listed as missing in state. It does not count as a run the archiver could have caught. |
| `first_seen` recorded for every run | 12 UTC: 2026-11-20 01:06:38, 00 UTC: 01:06:40 (both at the archiver's first tick, so they bound the real first appearance from above) |
| Silver rows = points x steps; re-parse identical | 00 UTC run: 514 points, 10,802 rows, 21 or 22 steps per point (lead 1 to 64 h). `sha256` of the partition before and after a re-parse: `4de55e93992401b6...` both times. No dead letters. |
| Run times seen | 12 UTC and 00 UTC, as expected |

## Differences From the Plan

- A response holds 21 or 22 steps, not the 17 or 18 seen in planning.
- When a new run appears, BMKG's cache keeps serving the old run to some points for a while (on the first sweep, 457 points already showed 00 UTC and 57 still showed 12 UTC). Crediting each response to the run it shows handles this.
- The two areas without their own accepted BMKG code (step 1 report) are swept with the fallback picks.

# Step 3: IMERG Early (Rain Truth)

Date: 2026-11-19/20. Status: done on the sample month; live latency measured over the first hours.

## What Was Built

- `nowcast imerg backfill --start --end`: CMR listing (never built file names), DAP4 crops 8 in flight, per-area weighted mean, max and wet share through a sparse area x cell matrix (about 2 ms per slot), one partition per month plus a provenance manifest, bronze deleted after the month is written, state for resuming.
- `nowcast imerg tick`: CMR granules after the cursor, merged into the month partition (newest version wins), latency log, health file. Live crops are kept for the map tiles until the retention window.
- `nowcast imerg gaps --month`.

## Done When

| Check | Result |
|---|---|
| 1,488 slots x 514 areas | 1,488/1,488 slots, 764,832 rows, 514 rows per slot, 0 null area-slots |
| Box wet share close to 27% | 27.44% |
| Gap report | `nowcast imerg gaps --month 2025-01`: no missing slots |
| Re-run gives identical output | Second run skips the done month; the writer is byte-identical on re-run (test) |
| Silver size and projection | 1.74 MB for the month, so about 0.21 GB for 120 months, under the 0.5 GB cap |
| Live tick, several slots in a row, latency | See below |

Backfill run: 1,488 granules, all `V07B`, 126 MB downloaded in 242 s, no failures.

## Live Latency (Measured 2026-11-19/20)

| Slot (UTC) | Seen (UTC) | Minutes after slot end |
|---|---|---:|
| 2026-11-19 17:00 | 22:35 (first tick, catch-up) | ≤ 306 |
| 2026-11-19 17:30 | 22:35 (first tick, catch-up) | ≤ 276 |
| 2026-11-19 18:00 | 23:37 | 307 |
| 2026-11-19 18:30 | 23:37 | 277 |
| 2026-11-19 19:00 | 00:38 | 309 |
| 2026-11-19 19:30 | 00:38 | 279 |

IMERG Early arrives in hourly batches of two slots, both registered in CMR at the same time (for example 22:25 UTC for 17:00 and 17:30). The :00 slot is therefore about 307 min old after its end, the :30 slot about 278 min. Measured from the slot start, the newest usable slot is at most about 339 min old, inside the 360-minute lag the model uses. The first tick's catch-up slots are upper bounds, not true latencies.

## Differences From the Plan

- The model's lag is measured from the slot start (360 min), not from the slot end (see `docs/decision-log.md`).
- Live crops are kept for 7 days (tiles), backfill crops are deleted per month as planned.

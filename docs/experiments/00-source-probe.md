# Source Probe

Date: 2026-11-19 at 21:52:25 UTC

**Question**  
Does every source answer from the dev machine, and what are the live latencies today?

**Method**  
Ran `uv run python experiments/00_source_probe.py` with a 30 s timeout per request, one request per probe except for the Himawari scan lookback, and a common UTC reference time at script start.

## Results

All ten probes succeeded with HTTP 200. [Full run data](results/00_source_probe.json)

| Probe | HTTP | Elapsed (ms) | Measured Response |
|---|---:|---:|---|
| BMKG | 200 | 958 | Analysis time `2026-11-19T12:00:00` UTC, 17 steps |
| CMR IMERG | 200 | 1622 | `GPM_3IMERGHHE.07:3B-HHR-E.MS.MRG.3IMERG.20261119-S163000-E165959.0990.V07C.HDF5`, version V07C, slot start 2026-11-19 16:30 UTC, 292.42 min since slot end |
| IMERG OPeNDAP | 200 | 4392 | 56,944 bytes, shape `(1, 480, 180)`, fraction of non-fill cells ≥ 0.1 mm/h was 0.1403009259259259 |
| CMR MERGIR | 200 | 596 | `GPM_MERGIR.1:merg_2026111723_4km-pixel.nc4`, slot start 2026-11-17 23:00 UTC, 46.87 h since slot start |
| MERGIR OPeNDAP | 200 | 5042 | 833,515 bytes, shape `(2, 495, 1319)`, fraction of valid cells < 233 K was 0.09792164423493815 |
| Himawari | 200 | 5567 | Scan start 2026-11-19 21:40 UTC, 12.42 min old. B13 segment sizes in bytes were 05 = 3,211,896, 06 = 3,095,678, 07 = 2,736,592 |
| GHCNh | 200 | 1955 | 10,174,985 bytes, last modified `Thu, 19 Nov 2026 06:49:16 GMT` |
| METAR | 200 | 1795 | 12 reports |
| geoBoundaries | 200 | 2595 | Canonical type `regency, city`, 519 ADM2 units |
| wilayah.id | 200 | 1003 | 38 rows, updated 2025-07-04 |

## Observations

- IMERG was 292.42 min old from slot end, 7.58 min below the 5 h planning value.
- MERGIR was 46.87 h old from slot start, 1.87 h above the 45 h planning value. CMR returned a literal colon in the title. The crop request succeeded with the colon encoded as `%3A`.
- The newest Himawari scan with B13 segment 07 was 12.42 min old, close to the 12 min planning value. Segments 05 through 07 totaled 9,044,166 bytes (9.04 MB), close to the 9.1 MB planning value.
- BMKG returned the 12:00 UTC run, one of the two planned run times. The 00:00 UTC run remains unverified.

# Step 8: API

Date: 2026-11-20. Status: done, awaiting review.

## What Was Built

`nowcast api` (FastAPI + DuckDB, read-only, `127.0.0.1:8000`). DuckDB reads the gold and silver Parquet in place. Endpoints: `/api/meta`, `/api/frames`, `/api/areas/{id}`, `/api/alerts`, `/api/scores`, `/api/model`, `/api/history/{id}`, `/api/pipeline`, `/tiles/rain/{slot}.png`, `/tiles/cloud/{scan}.webp`, `/geo/*.geojson`. `docs/api.md` maps every page of the mockup to the endpoints it uses. Finished slots get `Cache-Control: max-age=86400`, live ones 60 s.

## Done When

`uv run python experiments/05_api_timing.py` (20 calls each against a running server, ms):

```
Endpoint                                    HTTP    Median       p95       Max
/api/meta                                    200     14.40     15.99     71.19
/api/frames?layer=obs                        200     34.70     36.50     37.18
/api/areas/0                                 200     14.14     16.15     20.84
/api/alerts                                  200     35.60     49.29    101.84
/api/scores?scope=national                   200      1.02      1.29      1.64
/api/model                                   200      4.82      5.16      5.59
/api/history/0                               200      8.31      9.19     10.92
/api/pipeline                                200      1.44      1.80     20.66
/geo/kabkota.geojson                         200     19.80     23.69     25.88
/geo/neighbours.geojson                      200      2.27      2.63      3.24
/docs                                        200      0.58      0.63      0.75
/openapi.json                                200      0.66      0.94      4.17
/tiles/rain/20261119T1930Z.png               200      0.82      1.05      1.16
/tiles/cloud/20261120T0100Z.webp             200      0.90      1.10      1.10
```

Every endpoint is under 300 ms (worst p95 49 ms). The OpenAPI page at `/docs` lists all endpoints with summaries.

## Differences From the Plan

- The API lives in `src/nowcast/api/` (one package for the pipeline and the API) instead of a top-level `api/` folder.
- `/api/areas/{id}` returns province-scope scores where area scope does not exist (the daily scores have national, island and province scopes only).
- History has no ENSO labels: there is no ENSO data in the archive.

# API

`nowcast api` serves a read-only FastAPI app (`src/nowcast/api/app.py`) on `127.0.0.1:8000`. It reads the data root with DuckDB and serves the web app from `web/` at `/`. Interactive documentation is at `/docs`.

## Conventions

- Times are UTC ISO 8601. Query times need an offset or `Z`.
- A dataset that does not exist yet gives empty lists or `null`, not an error. An unknown area gives 404 when the area table exists.
- `Cache-Control`: `public, max-age=60` for live data; `public, max-age=86400` for tiles, GeoJSON and frame windows that ended more than 6 hours ago.
- The server also keeps `/api/meta`, `/api/pipeline`, `/api/areas/{id}` and `/api/history/{id}` in memory for 60 s.
- Responses are gzip-compressed when the client allows it and carry the security headers described in [operations](operations.md#api).

## Routes

| Route | Parameters | Returns |
|---|---|---|
| `GET /api/meta` | | Newest slot and freshness per source, the BMKG run and when it was first seen, the live model version, tile bounds and tile times of the last 31 days, the area catalog, METAR stations that reported in the last 24 h, and when the map frames last changed |
| `GET /api/frames` | `layer` (default `prob3`; also `obs`, `cold`, `prob1`, `prob2`, `bmkg`, `bmkg_code`, `bmkg_step`, `bmkg_age`, `disagree`), `from` (default 48 h ago), `to` (default 3 h ahead) | `slots` and, per slot, one value per area ID (519 positions, `null` where missing). The window must be in order and at most 31 days. Layer units are in [data.md](data.md#gold). |
| `GET /api/areas/{area_id}` | `area_id` 0 to 518 | For the last 48 h: observed rain, forecasts, cloud scans and BMKG weather per half hour up to 3 h ahead; 30 days of scores for the area, or its province when the area has none; climatology |
| `GET /api/alerts` | `level` (Waspada, Siaga, Awas or 1 to 3; default Waspada), `threshold` (0 to 100, default 50), `island`, `province` (name or code), `source` (`model` or `bmkg`) | The latest issue slot and one entry per area: probabilities per lead, cloud top, cold share, BMKG weather now and in the next 3 h. Model alerts: Awas at 85% or more with a cloud top at 203 K or colder, Siaga at 70% or more, Waspada at `threshold` or more; sorted by level, then probability. BMKG alerts: areas with a rain code in the next 3 h, sorted by code. |
| `GET /api/scores` | `scope` (`national`, `island`, `province`, `area`), `id`, `lead` (1 to 3), `days` (default 30) | Daily score rows |
| `GET /api/model` | | The live model card, the test BSS of every model version, and live CSI at a 50% cutoff over 30 days, by local hour and by island |
| `GET /api/history/{area_id}` | `area_id` | Climatology, monthly and yearly rain totals in local time with slot counts, ENSO phase per year (from `config/enso/roni.csv`) and wet-season onset |
| `GET /api/pipeline` | | Job health files, source latency (last, median and p90 over 24 h), storage per dataset with its retention cap, grid values processed, the IMERG backfill state and the 20 newest Dagster runs |
| `GET /api/pipeline/node/{node_id}` | `node_id`: `br_bmkg`, `br_himawari`, `br_imerg`, `br_gauges`, `br_mergir`, `br_ref`, `sv_bmkg`, `sv_cloud`, `sv_rain`, `sv_gauge`, `gd_frames`, `gd_tiles`, `gd_pred`, `gd_verif`, `gd_scores`, `gd_hindcast`, `gd_clim`, `md_models` | For Parquet datasets: columns, newest partition, row count, newest time and 3 sample rows. For file datasets: file count, the 3 newest files and a JSON preview for BMKG and METAR. For models: versions, the live version and its test BSS. 404 for other IDs. |
| `GET /tiles/rain/{YYYYMMDDTHHMMZ}.png` | | IMERG rain tile |
| `GET /tiles/cloud/{YYYYMMDDTHHMMZ}.webp` | | Himawari cloud tile |
| `GET /geo/kabkota.geojson`, `GET /geo/neighbours.geojson` | | Display boundaries of the areas and of neighbouring land |

## Code

| Module | Holds |
|---|---|
| `api/app.py` | Routes, parameter checks, caching, security headers, static files |
| `api/archive.py` | The DuckDB connection (limited to the data root), file discovery with partition pruning, and the shared dataset readers |
| `api/live.py` | Meta, area detail and alerts |
| `api/insight.py` | Model page and history page, ENSO phases |
| `api/pipeline.py` | Pipeline page and node inspector, Dagster runs, latency |

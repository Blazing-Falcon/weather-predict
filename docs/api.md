# Nowcast API

FastAPI serves the seven-page web app at `/`. The app uses this API on the same origin. MapLibre GL JS 4.7.1 loads from cdnjs, with its CSS served locally. The map uses GeoJSON and archive rasters without a tile basemap.

## Page Data

| Page | Data Needed | Endpoint |
|---|---|---|
| Map | Area and neighbouring outlines, area names and positions | `GET /geo/kabkota.geojson`, `GET /geo/neighbours.geojson`, `GET /api/meta` |
| Map | Timeline, latest observation and issue markers, model probabilities, observed rain, cold-cloud share, BMKG rain and disagreement | `GET /api/meta`, `GET /api/frames?layer=prob3` (also `prob1`, `prob2`, `obs`, `cold`, `bmkg`, `disagree`), `GET /api/alerts?threshold=0` for the latest issue if its frame is pending |
| Map | Raw rain image, cloud overlay and their bounds and available timestamps, METAR station markers | `GET /tiles/rain/{slot}.png`, `GET /tiles/cloud/{scan}.webp`, `GET /api/meta` |
| Map | Area drawer and tooltip with rain, cloud-top temperature and cold share, forecasts, BMKG weather, local scores and climatology. National score table and processing totals | `GET /api/areas/{area_id}`, `GET /api/scores?scope=national&lead=3`, `GET /api/pipeline` |
| Overview | Source freshness, area count, issue time, national rain and cold-cloud counts and trends, expected rain share and island breakdown | `GET /api/meta`, `GET /api/frames` (`obs`, `cold`, `prob1`–`prob3`). Aggregate over available areas and leave missing slots blank. |
| Overview | Alert counts by level, ranked forecasts with cloud-top temperature and BMKG weather | `GET /api/alerts`. Use `threshold=0` for the full forecast ranking and the selected threshold for counts. |
| Alerts | Issue time, level, area names and location filters, probabilities by lead, cloud-top temperature, cold share and BMKG weather. Level, island, province, name and watch filters use the returned rows. | `GET /api/alerts?threshold=`, `GET /api/meta` |
| Model | Training and test periods, metrics and baselines, calibration, importance, thresholds and version history. Live verification CSI by local hour and island | `GET /api/model` |
| History | Area comparison selector, local month × hour heatmap, monthly and annual rainfall, wet-season timing and archive coverage | `GET /api/meta`, `GET /api/history/{area_id}` for each selected area |
| Pipeline | Job health, recent Dagster runs, source availability and latency, disk use and retention caps, processed grid values and retained silver bytes, measured backfill progress | `GET /api/pipeline`, `GET /api/meta` |
| Settings | Area names for the watch list, METAR stations and alert threshold | `GET /api/meta`, `GET /api/alerts?threshold=`. Theme, threshold preference, initial zoom, opacity, gauge visibility and watch list stay in browser storage. |

The pipeline diagram and page labels are UI content. The mockup's generated forecasts, seeded history and ENSO labels have no corresponding production records. The API serves archived forecasts and measured rain, with the archive coverage reported alongside the totals. The live CSI breakdown uses verified production rows. The training card supplies the test-period results it records.

## Response Conventions

- Timestamps are UTC ISO 8601. Use an offset or `Z` in query parameters. A missing dataset returns empty arrays or nulls. An area absent from an existing area catalog returns HTTP 404. GeoJSON and tiles return HTTP 404 when their files are absent.
- `/api/meta` gives the area catalog, configured METAR stations, tile bounds and tile filename stems from the last seven days. Freshness is the elapsed time from each source's latest data timestamp to `as_of`. BMKG reports the run time and the earliest time that run was seen.
- `/api/frames` defaults to `prob3` and a window from 48 hours ago through three hours ahead. Both bounds are inclusive. Supply both bounds for a historical request. The maximum span is seven days. Each row has 519 positions indexed by area ID. Missing values are null. Whole slots appear only if they are in the archive for the requested layer.
- Frame units are tenths of mm/h for `obs`, percent for `cold` and `prob1` through `prob3`, 0/100 for `bmkg` and 0/1 for `disagree`. A probability is tied to its archived issue slot and gives the chance of rain over the following hour, two hours or three hours. The client can use the latest issue and all three leads for the future part of the slider. Store the issue time with any displayed outlook.
- `/api/areas/{area_id}` reports 48 hours of observed rain, forecasts and cloud scans. BMKG gives the archived weather code and rain flag at half-hour slots through three hours ahead. Each slot uses the latest run available at that slot or at request time for future slots. A slot outside the run's valid steps has null weather. Cloud-top temperature is in kelvin. The score block identifies its scope and ID. If area scores are missing in the 30-day window, it gives province scores when the area has a province.
- `/api/alerts` uses the maximum probability across leads 1–3 and the cloud top at the model's scan, with a recent cloud scan at or before issue as fallback. Awas requires at least 85% and at most 203 K, Siaga requires at least 70% and Waspada uses `threshold` (default 50). The `level` parameter sets the minimum level by name or rank. Province can be its name or code. Results sort by level, then probability.
- `/api/scores` returns daily rows. `days` defaults to 30 and includes the current UTC date. `scope` accepts `national`, `island`, `province` and `area`. Use `id=ID` for national, the island name for island, the province code for province, or the area ID for area. `lead` accepts 1 through 3.
- `/api/model` returns the full newest card and the cards' test Brier skill by lead. The live CSI charts cover the last 30 days of available verification and use a 50% cutoff, grouped by local issue hour or island.
- `/api/history/{area_id}` groups rain totals by local calendar month and year, using UTC if the area's time zone is unavailable. A half-hour at `rain_mean_x10 = 10` contributes 0.5 mm. Slot counts show how much data contributes to each total. Climatology reports the archived mean spatial wet share and event probabilities `p1`–`p3` by local month and hour. Wet-season years with insufficient rain to meet the onset rule have null onset. The reported last wet dekad reflects only the available archive.
- `/api/pipeline` reports configured retention caps and measured disk bytes. Its processed counts multiply distinct retained IMERG slots or MERGIR frames by the configured source grid size. Its latency median and p90 use arrival records from the last 24 hours. It also reports the most recent latency record, even when older, and up to 20 runs from the local Dagster SQLite run database.
- GeoJSON, raster tiles, and frame requests ending more than six hours ago use `Cache-Control: public, max-age=86400`. Live API responses use `public, max-age=60`. Meta and pipeline snapshots are held in memory for 60 seconds. Responses support gzip and use the same origin.

Run `uv run nowcast api` to serve on `127.0.0.1:8000`. Use `--host`, `--port` and `--reload` to override the defaults. Interactive API documentation is at `/docs`. Run `uv run python experiments/05_api_timing.py` while the server is running to measure median and p95 request times.

## Web App Behavior

- The map uses half-hour steps from 48 hours before the latest model issue to three hours after it. If no issue exists, the current half-hour from `as_of` anchors the slider. Display times use WIB. The IMERG gap starts at `latest_observed_slot`. For the +30 and +60 minute ticks, the map uses the one-hour probability from the latest issue. It uses the two- and three-hour probabilities for the subsequent pairs of ticks.
- When a model issue exists before its gold frames have been built, the app uses the lead probabilities from the matching issue in `/api/alerts?threshold=0`. Historical map frames use `/api/frames`. The BMKG and disagreement layers show the rain flag and binary disagreement respectively.
- The raw rain tile uses an exact slot match. The cloud overlay uses the newest available scan within the preceding half hour, including the selected slot, and stops after the issue. Each canvas uses its own dimensions and bounds from `tile_bounds`.
- No API response fields were added for the web app.

With the API running, run `uv run python experiments/06_web_check.py` to capture and check the seven views at desktop and phone sizes. It uses Playwright 1.62.0 with Chromium revision 1234 and writes to `DATA_ROOT/tmp/web-shots/`.

### Pipeline Node

`GET /api/pipeline/node/{node_id}` returns the newest data for a fixed dataset node from the Pipeline diagram. Unknown IDs return 404. Responses use `Cache-Control: public, max-age=60` and a 60-second server cache per dataset node.

Parquet responses contain `columns` (name and type, without hive partition columns), `newest_partition` (file path relative to the dataset), `rows` (metadata count), `newest` (maximum time), and up to three `sample` rows ordered newest first. Cloud and gauge nodes use only `part-himawari.parquet` and `part-metar.parquet`, respectively. Tables without a time column return the first three rows. Missing datasets return empty columns and samples, zero rows and null partition/time.

File responses contain `file_count` and up to three `files`, ordered by modification time, with relative `name`, `bytes` and UTC ISO `modified`. BMKG and METAR archives can include a `json_preview`, limited to 1,500 characters. Models return `versions`, available live card fields in `live`, and `test_bss` per lead. All returned timestamps use UTC ISO strings.

Only dataset nodes have endpoint IDs: `br_bmkg`, `br_himawari`, `br_imerg`, `br_gauges`, `br_mergir`, `br_ref`, `sv_bmkg`, `sv_cloud`, `sv_rain`, `sv_gauge`, `gd_frames`, `gd_tiles`, `gd_pred`, `gd_verif`, `gd_scores`, `gd_hindcast`, `gd_clim`, `md_models`. The browser requests a sample only when its node is selected, and caches it for 60 seconds.

`/api/pipeline` storage entries also include Parquet `columns` and `newest` from file metadata (or the latest file modification time for file datasets). Column and edge explainers use these without requesting node samples.

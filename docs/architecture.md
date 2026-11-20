# Architecture

The system turns satellite and station data into a rain forecast for every kab/kota. Data moves in one direction: sources to `bronze` (raw), `silver` (one row per area and time step), `gold` (products), and then to the API and the web app.

## Data Flow

```
IMERG Early ─┐
MERGIR ──────┤                      ┌─ rain_kabkota ──┐
Himawari-9 ──┼─ bronze (raw files) ─┼─ cloud_kabkota ─┼─ features (Spark) ─ model ─┬─ predictions ─ verification ─ scores_daily
BMKG ────────┤                      ├─ bmkg_forecast ─┤                            └─ hindcast
METAR, GHCNh ┘                      └─ gauge_obs      └─ climatology
                                                          map_frames, tiles ─────────── API (FastAPI + DuckDB) ─ web app
```

| Source | What it gives | How it arrives |
|---|---|---|
| NASA IMERG Early (GPM_3IMERGHHE V07) | Rain every 30 min at 0.1°, the truth for training and verification | OPeNDAP crop of Indonesia per granule, listed by CMR; about 5 h late |
| NASA GPM_MERGIR | Cloud-top brightness temperature at 4 km, 2016 to 2026-10 | OPeNDAP crop per hourly file (two frames) |
| Himawari-9 AHI band 13 (NOAA on AWS) | Live cloud-top temperature every 10 min | Three full-disk segments per scan from S3, regridded to the MERGIR grid |
| BMKG forecast API | The official 3-hourly forecast for a village in each kab/kota | One request per area for each new run, at 55 requests per minute |
| METAR (aviationweather.gov), GHCNh (NOAA NCEI) | Airport rain reports | Hourly METAR; GHCNh station-year files for history |
| geoBoundaries gbOpen ADM2, wilayah.id | Area boundaries and Kemendagri codes | Once, through `nowcast registry` |

Each source module turns its raw files into area values with `zonal.Zonal`: a sparse matrix of the share of each grid cell inside each area. Cloud values also include motion: optical flow between two frames moves the cold cloud 1, 2 and 3 h ahead (`cloud.CloudMotion`).

## Live Cycle

Every 30 min a new Himawari scan gives the cloud features for an issue slot. `live score` combines them with the newest IMERG rain that is available (6 h old) and writes probabilities for the next 1, 2 and 3 h. About 6 h later, when IMERG covers the forecast window, `live verify` compares the forecast with what fell and with the baselines. `gold frames` turns rain, cloud, forecasts and BMKG into the map layers. The schedules are in [operations](operations.md#live-loops).

## Code Map

All Python code is in `src/nowcast/`. Imports go one way: shared modules, then `registry`, `sources`, `features`, `model`, `gold`, `api`. Live source ticks also draw map tiles through `gold/tiles.py`.

| Module | Job |
|---|---|
| `cli.py` | The `nowcast` command tree; each command calls one function in the modules below |
| `config.py`, `paths.py` | Settings, retention policy, folders, the data root |
| `log.py`, `http.py` | Logging with token redaction, `command_errors`; HTTP client with retries and rate limits |
| `storage.py` | Atomic file publishing, monthly partition paths, Parquet reads, scaled integer columns |
| `timeslots.py` | Slot length, month steps, date and month parsing, local time offsets |
| `locks.py` | File and folder locks |
| `zonal.py` | Area means, maxima and shares from grid cells |
| `cloud.py` | Cloud schema, valid temperature range, cloud statistics and motion, cloud rows |
| `registry/` | Boundaries, area table, grids, memberships, neighbours, BMKG points, registry checks |
| `sources/earthdata.py` | CMR search, OPeNDAP crop download, parallel batch download, gap reports |
| `sources/imerg.py`, `sources/mergir.py` | IMERG tick, backfill, fill and gaps; MERGIR backfill and gaps |
| `sources/himawari.py` | Himawari tick and fill, segment file names, decoding and regridding |
| `sources/bmkg/` | `payload.py` (shared request and parsing), `archive.py` (`bmkg tick`), `forecast.py` (`bmkg parse`) |
| `sources/gauges.py` | GHCNh and METAR import, rain-group parsing |
| `features/` | `rows.py` (feature rows in Spark), `build.py` (`features build`), `climatology.py`, `spark.py` (session) |
| `model/` | `train.py`, `score.py` (live and hindcast), `verify.py`, `scores_daily.py`, `metrics.py`, `baselines.py`, `bmkg_baseline.py`, `cards.py` |
| `gold/` | `frames.py` (map layers), `tiles.py` (rain and cloud images), `gauge_check.py` |
| `api/` | See [api.md](api.md#code) |
| `doctor.py`, `retention.py` | Environment and upstream checks; retention and disk budget |

`config/schedule.yaml` defines the runner jobs and timers. `config/` holds the settings, the retention policy, the geographic reference files and the ENSO table. `tests/` covers each module with temporary data roots.

## Web App

`web/` is a static app served by the API, with no build step: `index.html`, `style.css`, MapLibre GL JS 4.7.1 (`maplibre-gl.js`, served locally) and ES modules in `web/js/`, loaded from `js/main.js`.

| Module | Job |
|---|---|
| `main.js` | Start, routing between the seven pages, the 60-second refresh |
| `state.js` | Shared state as one object (`state.x`), saved preferences, the watch list |
| `api.js` | Requests and the data-status banner |
| `timeline.js` | The 30-min time slider and the map frames per layer |
| `format.js`, `dom.js`, `palette.js`, `charts.js` | Text and number formats, DOM helpers, map colours, SVG charts |
| `map.js`, `drawer.js` | Map layers, rain and cloud images, playback; the area panel and search |
| `overview.js`, `alerts.js`, `model.js`, `history.js`, `pipeline.js`, `settings.js` | One module per page |

`pipeline-content.js` holds the Pipeline page text, nodes and edges. Browser checks read the running page through `window.nowcast`.

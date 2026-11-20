# Operations

## Setup

Python 3.12, [uv](https://docs.astral.sh/uv/), and Java 17 or 21 for Spark.

```bash
cp .env.example .env    # then set EARTHDATA_TOKEN
uv sync
uv run nowcast doctor
```

`nowcast doctor` checks Python, Java, the data root, the token expiry and every upstream service, and prints one line per check. The Earthdata token is read only from the environment and is never printed or logged; log lines pass through a filter that removes it.

## Environment Variables

| Variable | Default | Use |
|---|---|---|
| `DATA_ROOT` | `./data` | All data; a relative path is taken from the repo root |
| `EARTHDATA_TOKEN` | none | NASA Earthdata bearer token for IMERG and MERGIR |
| `NOWCAST_CONFIG_DIR` | `<repo>/config` | Folder with `settings.yaml`, `retention.yaml`, `geo/` and `enso/` |
| `NOWCAST_WEB_DIR` | `<repo>/web` | Static web app served by the API |
| `NOWCAST_GIT_COMMIT` | from git | Commit recorded in a trained model's version and card |
| `NASA_PARALLEL` | `nasa.parallel` (8) | Parallel OPeNDAP requests, at most 16 |
| `BMKG_MAX_RPM` | `bmkg.max_rpm` (55) | BMKG requests per minute, at most 55 (BMKG allows 60) |
| `IMERG_LAG_MINUTES` | `model.imerg_lag_minutes` (360) | Overrides the feature lag; leave unset |
| `LOG_LEVEL` | `INFO` | Log level |
| `DISK_BUDGET_GB` | `budget_gb` in `retention.yaml` | Budget for the daily disk check |

`.env` in the repo root is read when it exists; variables already set win.

## Building the History Once

```bash
uv run nowcast registry boundaries
uv run nowcast registry dim-area
uv run nowcast registry membership --grid all
uv run nowcast registry neighbours
uv run nowcast registry bmkg-candidates
uv run nowcast registry bmkg-picks
uv run nowcast registry check
uv run nowcast imerg backfill --start 2016-01 --end 2026-10
uv run nowcast mergir backfill --start 2016-01 --end 2026-10
uv run nowcast features climatology
uv run nowcast features build --start 2016-01-01 --end 2026-10-31
uv run nowcast model train
```

Backfills skip months that are done and can be stopped and started again; `--force` redoes done months. One failed granule stops the month with an error, so run the command again. Do not run the IMERG and MERGIR backfills at the same time, and do not run Spark jobs and training together on a 7 GB machine.

## Live Runner

`nowcast runner` executes `config/schedule.yaml` in two serial lanes. BMKG has its own lane so slow requests do not block other jobs. The runner replaced Dagster. See [deployment](deploy.md).

## Locks

Each job holds an `flock` lock in `state/`. The kernel releases it when the process ends, so a crash never leaves a lock behind.

| Lock | Held by |
|---|---|
| `imerg.lock` | `imerg tick` (skips the run if busy), `imerg fill`, `gold tiles` |
| `imerg_backfill.lock` | `imerg backfill` |
| the `state/` folder itself | exclusive: `imerg tick` (skips if busy) and `imerg backfill` while they write rain; shared: `live score` while it checks rain and writes forecasts |
| `mergir.lock` | `mergir backfill` |
| `himawari.lock` | `himawari tick` (skips if busy), `himawari fill` around each write, `gold tiles` |
| `bmkg.lock` | `bmkg tick` (skips if busy) |
| `score.lock`, `verify.lock`, `daily.lock`, `map_frames.lock` | `live score`, `live verify`, `live daily`, `gold frames` |
| `gauge_check/.lock` | `gold gauge-check` |
| `tmp/spark/` folder | one Spark session at a time |

## Health

Each job writes `state/health/<job>.json` with its last run and counts. `GET /api/pipeline` and the Pipeline page show them with latency, storage and the recent runner runs. `nowcast imerg gaps --month YYYY-MM` and `nowcast mergir gaps --month YYYY-MM` list missing slots.

## Recovery

| Situation | Action |
|---|---|
| A job failed | Run it again. Every write is atomic and every merge is keyed by time and area, so a second run gives the same result. |
| A crash or power cut | Nothing to repair. A hidden `.atomic-*.tmp` file may remain in a dataset folder; delete it when no job runs. |
| IMERG granules arrived late | The tick looks 12 h back. For older slots run `nowcast imerg fill --start ... --end ...`. |
| Himawari scans are missing | `nowcast himawari fill --start ... --end ...` |
| Map tiles are missing | `nowcast gold tiles --rain-from ... --cloud-from ...` (needs the bronze files, kept 7 days and 6 hours) |
| Map frames need a rebuild | `nowcast gold frames --days N` |
| BMKG returns no usable run | The tick logs it and waits. A run that is still incomplete 6 h after it was first seen is closed, with its missing areas recorded. |
| The disk is full | `nowcast disk`, then `nowcast retention --dry-run` and `nowcast retention` |

## API

```bash
uv run nowcast api --host 127.0.0.1 --port 8000
```

The API only reads. DuckDB may open files only inside `DATA_ROOT`; a symlink inside it that points elsewhere is refused. Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and a Content Security Policy that allows only the API's own origin (plus `blob:` and `data:` images and `blob:` workers for the map). Restart the API after editing `config/settings.yaml`, because it caches the settings. See [api.md](api.md) for the routes.

## Checks

```bash
uv run --frozen pytest -q
uv run --frozen ruff check src tests checks
```

With an API running, `checks/` holds three browser and timing checks: `api_timing.py` (median and p95 request times), `web_check.py --url ...` (every page at desktop and phone size, console errors and failed requests, screenshots in `DATA_ROOT/tmp/web-shots/`) and `render_check.py <label> --url ...` (map rendering while the timeline plays).

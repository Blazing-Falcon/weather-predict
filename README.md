# Rain Nowcast for Indonesia

Every 30 minutes, for each of the 514 kab/kota in Indonesia, the system gives the probability that area-mean rain reaches 0.5 mm/h or more within the next 1, 2 and 3 hours. Each forecast is scored against what the satellite later observed, against BMKG's official forecast, and against simple baselines.

## Data Sources

| Source | Role |
|---|---|
| NASA GES DISC IMERG Early (GPM_3IMERGHHE V07) | Rain truth, every 30 min |
| NASA GES DISC GPM_MERGIR | Cloud-top history for training |
| Himawari-9 AHI Band 13 (NOAA on AWS) | Live cloud tops and motion |
| BMKG forecast API | Baseline to beat. Forecast data: BMKG (Badan Meteorologi, Klimatologi, dan Geofisika) |
| GHCNh (NOAA NCEI), METAR (aviationweather.gov) | Gauge check |
| geoBoundaries gbOpen IDN ADM2 (CC BY 3.0 IGO), wilayah.id | Boundaries and Kemendagri codes |

## Setup

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and Java 17 or 21 for Spark.

```bash
cp .env.example .env    # then set EARTHDATA_TOKEN
uv sync
uv run nowcast doctor
```

`DATA_ROOT` (default `./data`) holds all data in `ref/`, `bronze/`, `silver/`, `gold/`, `models/`, `state/` and `tmp/`. It is never committed. The Earthdata token is read only from the environment and is never printed or logged.

## Commands to Know

```bash
uv run nowcast --help        # the command tree
uv run nowcast imerg tick    # archive new IMERG rain
uv run nowcast himawari tick # archive new Himawari scans
uv run nowcast live score    # issue forecasts for new slots
uv run nowcast api           # serve the API and the web app on 127.0.0.1:8000
```

## Layout

```
src/nowcast/   the package and the `nowcast` command
orchestration/ Dagster assets, sensors and schedules
config/        settings.yaml, retention.yaml, geographic reference files, ENSO table
web/           the web app (ES modules, no build step)
checks/        API timing, web and map render checks against a running server
tests/
docs/          how the system works
```

## Docs

- [Architecture](docs/architecture.md): data flow, live cycle, code map
- [Data](docs/data.md): every dataset, its layout, columns and retention
- [Model](docs/model.md): target, features, training, baselines, results
- [Operations](docs/operations.md): setup, environment, schedules, locks, recovery
- [API](docs/api.md): routes and responses

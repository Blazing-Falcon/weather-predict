# Rain Nowcast for Indonesia

Final project for Analisis Big Data (Informatika, Universitas Muhammadiyah Malang). Every 30 minutes, for each of the 514 kab/kota in Indonesia, the system gives the probability that area-mean rain reaches 0.5 mm/h or more within the next 1, 2 and 3 hours. Each forecast is scored against what the satellite later observed, against BMKG's official forecast, and against simple baselines.

Status: early build. See `docs/progress/` for one report per step and `docs/experiments/` for the experiments behind the choices.

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

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and, from the Spark step on, Java 17 or 21.

```bash
cp .env.example .env    # then set EARTHDATA_TOKEN
uv sync
uv run nowcast doctor
```

`DATA_ROOT` (default `./data`) holds all data in `ref/`, `bronze/`, `silver/`, `gold/`, `models/`, `state/` and `tmp/`. It is never committed. The Earthdata token is read only from the environment and is never printed or logged.

## Layout

```
config/        settings.yaml (source constants), retention.yaml (window per dataset)
src/nowcast/   package and the `nowcast` command
experiments/   one-off scripts; findings in docs/experiments/
docs/          progress reports, experiment notes, decision log
tests/
```

# Deployment

The runner replaces Dagster. The image runs no Spark, Java or training. Two serial lanes run the jobs in `config/schedule.yaml`: the fast lane handles ingest, scoring, verification, maps, METAR and housekeeping; the other handles BMKG. Interval jobs start immediately, then skip missed intervals. Daily jobs wait until their UTC time. Each job uses a fresh child process. Runs appear on the Pipeline page.

The runner is limited to 1500 MB and the API to 700 MB, with one API worker and a 256 MB DuckDB limit. Each service has a 128 MB `/tmp` tmpfs, charged to its memory limit.

Measured on dev over 12 minutes with two Himawari decodes and one scoring run: the runner used 140 MB on average and peaked at 706 MB; the API used 193 MB on average and peaked at 236 MB. The largest single job is a Himawari decode (about 870 MB). The worst case, a decode during a BMKG run plus the API, is about 1.5 GB, which leaves room on a 4 GB host.

## Prepare and Start

Install Docker Engine and Compose on Linux. Copy this repository and prepare the data folder before starting. The container UID and GID are 1000; that user must be able to write the data folder. Copy the dev machine's `ref/`, `models/` and `gold/climatology/` into it. Keep the model's required live feature reference tables and climatology together. Fresh live observations accumulate after startup; scoring waits for its required inputs.

Create `.env` with `EARTHDATA_TOKEN`. Optional names are `NOWCAST_DATA` (default `./data`), `API_BIND` (default `127.0.0.1`), and `API_PORT` (default `8000`). Use an absolute path for an external data folder. Only the runner receives the env file; the API mounts data read-only.

```bash
mkdir -p data
sudo chown -R 1000:1000 data
docker compose up -d
docker compose logs -f runner api
docker compose stop
```

The API serves the web app at `http://127.0.0.1:8000`. Public access is the host's job: point its reverse proxy or tunnel at that address, or set `API_BIND` to expose the port directly.

## Train and Update

Train on the dev machine with `uv sync` and the feature, climatology and training commands in [operations](operations.md). Copy `DATA_ROOT/models/` to the server's data folder, preserving version subfolders. Set `model.live_version` in `config/settings.yaml` to the copied version. Copy matching reference tables and climatology when these change. Rebuild after config or code changes:

```bash
docker compose up -d --build
docker image prune -f
```

Run history is stored in `state/runs.jsonl` and trimmed at 2 MB. Container logs rotate at three 10 MB files per service. Data retention runs daily at 01:30 UTC; the disk check runs at 01:45 UTC.

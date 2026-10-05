# Step 0: Repo and Dev Environment

Date: 2026-11-19. Status: done, awaiting review.

## What Was Built

- uv project `nowcast` (Python 3.12 only, exact pins, `uv.lock`) with the `nowcast` command (typer).
- `config/settings.yaml` and `config/retention.yaml` from the planning templates, `.env.example`, and `.gitignore` (ignores `.env` and `data/`).
- `nowcast.config` (YAML settings, `.env` loading, real env vars win), `nowcast.paths` (`DATA_ROOT` and the seven zones), `nowcast.log` (UTC log lines, token and `Bearer` values scrubbed from the final text), `nowcast.http` (timeouts, retry on transport errors, 429 and 5xx).
- Experiment 00: one probe per source (`experiments/00_source_probe.py`, note in `docs/experiments/00-source-probe.md`).
- `nowcast doctor`: Python, Java, `DATA_ROOT`, token set/missing with days to expiry, and one request to each of 8 sources.

## Dev Machine

8 cores, 7 GB RAM, 42 GB free disk, Python 3.12.3, uv 0.13.9, OpenJDK 21.0.13, Docker. The RAM is enough for the sample month on dev. It is below the 16 GB minimum planned for the server.

## Done When

`uv sync` then `uv run nowcast doctor`, from a deleted `.venv`, at 2026-11-19 about 21:56 UTC:

```
PASS  python           3.12.3
PASS  java             21.0.13.1
PASS  data_root        /home/juan/hermes_project/weather/data
PASS  earthdata_token  set (expires in 59 days)
PASS  bmkg             HTTP 200, 1 items (2054 ms)
PASS  cmr              HTTP 200, found an IMERG granule in the last 12 h (1623 ms)
PASS  gesdisc          HTTP 200, 56944 bytes (4237 ms)
PASS  himawari         B13 at 21:40 UTC (3583 ms)
PASS  ghcnh            HTTP 200 (1848 ms)
PASS  metar            HTTP 200, 12 items (12061 ms)
PASS  geoboundaries    HTTP 200 (1668 ms)
PASS  wilayah          HTTP 200, 38 items (1132 ms)
12 pass, 0 warn, 0 fail
exit=0
```

`git grep` for the token value over every commit found nothing. Only variable names match `EARTHDATA_TOKEN` (`.env.example`, `src/nowcast/{doctor,http,log}.py`, tests with dummy values). A local pre-commit hook also blocks `.env` and the token value.

Tests: `uv run pytest -q` gives `22 passed`.

## Measured Facts (Experiment 00)

| Item | Planned | Measured 2026-11-19 |
|---|---|---|
| IMERG Early lag after slot end | about 5 h | 292 min, version `V07C` |
| MERGIR lag | about 45 h | 46.9 h |
| Himawari B13 lag after scan start | about 12 min | 12.4 min |
| B13 segments 05 to 07 per scan | about 9.1 MB | 9.04 MB |
| BMKG run seen | 00 and 12 UTC expected | 12 UTC only so far |
| MERGIR OPeNDAP URL | `GPM_MERGIR.1%3Amerg_...` | CMR title has a literal `:`; it must be sent as `%3A` |

## Differences From the Plan

See `docs/decision-log.md`: no `AGENTS.md`, the planning folder is not tracked, dependencies arrive per step, `DATA_ROOT` defaults to `./data`. `nowcast doctor` reports Java as WARN, not FAIL, when it is missing, because Java is needed only from the Spark step.

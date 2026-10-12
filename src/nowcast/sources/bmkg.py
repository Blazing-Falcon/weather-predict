import fcntl
import gzip
import json
import os
import zlib
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import typer

from nowcast import config, http, paths
from nowcast.http import rate_limit
from nowcast.log import get_logger


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def compact_stamp(timestamp: str) -> str:
    return timestamp.replace("-", "").replace(":", "")


def save_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(temp, path)


def analysis_date(line: dict) -> str | None:
    if line["http_status"] != 200:
        return None
    try:
        payload = json.loads(line["body"])
        value = next(
            (
                step["analysis_date"]
                for point in payload.get("data", [])
                for group in point.get("cuaca", [])
                for step in group
                if step.get("analysis_date")
            ),
            None,
        )
        if value:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def response_line(response: httpx.Response, area_id: int, adm4: str) -> dict:
    response.read()
    return {
        "area_id": area_id,
        "adm4": adm4,
        "fetched_at": utc_now(),
        "http_status": response.status_code,
        "body": response.text,
    }


def fetch(client: httpx.Client, url: str, adm4: str) -> httpx.Response | None:
    try:
        return http.get(client, url, params={"adm4": adm4})
    except httpx.RequestError as error:
        get_logger(__name__).warning("BMKG request for %s failed: %s", adm4, type(error).__name__)
        return None


def credit(progress: dict, area_id: int) -> None:
    if area_id not in progress["done"]:
        progress["done"].append(area_id)
    if area_id in progress["missing"]:
        progress["missing"].remove(area_id)


def recover_chunk(directory: Path, runs: dict) -> None:
    # The last published chunk may have reached disk before its state checkpoint.
    latest = max(
        directory.glob("sweep=*.jsonl.gz"), key=lambda path: path.stat().st_mtime_ns, default=None
    )
    if latest is None:
        return
    try:
        with gzip.open(latest, "rt", encoding="utf-8") as source:
            lines = [json.loads(line) for line in source]
    except (OSError, EOFError, ValueError, zlib.error):
        get_logger(__name__).warning("BMKG could not read bronze chunk %s", latest.name)
        return
    for line in lines:
        run = analysis_date(line)
        if run in runs:
            first_seen = datetime.fromisoformat(runs[run]["first_seen"])
            if first_seen <= datetime.fromisoformat(line["fetched_at"]):
                credit(runs[run], line["area_id"])


def sweep(
    client: httpx.Client,
    url: str,
    picks: dict[int, str],
    run: str,
    state: dict,
    state_path: Path,
    directory: Path,
) -> tuple[int, int, int, int]:
    runs = state["runs"]
    progress = runs[run]
    done = set(progress["done"])
    directory.mkdir(parents=True, exist_ok=True)
    progress["last_sweep"] = utc_now()
    output = None
    chunk_number = lines = ok = failed = 0
    rows_written = bytes_written = 0
    chunk_done = {}

    def close_chunk() -> None:
        nonlocal output, lines, rows_written, bytes_written
        files.close()
        os.replace(temp, path)
        rows_written += lines
        bytes_written += path.stat().st_size
        for response_run, area_ids in chunk_done.items():
            credited = runs[response_run]
            for area_id in area_ids:
                credit(credited, area_id)
            if picks.keys() <= set(credited["done"]):
                credited["complete"] = True
        save_json(state_path, state)
        chunk_done.clear()
        output = None
        lines = 0

    def archive(response: httpx.Response) -> None:
        nonlocal output, path, temp, stamp, chunk_number, lines, ok
        line = response_line(response, area_id, adm4)
        if output is None:
            if chunk_number == 0:
                stamp = compact_stamp(line["fetched_at"])
            chunk_number += 1
            path = directory / f"sweep={stamp}-{chunk_number}.jsonl.gz"
            temp = path.with_suffix(".tmp")
            output = files.enter_context(gzip.open(temp, "wt", encoding="utf-8"))  # noqa: SIM115
        output.write(json.dumps(line, ensure_ascii=False) + "\n")
        response_run = analysis_date(line)
        if response_run in runs:
            chunk_done.setdefault(response_run, []).append(area_id)
            ok += response_run == run
        lines += 1
        if lines == 50:
            close_chunk()

    path: Path
    temp: Path
    stamp: str
    pending = ((area_id, adm4) for area_id, adm4 in picks.items() if area_id not in done)
    with ExitStack() as files:
        client.event_hooks["response"].append(archive)
        try:
            for index, (area_id, adm4) in enumerate(pending, start=1):
                before = ok
                fetch(client, url, adm4)
                failed += ok == before
                if index % 50 == 0:
                    save_json(state_path, state)
            if output is not None:
                close_chunk()
        finally:
            client.event_hooks["response"].remove(archive)

    save_json(state_path, state)
    return ok, failed, rows_written, bytes_written


def tick() -> dict:
    """Capture live BMKG forecast runs and retry missing points."""
    root = paths.ensure_layout()
    state_path = root / "state" / "bmkg.json"
    bronze = root / "bronze" / "bmkg"
    logger = get_logger(__name__)
    report = {"rows": 0, "bytes": 0, "latency_min": None}

    with (root / "state" / "bmkg.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("BMKG tick already running")
            return report

        settings = config.settings()["bmkg"]
        try:
            rpm = int(config.env("BMKG_MAX_RPM", str(settings["max_rpm"])))
            if rpm < 1:
                raise ValueError
        except ValueError:
            raise typer.BadParameter("BMKG_MAX_RPM must be a positive integer.") from None

        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            state = {"runs": {}}
            save_json(state_path, state)
        runs = state["runs"]

        rows = pq.read_table(
            root / "ref" / "dim_area.parquet",
            columns=["area_id", "kemendagri_code", "bmkg_adm4"],
            filters=[("is_admin", "=", True)],
        ).to_pylist()
        by_code = {row["kemendagri_code"]: row for row in rows}
        picks = {row["area_id"]: row["bmkg_adm4"] for row in rows if row["bmkg_adm4"] is not None}
        if skipped := sorted(row["area_id"] for row in rows if row["bmkg_adm4"] is None):
            logger.warning("BMKG null picks skipped for areas %s", skipped)

        ok = failed = 0
        with http.client() as client:
            client.event_hooks["request"].append(rate_limit(60 / min(rpm, 55)))
            for code in settings["sentinels"]:
                row = by_code[code]
                adm4 = row["bmkg_adm4"]
                if adm4 is None or (response := fetch(client, settings["url"], adm4)) is None:
                    continue
                line = response_line(response, row["area_id"], adm4)
                run = analysis_date(line)
                if run is None:
                    logger.warning(
                        "BMKG sentinel %s has no usable run (HTTP %d)", code, line["http_status"]
                    )
                    continue
                if run not in runs:
                    fetched_at = line["fetched_at"]
                    directory = bronze / f"run={run[:13]}"
                    directory.mkdir(parents=True, exist_ok=True)
                    stamp = compact_stamp(fetched_at)
                    path = directory / f"sentinel={stamp}.jsonl.gz"
                    with gzip.open(path, "wt", encoding="utf-8") as output:
                        output.write(json.dumps(line, ensure_ascii=False) + "\n")
                    report["rows"] += 1
                    report["bytes"] += path.stat().st_size
                    report["latency_min"] = (
                        datetime.fromisoformat(fetched_at) - datetime.fromisoformat(run)
                    ).total_seconds() / 60
                    runs[run] = {
                        "first_seen": fetched_at,
                        "done": [],
                        "missing": [],
                        "complete": False,
                        "last_sweep": None,
                    }
                    save_json(state_path, state)

            cutoff = datetime.fromisoformat(utc_now()) - timedelta(
                hours=settings["retry_missing_hours"]
            )
            newest = max(runs, key=datetime.fromisoformat, default=None)
            for hour in {run[:13] for run, progress in runs.items() if not progress["complete"]}:
                recover_chunk(bronze / f"run={hour}", runs)
            for run, progress in runs.items():
                if progress["complete"]:
                    continue
                missing = sorted(picks.keys() - set(progress["done"]))
                if not missing:
                    progress["complete"] = True
                    continue
                if datetime.fromisoformat(progress["first_seen"]) >= cutoff:
                    continue
                progress.update(missing=missing, complete=True)
                logger.warning("BMKG run %s expired; missing areas %s", run, missing)
            save_json(state_path, state)

            if newest is not None and not runs[newest]["complete"]:
                ok, failed, rows_written, bytes_written = sweep(
                    client,
                    settings["url"],
                    picks,
                    newest,
                    state,
                    state_path,
                    bronze / f"run={newest[:13]}",
                )
                report["rows"] += rows_written
                report["bytes"] += bytes_written

        save_json(
            root / "state" / "health" / "bmkg.json",
            {
                "last_tick": utc_now(),
                "last_run": newest,
                "points_ok": ok,
                "points_failed": failed,
                "runs_incomplete": sum(not progress["complete"] for progress in runs.values()),
            },
        )
    return report

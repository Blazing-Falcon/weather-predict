import gzip
import json
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq

from nowcast import config, http, paths
from nowcast.http import rate_limit
from nowcast.locks import file_lock
from nowcast.log import get_logger
from nowcast.sources.bmkg import payload
from nowcast.sources.bmkg.payload import fetch, request_rate
from nowcast.storage import atomic_write, write_json
from nowcast.timeslots import utc_now


def compact_stamp(timestamp: str) -> str:
    return timestamp.replace("-", "").replace(":", "")


def analysis_date(line: dict) -> str | None:
    if line["http_status"] != 200:
        return None
    try:
        return payload.analysis_date(json.loads(line["body"]))
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


def write_chunk(path: Path, lines: list[dict], picks: dict, state: dict, state_path: Path) -> int:
    with atomic_write(path) as file, gzip.open(file, "wt", encoding="utf-8") as output:
        for line in lines:
            output.write(json.dumps(line, ensure_ascii=False) + "\n")
    for line in lines:
        response_run = analysis_date(line)
        if response_run in state["runs"]:
            credited = state["runs"][response_run]
            credit(credited, line["area_id"])
            if picks.keys() <= set(credited["done"]):
                credited["complete"] = True
    write_json(state_path, state, indent=None)
    return path.stat().st_size


def fetch_lines(client: httpx.Client, url: str, area_id: int, adm4: str) -> list[dict]:
    lines = []
    # Capture retries at response time so their bodies and timestamps stay in the archive.
    capture = lambda response: lines.append(response_line(response, area_id, adm4))
    client.event_hooks["response"].append(capture)
    try:
        fetch(client, url, adm4)
    finally:
        client.event_hooks["response"].remove(capture)
    return lines


def sweep(
    client: httpx.Client,
    url: str,
    picks: dict[int, str],
    run: str,
    state: dict,
    state_path: Path,
    directory: Path,
) -> tuple[int, int, int, int]:
    progress = state["runs"][run]
    done = set(progress["done"])
    directory.mkdir(parents=True, exist_ok=True)
    progress["last_sweep"] = utc_now()
    chunk = []
    stamp = None
    chunk_number = ok = failed = rows_written = bytes_written = 0
    pending = ((area_id, adm4) for area_id, adm4 in picks.items() if area_id not in done)
    for index, (area_id, adm4) in enumerate(pending, start=1):
        before = ok
        for line in fetch_lines(client, url, area_id, adm4):
            if stamp is None:
                stamp = compact_stamp(line["fetched_at"])
            ok += analysis_date(line) == run
            chunk.append(line)
            if len(chunk) == 50:
                chunk_number += 1
                path = directory / f"sweep={stamp}-{chunk_number}.jsonl.gz"
                bytes_written += write_chunk(path, chunk, picks, state, state_path)
                rows_written += len(chunk)
                chunk.clear()
        failed += ok == before
        if index % 50 == 0:
            write_json(state_path, state, indent=None)
    if chunk:
        path = directory / f"sweep={stamp}-{chunk_number + 1}.jsonl.gz"
        bytes_written += write_chunk(path, chunk, picks, state, state_path)
        rows_written += len(chunk)
    write_json(state_path, state, indent=None)
    return ok, failed, rows_written, bytes_written


def read_picks(root: Path) -> tuple[dict, dict[int, str]]:
    rows = pq.read_table(
        root / "ref" / "dim_area.parquet",
        columns=["area_id", "kemendagri_code", "bmkg_adm4"],
        filters=[("is_admin", "=", True)],
    ).to_pylist()
    by_code = {row["kemendagri_code"]: row for row in rows}
    picks = {row["area_id"]: row["bmkg_adm4"] for row in rows if row["bmkg_adm4"] is not None}
    if skipped := sorted(row["area_id"] for row in rows if row["bmkg_adm4"] is None):
        get_logger(__name__).warning("BMKG null picks skipped for areas %s", skipped)
    return by_code, picks


def check_sentinels(client, settings: dict, by_code: dict, state: dict, state_path: Path, bronze: Path):
    report = {"rows": 0, "bytes": 0, "latency_min": None}
    runs = state["runs"]
    for code in settings["sentinels"]:
        row = by_code[code]
        adm4 = row["bmkg_adm4"]
        if adm4 is None or (response := fetch(client, settings["url"], adm4)) is None:
            continue
        line = response_line(response, row["area_id"], adm4)
        run = analysis_date(line)
        if run is None:
            get_logger(__name__).warning(
                "BMKG sentinel %s has no usable run (HTTP %d)", code, line["http_status"]
            )
            continue
        if run not in runs:
            fetched_at = line["fetched_at"]
            directory = bronze / f"run={run[:13]}"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = compact_stamp(fetched_at)
            path = directory / f"sentinel={stamp}.jsonl.gz"
            with atomic_write(path) as file, gzip.open(file, "wt", encoding="utf-8") as output:
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
            write_json(state_path, state, indent=None)
    return report


def recover_runs(bronze: Path, runs: dict) -> None:
    for hour in {run[:13] for run, progress in runs.items() if not progress["complete"]}:
        recover_chunk(bronze / f"run={hour}", runs)


def expire_runs(runs: dict, picks: dict, cutoff: datetime) -> None:
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
        get_logger(__name__).warning("BMKG run %s expired; missing areas %s", run, missing)


def write_health(root: Path, runs: dict, newest: str | None, ok: int, failed: int) -> None:
    write_json(root / "state" / "health" / "bmkg.json", {
        "last_tick": utc_now(),
        "last_run": newest,
        "points_ok": ok,
        "points_failed": failed,
        "runs_incomplete": sum(not progress["complete"] for progress in runs.values()),
    }, indent=None)


def tick() -> dict:
    """Capture live BMKG forecast runs and retry missing points."""
    root = paths.ensure_layout()
    state_path = root / "state" / "bmkg.json"
    bronze = root / "bronze" / "bmkg"
    report = {"rows": 0, "bytes": 0, "latency_min": None}
    with file_lock(root / "state", "bmkg.lock", blocking=False) as acquired:
        if not acquired:
            get_logger(__name__).info("BMKG tick already running")
            return report
        settings = config.settings()["bmkg"]
        rpm = request_rate()
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            state = {"runs": {}}
            write_json(state_path, state, indent=None)
        runs = state["runs"]
        by_code, picks = read_picks(root)
        ok = failed = 0
        with http.client() as client:
            client.event_hooks["request"].append(rate_limit(60 / rpm))
            report = check_sentinels(client, settings, by_code, state, state_path, bronze)
            cutoff = datetime.fromisoformat(utc_now()) - timedelta(
                hours=settings["retry_missing_hours"]
            )
            newest = max(runs, key=datetime.fromisoformat, default=None)
            recover_runs(bronze, runs)
            expire_runs(runs, picks, cutoff)
            write_json(state_path, state, indent=None)
            if newest is not None and not runs[newest]["complete"]:
                ok, failed, rows_written, bytes_written = sweep(
                    client, settings["url"], picks, newest, state, state_path,
                    bronze / f"run={newest[:13]}",
                )
                report["rows"] += rows_written
                report["bytes"] += bytes_written
        write_health(root, runs, newest, ok, failed)
    return report

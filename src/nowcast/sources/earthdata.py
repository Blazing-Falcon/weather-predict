import io
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from urllib.parse import quote

import httpx
import typer

from nowcast import config, http, paths
from nowcast.log import get_logger, redact
from nowcast.storage import month_path, write_bytes, write_json
from nowcast.timeslots import SLOT, next_month, parse_month, slot_range


class EarthdataForbidden(RuntimeError):
    pass


def parallel_requests() -> int:
    parallel = int(config.env("NASA_PARALLEL", str(config.settings()["nasa"]["parallel"])))
    if parallel < 1:
        raise ValueError("NASA_PARALLEL must be positive")
    return min(parallel, 16)


def granules(collection: str, start: datetime, end: datetime) -> list[dict]:
    bounds = [dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for dt in (start, end)]
    params = {
        "collection_concept_id": collection,
        "temporal": ",".join(bounds),
        "sort_key": "start_date",
        "page_size": 2000,
    }
    url = config.settings()["cmr"]["granules"]
    header = "CMR-Search-After"
    headers = {}
    results = []
    with http.client() as client:
        while True:
            response = http.get(client, url, params=params, headers=headers).raise_for_status()
            entries = response.json()["feed"]["entry"]
            if not entries:
                return results
            results.extend(
                {key: entry[key] for key in ("title", "time_start", "time_end")}
                for entry in entries
            )
            if header not in response.headers:
                return results
            headers = {header: response.headers[header]}


def latest_granule(
    collection: str, start: datetime | None = None, end: datetime | None = None
) -> dict | None:
    params = {
        "collection_concept_id": collection,
        "sort_key": "-start_date",
        "page_size": 1,
    }
    if start is not None and end is not None:
        bounds = [dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for dt in (start, end)]
        params["temporal"] = ",".join(bounds)
    with http.client() as client:
        response = http.get(
            client, config.settings()["cmr"]["granules"], params=params
        ).raise_for_status()
        entries = response.json()["feed"]["entry"]
    return entries[0] if entries else None


def dap4_url(
    source: dict, title: str, constraint: str, *, title_safe: str = "", constraint_safe: str = "/:"
) -> str:
    return (
        f"{source['opendap']}{quote(title, safe=title_safe)}.dap.nc4?dap4.ce="
        + quote(constraint, safe=constraint_safe)
    )


def download_crop(
    client: httpx.Client, source: dict, title: str, constraint: str, path: Path,
    reader: Callable, *, title_safe: str = "",
) -> int:
    if path.is_file():
        try:
            reader(path)
        except (OSError, KeyError, ValueError):
            pass
        else:
            return 0
    response = http.get(client, dap4_url(source, title, constraint, title_safe=title_safe))
    if response.status_code == 403:
        raise EarthdataForbidden("Earthdata 403: EULA not accepted or token invalid")
    response.raise_for_status()
    content = response.content
    reader(io.BytesIO(content))
    write_bytes(path, content)
    return len(content)


def download_batch(
    granules: list, bronze: Path, parallel: int, product: str, fetch: Callable,
    *, progress: dict | None = None, verify_first: Callable | None = None,
) -> None:
    bronze.mkdir(parents=True, exist_ok=True)
    logger = get_logger(__name__)
    stopped = Event()
    if progress is None:
        progress = {"downloaded": 0, "failed": 0, "bytes": 0}
    with http.client(auth=True) as client, ThreadPoolExecutor(max_workers=parallel) as pool:
        remaining = granules
        completed_first = 0
        if verify_first is not None:
            first, *remaining = granules
            fetch(client, first)
            verify_first(first)
            completed_first = 1

        def download(granule):
            if stopped.is_set():
                return 0
            try:
                return fetch(client, granule)
            except EarthdataForbidden:
                stopped.set()
                raise

        futures = {pool.submit(download, granule): granule.title for granule in remaining}
        for completed, future in enumerate(as_completed(futures), start=completed_first + 1):
            try:
                size = future.result()
            except (httpx.HTTPError, OSError, KeyError, ValueError, EarthdataForbidden) as error:
                progress["failed"] += 1
                logger.error(
                    "%s download failed for %s: %s", product, futures[future], redact(str(error))
                )
            else:
                progress["downloaded"] += int(size > 0)
                progress["bytes"] += size
            if verify_first is not None and completed % 100 == 0 and not stopped.is_set():
                logger.info("%s crops checked: %d/%d", product, completed, len(granules))
    if stopped.is_set():
        raise EarthdataForbidden("Earthdata 403: EULA not accepted or token invalid")
    if progress["failed"]:
        raise RuntimeError(f"{progress['failed']} {product} downloads failed")


def month_bounds(start: str, end: str) -> tuple[datetime, datetime]:
    if not all(re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) for month in (start, end)):
        raise ValueError("Month bounds must use YYYY-MM")
    first, last = parse_month(start), parse_month(end)
    if first > last:
        raise ValueError("--start must be on or before --end")
    return first, last


def gap_month(month: str) -> datetime:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("--month must use YYYY-MM")
    return parse_month(month)


def archive_months(months, state: dict, force: bool, product: str):
    """Yield pending months and remove crops after each completed or skipped month."""
    for month, following in months:
        label = month.strftime("%Y-%m")
        if state.get(label, {}).get("done") and not force:
            get_logger(__name__).info("%s %s already done, skipping", product, label)
        else:
            yield month, following
        bronze = month_path(paths.zone("bronze") / product.lower(), month)
        for crop in bronze.glob("*.nc4*"):
            crop.unlink()


def gap_report(month: str, counts: dict, now: datetime) -> dict:
    start = parse_month(month)
    end = next_month(start)
    if month == now.strftime("%Y-%m"):
        end = max(counts) + SLOT if counts else start
    expected = slot_range(start, end)
    missing = [slot for slot in expected if slot not in counts]
    runs = []
    for slot in missing:
        if runs and slot == runs[-1]["end"] + SLOT:
            runs[-1]["end"] = slot
            runs[-1]["slots"] += 1
        else:
            runs.append({"start": slot, "end": slot, "slots": 1})
    for run in runs:
        run["start"] = run["start"].isoformat()
        run["end"] = run["end"].isoformat()
    return {
        "month": month,
        "expected_slots": len(expected),
        "present_slots": len(counts),
        "missing_slots": [slot.isoformat() for slot in missing],
        "missing_runs": runs,
        "rows_per_slot_min": min(counts.values(), default=None),
        "rows_per_slot_max": max(counts.values(), default=None),
    }


def write_gap_report(product: str, month: str, report: dict) -> None:
    output = paths.zone("state") / f"{product}_gaps"
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / f"{month}.json", report)
    typer.echo(json.dumps(report, indent=2))

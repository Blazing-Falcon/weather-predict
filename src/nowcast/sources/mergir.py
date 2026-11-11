import io
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Annotated
from urllib.parse import quote

import cv2
import h5py
import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import typer
from numpy.typing import NDArray

from nowcast import config, http, paths
from nowcast.cloud import CLOUD_PARTS, CloudMotion, cloud_table, features, valid_tb
from nowcast.locks import file_lock
from nowcast.log import get_logger, redact
from nowcast.registry.grids import Grid, mergir_grid
from nowcast.sources import cmr
from nowcast.sources.imerg import EarthdataForbidden, _parallel
from nowcast.storage import atomic_write, month_path, write_bytes, write_json, write_table
from nowcast.timeslots import HOUR, SLOT, UTC_US, next_month, parse_month, slot_range
from nowcast.zonal import Zonal

MANIFEST_SCHEMA = pa.schema([
    ("name", pa.string()),
    ("fetched_at", UTC_US),
    ("bytes", pa.int64()),
    ("frames", pa.uint8()),
])


@dataclass(frozen=True)
class Granule:
    slot: datetime
    title: str


def select_granules(entries: list[dict], start: datetime, end: datetime) -> list[Granule]:
    granules = {}
    for entry in entries:
        title = entry["title"]
        match = re.fullmatch(r"GPM_MERGIR\.1:merg_(\d{10})_4km-pixel\.nc4", title)
        if not match:
            raise ValueError(f"Unexpected MERGIR title: {title}")
        slot = datetime.strptime(match[1], "%Y%m%d%H").replace(tzinfo=UTC)
        if start <= slot < end:
            granules[slot] = Granule(slot, title)
    return [granules[slot] for slot in sorted(granules)]


def _verify_time(file: h5py.File, slot: datetime) -> None:
    def value(raw: str | bytes) -> str:
        return raw.decode() if isinstance(raw, bytes) else raw

    dataset = file.get("time")
    expected = [slot, slot + SLOT]
    if dataset is not None and not value(dataset.attrs.get("NAME", "")).startswith(
        "This is a netCDF dimension but not a netCDF variable."
    ):
        units = value(dataset.attrs.get("units", ""))
        values = dataset[:].tolist()
        get_logger(__name__).info("MERGIR time coordinate: %s %s", values, units)
        unit, _, epoch = units.partition(" since ")
        if unit not in {"days", "hours", "minutes", "seconds"} or not epoch:
            raise ValueError(f"Unsupported MERGIR time units: {units!r}")
        origin = datetime.fromisoformat(epoch)
        if origin.tzinfo is None:
            origin = origin.replace(tzinfo=UTC)
        if [origin + timedelta(**{unit: offset}) for offset in values] == expected:
            return
    else:
        get_logger(__name__).info(
            "MERGIR time coordinate: %s", "NetCDF dimension placeholder" if dataset else "absent"
        )
        attrs = file.attrs
        bounds = [
            value(attrs.get(f"{key}Date", "")) + "T" + value(attrs.get(f"{key}Time", ""))
            for key in ("Begin", "End")
        ]
        get_logger(__name__).info("MERGIR time coverage: %s to %s", *bounds)
        if bounds != ["T", "T"]:
            begin, end = map(datetime.fromisoformat, bounds)
            if begin == slot and slot + SLOT <= end <= slot + HOUR:
                return
    raise ValueError(f"MERGIR time metadata disagrees with HH:00/HH:30 for {slot.isoformat()}")


def read_crop(
    path: Path | io.BytesIO, grid: Grid, *, verify_slot: datetime | None = None
) -> NDArray:
    with h5py.File(path, "r") as file:
        dataset = file[config.settings()["mergir"]["variable"]]
        if dataset.shape != (2, grid.height, grid.width) or dataset.dtype != np.int16:
            raise ValueError(f"Unexpected MERGIR crop: shape {dataset.shape}, dtype {dataset.dtype}")
        if verify_slot is not None:
            _verify_time(file, verify_slot)
        return dataset[:]


def download(client: httpx.Client, granule: Granule, bronze: Path, grid: Grid) -> int:
    path = bronze / granule.title
    if path.is_file():
        try:
            read_crop(path, grid)
        except (OSError, KeyError, ValueError):
            pass
        else:
            return 0

    source = config.settings()["mergir"]
    lat, lon = source["lat_index"], source["lon_index"]
    constraint = f"{source['variable']}[0:1:1][{lat[0]}:1:{lat[1]}][{lon[0]}:1:{lon[1]}]"
    url = f"{source['opendap']}{quote(granule.title, safe='')}.dap.nc4?dap4.ce="
    response = http.get(client, url + quote(constraint, safe="/:"))
    if response.status_code == 403:
        raise EarthdataForbidden("Earthdata 403: EULA not accepted or token invalid")
    response.raise_for_status()
    content = response.content
    read_crop(io.BytesIO(content), grid)
    write_bytes(path, content)
    return len(content)


def download_month(granules: list[Granule], bronze: Path, grid: Grid, parallel: int) -> None:
    if not granules:
        return
    bronze.mkdir(parents=True, exist_ok=True)
    logger = get_logger(__name__)
    stopped = Event()
    failed = 0
    with http.client(auth=True) as client, ThreadPoolExecutor(max_workers=parallel) as pool:
        first, *remaining = granules
        download(client, first, bronze, grid)
        read_crop(bronze / first.title, grid, verify_slot=first.slot)

        def fetch(granule: Granule) -> None:
            if stopped.is_set():
                return
            try:
                download(client, granule, bronze, grid)
            except EarthdataForbidden:
                stopped.set()
                raise

        futures = {pool.submit(fetch, granule): granule.title for granule in remaining}
        for completed, future in enumerate(as_completed(futures), start=2):
            try:
                future.result()
            except (httpx.HTTPError, OSError, KeyError, ValueError, EarthdataForbidden) as error:
                failed += 1
                logger.error("MERGIR download failed for %s: %s", futures[future], redact(str(error)))
            if completed % 100 == 0 and not stopped.is_set():
                logger.info("MERGIR crops checked: %d/%d", completed, len(granules))
    if stopped.is_set():
        raise EarthdataForbidden("Earthdata 403: EULA not accepted or token invalid")
    if failed:
        raise RuntimeError(f"{failed} MERGIR downloads failed")


def write_month(
    granules: list[Granule],
    bronze: Path,
    silver: Path,
    grid: Grid,
    zonal: Zonal,
    motion: CloudMotion,
    previous: tuple[datetime, NDArray] | None = None,
) -> tuple[dict, tuple[datetime, NDArray] | None]:
    slots = [slot for g in granules for slot in (g.slot, g.slot + SLOT)]
    assert slots == sorted(set(slots)) and np.all(np.diff(zonal.area_ids.astype(int)) > 0)
    areas, frames = len(zonal.area_ids), len(slots)
    metrics = np.full((7, areas, frames), np.nan)
    cold_k = config.settings()["mergir"]["cold_k"]
    previous_image = motion.image(previous[1], valid_tb(previous[1])) if previous else None
    previous_slot = previous[0] if previous else None
    manifest = []
    cold_speeds = []
    invalid = 0
    for index, granule in enumerate(granules):
        crop = bronze / granule.title
        stat = crop.stat()
        manifest.append({
            "name": crop.name, "fetched_at": datetime.fromtimestamp(stat.st_mtime, UTC),
            "bytes": stat.st_size, "frames": 2,
        })
        for offset, tb in enumerate(read_crop(crop, grid)):
            column = 2 * index + offset
            slot = slots[column]
            valid = valid_tb(tb)
            invalid += tb.size - np.count_nonzero(valid)
            metrics[:3, :, column] = features(tb, valid, cold_k, zonal)
            image = motion.image(tb, valid)
            if (
                previous_slot == slot - SLOT
                and previous_image is not None
                and image is not None
            ):
                metrics[3:, :, column], speed = motion.features(
                    previous_image, image, valid & (tb < cold_k), zonal
                )
                if speed is not None:
                    cold_speeds.append(speed)
            previous, previous_slot, previous_image = (slot, tb), slot, image
        if (index + 1) % 100 == 0:
            get_logger(__name__).info("MERGIR frames processed: %d/%d", 2 * (index + 1), frames)

    table = cloud_table(slots, zonal.area_ids, 1, metrics)
    silver.mkdir(parents=True, exist_ok=True)
    for name, output in (
        (CLOUD_PARTS[1], table),
        ("_manifest-mergir.parquet", pa.Table.from_pylist(manifest, schema=MANIFEST_SCHEMA)),
    ):
        write_table(output, silver / name)
    return {
        "frames": frames,
        "invalid_cell_pct": 100 * invalid / (frames * grid.width * grid.height) if frames else None,
        "cold_speed_kmh_median": float(np.median(cold_speeds)) if cold_speeds else None,
        "silver_bytes": (silver / CLOUD_PARTS[1]).stat().st_size,
    }, previous


def _previous_frame(
    month: datetime, carry: Path, bronze: Path, grid: Grid
) -> tuple[datetime, NDArray] | None:
    target = month - SLOT
    if carry.exists():
        with np.load(carry) as saved:
            if datetime.fromisoformat(saved["slot"].item()) == target:
                return target, saved["tb"]
    last_hour = month - HOUR
    collection = config.settings()["mergir"]["collection"]
    entries = cmr.granules(collection, last_hour, month - timedelta(seconds=1))
    granules = select_granules(entries, last_hour, month)
    if not granules:
        return None
    seed = granules[0]
    bronze.mkdir(parents=True, exist_ok=True)
    with http.client(auth=True) as client:
        download(client, seed, bronze, grid)
    tb = read_crop(bronze / seed.title, grid, verify_slot=seed.slot)
    return target, tb[1]


def _backfill(start: datetime, end: datetime, force: bool, state_path: Path) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    carry = state_path.with_name("mergir_last_frame.npz")
    logger = get_logger(__name__)
    source = config.settings()["mergir"]
    grid = mergir_grid()
    parallel = _parallel()
    zonal = motion = None
    reports = {}
    months = []
    month = start
    while month <= end:
        following = next_month(month)
        months.append((month, following))
        month = following
    pending = (
        (month, following)
        for month, following in months
        if force or not state.get(f"{month:%Y-%m}", {}).get("done")
    )

    def fetch_month(month, following):
        label = f"{month:%Y-%m}"
        bronze = month_path(paths.zone("bronze") / "mergir", month)
        entries = cmr.granules(source["collection"], month, following - timedelta(seconds=1))
        granules = select_granules(entries, month, following)
        logger.info("MERGIR %s listed %d hourly files, NASA parallelism=%d",
                    label, len(granules), parallel)
        download_month(granules, bronze, grid, parallel)
        return granules

    with ThreadPoolExecutor(max_workers=1) as pool:
        download_future = None
        for month, following in months:
            label = f"{month:%Y-%m}"
            partition = month_path(Path(), month)
            bronze = paths.zone("bronze") / "mergir" / partition
            silver = paths.zone("silver") / "cloud_kabkota" / partition
            if state.get(label, {}).get("done") and not force:
                logger.info("MERGIR %s already done, skipping", label)
            else:
                started = perf_counter()
                if zonal is None:
                    membership = pq.read_table(paths.zone("ref") / "mergir_membership.parquet")
                    zonal = Zonal(membership, grid.width * grid.height)
                    cv2.setNumThreads(1)
                    motion = CloudMotion(grid, source["flow"])
                    logger.info("MERGIR pixel size %.3f km, smoothing sigma %.2f px",
                                motion.pixel_km, motion.sigma)
                if label in state:
                    del state[label]
                    write_json(state_path, state)
                waiting = perf_counter()
                if download_future is None:
                    download_future = pool.submit(fetch_month, *next(pending))
                granules = download_future.result()
                download_seconds = perf_counter() - waiting
                previous = _previous_frame(month, carry, bronze, grid) if granules else None
                # The carry fallback can download a crop, so resolve it before prefetching.
                next_pending = next(pending, None)
                download_future = pool.submit(fetch_month, *next_pending) if next_pending else None
                processing = perf_counter()
                logger.info("MERGIR %s download wait %.2fs, processing %d frames",
                            label, download_seconds, 2 * len(granules))
                stats, last = write_month(granules, bronze, silver, grid, zonal, motion, previous)
                if last is not None:
                    slot, tb = last
                    with atomic_write(carry) as file:
                        np.savez(file, tb=tb, slot=slot.isoformat())
                available = {slot for g in granules for slot in (g.slot, g.slot + SLOT)}
                expected = slot_range(month, following)
                finished = perf_counter()
                state[label] = {
                    "done": True,
                    **stats,
                    "expected": len(expected),
                    "missing_frames": [slot.isoformat() for slot in expected if slot not in available],
                    "download_seconds": download_seconds,
                    "processing_seconds": finished - processing,
                    "elapsed_seconds": finished - started,
                }
                write_json(state_path, state)
                manifest = pq.ParquetFile(silver / "_manifest-mergir.parquet").read(columns=["bytes"])
                reports[label] = {
                    "slots": stats["frames"], "bronze_bytes": sum(manifest["bytes"].to_pylist()),
                    "rows": stats["frames"] * len(zonal.area_ids), "silver_bytes": stats["silver_bytes"],
                }
                logger.info(
                    "MERGIR %s frames=%d/%d cold_speed_kmh_median=%s "
                    "download=%.2fs processing=%.2fs elapsed=%.2fs",
                    label, stats["frames"], len(expected), stats["cold_speed_kmh_median"],
                    download_seconds, finished - processing, finished - started,
                )
            for crop in bronze.glob("*.nc4*"):
                crop.unlink()
    return reports


def backfill(
    start: Annotated[str, typer.Option(help="First UTC month (YYYY-MM).")],
    end: Annotated[str, typer.Option(help="Last UTC month (YYYY-MM), inclusive.")],
    force: Annotated[bool, typer.Option(help="Process completed months again.")] = False,
) -> dict:
    try:
        if not all(re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) for month in (start, end)):
            raise ValueError("Month bounds must use YYYY-MM")
        first = parse_month(start)
        last = parse_month(end)
        if first > last:
            raise ValueError("--start must be on or before --end")
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        with file_lock(state, "mergir.lock"):
            return _backfill(first, last, force, state / "mergir_backfill.json")
    except (OSError, KeyError, ValueError, RuntimeError, httpx.HTTPError) as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None


def gaps(month: Annotated[str, typer.Option(help="UTC month (YYYY-MM).")]) -> None:
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError("--month must use YYYY-MM")
        start = parse_month(month)
        end = next_month(start)
        path = month_path(paths.zone("silver") / "cloud_kabkota", start, CLOUD_PARTS[1])
        table = pq.ParquetFile(path).read(columns=["slot"]) if path.exists() else pa.table(
            {"slot": pa.array([], type=UTC_US)}
        )
        counts = {
            row["slot"]: row["slot_count"]
            for row in table.group_by("slot").aggregate([("slot", "count")]).to_pylist()
        }
        if month == datetime.now(UTC).strftime("%Y-%m"):
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
        report = {
            "month": month,
            "expected_slots": len(expected),
            "present_slots": len(counts),
            "missing_slots": [slot.isoformat() for slot in missing],
            "missing_runs": runs,
            "rows_per_slot_min": min(counts.values(), default=None),
            "rows_per_slot_max": max(counts.values(), default=None),
        }
        output = paths.zone("state") / "mergir_gaps"
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / f"{month}.json", report)
        typer.echo(json.dumps(report, indent=2))
    except (OSError, ValueError) as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None

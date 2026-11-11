import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Annotated

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
from nowcast.log import command_errors, get_logger
from nowcast.registry.grids import Grid, mergir_grid
from nowcast.sources import earthdata
from nowcast.storage import atomic_write, month_path, write_json, write_table
from nowcast.timeslots import HOUR, SLOT, UTC_US, month_ranges, next_month, slot_range
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
    source = config.settings()["mergir"]
    lat, lon = source["lat_index"], source["lon_index"]
    constraint = f"{source['variable']}[0:1:1][{lat[0]}:1:{lat[1]}][{lon[0]}:1:{lon[1]}]"
    return earthdata.download_crop(
        client, source, granule.title, constraint, path, lambda path: read_crop(path, grid)
    )


def download_month(granules: list[Granule], bronze: Path, grid: Grid, parallel: int) -> None:
    if granules:
        earthdata.download_batch(
            granules, bronze, parallel, "MERGIR",
            lambda client, granule: download(client, granule, bronze, grid),
            verify_first=lambda granule: read_crop(bronze / granule.title, grid, verify_slot=granule.slot),
        )


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
    entries = earthdata.granules(collection, last_hour, month - timedelta(seconds=1))
    granules = select_granules(entries, last_hour, month)
    if not granules:
        return None
    seed = granules[0]
    bronze.mkdir(parents=True, exist_ok=True)
    with http.client(auth=True) as client:
        download(client, seed, bronze, grid)
    tb = read_crop(bronze / seed.title, grid, verify_slot=seed.slot)
    return target, tb[1]


def _fetch_month(month: datetime, following: datetime, grid: Grid, parallel: int) -> list[Granule]:
    bronze = month_path(paths.zone("bronze") / "mergir", month)
    entries = earthdata.granules(
        config.settings()["mergir"]["collection"], month, following - timedelta(seconds=1)
    )
    granules = select_granules(entries, month, following)
    get_logger(__name__).info("MERGIR %s listed %d hourly files, NASA parallelism=%d",
                              month.strftime("%Y-%m"), len(granules), parallel)
    download_month(granules, bronze, grid, parallel)
    return granules


def _publish_backfill_month(
    month: datetime, granules: list[Granule], state_path: Path, grid: Grid,
    zonal: Zonal, motion: CloudMotion, previous: tuple[datetime, NDArray] | None,
) -> dict:
    bronze = month_path(paths.zone("bronze") / "mergir", month)
    silver = month_path(paths.zone("silver") / "cloud_kabkota", month)
    stats, last = write_month(granules, bronze, silver, grid, zonal, motion, previous)
    if last is not None:
        slot, tb = last
        with atomic_write(state_path.with_name("mergir_last_frame.npz")) as file:
            np.savez(file, tb=tb, slot=slot.isoformat())
    return stats


def _record_backfill_month(
    state_path: Path, state: dict, month: datetime, granules: list[Granule],
    stats: dict, area_count: int, download_seconds: float, processing: float, started: float,
) -> dict:
    label = month.strftime("%Y-%m")
    available = {slot for g in granules for slot in (g.slot, g.slot + SLOT)}
    expected = slot_range(month, next_month(month))
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
    silver = month_path(paths.zone("silver") / "cloud_kabkota", month)
    manifest = pq.ParquetFile(silver / "_manifest-mergir.parquet").read(columns=["bytes"])
    report = {
        "slots": stats["frames"], "bronze_bytes": sum(manifest["bytes"].to_pylist()),
        "rows": stats["frames"] * area_count, "silver_bytes": stats["silver_bytes"],
    }
    get_logger(__name__).info(
        "MERGIR %s frames=%d/%d cold_speed_kmh_median=%s "
        "download=%.2fs processing=%.2fs elapsed=%.2fs",
        label, stats["frames"], len(expected), stats["cold_speed_kmh_median"],
        download_seconds, finished - processing, finished - started,
    )
    return report


def _cloud_processors(grid: Grid) -> tuple[Zonal, CloudMotion]:
    membership = pq.read_table(paths.zone("ref") / "mergir_membership.parquet")
    zonal = Zonal(membership, grid.width * grid.height)
    cv2.setNumThreads(1)
    motion = CloudMotion(grid, config.settings()["mergir"]["flow"])
    get_logger(__name__).info("MERGIR pixel size %.3f km, smoothing sigma %.2f px",
                              motion.pixel_km, motion.sigma)
    return zonal, motion


def _backfill(start: datetime, end: datetime, force: bool, state_path: Path) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    carry = state_path.with_name("mergir_last_frame.npz")
    logger = get_logger(__name__)
    grid = mergir_grid()
    parallel = earthdata.parallel_requests()
    zonal = motion = None
    reports = {}
    months = list(month_ranges(start, next_month(end)))
    pending = (
        (month, following) for month, following in months
        if force or not state.get(f"{month:%Y-%m}", {}).get("done")
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        download_future = None
        for month, following in earthdata.archive_months(months, state, force, "MERGIR"):
            label = month.strftime("%Y-%m")
            bronze = month_path(paths.zone("bronze") / "mergir", month)
            started = perf_counter()
            if zonal is None:
                zonal, motion = _cloud_processors(grid)
            if label in state:
                del state[label]
                write_json(state_path, state)
            waiting = perf_counter()
            if download_future is None:
                download_future = pool.submit(_fetch_month, *next(pending), grid, parallel)
            granules = download_future.result()
            download_seconds = perf_counter() - waiting
            previous = _previous_frame(month, carry, bronze, grid) if granules else None
            # The carry fallback can download a crop, so resolve it before prefetching.
            next_pending = next(pending, None)
            download_future = (
                pool.submit(_fetch_month, *next_pending, grid, parallel) if next_pending else None
            )
            processing = perf_counter()
            logger.info("MERGIR %s download wait %.2fs, processing %d frames",
                        label, download_seconds, 2 * len(granules))
            stats = _publish_backfill_month(month, granules, state_path, grid, zonal, motion, previous)
            reports[label] = _record_backfill_month(
                state_path, state, month, granules, stats, len(zonal.area_ids),
                download_seconds, processing, started,
            )
    return reports


def backfill(
    start: Annotated[str, typer.Option(help="First UTC month (YYYY-MM).")],
    end: Annotated[str, typer.Option(help="Last UTC month (YYYY-MM), inclusive.")],
    force: Annotated[bool, typer.Option(help="Process completed months again.")] = False,
) -> dict:
    with command_errors(OSError, KeyError, ValueError, RuntimeError, httpx.HTTPError):
        first, last = earthdata.month_bounds(start, end)
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        with file_lock(state, "mergir.lock"):
            return _backfill(first, last, force, state / "mergir_backfill.json")


def gaps(month: Annotated[str, typer.Option(help="UTC month (YYYY-MM).")]) -> None:
    with command_errors(OSError, ValueError):
        start = earthdata.gap_month(month)
        path = month_path(paths.zone("silver") / "cloud_kabkota", start, CLOUD_PARTS[1])
        table = pq.ParquetFile(path).read(columns=["slot"]) if path.exists() else pa.table(
            {"slot": pa.array([], type=UTC_US)}
        )
        counts = {
            row["slot"]: row["slot_count"]
            for row in table.group_by("slot").aggregate([("slot", "count")]).to_pylist()
        }
        report = earthdata.gap_report(month, counts, datetime.now(UTC))
        earthdata.write_gap_report("mergir", month, report)

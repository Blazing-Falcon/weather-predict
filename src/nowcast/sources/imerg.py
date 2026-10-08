import io
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, flock
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Annotated
from urllib.parse import quote

import h5py
import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import typer
from numpy.typing import NDArray

from nowcast import config, http, paths
from nowcast.log import get_logger, redact
from nowcast.parquet import write_table
from nowcast.registry.grids import Grid, imerg_grid
from nowcast.sources import cmr
from nowcast.zonal import Zonal

SLOT_LENGTH = timedelta(minutes=30)
UTC_US = pa.timestamp("us", tz="UTC")
VERSION = pa.dictionary(pa.int8(), pa.string())
RAIN_SCHEMA = pa.schema(
    [
        ("slot", UTC_US),
        ("area_id", pa.int16()),
        ("rain_mean_x10", pa.int16()),
        ("rain_max_x10", pa.int16()),
        ("wet_pct", pa.uint8()),
        ("product_version", VERSION),
    ]
)
MANIFEST_SCHEMA = pa.schema(
    [
        ("slot", UTC_US),
        ("title", pa.string()),
        ("product_version", VERSION),
        ("fetched_at", UTC_US),
        ("bytes", pa.int64()),
    ]
)


@dataclass(frozen=True)
class Granule:
    slot: datetime
    title: str
    version: str


class EarthdataForbidden(RuntimeError):
    pass


def parse_title(title: str) -> Granule:
    match = re.search(r"\.(\d{8}-S\d{6})-E\d{6}\.[^.]+\.(V\d+[A-Z])\.", title)
    if not match:
        raise ValueError(f"Unexpected IMERG title: {title}")
    slot, version = match.groups()
    return Granule(datetime.strptime(slot, "%Y%m%d-S%H%M%S").replace(tzinfo=UTC), title, version)


def select_granules(entries: list[dict], start: datetime, end: datetime) -> list[Granule]:
    logger = get_logger(__name__)
    selected = {}
    for entry in entries:
        granule = parse_title(entry["title"])
        if not start <= granule.slot < end:
            continue
        previous = selected.get(granule.slot)
        if previous is not None:
            granule, other = sorted(
                (granule, previous),
                key=lambda item: (int(item.version[1:-1]), item.version[-1], item.title),
                reverse=True,
            )
            logger.info(
                "Duplicate IMERG slot %s: keeping %s over %s",
                granule.slot.isoformat(),
                granule.title,
                other.title,
            )
        selected[granule.slot] = granule
    return [selected[slot] for slot in sorted(selected)]


def read_crop(path: Path | io.BytesIO, grid: Grid) -> NDArray:
    variable = config.settings()["imerg"]["variable"]
    with h5py.File(path, "r") as file:
        dataset = file[variable]
        if dataset.shape != (1, grid.width, grid.height):
            raise ValueError(f"Unexpected IMERG crop shape: {dataset.shape}")
        return dataset[0].T.ravel()


def download(client: httpx.Client, title: str, path: Path, grid: Grid) -> int:
    if path.is_file():
        try:
            read_crop(path, grid)
        except (OSError, KeyError, ValueError):
            pass
        else:
            return 0

    source = config.settings()["imerg"]
    lon, lat = source["lon_index"], source["lat_index"]
    constraint = f"{source['variable']}[0:1:0][{lon[0]}:1:{lon[1]}][{lat[0]}:1:{lat[1]}]"
    url = f"{source['opendap']}{quote(title, safe=':')}.dap.nc4?dap4.ce="
    response = http.get(client, url + quote(constraint, safe="/:"))
    if response.status_code == 403:
        raise EarthdataForbidden("Earthdata 403: EULA not accepted or token invalid")
    response.raise_for_status()

    content = response.content
    read_crop(io.BytesIO(content), grid)
    temp = path.with_suffix(".nc4.tmp")
    temp.write_bytes(content)
    temp.replace(path)
    return len(content)


def download_month(
    granules: list[Granule], bronze: Path, grid: Grid, parallel: int, progress: dict
) -> None:
    logger = get_logger(__name__)
    bronze.mkdir(parents=True, exist_ok=True)
    stopped = Event()

    with http.client(auth=True) as client, ThreadPoolExecutor(max_workers=parallel) as pool:

        def fetch(granule: Granule) -> int:
            if stopped.is_set():
                return 0
            try:
                return download(client, granule.title, bronze / f"{granule.title}.nc4", grid)
            except EarthdataForbidden:
                stopped.set()
                raise

        futures = {pool.submit(fetch, granule): granule.title for granule in granules}
        for future in as_completed(futures):
            try:
                size = future.result()
            except (httpx.HTTPError, OSError, KeyError, ValueError, EarthdataForbidden) as error:
                progress["failed"] += 1
                logger.error(
                    "IMERG download failed for %s: %s", futures[future], redact(str(error))
                )
            else:
                progress["downloaded"] += int(size > 0)
                progress["bytes"] += size
    if stopped.is_set():
        raise EarthdataForbidden("Earthdata 403: EULA not accepted or token invalid")
    if progress["failed"]:
        raise RuntimeError(f"{progress['failed']} IMERG downloads failed")


def _scaled(values: NDArray, scale: int, dtype: type) -> pa.Array:
    missing = np.isnan(values)
    limits = np.iinfo(dtype)
    numbers = np.clip(np.rint(np.where(missing, 0, values) * scale), limits.min, limits.max)
    return pa.array(numbers.astype(dtype), mask=missing)


def write_month(
    granules: list[Granule], bronze: Path, silver: Path, grid: Grid, zonal: Zonal
) -> dict:
    slots = [granule.slot for granule in granules]
    versions = [granule.version for granule in granules]
    assert slots == sorted(set(slots)) and np.all(np.diff(zonal.area_ids.astype(int)) > 0)
    count = len(zonal.area_ids)
    metrics = np.empty((3, count, len(slots)))
    fetched = []
    sizes = []
    valid_count = box_wet = 0
    wet_mm_h = config.settings()["imerg"]["wet_mm_h"]
    for index, granule in enumerate(granules):
        crop = bronze / f"{granule.title}.nc4"
        values = read_crop(crop, grid)
        stat = crop.stat()
        fetched.append(datetime.fromtimestamp(stat.st_mtime, UTC))
        sizes.append(stat.st_size)
        valid = np.isfinite(values) & (values >= 0)
        valid_count += np.count_nonzero(valid)
        box_wet += np.count_nonzero(valid & (values >= 0.1))
        metrics[:, :, index] = (
            zonal.mean(values, valid),
            zonal.max(values, valid),
            zonal.share(values >= wet_mm_h, valid),
        )

    timestamps = pa.array(slots, type=UTC_US)
    products = pa.array(versions, type=pa.string()).dictionary_encode().cast(VERSION)
    slot_indices = np.tile(np.arange(len(slots), dtype=np.int32), count)
    rain = pa.Table.from_arrays(
        [
            timestamps.take(slot_indices),
            pa.array(np.repeat(zonal.area_ids, len(slots)), type=pa.int16()),
            _scaled(metrics[0].ravel(), 10, np.int16),
            _scaled(metrics[1].ravel(), 10, np.int16),
            _scaled(metrics[2].ravel(), 100, np.uint8),
            products.take(slot_indices),
        ],
        schema=RAIN_SCHEMA,
    )
    manifest = pa.Table.from_arrays(
        [
            timestamps,
            pa.array([granule.title for granule in granules], type=pa.string()),
            products,
            pa.array(fetched, type=UTC_US),
            pa.array(sizes, type=pa.int64()),
        ],
        schema=MANIFEST_SCHEMA,
    )
    silver.mkdir(parents=True, exist_ok=True)
    for name, table in (("part.parquet", rain), ("_manifest.parquet", manifest)):
        (silver / f"{name}.tmp").unlink(missing_ok=True)
        write_table(table, silver / name)
    cell_count = len(slots) * grid.width * grid.height
    return {
        "box_wet_pct": 100 * box_wet / valid_count if valid_count else None,
        "invalid_cell_pct": 100 * (cell_count - valid_count) / cell_count if cell_count else None,
        "silver_bytes": (silver / "part.parquet").stat().st_size,
        "rows": rain.num_rows,
        "null_area_slots": rain["rain_mean_x10"].null_count,
    }


def _save_state(path: Path, state: dict) -> None:
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _backfill(start: datetime, end: datetime, force: bool, state_path: Path) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    logger = get_logger(__name__)
    grid = imerg_grid()
    zonal = None
    settings = config.settings()
    parallel = int(config.env("NASA_PARALLEL", str(settings["nasa"]["parallel"])))
    if parallel < 1:
        raise ValueError("NASA_PARALLEL must be positive")
    parallel = min(parallel, 8)
    month = start
    while month <= end:
        following = (month + timedelta(days=32)).replace(day=1)
        label = month.strftime("%Y-%m")
        partition = Path(f"year={month:%Y}") / f"month={month:%m}"
        bronze = paths.zone("bronze") / "imerg" / partition
        silver = paths.zone("silver") / "rain_kabkota" / partition
        if state.get(label, {}).get("done") and not force:
            logger.info("IMERG %s already done, skipping", label)
        else:
            started = perf_counter()
            progress = {
                "listed": 0,
                "downloaded": 0,
                "failed": 0,
                "bytes": 0,
                "silver_bytes": 0,
                "rows": 0,
            }
            try:
                if zonal is None:
                    membership = pq.read_table(paths.zone("ref") / "imerg_membership.parquet")
                    zonal = Zonal(membership, grid.width * grid.height)
                if label in state:
                    del state[label]
                    _save_state(state_path, state)
                entries = cmr.granules(
                    settings["imerg"]["collection"], month, following - timedelta(seconds=1)
                )
                progress["listed"] = len(entries)
                granules = select_granules(entries, month, following)
                expected = [
                    month + i * SLOT_LENGTH for i in range((following - month) // SLOT_LENGTH)
                ]
                slots = {granule.slot for granule in granules}
                assert slots <= set(expected)
                if granules:
                    download_month(granules, bronze, grid, parallel, progress)
                stats = write_month(granules, bronze, silver, grid, zonal)
                progress.update(stats)
                logger.info("IMERG %s null area-slots=%d", label, stats["null_area_slots"])
                state[label] = {
                    "done": True,
                    "slots": len(slots),
                    "expected": len(expected),
                    "missing_slots": [slot.isoformat() for slot in expected if slot not in slots],
                    "versions": dict(sorted(Counter(g.version for g in granules).items())),
                    "box_wet_pct": stats["box_wet_pct"],
                    "invalid_cell_pct": stats["invalid_cell_pct"],
                    "silver_bytes": stats["silver_bytes"],
                    "finished_at": datetime.now(UTC).isoformat(),
                }
                _save_state(state_path, state)
            finally:
                logger.info(
                    "IMERG %s listed=%d downloaded=%d failed=%d elapsed=%.2fs "
                    "downloaded_mb=%.2f silver_bytes=%d rows=%d",
                    label,
                    progress["listed"],
                    progress["downloaded"],
                    progress["failed"],
                    perf_counter() - started,
                    progress["bytes"] / 1_000_000,
                    progress["silver_bytes"],
                    progress["rows"],
                )
        for crop in bronze.glob("*.nc4*"):
            crop.unlink()
        month = following


def backfill(
    start: Annotated[str, typer.Option(help="First UTC month (YYYY-MM).")],
    end: Annotated[str, typer.Option(help="Last UTC month (YYYY-MM), inclusive.")],
    force: Annotated[bool, typer.Option(help="Process completed months again.")] = False,
) -> None:
    try:
        if not all(re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) for month in (start, end)):
            raise ValueError("Month bounds must use YYYY-MM")
        first = datetime.strptime(start, "%Y-%m").replace(tzinfo=UTC)
        last = datetime.strptime(end, "%Y-%m").replace(tzinfo=UTC)
        if first > last:
            raise ValueError("--start must be on or before --end")
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        lock = os.open(state, os.O_RDONLY)
        try:
            # Lock the directory because saving state replaces the JSON file.
            flock(lock, LOCK_EX)
            _backfill(first, last, force, state / "imerg_backfill.json")
        finally:
            os.close(lock)
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None

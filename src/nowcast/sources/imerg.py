import io
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Annotated
from urllib.parse import quote

import h5py
import httpx
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import typer
from numpy.typing import NDArray

from nowcast import config, http, paths
from nowcast.gold.tiles import tile_path, write_bounds, write_rain
from nowcast.log import get_logger, redact
from nowcast.parquet import write_table
from nowcast.registry.grids import Grid, imerg_grid, mergir_grid
from nowcast.sources import cmr
from nowcast.zonal import Zonal

SLOT_LENGTH = timedelta(minutes=30)
LIVE_LOOKBACK = timedelta(hours=12)
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
    granules: list[Granule],
    bronze: Path,
    silver: Path,
    grid: Grid,
    zonal: Zonal,
    *,
    merge: bool = False,
    tiles: Path | None = None,
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
        if tiles is not None:
            write_rain(tile_path(tiles, "rain", granule.slot), values, grid)
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
        path = silver / name
        if merge and path.exists():
            existing = pq.ParquetFile(path).read()
            replaced = pc.is_in(existing["slot"], value_set=timestamps)
            sort_keys = [("slot", "ascending")]
            if name == "part.parquet":
                # The new table contains every combination of its slots and area IDs.
                areas = pa.array(zonal.area_ids, type=pa.int16())
                replaced = pc.and_(
                    replaced, pc.is_in(existing["area_id"], value_set=areas)
                )
                sort_keys.insert(0, ("area_id", "ascending"))
            table = pa.concat_tables([existing.filter(pc.invert(replaced)), table])
            table = table.sort_by(sort_keys)
        (silver / f"{name}.tmp").unlink(missing_ok=True)
        write_table(table, path)
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


def _parallel() -> int:
    parallel = int(config.env("NASA_PARALLEL", str(config.settings()["nasa"]["parallel"])))
    if parallel < 1:
        raise ValueError("NASA_PARALLEL must be positive")
    return min(parallel, 16)


def _partition(month: datetime) -> Path:
    return Path(f"year={month:%Y}") / f"month={month:%m}"


def _slot_range(start: datetime, end: datetime) -> list[datetime]:
    return [start + i * SLOT_LENGTH for i in range((end - start) // SLOT_LENGTH)]


def _backfill(start: datetime, end: datetime, force: bool, state_path: Path) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    logger = get_logger(__name__)
    grid = imerg_grid()
    zonal = None
    settings = config.settings()
    parallel = _parallel()
    reports = {}
    month = start
    while month <= end:
        following = (month + timedelta(days=32)).replace(day=1)
        label = month.strftime("%Y-%m")
        partition = _partition(month)
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
                expected = _slot_range(month, following)
                slots = {granule.slot for granule in granules}
                assert slots <= set(expected)
                if granules:
                    download_month(granules, bronze, grid, parallel, progress)
                # The live tick and scorer lock the state directory to write or read rain.
                directory = os.open(state_path.parent, os.O_RDONLY)
                try:
                    flock(directory, LOCK_EX)
                    stats = write_month(granules, bronze, silver, grid, zonal)
                finally:
                    os.close(directory)
                progress.update(stats)
                reports[label] = {
                    "slots": len(slots), "bronze_bytes": progress["bytes"],
                    "rows": stats["rows"], "silver_bytes": stats["silver_bytes"],
                }
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
    return reports


def backfill(
    start: Annotated[str, typer.Option(help="First UTC month (YYYY-MM).")],
    end: Annotated[str, typer.Option(help="Last UTC month (YYYY-MM), inclusive.")],
    force: Annotated[bool, typer.Option(help="Process completed months again.")] = False,
) -> dict:
    try:
        if not all(re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) for month in (start, end)):
            raise ValueError("Month bounds must use YYYY-MM")
        first = datetime.strptime(start, "%Y-%m").replace(tzinfo=UTC)
        last = datetime.strptime(end, "%Y-%m").replace(tzinfo=UTC)
        if first > last:
            raise ValueError("--start must be on or before --end")
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        with (state / "imerg_backfill.lock").open("a") as lock:
            flock(lock, LOCK_EX)
            return _backfill(first, last, force, state / "imerg_backfill.json")
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None


def _live_cursor(state: Path) -> dict:
    path = state / "imerg_live.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "last_slot": None, "processed": 0,
    }


def _live_start(cursor: dict, now: datetime) -> datetime:
    last = cursor["last_slot"]
    return datetime.fromisoformat(last) + timedelta(seconds=1) if last else now - timedelta(hours=12)


def newest_granule(now: datetime) -> Granule | None:
    start = _live_start(_live_cursor(paths.zone("state")), now)
    if start > now:
        return None
    entry = cmr.latest_granule(config.settings()["imerg"]["collection"], start, now)
    granules = select_granules([entry], start, now + timedelta(microseconds=1)) if entry else []
    return granules[0] if granules else None


def _archived_slots(start: datetime, end: datetime) -> set[datetime]:
    slots = set()
    month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month <= end:
        manifest = paths.zone("silver") / "rain_kabkota" / _partition(month) / "_manifest.parquet"
        if manifest.exists():
            slots.update(pq.read_table(manifest, columns=["slot"])["slot"].to_pylist())
        month = (month + timedelta(days=32)).replace(day=1)
    return slots


def _tick(state: Path, now: datetime) -> dict:
    cursor_path = state / "imerg_live.json"
    cursor = _live_cursor(state)
    last_slot = cursor["last_slot"]
    after = _live_start(cursor, now)
    # CMR can index a granule after a newer one, so slots behind the cursor are retried.
    start = min(after, now - LIVE_LOOKBACK)
    entries = cmr.granules(config.settings()["imerg"]["collection"], start, now)
    seen_at = datetime.now(UTC)
    granules = select_granules(entries, start, now + timedelta(microseconds=1))
    if start < after:
        archived = _archived_slots(start, after)
        granules = [
            granule for granule in granules if granule.slot >= after or granule.slot not in archived
        ]

    reports = {}
    if granules:
        grid = imerg_grid()
        tiles = paths.zone("gold") / "tiles"
        write_bounds(tiles, grid, mergir_grid())
        membership = pq.read_table(paths.zone("ref") / "imerg_membership.parquet")
        zonal = Zonal(membership, grid.width * grid.height)
        parallel = _parallel()
        months: dict[Path, list[Granule]] = {}
        for granule in granules:
            months.setdefault(_partition(granule.slot), []).append(granule)
        for partition, batch in months.items():
            bronze = paths.zone("bronze") / "imerg" / partition
            silver = paths.zone("silver") / "rain_kabkota" / partition
            progress = {"downloaded": 0, "failed": 0, "bytes": 0}
            download_month(batch, bronze, grid, parallel, progress)
            stats = write_month(batch, bronze, silver, grid, zonal, merge=True, tiles=tiles)
            last = batch[-1].slot
            reports[last.strftime("%Y-%m")] = {
                "slots": len(batch), "bronze_bytes": progress["bytes"],
                "rows": stats["rows"], "silver_bytes": stats["silver_bytes"],
                "latency_min": (seen_at - (last + SLOT_LENGTH)).total_seconds() / 60,
            }
        newest = granules[-1].slot
        if last_slot:
            newest = max(newest, datetime.fromisoformat(last_slot))
        last_slot = newest.isoformat()

    latency_path = state / "imerg_latency.jsonl"
    recent = []
    recorded = set()
    last_latency = None
    cutoff = seen_at - timedelta(hours=24)
    if latency_path.exists():
        with latency_path.open(encoding="utf-8") as log:
            for line in log:
                entry = json.loads(line)
                last_latency = entry["latency_min"]
                if datetime.fromisoformat(entry["seen_at"]) >= cutoff:
                    recent.append(last_latency)
                if granules and datetime.fromisoformat(entry["slot"]) >= start:
                    recorded.add((entry["slot"], entry["title"]))
    if granules:
        logger = get_logger(__name__)
        with latency_path.open("a", encoding="utf-8") as log:
            for granule in granules:
                slot = granule.slot.isoformat()
                if (slot, granule.title) in recorded:
                    continue
                last_latency = (seen_at - (granule.slot + SLOT_LENGTH)).total_seconds() / 60
                log.write(json.dumps({
                    "slot": slot,
                    "title": granule.title,
                    "version": granule.version,
                    "seen_at": seen_at.isoformat(),
                    "latency_min": last_latency,
                }) + "\n")
                recent.append(last_latency)
                logger.info("IMERG slot %s latency_min=%.2f", slot, last_latency)

    health = state / "health"
    health.mkdir(exist_ok=True)
    _save_state(health / "imerg.json", {
        "last_tick": now.isoformat(),
        "last_slot": last_slot,
        "new_slots": len(granules),
        "latency_min_last": last_latency,
        "latency_min_p90_24h": float(np.percentile(recent, 90)) if recent else None,
    })
    if granules:
        _save_state(cursor_path, {
            "last_slot": last_slot,
            "processed": cursor["processed"] + len(granules),
        })
    get_logger(__name__).info("IMERG tick new_slots=%d", len(granules))
    return reports


def _fill(start: datetime, end: datetime) -> int:
    entries = cmr.granules(config.settings()["imerg"]["collection"], start, end)
    granules = select_granules(entries, start, end)
    grid = imerg_grid()
    tiles = paths.zone("gold") / "tiles"
    write_bounds(tiles, grid, mergir_grid())
    zonal = Zonal(pq.read_table(paths.zone("ref") / "imerg_membership.parquet"),
                  grid.width * grid.height)
    months: dict[Path, list[Granule]] = {}
    for granule in granules:
        months.setdefault(_partition(granule.slot), []).append(granule)
    for partition, batch in months.items():
        bronze = paths.zone("bronze") / "imerg" / partition
        download_month(batch, bronze, grid, _parallel(), {"downloaded": 0, "failed": 0, "bytes": 0})
        write_month(batch, bronze, paths.zone("silver") / "rain_kabkota" / partition,
                    grid, zonal, merge=True, tiles=tiles)
    return len(granules)


def fill(
    start: Annotated[str, typer.Option(help="UTC start (ISO).")],
    end: Annotated[str, typer.Option(help="UTC end (ISO).")],
) -> None:
    """Merge past slots into silver and draw their tiles, without touching the live cursor."""
    state = paths.zone("state")
    with (state / "imerg.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        count = _fill(datetime.fromisoformat(start), datetime.fromisoformat(end))
    get_logger(__name__).info("IMERG fill slots=%d", count)


def tick() -> dict:
    try:
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        with (state / "imerg.lock").open("a") as lock:
            try:
                flock(lock, LOCK_EX | LOCK_NB)
            except BlockingIOError:
                return {}
            directory = os.open(state, os.O_RDONLY)
            try:
                try:
                    # Backfill also locks the directory while it writes the same partitions.
                    flock(directory, LOCK_EX | LOCK_NB)
                except BlockingIOError:
                    return {}
                return _tick(state, datetime.now(UTC))
            finally:
                os.close(directory)
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None


def gaps(month: Annotated[str, typer.Option(help="UTC month (YYYY-MM).")]) -> None:
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError("--month must use YYYY-MM")
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
        end = (start + timedelta(days=32)).replace(day=1)
        part = paths.zone("silver") / "rain_kabkota" / _partition(start) / "part.parquet"
        table = (
            pq.ParquetFile(part).read(columns=["slot", "rain_mean_x10", "product_version"])
            if part.exists() else RAIN_SCHEMA.empty_table()
        )
        rows_per_slot: Counter[datetime] = Counter()
        versions: Counter[str] = Counter()
        products = pa.table({
            "slot": table["slot"],
            "product_version": pc.cast(table["product_version"], pa.string()),
        })
        for row in products.group_by(["slot", "product_version"]).aggregate(
            [("slot", "count")]
        ).to_pylist():
            rows_per_slot[row["slot"]] += row["slot_count"]
            versions[row["product_version"]] += 1
        if month == datetime.now(UTC).strftime("%Y-%m"):
            end = max(rows_per_slot) + SLOT_LENGTH if rows_per_slot else start
        expected = _slot_range(start, end)
        missing = [slot for slot in expected if slot not in rows_per_slot]
        runs = []
        for slot in missing:
            if runs and slot == runs[-1]["end"] + SLOT_LENGTH:
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
            "present_slots": len(rows_per_slot),
            "missing_slots": [slot.isoformat() for slot in missing],
            "missing_runs": runs,
            "rows_per_slot_min": min(rows_per_slot.values(), default=None),
            "rows_per_slot_max": max(rows_per_slot.values(), default=None),
            "versions": dict(sorted(versions.items())),
            "null_area_slots": table["rain_mean_x10"].null_count,
        }
        output = paths.zone("state") / "imerg_gaps"
        output.mkdir(parents=True, exist_ok=True)
        _save_state(output / f"{month}.json", report)
        typer.echo(json.dumps(report, indent=2))
    except (OSError, ValueError) as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None

import io
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Annotated

import h5py
import httpx
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import typer
from numpy.typing import NDArray

from nowcast import config, paths
from nowcast.gold.tiles import tile_path, write_bounds, write_rain
from nowcast.locks import directory_lock, file_lock
from nowcast.log import command_errors, get_logger
from nowcast.registry.grids import Grid, imerg_grid, mergir_grid
from nowcast.sources import earthdata
from nowcast.storage import month_path, scaled, write_json, write_table
from nowcast.timeslots import SLOT, UTC_US, month_ranges, next_month, slot_range
from nowcast.zonal import Zonal

LIVE_LOOKBACK = timedelta(hours=12)
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
    source = config.settings()["imerg"]
    lon, lat = source["lon_index"], source["lat_index"]
    constraint = f"{source['variable']}[0:1:0][{lon[0]}:1:{lon[1]}][{lat[0]}:1:{lat[1]}]"
    return earthdata.download_crop(
        client, source, title, constraint, path, lambda path: read_crop(path, grid), title_safe=":"
    )


def download_month(
    granules: list[Granule], bronze: Path, grid: Grid, parallel: int, progress: dict
) -> None:
    earthdata.download_batch(
        granules, bronze, parallel, "IMERG",
        lambda client, granule: download(client, granule.title, bronze / f"{granule.title}.nc4", grid),
        progress=progress,
    )


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
            scaled(metrics[0].ravel(), 10, np.int16),
            scaled(metrics[1].ravel(), 10, np.int16),
            scaled(metrics[2].ravel(), 100, np.uint8),
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
        write_table(table, path)
    cell_count = len(slots) * grid.width * grid.height
    return {
        "box_wet_pct": 100 * box_wet / valid_count if valid_count else None,
        "invalid_cell_pct": 100 * (cell_count - valid_count) / cell_count if cell_count else None,
        "silver_bytes": (silver / "part.parquet").stat().st_size,
        "rows": rain.num_rows,
        "null_area_slots": rain["rain_mean_x10"].null_count,
    }


def _record_backfill_month(
    state_path: Path, state: dict, month: datetime, granules: list[Granule],
    expected: list[datetime], stats: dict, bronze_bytes: int,
) -> dict:
    label = month.strftime("%Y-%m")
    slots = {granule.slot for granule in granules}
    get_logger(__name__).info("IMERG %s null area-slots=%d", label, stats["null_area_slots"])
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
    write_json(state_path, state)
    return {
        "slots": len(slots), "bronze_bytes": bronze_bytes,
        "rows": stats["rows"], "silver_bytes": stats["silver_bytes"],
    }


def _backfill_month(
    month: datetime, following: datetime, state_path: Path, state: dict,
    grid: Grid, zonal: Zonal | None, parallel: int,
) -> tuple[dict, Zonal]:
    logger = get_logger(__name__)
    label = month.strftime("%Y-%m")
    bronze = month_path(paths.zone("bronze") / "imerg", month)
    silver = month_path(paths.zone("silver") / "rain_kabkota", month)
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
            write_json(state_path, state)
        entries = earthdata.granules(
            config.settings()["imerg"]["collection"], month, following - timedelta(seconds=1)
        )
        progress["listed"] = len(entries)
        granules = select_granules(entries, month, following)
        expected = slot_range(month, following)
        slots = {granule.slot for granule in granules}
        assert slots <= set(expected)
        if granules:
            download_month(granules, bronze, grid, parallel, progress)
        # The live tick and scorer lock the state directory to write or read rain.
        with directory_lock(state_path.parent):
            stats = write_month(granules, bronze, silver, grid, zonal)
        progress.update(stats)
        report = _record_backfill_month(
            state_path, state, month, granules, expected, stats, progress["bytes"]
        )
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
    return report, zonal


def _backfill(start: datetime, end: datetime, force: bool, state_path: Path) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    grid = imerg_grid()
    parallel = earthdata.parallel_requests()
    zonal = None
    reports = {}
    months = month_ranges(start, next_month(end))
    for month, following in earthdata.archive_months(months, state, force, "IMERG"):
        reports[month.strftime("%Y-%m")], zonal = _backfill_month(
            month, following, state_path, state, grid, zonal, parallel
        )
    return reports


def backfill(
    start: Annotated[str, typer.Option(help="First UTC month (YYYY-MM).")],
    end: Annotated[str, typer.Option(help="Last UTC month (YYYY-MM), inclusive.")],
    force: Annotated[bool, typer.Option(help="Process completed months again.")] = False,
) -> dict:
    with command_errors(OSError, ValueError, RuntimeError, httpx.HTTPError):
        first, last = earthdata.month_bounds(start, end)
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        with file_lock(state, "imerg_backfill.lock"):
            return _backfill(first, last, force, state / "imerg_backfill.json")


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
    entry = earthdata.latest_granule(config.settings()["imerg"]["collection"], start, now)
    granules = select_granules([entry], start, now + timedelta(microseconds=1)) if entry else []
    return granules[0] if granules else None


def _archived_slots(start: datetime, end: datetime) -> set[datetime]:
    slots = set()
    month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month <= end:
        manifest = month_path(paths.zone("silver") / "rain_kabkota", month, "_manifest.parquet")
        if manifest.exists():
            slots.update(pq.read_table(manifest, columns=["slot"])["slot"].to_pylist())
        month = next_month(month)
    return slots


def _recent_granules(cursor: dict, now: datetime) -> tuple[datetime, datetime, list[Granule]]:
    after = _live_start(cursor, now)
    # CMR can index a granule after a newer one, so slots behind the cursor are retried.
    start = min(after, now - LIVE_LOOKBACK)
    entries = earthdata.granules(config.settings()["imerg"]["collection"], start, now)
    seen_at = datetime.now(UTC)
    granules = select_granules(entries, start, now + timedelta(microseconds=1))
    return start, seen_at, granules


def _drop_archived_granules(granules: list[Granule], start: datetime, after: datetime) -> list[Granule]:
    if start < after:
        archived = _archived_slots(start, after)
        granules = [
            granule for granule in granules if granule.slot >= after or granule.slot not in archived
        ]
    return granules


def _publish_months(granules: list[Granule], seen_at: datetime | None = None) -> dict:
    grid = imerg_grid()
    tiles = paths.zone("gold") / "tiles"
    write_bounds(tiles, grid, mergir_grid())
    membership = pq.read_table(paths.zone("ref") / "imerg_membership.parquet")
    zonal = Zonal(membership, grid.width * grid.height)
    parallel = earthdata.parallel_requests() if granules else None
    months: dict[Path, list[Granule]] = {}
    for granule in granules:
        months.setdefault(month_path(Path(), granule.slot), []).append(granule)
    reports = {}
    for partition, batch in months.items():
        bronze = paths.zone("bronze") / "imerg" / partition
        silver = paths.zone("silver") / "rain_kabkota" / partition
        progress = {"downloaded": 0, "failed": 0, "bytes": 0}
        download_month(batch, bronze, grid, parallel, progress)
        stats = write_month(batch, bronze, silver, grid, zonal, merge=True, tiles=tiles)
        last = batch[-1].slot
        report = {
            "slots": len(batch), "bronze_bytes": progress["bytes"],
            "rows": stats["rows"], "silver_bytes": stats["silver_bytes"],
        }
        if seen_at is not None:
            report["latency_min"] = (seen_at - (last + SLOT)).total_seconds() / 60
        reports[last.strftime("%Y-%m")] = report
    return reports


def _record_latency(
    state: Path, granules: list[Granule], start: datetime, seen_at: datetime
) -> tuple[float | None, list[float]]:
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
                last_latency = (seen_at - (granule.slot + SLOT)).total_seconds() / 60
                log.write(json.dumps({
                    "slot": slot,
                    "title": granule.title,
                    "version": granule.version,
                    "seen_at": seen_at.isoformat(),
                    "latency_min": last_latency,
                }) + "\n")
                recent.append(last_latency)
                logger.info("IMERG slot %s latency_min=%.2f", slot, last_latency)

    return last_latency, recent


def _write_health(
    state: Path, now: datetime, last_slot: str | None, count: int,
    last_latency: float | None, recent: list[float],
) -> None:
    health = state / "health"
    health.mkdir(exist_ok=True)
    write_json(health / "imerg.json", {
        "last_tick": now.isoformat(),
        "last_slot": last_slot,
        "new_slots": count,
        "latency_min_last": last_latency,
        "latency_min_p90_24h": float(np.percentile(recent, 90)) if recent else None,
    })


def _advance_cursor(state: Path, cursor: dict, last_slot: str | None, count: int) -> None:
    if count:
        write_json(state / "imerg_live.json", {
            "last_slot": last_slot,
            "processed": cursor["processed"] + count,
        })


def _tick(state: Path, now: datetime) -> dict:
    cursor = _live_cursor(state)
    start, seen_at, granules = _recent_granules(cursor, now)
    granules = _drop_archived_granules(granules, start, _live_start(cursor, now))
    reports = _publish_months(granules, seen_at) if granules else {}
    last_slot = cursor["last_slot"]
    if granules:
        newest = granules[-1].slot
        if last_slot:
            newest = max(newest, datetime.fromisoformat(last_slot))
        last_slot = newest.isoformat()
    last_latency, recent = _record_latency(state, granules, start, seen_at)
    _write_health(state, now, last_slot, len(granules), last_latency, recent)
    _advance_cursor(state, cursor, last_slot, len(granules))
    get_logger(__name__).info("IMERG tick new_slots=%d", len(granules))
    return reports


def _fill(start: datetime, end: datetime) -> int:
    entries = earthdata.granules(config.settings()["imerg"]["collection"], start, end)
    granules = select_granules(entries, start, end)
    _publish_months(granules)
    return len(granules)


def fill(
    start: Annotated[str, typer.Option(help="UTC start (ISO).")],
    end: Annotated[str, typer.Option(help="UTC end (ISO).")],
) -> None:
    """Merge past slots into silver and draw their tiles, without touching the live cursor."""
    state = paths.zone("state")
    with file_lock(state, "imerg.lock"):
        count = _fill(datetime.fromisoformat(start), datetime.fromisoformat(end))
    get_logger(__name__).info("IMERG fill slots=%d", count)


def tick() -> dict:
    with command_errors(OSError, ValueError, RuntimeError, httpx.HTTPError):
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        with file_lock(state, "imerg.lock", blocking=False) as acquired:
            if not acquired:
                return {}
            with directory_lock(state, blocking=False) as acquired:
                if not acquired:
                    return {}
                return _tick(state, datetime.now(UTC))


def gaps(month: Annotated[str, typer.Option(help="UTC month (YYYY-MM).")]) -> None:
    with command_errors(OSError, ValueError):
        start = earthdata.gap_month(month)
        part = month_path(paths.zone("silver") / "rain_kabkota", start, "part.parquet")
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
        report = {
            **earthdata.gap_report(month, rows_per_slot, datetime.now(UTC)),
            "versions": dict(sorted(versions.items())),
            "null_area_slots": table["rain_mean_x10"].null_count,
        }
        earthdata.write_gap_report("imerg", month, report)

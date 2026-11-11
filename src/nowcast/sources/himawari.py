import json
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Annotated

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import typer
from numpy.typing import NDArray
from pyproj import Proj

from nowcast import config, http, paths
from nowcast.cloud import CLOUD_PARTS, CloudMotion, cloud_rows, valid_tb
from nowcast.gold.tiles import tile_path, write_bounds, write_cloud
from nowcast.locks import file_lock
from nowcast.log import get_logger, redact
from nowcast.registry.grids import Grid, imerg_grid, mergir_grid
from nowcast.storage import atomic_write, month_path, write_bytes, write_json, write_table
from nowcast.timeslots import SLOT, UTC_US
from nowcast.zonal import Zonal

SCAN_LENGTH = timedelta(minutes=10)
LOOKBACK = timedelta(hours=1)
RETENTION = timedelta(hours=6)
MANIFEST = "_manifest-himawari.parquet"
MANIFEST_SCHEMA = pa.schema([
    ("scan", UTC_US),
    ("files", pa.list_(pa.string())),
    ("fetched_at", UTC_US),
    ("bytes", pa.int64()),
])


def is_model_slot(scan: datetime) -> bool:
    return scan.minute in (0, 30)


def pixel_index(lon: NDArray, lat: NDArray, grid: Grid) -> NDArray[np.int32]:
    x = np.searchsorted(grid.lon_edges, lon, side="right") - 1
    y = np.searchsorted(grid.lat_edges, lat, side="right") - 1
    inside = (0 <= x) & (x < grid.width) & (0 <= y) & (y < grid.height)
    return np.where(inside, y * grid.width + x, -1).astype(np.int32)


def block_mean(tb: NDArray, index: NDArray, grid: Grid) -> NDArray[np.float32]:
    valid = (index >= 0) & np.isfinite(tb)
    cells = index[valid]
    size = grid.width * grid.height
    counts = np.bincount(cells, minlength=size)
    sums = np.bincount(cells, weights=tb[valid], minlength=size)
    means = np.full(size, np.nan, dtype=np.float32)
    np.divide(sums, counts, out=means, where=counts > 0)
    return means.reshape(grid.height, grid.width)


def _check_coverage(area, grid: Grid, segments: list[int]) -> None:
    south_y, north_y = area.area_extent[1::2]
    proj = Proj(area.crs)
    longitudes = np.linspace(94, 142, 481)
    bounds = []
    for edge_y in (south_y, north_y):
        x = area.projection_x_coords
        lon, lat = proj(x, np.full_like(x, edge_y), inverse=True)
        on_disk = np.isfinite(lon) & np.isfinite(lat)
        # The full-disk boundary crosses the antimeridian.
        edge_lon = np.unwrap(lon[on_disk], period=360)
        bounds.append(np.interp(longitudes, edge_lon, lat[on_disk], left=np.nan, right=np.nan))
    south, north = np.max(bounds[0]), np.min(bounds[1])
    logger = get_logger(__name__)
    logger.info(
        "Himawari B13 segments %s common latitude coverage over 94 to 142 E: %.4f to %.4f deg",
        ", ".join(f"{segment:02}" for segment in segments), south, north,
    )
    if not np.isfinite([south, north]).all():
        raise ValueError("Himawari B13 segment boundaries do not span 94 to 142 E")
    if south <= grid.lat_edges[0] and north >= grid.lat_edges[-1]:
        return

    _, box_south = proj(longitudes, np.full_like(longitudes, grid.lat_edges[0]))
    _, box_north = proj(longitudes, np.full_like(longitudes, grid.lat_edges[-1]))
    segment_height = (north_y - south_y) / len(segments)
    above = max(0, int(np.ceil((box_north.max() - north_y) / segment_height)))
    below = max(0, int(np.ceil((south_y - box_south.min()) / segment_height)))
    extra = [*range(min(segments) - above, min(segments)),
             *range(max(segments) + 1, max(segments) + below + 1)]
    raise ValueError(
        f"Himawari B13 does not cover the MERGIR box; additional segments required: "
        f"{', '.join(f'{segment:02}' for segment in extra)}"
    )


def decode(files: list[Path], index_path: Path, grid: Grid, source: dict) -> NDArray:
    from satpy import Scene

    band = source["band"]
    scene = Scene(reader="ahi_hsd", filenames=[str(file) for file in files])
    scene.load([band], calibration="brightness_temperature", pad_data=False)
    dataset = scene[band]
    values = dataset.values
    started = perf_counter()
    index = None
    if index_path.exists():
        with np.load(index_path) as saved:
            if tuple(saved["shape"]) == dataset.shape:
                index = saved["index"]
    if index is None:
        area = dataset.attrs["area"]
        _check_coverage(area, grid, source["segments"])
        lon, lat = area.get_lonlats()
        index = pixel_index(lon, lat, grid)
        with atomic_write(index_path) as file:
            np.savez(file, shape=dataset.shape, index=index)
    tb = block_mean(values, index, grid)
    get_logger(__name__).info(
        "Himawari B13 regrid invalid_cell_pct=%.3f elapsed=%.2fs",
        100 * (1 - valid_tb(tb).mean()), perf_counter() - started,
    )
    return tb


def merge_scan(path: Path, table: pa.Table, scan: datetime) -> None:
    timestamp = "slot" if path.name == CLOUD_PARTS[2] else "scan"
    sort_keys = [(timestamp, "ascending")]
    if path.name == CLOUD_PARTS[2]:
        sort_keys.insert(0, ("area_id", "ascending"))
    if path.exists():
        existing = pq.ParquetFile(path).read()
        replaced = pc.equal(existing[timestamp], pa.scalar(scan, type=UTC_US))
        if path.name == CLOUD_PARTS[2]:
            replaced = pc.and_(replaced, pc.and_(
                pc.is_in(existing["area_id"], value_set=table["area_id"]),
                pc.is_in(existing["source"], value_set=table["source"]),
            ))
        table = pa.concat_tables([existing.filter(pc.invert(replaced)), table])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_table(table.sort_by(sort_keys), path)


def _ready_scans(
    client: httpx.Client, now: datetime, last: datetime | None, source: dict,
) -> list[tuple[datetime, datetime, dict[str, int]]]:
    newest = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    ready = []
    for offset in range(LOOKBACK // SCAN_LENGTH, -1, -1):
        scan = newest - offset * SCAN_LENGTH
        if scan < now - LOOKBACK or (last is not None and scan <= last):
            continue
        prefix = source["prefix"].format(
            yyyy=f"{scan:%Y}", mm=f"{scan:%m}", dd=f"{scan:%d}", HHMM=f"{scan:%H%M}",
        )
        keys = [
            f"{prefix}HS_H09_{scan:%Y%m%d_%H%M}_{source['band']}_FLDK_R20_S{segment:02}10.DAT.bz2"
            for segment in source["segments"]
        ]
        response = http.get(client, source["bucket_url"], params={
            "list-type": "2", "prefix": prefix,
        }).raise_for_status()
        objects = {
            item.findtext("{*}Key"): int(item.findtext("{*}Size"))
            for item in ET.fromstring(response.content).findall("{*}Contents")
        }
        if all(key in objects for key in keys):
            ready.append((scan, datetime.now(UTC), {key: objects[key] for key in keys}))
    return ready


def _download(
    client: httpx.Client, bucket: str, key: str, size: int, directory: Path,
) -> Path:
    path = directory / Path(key).name
    if path.exists() and path.stat().st_size == size:
        return path
    response = http.get(client, bucket + key).raise_for_status()
    if len(response.content) != size:
        raise ValueError(f"Himawari size differs from S3 listing: {key}")
    write_bytes(path, response.content)
    return path


def _prune(bronze: Path, silver: Path, cutoff: datetime, last: datetime | None) -> None:
    processed = {}
    for file in bronze.glob("date=*/hour=*/HS_H09_*_FLDK_R20_S*.DAT.bz2"):
        scan = datetime.strptime(file.name[7:20], "%Y%m%d_%H%M").replace(tzinfo=UTC)
        if scan >= cutoff or last is None or scan > last:
            continue
        partition = month_path(Path(), scan)
        if partition not in processed:
            manifest = silver / partition / MANIFEST
            processed[partition] = (
                set(pq.ParquetFile(manifest).read(columns=["scan"])["scan"].to_pylist())
                if manifest.exists() else set()
            )
        if scan in processed[partition]:
            file.unlink()


def _live_cursor(state: Path) -> dict:
    path = state / "himawari_live.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def newest_scan(now: datetime) -> datetime | None:
    cursor = _live_cursor(paths.zone("state"))
    last = datetime.fromisoformat(cursor["last_scan"]) if cursor else None
    with http.client(timeout=15) as client:
        ready = _ready_scans(client, now, last, config.settings()["himawari"])
    return ready[-1][0] if ready else None


def _tick(state: Path, now: datetime) -> dict:
    source = config.settings()["himawari"]
    bronze = paths.zone("bronze") / "himawari"
    silver = paths.zone("silver") / "cloud_kabkota"
    tiles = paths.zone("gold") / "tiles"
    cursor_path = state / "himawari_live.json"
    frame_path = state / "himawari_last_frame.npz"
    latency_path = state / "himawari_latency.jsonl"
    cursor = _live_cursor(state)
    last = datetime.fromisoformat(cursor["last_scan"]) if cursor else None
    last_latency = cursor.get("latency_min_last")
    last_record = None
    if latency_path.exists():
        with latency_path.open("rb") as log:
            log.seek(-min(latency_path.stat().st_size, 4096), 2)
            lines = log.read().splitlines()
            if lines:
                last_record = json.loads(lines[-1])

    logger = get_logger(__name__)
    zonal = None
    previous = None
    reports = {}
    with http.client(timeout=15) as client, ThreadPoolExecutor(max_workers=3) as pool:
        ready = _ready_scans(client, now, last, source)
        if ready:
            grid = mergir_grid()
            write_bounds(tiles, imerg_grid(), grid)
        for scan, ready_at, objects in ready:
            report = reports.setdefault(scan.strftime("%Y-%m"), {
                "slots": 0, "bronze_bytes": 0, "rows": 0, "silver_bytes": 0,
            })
            directory = bronze / f"date={scan:%Y-%m-%d}" / f"hour={scan:%H}"
            directory.mkdir(parents=True, exist_ok=True)
            downloads = [
                pool.submit(_download, client, source["bucket_url"], key, size, directory)
                for key, size in objects.items()
            ]
            files = [download.result() for download in downloads]
            partition = month_path(silver, scan)
            model_slot = is_model_slot(scan)
            if model_slot and zonal is None:
                settings = config.settings()["mergir"]
                membership = pq.read_table(paths.zone("ref") / "mergir_membership.parquet")
                zonal = Zonal(membership, grid.width * grid.height)
                motion = CloudMotion(grid, settings["flow"])
                if frame_path.exists():
                    with np.load(frame_path) as frame:
                        previous = datetime.fromisoformat(frame["slot"].item()), frame["tb"]
            # A saved frame for this scan means its feature rows were already published.
            saved = previous is not None and previous[0] == scan
            tb = previous[1] if saved else decode(files, state / "himawari_index.npz", grid, source)
            write_cloud(tile_path(tiles, "cloud", scan), tb)
            if model_slot and not saved:
                rows = cloud_rows(scan, tb, previous, zonal, motion, settings["cold_k"])
                merge_scan(partition / CLOUD_PARTS[2], rows, scan)
                report["rows"] += rows.num_rows
                report["silver_bytes"] = (partition / CLOUD_PARTS[2]).stat().st_size
                with atomic_write(frame_path) as file:
                    np.savez(file, tb=tb, slot=scan.isoformat())
                previous = scan, tb

            stats = [file.stat() for file in files]
            size = sum(stat.st_size for stat in stats)
            manifest = pa.Table.from_pylist([{
                "scan": scan,
                "files": [file.name for file in files],
                "fetched_at": datetime.fromtimestamp(max(stat.st_mtime for stat in stats), UTC),
                "bytes": size,
            }], schema=MANIFEST_SCHEMA)
            merge_scan(partition / MANIFEST, manifest, scan)
            last_latency = (ready_at - scan).total_seconds() / 60
            if last_record is not None and last_record["scan"] == scan.isoformat():
                last_latency = last_record["latency_min"]
            else:
                with latency_path.open("a", encoding="utf-8") as log:
                    log.write(json.dumps({
                        "scan": scan.isoformat(), "ready_at": ready_at.isoformat(),
                        "latency_min": last_latency, "model_slot": model_slot,
                    }) + "\n")
            logger.info(
                "Himawari scan %s latency_min=%.2f model_slot=%s",
                scan.isoformat(), last_latency, model_slot,
            )
            write_json(cursor_path, {
                "last_scan": scan.isoformat(), "latency_min_last": last_latency,
            })
            last = scan
            report["slots"] += 1
            report["bronze_bytes"] += size
            report["latency_min"] = last_latency

    _prune(bronze, silver, now - RETENTION, last)
    health = state / "health"
    health.mkdir(exist_ok=True)
    write_json(health / "himawari.json", {
        "last_tick": now.isoformat(), "last_scan": last.isoformat() if last else None,
        "latency_min_last": last_latency, "scans_processed": len(ready),
    })
    logger.info("Himawari tick scans_processed=%d", len(ready))
    return reports


def tick() -> dict:
    try:
        state = paths.zone("state")
        state.mkdir(parents=True, exist_ok=True)
        with file_lock(state, "himawari.lock", blocking=False) as acquired:
            if not acquired:
                return {}
            return _tick(state, datetime.now(UTC))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None


def fill(
    start: Annotated[str, typer.Option(help="UTC start (ISO), a model slot.")],
    end: Annotated[str, typer.Option(help="UTC end (ISO).")],
) -> None:
    """Add past model slots (features and cloud tiles) without touching the live cursor."""
    source = config.settings()["himawari"]
    settings = config.settings()["mergir"]
    state = paths.zone("state")
    silver = paths.zone("silver") / "cloud_kabkota"
    tiles = paths.zone("gold") / "tiles"
    work = paths.zone("tmp") / "himawari_fill"
    work.mkdir(parents=True, exist_ok=True)
    grid = mergir_grid()
    zonal = Zonal(pq.read_table(paths.zone("ref") / "mergir_membership.parquet"),
                  grid.width * grid.height)
    motion = CloudMotion(grid, settings["flow"])
    logger = get_logger(__name__)
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    scans = []
    scan = first - SLOT
    while scan <= last:
        scans.append(scan)
        scan += SLOT

    def fetch(scan: datetime) -> list[Path] | None:
        prefix = source["prefix"].format(
            yyyy=f"{scan:%Y}", mm=f"{scan:%m}", dd=f"{scan:%d}", HHMM=f"{scan:%H%M}",
        )
        files = []
        for segment in source["segments"]:
            key = f"{prefix}HS_H09_{scan:%Y%m%d_%H%M}_{source['band']}_FLDK_R20_S{segment:02}10.DAT.bz2"
            response = http.get(client, source["bucket_url"] + key)
            if response.status_code != 200:
                return None
            path = work / Path(key).name
            write_bytes(path, response.content)
            files.append(path)
        return files

    previous = None
    # S3 is slow per connection from here, so segments download 8 at a time ahead of decoding.
    with http.client(timeout=300) as client, ThreadPoolExecutor(max_workers=8) as pool:
        for scan, files in zip(scans, pool.map(fetch, scans)):
            if files is None:
                logger.warning("Himawari fill %s incomplete on S3, skipped", scan.isoformat())
                previous = None
                continue
            tb = decode(files, state / "himawari_index.npz", grid, source)
            if scan >= first:
                rows = cloud_rows(scan, tb, previous, zonal, motion, settings["cold_k"])
                write_cloud(tile_path(tiles, "cloud", scan), tb)
                with file_lock(state, "himawari.lock"):
                    merge_scan(month_path(silver, scan, CLOUD_PARTS[2]), rows, scan)
                logger.info("Himawari fill %s rows=%d", scan.isoformat(), rows.num_rows)
            previous = scan, tb
            for path in files:
                path.unlink()

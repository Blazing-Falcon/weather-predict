from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import numpy as np
import typer
from numpy.typing import NDArray
from PIL import Image

from nowcast import config, paths
from nowcast.cloud import valid_tb
from nowcast.locks import file_lock
from nowcast.log import get_logger
from nowcast.registry.grids import Grid, imerg_grid, mergir_grid
from nowcast.storage import atomic_write, write_json

RAIN_BOUNDS = {"west": 94.0, "east": 142.0, "south": -11.5, "north": 6.5}


def tile_path(root: Path, layer: str, time: datetime) -> Path:
    suffix = "png" if layer == "rain" else "webp"
    return root / layer / f"{time:%Y%m%dT%H%MZ}.{suffix}"


def write_bounds(root: Path, rain: Grid, cloud: Grid) -> None:
    bounds = {
        "rain": {**RAIN_BOUNDS, "width": rain.width, "height": rain.height},
        "cloud": {
            "west": cloud.lon_edges[0],
            "east": cloud.lon_edges[-1],
            "south": cloud.lat_edges[0],
            "north": cloud.lat_edges[-1],
            "width": cloud.width,
            "height": cloud.height,
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "bounds.json", bounds)


def write_image(path: Path, pixels: NDArray, format: str, **options) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_write(path) as file:
        Image.fromarray(pixels[::-1]).save(file, format=format, **options)
    get_logger(__name__).info("Tile %s bytes=%d", path.name, path.stat().st_size)


def write_rain(path: Path, values: NDArray, grid: Grid) -> None:
    rain = np.where(np.isfinite(values) & (values >= 0), values, 0)
    pixels = np.rint(np.clip(rain, 0, 25.5) * 10).astype(np.uint8)
    write_image(path, pixels.reshape(grid.height, grid.width), "PNG")


def cloud_pixels(tb: NDArray) -> NDArray:
    cold = np.clip((288.15 - np.where(valid_tb(tb), tb, 288.15)) / 95, 0, 1)
    return np.rint(cold**0.85 * 255).astype(np.uint8)


def write_cloud(path: Path, tb: NDArray) -> None:
    write_image(path, cloud_pixels(tb), "WEBP", quality=80)


def backfill(
    rain_from: Annotated[str, typer.Option(help="Earliest rain slot, as an ISO timestamp.")],
    cloud_from: Annotated[str, typer.Option(help="Earliest cloud scan, as an ISO timestamp.")],
) -> None:
    """Render missing rain and cloud tiles from retained bronze data."""
    from nowcast.sources.himawari import decode
    from nowcast.sources.imerg import read_crop, select_granules

    try:
        rain_since, cloud_since = map(datetime.fromisoformat, (rain_from, cloud_from))
        if rain_since.tzinfo is None or cloud_since.tzinfo is None:
            raise ValueError
    except ValueError:
        raise typer.BadParameter("Supply ISO timestamps with a UTC offset or Z.") from None
    rain_since, cloud_since = rain_since.astimezone(UTC), cloud_since.astimezone(UTC)

    bronze = paths.zone("bronze")
    root = paths.zone("gold") / "tiles"
    state = paths.zone("state")
    state.mkdir(parents=True, exist_ok=True)
    rain, cloud = imerg_grid(), mergir_grid()
    write_bounds(root, rain, cloud)
    crops = {
        path.stem: path
        for part in (bronze / "imerg").glob("year=*/month=*")
        if (part.parent.name, part.name) >= (f"year={rain_since:%Y}", f"month={rain_since:%m}")
        for path in part.glob("*.nc4")
    }
    granules = select_granules(
        [{"title": title} for title in crops], rain_since, datetime.max.replace(tzinfo=UTC)
    )
    rain_count = cloud_count = 0
    for granule in granules:
        target = tile_path(root, "rain", granule.slot)
        if target.exists():
            continue
        with file_lock(state, "imerg.lock"):
            if not target.exists():
                write_rain(target, read_crop(crops[granule.title], rain), rain)
                rain_count += 1

    source = config.settings()["himawari"]
    scans = defaultdict(dict)
    for file in (bronze / "himawari").glob(
        f"date=*/hour=*/HS_H09_*_{source['band']}_FLDK_R20_S??10.DAT.bz2"
    ):
        scan = datetime.strptime(file.name[7:20], "%Y%m%d_%H%M").replace(tzinfo=UTC)
        if scan >= cloud_since:
            scans[scan][int(file.name.split("_S")[-1][:2])] = file

    for scan, files in sorted(scans.items()):
        target = tile_path(root, "cloud", scan)
        segments = source["segments"]
        if target.exists() or not all(segment in files for segment in segments):
            continue
        with file_lock(state, "himawari.lock"):
            selected = [files[s] for s in segments]
            if not target.exists() and all(file.exists() for file in selected):
                tb = decode(selected, state / "himawari_index.npz", cloud, source)
                write_cloud(target, tb)
                cloud_count += 1
    typer.echo(f"Rendered {rain_count} rain tiles and {cloud_count} cloud tiles.")

from time import perf_counter

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from numpy.typing import NDArray
from shapely.geometry.base import BaseGeometry

from nowcast import paths
from nowcast.log import get_logger
from nowcast.registry.boundaries import BOUNDARIES_NAME
from nowcast.registry.grids import Grid, imerg_grid, mergir_grid
from nowcast.storage import write_table

SCHEMA = pa.schema([("area_id", pa.int16()), ("cell", pa.int32()), ("weight", pa.float32())])


def admin_areas(fields: list[str]) -> gpd.GeoDataFrame:
    ref = paths.zone("ref")
    areas = (
        pq.read_table(
            ref / "dim_area.parquet",
            columns=["area_id", "geo_name", *fields],
            filters=[("is_admin", "=", True)],
        )
        .to_pandas()
        .sort_values("area_id")
    )
    if not areas["area_id"].is_unique:
        raise ValueError("Duplicate admin area_id in dim_area")
    polygons = gpd.read_file(ref / BOUNDARIES_NAME).set_index("shapeName").geometry
    return gpd.GeoDataFrame(areas, geometry=polygons.loc[areas["geo_name"]].array)


def overlap_weights(
    geom: BaseGeometry, grid: Grid
) -> tuple[NDArray[np.int32], NDArray[np.float32]]:
    west, south, east, north = geom.bounds
    x0 = max(0, grid.lon_edges.searchsorted(west, side="right") - 1)
    y0 = max(0, grid.lat_edges.searchsorted(south, side="right") - 1)
    x1 = min(grid.width, grid.lon_edges.searchsorted(east))
    y1 = min(grid.height, grid.lat_edges.searchsorted(north))
    columns = np.arange(x0, x1, dtype=np.int32)
    rows = np.arange(y0, y1, dtype=np.int32)[:, None]
    cells = shapely.box(
        grid.lon_edges[columns],
        grid.lat_edges[rows],
        grid.lon_edges[columns + 1],
        grid.lat_edges[rows + 1],
    )
    shapely.prepare(geom)
    intersects = shapely.intersects(geom, cells)
    indices = (rows * grid.width + columns)[intersects]
    cells = cells[intersects]
    partial = ~shapely.covers(geom, cells)
    weights = np.ones(len(cells), dtype=np.float32)
    weights[partial] = shapely.area(shapely.intersection(geom, cells[partial])) / shapely.area(
        cells[partial]
    )
    positive = weights > 0
    return indices[positive], weights[positive]


def build(selection: str) -> None:
    areas = admin_areas(["area_km2"])
    factories = {"imerg": imerg_grid, "mergir": mergir_grid}
    for name in factories if selection == "all" else (selection,):
        _build_grid(areas, factories[name]())


def _build_grid(areas: gpd.GeoDataFrame, grid: Grid) -> None:
    started = perf_counter()
    logger = get_logger(__name__)
    tables = []
    counts = []
    errors = []
    failed = 0
    for area in areas.itertuples(index=False):
        cells, weights = overlap_weights(area.geometry, grid)
        covered = np.dot(weights, grid.cell_km2[cells // grid.width])
        error = 100 * abs(covered / area.area_km2 - 1)
        counts.append(len(cells))
        errors.append(error)
        if not len(cells) or not error <= 2:
            failed += 1
            logger.error(
                "%s area_id=%d %s, cells=%d, overlap=%.6f km2, reference=%.6f km2, error=%.4f%%",
                grid.name,
                area.area_id,
                area.geo_name,
                len(cells),
                covered,
                area.area_km2,
                error,
            )
        tables.append(
            pa.Table.from_arrays(
                [np.full(len(cells), area.area_id, dtype=np.int16), cells, weights], schema=SCHEMA
            )
        )

    logger.info(
        "%s cells per area min/median/max %d/%g/%d, area error median/max %.4f%%/%.4f%%",
        grid.name,
        min(counts),
        np.median(counts),
        max(counts),
        np.median(errors),
        max(errors),
    )
    if failed:
        raise ValueError(f"{grid.name} membership validation failed for {failed} admin areas")

    output = paths.zone("ref") / f"{grid.name}_membership.parquet"
    table = pa.concat_tables(tables)
    write_table(table, output)
    logger.info(
        "Wrote %s (%d rows, %d bytes, %.2f s)",
        output,
        table.num_rows,
        output.stat().st_size,
        perf_counter() - started,
    )

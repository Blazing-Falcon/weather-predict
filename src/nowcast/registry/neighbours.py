import geopandas as gpd
import numpy as np
import pyarrow as pa
import shapely

from nowcast import paths
from nowcast.log import get_logger
from nowcast.parquet import write_table
from nowcast.registry.grids import EARTH_RADIUS_KM
from nowcast.registry.membership import admin_areas

SCHEMA = pa.schema(
    [
        ("area_id", pa.int16()),
        ("neighbour_id", pa.int16()),
        ("kind", pa.string()),
        ("dist_km", pa.float32()),
    ]
)


def neighbour_table(areas: gpd.GeoDataFrame) -> pa.Table:
    areas = areas.sort_values("area_id")
    ids = areas["area_id"].to_numpy()
    buffered = shapely.buffer(areas.geometry.to_numpy(), 0.001)
    left, right = shapely.STRtree(buffered).query(buffered, predicate="intersects")
    borders = [set() for _ in ids]
    for i, j in zip(left, right, strict=True):
        if i != j:
            borders[i].add(j)

    lon = np.deg2rad(areas["centroid_lon"].to_numpy(dtype=np.float64))
    lat = np.deg2rad(areas["centroid_lat"].to_numpy(dtype=np.float64))
    haversine = (
        np.sin((lat[:, None] - lat) / 2) ** 2
        + np.cos(lat[:, None]) * np.cos(lat) * np.sin((lon[:, None] - lon) / 2) ** 2
    )
    distances = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(haversine, 0, 1)))
    np.fill_diagonal(distances, np.inf)

    records = []
    counts = []
    for i, border in enumerate(borders):
        if border:
            neighbours = sorted(border)
            kind = "border"
        else:
            nearest = np.argsort(distances[i], kind="stable")[: min(6, len(ids) - 1)]
            neighbours = sorted(nearest)
            kind = "nearest"
        counts.append(len(neighbours))
        for j in neighbours:
            records.append(
                {
                    "area_id": ids[i],
                    "neighbour_id": ids[j],
                    "kind": kind,
                    "dist_km": distances[i, j],
                }
            )

    logger = get_logger(__name__)
    logger.info("Border pairs %d", sum(map(len, borders)) // 2)
    isolated = [name for name, border in zip(areas["geo_name"], borders, strict=True) if not border]
    logger.info("Areas with zero border neighbours (%d): %s", len(isolated), ", ".join(isolated))
    logger.info(
        "Neighbours per area min/median/max %d/%g/%d", min(counts), np.median(counts), max(counts)
    )
    return pa.Table.from_pylist(records, schema=SCHEMA)


def build() -> None:
    areas = admin_areas(["centroid_lon", "centroid_lat"])
    table = neighbour_table(areas)
    output = paths.zone("ref") / "neighbours.parquet"
    write_table(table, output)
    get_logger(__name__).info("Wrote %s (%d rows)", output, table.num_rows)

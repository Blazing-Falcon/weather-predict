import csv
from collections import Counter

import geopandas as gpd
import pyarrow as pa

from nowcast import config, paths
from nowcast.log import get_logger
from nowcast.parquet import write_table
from nowcast.registry.boundaries import BOUNDARIES_NAME

PROVINCES = (
    (11, 21, "Sumatera", "WIB"),
    (31, 36, "Jawa", "WIB"),
    (51, 53, "Bali-Nusa Tenggara", "WITA"),
    (61, 62, "Kalimantan", "WIB"),
    (63, 65, "Kalimantan", "WITA"),
    (71, 76, "Sulawesi", "WITA"),
    (81, 82, "Maluku", "WIT"),
    (91, 96, "Papua", "WIT"),
)
ADMIN_FIELDS = ("kemendagri_code", "kemendagri_name", "prov_code", "prov_name")
SCHEMA = pa.schema(
    [
        ("area_id", pa.int16()),
        ("geo_name", pa.string()),
        *((field, pa.string()) for field in ADMIN_FIELDS),
        ("island", pa.string()),
        ("tz", pa.string()),
        ("is_admin", pa.bool_()),
        ("centroid_lon", pa.float32()),
        ("centroid_lat", pa.float32()),
        ("area_km2", pa.float32()),
        ("bmkg_adm4", pa.string()),
        ("bmkg_lon", pa.float32()),
        ("bmkg_lat", pa.float32()),
    ]
)


def island_and_tz(prov_code: str) -> tuple[str, str]:
    if len(prov_code) == 2 and prov_code.isascii() and prov_code.isdecimal():
        code = int(prov_code)
        for first, last, island, tz in PROVINCES:
            if first <= code <= last:
                return island, tz
    raise ValueError(f"Unknown province code {prov_code!r}")


def build() -> None:
    geo = config.CONFIG_DIR / "geo"
    ref = paths.zone("ref")
    with (geo / "kabkota_codes.csv").open(encoding="utf-8", newline="") as source:
        rows = sorted(csv.DictReader(source), key=lambda row: int(row["area_id"]))
    if len(rows) != 519:
        raise ValueError(f"Expected 519 CSV rows, found {len(rows)}")

    names = {int(row["area_id"]): row["geo_name"] for row in rows}
    if len(names) != len(rows) or len(set(names.values())) != len(rows):
        raise ValueError("CSV area_id and geo_name must each contain 519 unique values")

    admin_codes = [row["kemendagri_code"] for row in rows if row["is_admin"] == "1"]
    if len(admin_codes) != 514 or not all(admin_codes) or len(set(admin_codes)) != len(admin_codes):
        raise ValueError("Expected 514 admin rows with unique nonempty kemendagri codes")

    full = gpd.read_file(ref / BOUNDARIES_NAME)
    if "shapeName" not in full:
        raise ValueError(f"{BOUNDARIES_NAME} is missing the shapeName property")
    counts = Counter(full["shapeName"])
    mismatches = [name for name in names.values() if counts[name] != 1]
    if mismatches:
        details = ", ".join(f"{name!r} ({counts[name]} matches)" for name in mismatches[:5])
        raise ValueError(f"Expected one shapeName per CSV geo_name. Mismatches: {details}")
    if len(full) != 519:
        raise ValueError(f"Expected 519 full-resolution features, found {len(full)}")

    display = gpd.read_file(geo / "kabkota.geojson")
    if not {"i", "n"}.issubset(display.columns):
        raise ValueError("Display GeoJSON must contain i and n properties")
    for area_id, name in zip(display["i"], display["n"], strict=True):
        if not isinstance(name, str) or names.get(area_id) != name:
            raise ValueError(f"Display feature i={area_id!r}, n={name!r} does not match the CSV")
    if len(display) != 519 or not display["i"].is_unique:
        raise ValueError("Expected 519 display features with unique i properties")

    polygons = full.set_index("shapeName").geometry.reindex(list(names.values())).to_crs(6933)
    polygons.index = list(names)
    display_polygons = display.set_index("i").geometry.reindex(list(names)).to_crs(6933)
    if not polygons.geom_type.isin(("Polygon", "MultiPolygon")).all():
        raise ValueError("Full-resolution features must all contain polygon geometry")
    if not display_polygons.geom_type.isin(("Polygon", "MultiPolygon")).all():
        raise ValueError("Display features must all contain polygon geometry")

    centroids = polygons.centroid
    lon_lat = centroids.to_crs(4326)
    area = polygons.area
    if not ((area > 0) & (area < float("inf"))).all():
        raise ValueError("Full-resolution polygons must all have finite positive area")
    admin_regions = {
        int(row["area_id"]): island_and_tz(row["prov_code"])
        for row in rows
        if row["is_admin"] == "1"
    }
    admin_centroids = centroids.loc[list(admin_regions)]

    records = []
    for row in rows:
        area_id = int(row["area_id"])
        is_admin = area_id in admin_regions
        region_id = area_id if is_admin else admin_centroids.distance(centroids[area_id]).idxmin()
        island, tz = admin_regions[region_id]
        records.append(
            {
                "area_id": area_id,
                "geo_name": row["geo_name"],
                **{field: row[field] if is_admin else None for field in ADMIN_FIELDS},
                "island": island,
                "tz": tz,
                "is_admin": is_admin,
                "centroid_lon": lon_lat[area_id].x,
                "centroid_lat": lon_lat[area_id].y,
                "area_km2": area[area_id] / 1_000_000,
            }
        )

    output = ref / "dim_area.parquet"
    write_table(pa.Table.from_pylist(records, schema=SCHEMA), output)

    difference = (display_polygons.area - area).abs() / area * 100
    largest = ", ".join(
        f"{names[area_id]} ({percent:.2f}%)" for area_id, percent in difference.nlargest(5).items()
    )
    logger = get_logger(__name__)
    logger.info("Wrote %s (%d rows)", output, len(records))
    logger.info(
        "Full-resolution vs display area difference: median %.2f%%, max %.2f%%. Largest: %s",
        difference.median(),
        difference.max(),
        largest,
    )

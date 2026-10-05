import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import quote

import geopandas as gpd
import h5py
import numpy as np
import pyarrow.parquet as pq
import shapely
from matplotlib import pyplot as plt

from nowcast import http
from nowcast.config import settings
from nowcast.paths import zone

OUTPUT = Path(__file__).resolve().parents[1] / "docs/experiments"
EARTH_RADIUS_KM = 6371.0072


def imerg_crop(now, config):
    with http.client(timeout=30) as client:
        response = http.get(
            client,
            settings()["cmr"]["granules"],
            params={
                "collection_concept_id": config["collection"],
                "temporal": f"{now - timedelta(hours=12):%Y-%m-%dT%H:%M:%SZ},"
                f"{now:%Y-%m-%dT%H:%M:%SZ}",
                "sort_key": "-start_date",
                "page_size": 1,
            },
        ).raise_for_status()
    entries = response.json()["feed"]["entry"]
    if not entries:
        raise ValueError("CMR returned no IMERG Early granules in the last 12 hours")
    entry = entries[0]
    title = quote(entry["title"], safe=":")
    constraint = f"{config['variable']}[0:1:0]"
    for axis in ("lon", "lat"):
        first, last = config[f"{axis}_index"]
        constraint += f"[{first}:1:{last}]"
    url = f"{config['opendap']}{title}.dap.nc4?dap4.ce={quote(constraint, safe='/:')}"
    with http.client(auth=True, timeout=30) as client:
        content = http.get(client, url).raise_for_status().content
    with h5py.File(io.BytesIO(content), "r") as file:
        rain = file[config["variable"]][0].T
    return entry, rain


def membership(polygons, lon_edges, lat_edges):
    west, south = np.meshgrid(lon_edges[:-1], lat_edges[:-1])
    east, north = np.meshgrid(lon_edges[1:], lat_edges[1:])

    started = perf_counter()
    cells = shapely.box(west.ravel(), south.ravel(), east.ravel(), north.ravel())
    tree = shapely.STRtree(cells)
    overlaps = []
    for polygon in polygons:
        indices = tree.query(polygon, predicate="intersects")
        shapely.prepare(polygon)
        covered = shapely.covers(polygon, cells[indices])
        boundary = cells[indices[~covered]]
        weights = np.ones(len(indices))
        weights[~covered] = shapely.area(shapely.intersection(polygon, boundary)) / shapely.area(
            boundary
        )
        overlaps.append((indices, weights))
    return overlaps, perf_counter() - started


def figures(boundaries, rows):
    plt.switch_backend("Agg")
    image_dir = OUTPUT / "img"
    image_dir.mkdir(parents=True, exist_ok=True)
    missing = [row["geo_name"] for row in rows if row["cells_A"] == 0]
    highlighted = boundaries.loc[missing].geometry
    fig, ax = plt.subplots(figsize=(11, 5))
    boundaries.geometry.simplify(0.005).plot(
        ax=ax, color="#e2e8f0", edgecolor="#64748b", linewidth=0.25
    )
    if missing:
        highlighted.plot(ax=ax, color="#c92a2a", edgecolor="#c92a2a", linewidth=0.9)
        points = highlighted.representative_point()
        ax.scatter(
            points.x,
            points.y,
            s=40,
            facecolors="none",
            edgecolors="#c92a2a",
            linewidths=1,
            label=f"Admin areas with zero cell centres ({len(missing)})",
        )
        ax.legend(loc="upper right", frameon=False)
    ax.set(title="IMERG Centre Membership", xlabel="Longitude (°)", ylabel="Latitude (°)")
    fig.savefig(image_dir / "01_zero_centre_cells.png", dpi=170, bbox_inches="tight")
    plt.close(fig)

    counts = [row["cells_C"] for row in rows]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(
        [count for count in counts if count > 0],
        bins=np.geomspace(1, max(counts), 23),
        color="#246b8f",
        edgecolor="white",
    )
    ax.set(
        xscale="log",
        title="IMERG Area-Weighted Membership",
        xlabel="Cells with positive overlap per admin area",
        ylabel="Admin areas",
    )
    fig.tight_layout()
    fig.savefig(image_dir / "01_area_weighted_cell_counts.png", dpi=170)
    plt.close(fig)


def main():
    now = datetime.now(UTC).replace(microsecond=0)
    config = settings()["imerg"]
    lon_first, lon_last = config["lon_index"]
    lat_first, lat_last = config["lat_index"]
    lon_edges = (np.arange(lon_first, lon_last + 2) - 1800) / 10
    lat_edges = (np.arange(lat_first, lat_last + 2) - 900) / 10
    shape = (len(lat_edges) - 1, len(lon_edges) - 1)
    lat_rad = np.deg2rad(lat_edges)
    cell_km2 = np.repeat(EARTH_RADIUS_KM**2 * np.deg2rad(0.1) * np.diff(np.sin(lat_rad)), shape[1])
    edge_cos = np.cos(lat_rad)
    cos_change = np.abs(np.diff(edge_cos))
    lon_centres = (2 * (np.arange(lon_first, lon_last + 1) - 1800) + 1) / 20
    lat_centres = (2 * (np.arange(lat_first, lat_last + 1) - 900) + 1) / 20
    x, y = np.meshgrid(lon_centres, lat_centres)
    x, y = x.ravel(), y.ravel()

    ref = zone("ref")
    boundaries = gpd.read_file(ref / "kabkota_full.geojson").set_index("shapeName")
    fields = ["area_id", "geo_name", "kemendagri_name", "area_km2"]
    rows = pq.read_table(
        ref / "dim_area.parquet", columns=fields, filters=[("is_admin", "=", True)]
    ).to_pylist()
    polygons = boundaries.loc[[row["geo_name"] for row in rows]].geometry.array
    entry, rain = imerg_crop(now, config)
    if rain.shape != shape:
        raise ValueError(f"Expected IMERG crop shape {shape}, found {rain.shape}")
    rain = rain.ravel()
    valid = np.isfinite(rain) & (rain >= 0)

    overlaps, elapsed = membership(polygons, lon_edges, lat_edges)
    for row, polygon, (indices, weights) in zip(rows, polygons, overlaps, strict=True):
        centres = indices[shapely.contains_xy(polygon, x[indices], y[indices])]
        positive = weights > 0
        overlap = indices[positive]
        weights = weights[positive]
        covered_km2 = float(np.dot(weights, cell_km2[overlap]))
        centre_rain = rain[centres[valid[centres]]]
        overlap_valid = valid[overlap]
        mean_a = float(np.mean(centre_rain, dtype=np.float64)) if centre_rain.size else None
        mean_c = (
            float(np.average(rain[overlap[overlap_valid]], weights=weights[overlap_valid]))
            if overlap_valid.any()
            else None
        )
        row.update(
            cells_A=len(centres),
            cells_B=len(indices),
            cells_C=len(overlap),
            covered_km2_C=covered_km2,
            absolute_area_error_percent=100 * abs(covered_km2 / row["area_km2"] - 1),
            rain_A_mm_h=mean_a,
            rain_C_mm_h=mean_c,
            absolute_rain_difference_mm_h=(
                abs(mean_a - mean_c) if mean_a is not None and mean_c is not None else None
            ),
        )

    cell_counts = {}
    for method in "ABC":
        counts = np.array([row[f"cells_{method}"] for row in rows])
        cell_counts[method] = {
            "zero": int(np.count_nonzero(counts == 0)),
            "min": int(counts.min()),
            "median": float(np.median(counts)),
            "max": int(counts.max()),
        }
    errors = np.array([row["absolute_area_error_percent"] for row in rows])
    comparable = [row for row in rows if row["absolute_rain_difference_mm_h"] is not None]
    by_difference = sorted(
        comparable, key=lambda row: (-row["absolute_rain_difference_mm_h"], row["area_id"])
    )
    differences = [row["absolute_rain_difference_mm_h"] for row in by_difference]
    by_size = sorted(rows, key=lambda row: (row["area_km2"], row["area_id"]))
    result = {
        "run_at": now.isoformat(),
        "imerg_title": entry["title"],
        "imerg_slot_start": entry["time_start"],
        "crop_shape_lat_lon": list(shape),
        "valid_rain_cells": int(np.count_nonzero(valid)),
        "max_cos_lat_absolute_change": float(cos_change.max()),
        "max_cos_lat_relative_change_percent": float(
            100 * (cos_change / np.minimum(edge_cos[:-1], edge_cos[1:])).max()
        ),
        "admin_areas": len(rows),
        "cell_counts": cell_counts,
        "area_check_C": {
            "median_absolute_percent_error": float(np.median(errors)),
            "max_absolute_percent_error": float(errors.max()),
            "areas_over_2_percent": int(np.count_nonzero(errors > 2)),
        },
        "rain_difference": {
            "areas_compared": len(comparable),
            "areas_with_A_mean": sum(row["rain_A_mm_h"] is not None for row in rows),
            "areas_with_C_mean": sum(row["rain_C_mm_h"] is not None for row in rows),
            "median_mm_h": float(np.median(differences)) if differences else None,
            "p90_mm_h": float(np.percentile(differences, 90)) if differences else None,
            "max_mm_h": max(differences, default=None),
        },
        "runtime_C_seconds": elapsed,
        "largest_rain_differences": by_difference[:10],
        "smallest_areas": by_size[:10],
        "areas": rows,
    }
    figures(boundaries, rows)
    path = OUTPUT / "results/01_membership_methods.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    print(f"IMERG {entry['title']}")
    print(f"Crop {shape[0]} x {shape[1]} (lat x lon), valid rain cells {valid.sum():,}")
    print(f"Admin areas {len(rows)}")
    for method, stats in cell_counts.items():
        print(
            f"{method} cells: zero {stats['zero']}, min {stats['min']}, "
            f"median {stats['median']:g}, max {stats['max']}"
        )
    area = result["area_check_C"]
    print(
        f"C area error: median {area['median_absolute_percent_error']:.4f}%, "
        f"max {area['max_absolute_percent_error']:.4f}%, over 2% {area['areas_over_2_percent']}"
    )
    print(f"Max cos(lat) change across one cell: {result['max_cos_lat_absolute_change']:.8f}")
    if differences:
        diff = result["rain_difference"]
        print(
            f"|A - C| rain ({diff['areas_compared']} areas): median {diff['median_mm_h']:.4f}, "
            f"p90 {diff['p90_mm_h']:.4f}, max {diff['max_mm_h']:.4f} mm/h"
        )
    else:
        print("|A - C| rain: no comparable areas")
    print(f"C runtime: {elapsed:.2f} s")


if __name__ == "__main__":
    main()

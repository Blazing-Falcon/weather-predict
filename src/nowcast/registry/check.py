import csv

import geopandas as gpd
import pyarrow.parquet as pq
import shapely

from nowcast import config, http, paths
from nowcast.registry.bmkg_points import NEAR_LIMIT_KM, fetch_json
from nowcast.registry.boundaries import BOUNDARIES_NAME
from nowcast.registry.dim_area import ADMIN_FIELDS
from nowcast.registry.grids import imerg_grid, mergir_grid


def wilayah_mismatches():
    with (config.CONFIG_DIR / "geo" / "kabkota_codes.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        aliases = [row for row in csv.DictReader(source) if row["match"] == "alias"]

    mismatches = []
    provinces = {}
    with http.client() as client:
        client.event_hooks["request"].append(http.rate_limit(1 / 5))
        for province in sorted({row["prov_code"] for row in aliases}):
            url = f"https://wilayah.id/api/regencies/{province}.json"
            payload, error = fetch_json(client, url, retries=0)
            provinces[province] = ({r["code"]: r["name"] for r in payload.get("data", [])}, error)

    for row in aliases:
        code, expected = row["kemendagri_code"], row["kemendagri_name"]
        names, error = provinces[row["prov_code"]]
        actual = names.get(code)
        if error is not None:
            mismatches.append(f"{code} {expected!r}: province request failed ({error})")
        elif actual is None:
            mismatches.append(f"{code} {expected!r}: code missing")
        elif actual.strip().casefold() != expected.strip().casefold():
            mismatches.append(f"{code} expected {expected!r}, found {actual!r}")
    return len(aliases), mismatches


def run(wilayah: bool) -> bool:
    ok = True

    def report(name, passed, detail):
        nonlocal ok
        print(f"{'PASS' if passed else 'FAIL'} {name} {detail}", flush=True)
        ok = ok and passed

    ref = paths.zone("ref")
    areas = pq.read_table(ref / "dim_area.parquet").to_pandas().set_index("area_id")
    admin = areas[areas["is_admin"].eq(True)]
    non_admin = areas[areas["is_admin"].eq(False)]
    report(
        "dim_area",
        len(areas) == 519
        and len(admin) == 514
        and len(non_admin) == 5
        and non_admin[list(ADMIN_FIELDS)].isna().all().all(),
        f"rows={len(areas)} admin={len(admin)} non-admin={len(non_admin)} "
        f"({', '.join(non_admin['geo_name'])})",
    )

    for grid in (imerg_grid(), mergir_grid()):
        membership = pq.read_table(ref / f"{grid.name}_membership.parquet").to_pandas()
        counts = membership.groupby("area_id").size().reindex(admin.index, fill_value=0)
        report(
            f"{grid.name}_cells",
            counts.min() > 0,
            f"min={counts.min():g} median={counts.median():g}",
        )
        cell_km2 = grid.cell_km2[membership["cell"].to_numpy() // grid.width]
        covered = (membership["weight"] * cell_km2).groupby(membership["area_id"]).sum()
        errors = (covered.reindex(admin.index, fill_value=0) / admin["area_km2"] - 1).abs() * 100
        worst = admin.loc[errors.idxmax(), "geo_name"] if len(errors) else "none"
        report(
            f"{grid.name}_weights",
            len(errors) > 0 and errors.le(2).all(),
            f"max_error={errors.max():.4f}% worst={worst}",
        )

    picks = areas[areas["bmkg_adm4"].notna()]
    missing = int(admin["bmkg_adm4"].isna().sum())
    own_inside = invalid_outside = unknown = 0
    other_area = []
    near = []
    if not picks.empty:
        polygons = gpd.read_file(ref / BOUNDARIES_NAME).set_index("shapeName").geometry
        polygons = polygons.loc[picks["geo_name"]].set_axis(picks.index)
        points = gpd.GeoSeries(
            gpd.points_from_xy(picks["bmkg_lon"], picks["bmkg_lat"]),
            index=picks.index,
            crs=4326,
        )
        inside = shapely.contains(polygons, points)
        candidates = (
            pq.read_table(
                ref / "bmkg_candidates.parquet", columns=["adm4", "area_id", "lon", "lat"]
            )
            .to_pandas()
            .set_index("adm4")
        )
        adm4 = picks["bmkg_adm4"]
        owner = adm4.map(candidates["area_id"])
        unknown = int(owner.isna().sum())
        own = owner == picks.index
        own_inside = int((inside & own).sum())

        projected = points.to_crs(6933)
        other_picks = inside & owner.notna() & ~own
        centres = polygons[other_picks].representative_point().to_crs(6933)
        other_km = projected[other_picks].distance(centres) / 1000
        other_area = [
            f"{picks.at[area_id, 'geo_name']} ({km:.3f} km)" for area_id, km in other_km.items()
        ]

        near_picks = (
            ~inside
            & own
            & picks["bmkg_lon"].eq(adm4.map(candidates["lon"]))
            & picks["bmkg_lat"].eq(adm4.map(candidates["lat"]))
        )
        near_km = projected[near_picks].distance(polygons[near_picks].to_crs(6933)) / 1000
        near = [
            f"{picks.at[area_id, 'geo_name']} ({km:.3f} km)"
            for area_id, km in near_km[near_km <= NEAR_LIMIT_KM].items()
        ]
        invalid_outside = int((~inside).sum()) - len(near)
    report(
        "bmkg_picks",
        missing == invalid_outside == unknown == 0,
        f"picked={len(picks)} own_inside={own_inside} other_area={len(other_area)} near={len(near)} "
        f"missing={missing} invalid_outside={invalid_outside} unknown_adm4={unknown} "
        f"other_area_to_representative=[{', '.join(other_area)}] "
        f"near_to_polygon=[{', '.join(near)}]",
    )

    links = pq.read_table(
        ref / "neighbours.parquet", columns=["area_id", "neighbour_id"]
    ).to_pandas()
    linked = links.loc[links["area_id"] != links["neighbour_id"], "area_id"]
    no_neighbour = int((~admin.index.isin(linked)).sum())
    report("neighbours", no_neighbour == 0, f"admin={len(admin)} missing={no_neighbour}")

    if wilayah:
        count, mismatches = wilayah_mismatches()
        for mismatch in mismatches:
            report("wilayah", False, mismatch)
        report(
            "wilayah",
            count == 29 and not mismatches,
            f"matched={count - len(mismatches)}/{count} expected=29",
        )
    return ok

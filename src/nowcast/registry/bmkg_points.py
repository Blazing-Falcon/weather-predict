import csv
import json
from datetime import UTC, datetime
from time import monotonic, sleep

import geopandas as gpd
import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
import typer

from nowcast import config, http, paths
from nowcast.http import rate_limit
from nowcast.log import get_logger
from nowcast.registry.membership import admin_areas
from nowcast.storage import write_table
from nowcast.timeslots import UTC_US, utc_now, utc_text

VILLAGES_URL = "https://wilayah.id/api/villages"
NEAR_LIMIT_KM = 10
CANDIDATE_SCHEMA = pa.schema(
    [
        ("kecamatan_code", pa.string()),
        ("adm4", pa.string()),
        ("desa", pa.string()),
        ("lat", pa.float32()),
        ("lon", pa.float32()),
        ("area_id", pa.int16()),
        ("fetched_at", UTC_US),
    ]
)


def fetch_json(client, url, **kwargs):
    try:
        response = http.get(client, url, **kwargs)
        if response.status_code != 200:
            return {}, response.status_code
        payload = response.json()
    except (httpx.RequestError, ValueError) as error:
        return {}, type(error).__name__
    if not isinstance(payload, dict):
        return {}, "TypeError"
    return payload, None


def candidate(kecamatan, wilayah_client, bmkg_client, bmkg_url, max_tries=3):
    record = {
        "adm4": None,
        "desa": None,
        "lat": None,
        "lon": None,
        "analysis_date": None,
        "tries": 0,
    }
    payload, error = fetch_json(wilayah_client, f"{VILLAGES_URL}/{kecamatan}.json")
    if error is not None:
        return record | {"error": error}

    error = "LookupError"
    for tries, village in enumerate(payload.get("data", [])[:max_tries], start=1):
        record["tries"] = tries
        code = village["code"]
        forecast, error = fetch_json(bmkg_client, bmkg_url, params={"adm4": code})
        if error is not None:
            continue
        try:
            location = forecast["lokasi"]
            desa, lat, lon = location["desa"], location["lat"], location["lon"]
        except (KeyError, TypeError) as exc:
            error = type(exc).__name__
            continue
        if lat is None or lon is None:
            error = "ValueError"
            continue

        analysis_date = next(
            (
                period["analysis_date"]
                for point in forecast.get("data", [])
                for group in point.get("cuaca", [])
                for period in group
                if period.get("analysis_date")
            ),
            None,
        )
        if analysis_date:
            timestamp = datetime.fromisoformat(analysis_date)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            analysis_date = utc_text(timestamp)
        return record | {
            "adm4": code,
            "desa": desa,
            "lat": lat,
            "lon": lon,
            "analysis_date": analysis_date,
        }

    return record | {"error": error}


def sweep(limit: int | None, max_tries: int = 3) -> None:
    bmkg = config.settings()["bmkg"]
    try:
        rpm = int(config.env("BMKG_MAX_RPM", str(bmkg["max_rpm"])))
        if rpm < 1:
            raise ValueError
    except ValueError:
        raise typer.BadParameter("BMKG_MAX_RPM must be a positive integer.") from None

    geo = config.CONFIG_DIR / "geo"
    with (geo / "kabkota_codes.csv").open(encoding="utf-8", newline="") as source:
        area_ids = {
            row["kemendagri_code"]: int(row["area_id"])
            for row in csv.DictReader(source)
            if row["is_admin"] == "1"
        }
    with (geo / "kecamatan.csv").open(encoding="utf-8", newline="") as source:
        kecamatan = list(csv.DictReader(source))

    output = paths.zone("state") / "registry" / "bmkg_candidates.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    logger = get_logger(__name__)
    latest = {}
    with output.open("a+b") as history:
        history.seek(0)
        while line := history.readline():
            # A kill can leave a partial final line. Keep all newline-terminated records.
            if not line.endswith(b"\n"):
                history.truncate(history.tell() - len(line))
                break
            record = json.loads(line)
            latest[record["kecamatan_code"]] = record["adm4"]

        pending = [row for row in kecamatan if latest.get(row["kecamatan_code"]) is None]
        skipped = len(kecamatan) - len(pending)
        batch = pending[:limit]
        done = failed = 0
        started = monotonic()
        with http.client() as wilayah_client, http.client() as bmkg_client:
            wilayah_client.event_hooks["request"].append(
                rate_limit(1 / 5, monotonic=monotonic, sleep=sleep)
            )
            bmkg_client.event_hooks["request"].append(
                rate_limit(60 / min(rpm, 55), monotonic=monotonic, sleep=sleep)
            )

            for processed, row in enumerate(batch, start=1):
                code = row["kecamatan_code"]
                kabkota = row["kabkota_code"]
                record = {
                    "kecamatan_code": code,
                    "kabkota_code": kabkota,
                    "area_id": area_ids[kabkota],
                    **candidate(code, wilayah_client, bmkg_client, bmkg["url"], max_tries),
                    "fetched_at": utc_now(),
                }
                history.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
                history.flush()
                if record["adm4"] is None:
                    failed += 1
                else:
                    done += 1
                if processed % 100 == 0:
                    eta = (monotonic() - started) / processed * (len(batch) - processed)
                    logger.info("done=%d failed=%d eta=%.0fs", done, failed, eta)

    logger.info(
        "BMKG candidates done=%d failed=%d skipped=%d deferred=%d",
        done,
        failed,
        skipped,
        len(pending) - len(batch),
    )


def pick_candidates(areas, candidates):
    points = gpd.GeoSeries(
        gpd.points_from_xy([row["lon"] for row in candidates], [row["lat"] for row in candidates]),
        crs=4326,
    )
    owners = np.array([row["area_id"] for row in candidates])
    tree = shapely.STRtree(points.to_numpy())
    projected = points.to_crs(6933).to_numpy()
    polygons = areas.geometry.to_crs(6933)
    centres = areas.geometry.representative_point().to_crs(6933)
    picks = {}
    for area, polygon, centre in zip(areas.itertuples(index=False), polygons, centres, strict=True):
        inside = tree.query(area.geometry, predicate="contains")
        polygon_km = None
        if len(inside):
            own = inside[owners[inside] == area.area_id]
            eligible = np.sort(own if len(own) else inside)
            nearest = eligible[shapely.distance(projected[eligible], centre).argmin()]
            kind = "inside" if len(own) else "other-area"
        else:
            own = np.flatnonzero(owners == area.area_id)
            if not len(own):
                continue
            distances = shapely.distance(projected[own], polygon) / 1000
            closest = distances.argmin()
            polygon_km = distances[closest]
            if not polygon_km <= NEAR_LIMIT_KM:
                continue
            nearest = own[closest]
            kind = "near"
        picks[area.area_id] = (
            candidates[nearest],
            kind,
            projected[nearest].distance(centre) / 1000,
            polygon_km,
        )
    return picks


def build_picks() -> None:
    ref = paths.zone("ref")
    history = paths.zone("state") / "registry" / "bmkg_candidates.jsonl"
    with history.open(encoding="utf-8") as source:
        latest = {row["kecamatan_code"]: row for row in map(json.loads, source)}
    failed = sorted(code for code, row in latest.items() if row["adm4"] is None)
    rows = [
        row
        | {
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "fetched_at": datetime.fromisoformat(row["fetched_at"]),
        }
        for row in latest.values()
        if row["adm4"] is not None
    ]
    if len({row["adm4"] for row in rows}) != len(rows):
        raise ValueError("Expected unique adm4 codes in the latest BMKG candidates")
    table = pa.Table.from_pylist(rows, schema=CANDIDATE_SCHEMA).sort_by(
        [("area_id", "ascending"), ("kecamatan_code", "ascending")]
    )
    write_table(table, ref / "bmkg_candidates.parquet")

    logger = get_logger(__name__)
    logger.info(
        "BMKG candidates ok=%d failed=%d, failed kecamatan: %s",
        len(rows),
        len(failed),
        ", ".join(failed) or "none",
    )
    areas = admin_areas([])
    picks = pick_candidates(areas, table.to_pylist())
    dimension = ref / "dim_area.parquet"
    dim = pq.read_table(dimension)
    records = dim.to_pylist()
    distances = []
    fallback = 0
    near = 0
    for row in records:
        pick = picks.get(row["area_id"])
        row.update(bmkg_adm4=None, bmkg_lon=None, bmkg_lat=None)
        if not row["is_admin"]:
            continue
        if pick is None:
            logger.warning(
                "area_id=%d %s has no BMKG candidate inside or own candidate within %d km",
                row["area_id"],
                row["geo_name"],
                NEAR_LIMIT_KM,
            )
            continue
        candidate, kind, distance, polygon_km = pick
        row.update(
            bmkg_adm4=candidate["adm4"], bmkg_lon=candidate["lon"], bmkg_lat=candidate["lat"]
        )
        distances.append(distance)
        if kind == "other-area":
            fallback += 1
            logger.warning(
                "area_id=%d %s fallback adm4=%s from area_id=%d: no own candidate inside polygon",
                row["area_id"],
                row["geo_name"],
                candidate["adm4"],
                candidate["area_id"],
            )
        elif kind == "near":
            near += 1
            logger.warning(
                "area_id=%d %s near adm4=%s distance_to_polygon=%.3f km: no candidate inside",
                row["area_id"],
                row["geo_name"],
                candidate["adm4"],
                polygon_km,
            )

    write_table(pa.Table.from_pylist(records, schema=dim.schema), dimension)
    logger.info(
        "BMKG areas own_inside=%d other_area=%d near=%d null=%d",
        len(picks) - fallback - near,
        fallback,
        near,
        len(areas) - len(picks),
    )
    if distances:
        logger.info(
            "BMKG pick distance km median=%.3f max=%.3f", np.median(distances), max(distances)
        )

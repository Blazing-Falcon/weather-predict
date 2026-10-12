import csv
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from fcntl import LOCK_EX, flock
from functools import partial
from itertools import batched
from pathlib import Path
from typing import Annotated

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
import typer

from nowcast import config, http, paths
from nowcast.log import get_logger
from nowcast.parquet import write_table
from nowcast.registry.membership import admin_areas

GHCNH = "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly"
AWC = "https://aviationweather.gov/api/data"
GEO = config.CONFIG_DIR / "geo"
GHCNH_STATIONS = "ghcnh_stations_id.txt"
METAR_STATIONS = "metar_stations.csv"
REPORT_START = re.compile(r"^(?:MET[0-9]{3})?(?:METAR|SPECI)\s+")
REPORT_TIME = re.compile(r"[0-9]{6}Z")
VISIBILITY = re.compile(r"(?:[0-9]{4}(?:NDV)?|[MP]?[0-9]+(?:/[0-9]+)?SM|CAVOK|////)")
RVR = re.compile(r"R[0-9]{2}[LCR]?/[MP0-9][0-9A-Z/]*")
WEATHER_GROUP = re.compile(
    r"(?P<qualifier>[-+]|VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?"
    r"(?P<phenomena>(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)*)"
)
WEATHER_COLUMNS = [f"pres_wx_{kind}{n}" for kind in ("MW", "AU", "AW") for n in range(1, 4)]
SCHEMA = pa.schema(
    [
        ("station_id", pa.string()),
        ("source", pa.string()),
        ("obs_time", pa.timestamp("us", tz="UTC")),
        ("lat", pa.float32()),
        ("lon", pa.float32()),
        ("area_id", pa.int16()),
        ("wx", pa.string()),
        ("is_rain", pa.bool_()),
        ("precip_mm", pa.float32()),
    ]
)


def rain_groups(text: str) -> list[str]:
    text = text.strip().upper()
    report = REPORT_START.match(text)
    if report:
        text = text[report.end() :]
    tokens = re.split(r"\b(?:RMK|TEMPO|BECMG|NOSIG)\b", text, maxsplit=1)[0].rstrip(" =").split()
    time_index = next((i for i, token in enumerate(tokens) if REPORT_TIME.fullmatch(token)), None)
    if report or time_index is not None:
        start = time_index + 1 if time_index is not None else 0
        visibility = next(
            (i for i in range(start, len(tokens)) if VISIBILITY.fullmatch(tokens[i])), None
        )
        if visibility is None or tokens[visibility] == "CAVOK":
            return []
        tokens = tokens[visibility + 1 :]

    groups = []
    for token in tokens:
        if RVR.fullmatch(token) or re.fullmatch(r"[0-9]{4}[NSEW]{1,2}", token):
            continue
        match = WEATHER_GROUP.fullmatch(token)
        if match is None:
            break
        if match["qualifier"] != "VC" and any(
            code in match["phenomena"] for code in ("RA", "DZ", "GR")
        ):
            groups.append(token)
    return groups


def is_rain(text: str) -> bool:
    return bool(rain_groups(text))


def parse_ghcnh(path: Path, year: int, month: int | None) -> list[dict]:
    rows = {}
    prefix = f"{year:04d}-" if month is None else f"{year:04d}-{month:02d}-"
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, delimiter="|")
        weather = [col for col in WEATHER_COLUMNS if col in (reader.fieldnames or ())]
        for row in reader:
            date = row["DATE"]
            if not date.startswith(prefix):
                continue
            station_id = row["STATION"]
            obs_time = datetime.fromisoformat(date).replace(tzinfo=UTC)
            key = (station_id, obs_time)
            if key in rows:
                continue

            raw = (row.get("REM") or row.get("remarks") or "").strip()
            if REPORT_START.match(raw):
                wx = rain_groups(raw)
            else:
                wx = [
                    group
                    for col in weather
                    if row[col]
                    for group in rain_groups(row[col].partition(":")[0])
                ]
            precip = row["precipitation"]
            rows[key] = {
                "station_id": station_id,
                "source": "ghcnh",
                "obs_time": obs_time,
                "lat": float(row["LATITUDE"]),
                "lon": float(row["LONGITUDE"]),
                "wx": " ".join(dict.fromkeys(wx)),
                "is_rain": bool(wx),
                "precip_mm": float(precip) if precip else None,
            }
    return list(rows.values())


def parse_metar(payload: list[dict]) -> list[dict]:
    rows = []
    for report in payload:
        wx = rain_groups(report.get("wxString") or report.get("rawOb") or "")
        precip = report.get("precip")
        rows.append(
            {
                "station_id": report["icaoId"],
                "source": "metar",
                "obs_time": datetime.fromtimestamp(report["obsTime"], UTC),
                "lat": report["lat"],
                "lon": report["lon"],
                "wx": " ".join(wx),
                "is_rain": bool(wx),
                "precip_mm": precip * 25.4 if precip is not None else None,
            }
        )
    return rows


def assign_areas(rows: list[dict]) -> None:
    areas = admin_areas([])
    tree = shapely.STRtree(areas.geometry.array)
    area_ids = areas["area_id"].to_numpy()
    locations = {}
    for row in rows:
        location = row["lon"], row["lat"]
        if location not in locations:
            hits = tree.query(shapely.Point(*location), predicate="covered_by")
            locations[location] = int(min(area_ids[hits])) if hits.size else None
        row["area_id"] = locations[location]


def merge(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        flock(directory, LOCK_EX)
        # A stopped writer can leave a temporary file behind before the atomic rename.
        path.with_name(f"{path.name}.tmp").unlink(missing_ok=True)
        old = pq.ParquetFile(path).read().to_pylist() if path.exists() else []
        merged = {(row["station_id"], row["obs_time"]): row for row in old}
        merged.update(((row["station_id"], row["obs_time"]), row) for row in rows)
        table = pa.Table.from_pylist(list(merged.values()), schema=SCHEMA)
        write_table(table.sort_by([("area_id", "ascending"), ("obs_time", "ascending")]), path)
    finally:
        os.close(directory)


def publish(source: str, rows: list[dict], requested: int, downloaded: int) -> dict:
    reports = {}
    if rows:
        assign_areas(rows)
        partitions = defaultdict(list)
        for row in rows:
            obs_time = row["obs_time"]
            partitions[obs_time.year, obs_time.month].append(row)

        root = paths.zone("silver") / "gauge_obs"
        for (year, month), batch in partitions.items():
            part = root / f"year={year:04d}" / f"month={month:02d}" / f"part-{source}.parquet"
            merge(part, batch)
            reports[f"{year:04d}-{month:02d}"] = {
                "rows": len(batch),
                "silver_bytes": part.stat().st_size,
            }
            if source == "metar":
                newest = max(row["obs_time"] for row in batch)
                reports[f"{year:04d}-{month:02d}"]["latency_min"] = (
                    datetime.now(UTC) - newest
                ).total_seconds() / 60

    get_logger(__name__).info(
        "%s requested_stations=%d stations=%d rows=%d rain_share=%.4f%% "
        "stations_inside=%d rows_with_precip_mm=%d downloaded_bytes=%d",
        source,
        requested,
        len({r["station_id"] for r in rows}),
        len(rows),
        100 * sum(r["is_rain"] for r in rows) / len(rows) if rows else 0,
        len({r["station_id"] for r in rows if r["area_id"] is not None}),
        sum(r["precip_mm"] is not None for r in rows),
        downloaded,
    )
    return reports


def stations() -> None:
    """Refresh the Indonesian GHCNh and METAR station lists."""
    with http.client() as client:
        ghcnh = http.get(client, f"{GHCNH}/doc/ghcnh-station-list.txt").raise_for_status()
        metar = http.get(
            client,
            f"{AWC}/stationinfo",
            params={"bbox": "-11.5,94,6.5,142", "format": "json"},
        ).raise_for_status()

    history = [line.rstrip() for line in ghcnh.text.splitlines() if line.startswith("ID")]
    airports = {
        row["icaoId"]: (row["site"], row["lat"], row["lon"])
        for row in metar.json()
        if row["country"] == "ID" and (row.get("icaoId") or "").startswith("W")
    }
    (GEO / GHCNH_STATIONS).write_text("\n".join(history) + "\n", encoding="utf-8")
    with (GEO / METAR_STATIONS).open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["icao", "name", "lat", "lon"])
        writer.writerows((icao, *airports[icao]) for icao in sorted(airports))
    get_logger(__name__).info("Gauges station lists ghcnh=%d metar=%d", len(history), len(airports))


def download_ghcnh(
    station_id: str, *, client: httpx.Client, year: int, bronze: Path
) -> tuple[Path | None, int]:
    filename = f"GHCNh_{station_id}_{year:04d}.psv"
    response = http.get(client, f"{GHCNH}/access/by-year/{year:04d}/psv/{filename}")
    if response.status_code == 404:
        get_logger(__name__).info("ghcnh %s %04d has no data (404)", station_id, year)
        return None, 0
    response.raise_for_status()
    path = bronze / filename
    temp = path.with_suffix(".tmp")
    temp.write_bytes(response.content)
    temp.replace(path)
    return path, response.num_bytes_downloaded


def ghcnh(
    year: Annotated[int, typer.Option(min=1, max=9999)],
    month: Annotated[int | None, typer.Option(min=1, max=12)] = None,
) -> dict:
    """Import a UTC year or month of GHCNh observations."""
    stations = [
        line.split()[0]
        for line in (GEO / GHCNH_STATIONS).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    bronze = paths.zone("bronze") / "gauges" / "ghcnh" / f"date={datetime.now(UTC):%Y-%m-%d}"
    bronze.mkdir(parents=True, exist_ok=True)
    directory = os.open(bronze, os.O_RDONLY)
    try:
        flock(directory, LOCK_EX)
        rows = []
        downloaded = failures = 0
        completed = []
        with http.client(timeout=60) as client, ThreadPoolExecutor(max_workers=4) as executor:
            fetch = partial(download_ghcnh, client=client, year=year, bronze=bronze)
            jobs = {executor.submit(fetch, station): station for station in stations}
            for job, station in jobs.items():
                try:
                    path, size = job.result()
                except httpx.HTTPError as error:
                    get_logger(__name__).error("ghcnh %s %04d: %s", station, year, error)
                    failures += 1
                    continue
                if path is not None:
                    downloaded += size
                    rows.extend(parse_ghcnh(path, year, month))
                    completed.append(path)
        reports = publish("ghcnh", rows, len(stations), downloaded)
        for path in completed:
            path.unlink()
        if failures:
            raise typer.Exit(1)
        return {
            f"{year:04d}-{month:02d}": {
                **reports.get(f"{year:04d}-{month:02d}", {}),
                "reports": len(rows), "bronze_bytes": downloaded,
            }
        } if month is not None else reports
    finally:
        os.close(directory)


def metar() -> dict:
    """Archive the latest three hours of Indonesian METARs."""
    with (GEO / METAR_STATIONS).open(encoding="utf-8", newline="") as file:
        stations = [row["icao"] for row in csv.DictReader(file)]

    rows = {}
    downloaded = 0
    reports = {}
    with http.client() as client:
        for batch in batched(stations, 100):
            response = http.get(
                client,
                f"{AWC}/metar",
                params={"ids": ",".join(batch), "format": "json", "hours": 3},
            ).raise_for_status()
            if response.status_code == 204:
                continue
            fetched = datetime.now(UTC)
            bronze = paths.zone("bronze") / "gauges" / "metar" / f"date={fetched:%Y-%m-%d}"
            bronze.mkdir(parents=True, exist_ok=True)
            path = bronze / f"{fetched:%Y%m%dT%H%M%S.%fZ}.json"
            temp = path.with_suffix(".tmp")
            temp.write_bytes(response.content)
            temp.replace(path)
            downloaded += response.num_bytes_downloaded
            parsed = parse_metar(response.json())
            report = reports.setdefault(f"{fetched:%Y-%m}", {"reports": 0, "bronze_bytes": 0})
            report["reports"] += len(parsed)
            report["bronze_bytes"] += path.stat().st_size
            if parsed:
                newest = max(row["obs_time"] for row in parsed)
                report["bronze_latency_min"] = (fetched - newest).total_seconds() / 60
            for row in parsed:
                rows[row["station_id"], row["obs_time"]] = row
    for month, summary in publish("metar", list(rows.values()), len(stations), downloaded).items():
        reports.setdefault(month, {}).update(summary)
    return reports

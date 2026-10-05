import csv
import json
from datetime import UTC, datetime
from time import monotonic, sleep

import httpx
import typer

from nowcast import config, http, paths
from nowcast.log import get_logger

VILLAGES_URL = "https://wilayah.id/api/villages"


def rate_limit(interval: float):
    next_request = 0.0

    def wait(_request: httpx.Request) -> None:
        nonlocal next_request
        delay = next_request - monotonic()
        if delay > 0:
            sleep(delay)
        next_request = monotonic() + interval

    return wait


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


def candidate(kecamatan, wilayah_client, bmkg_client, bmkg_url):
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
    for tries, village in enumerate(payload.get("data", [])[:3], start=1):
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
            analysis_date = timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return record | {
            "adm4": code,
            "desa": desa,
            "lat": lat,
            "lon": lon,
            "analysis_date": analysis_date,
        }

    return record | {"error": error}


def sweep(limit: int | None) -> None:
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
            wilayah_client.event_hooks["request"].append(rate_limit(1 / 5))
            bmkg_client.event_hooks["request"].append(rate_limit(60 / min(rpm, 55)))

            for processed, row in enumerate(batch, start=1):
                code = row["kecamatan_code"]
                kabkota = row["kabkota_code"]
                record = {
                    "kecamatan_code": code,
                    "kabkota_code": kabkota,
                    "area_id": area_ids[kabkota],
                    **candidate(code, wilayah_client, bmkg_client, bmkg["url"]),
                    "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
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

import io
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from time import perf_counter
from urllib.parse import quote

import h5py
import httpx
import numpy as np

from nowcast.config import settings
from nowcast.http import client, get
from nowcast.log import redact

RESULT_PATH = Path(__file__).resolve().parents[1] / "docs/experiments/results/00_source_probe.json"


def probe(function):
    @wraps(function)
    def run(*args):
        started = perf_counter()
        result = {"ok": False, "status": None}
        try:
            result.update(function(result, *args), ok=True)
        except Exception as error:  # noqa: BLE001
            message = (
                f"HTTP {error.response.status_code}"
                if isinstance(error, httpx.HTTPStatusError)
                else redact(str(error))
            )
            result["error"] = f"{type(error).__name__}: {message}"
        result["elapsed_ms"] = round((perf_counter() - started) * 1000)
        return result

    return run


def get_response(http, result, url, **kwargs):
    response = get(http, url, retries=0, **kwargs)
    result["status"] = response.status_code
    return response.raise_for_status()


def utc_string(timestamp):
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


@probe
def bmkg(result, http):
    response = get_response(
        http, result, settings()["bmkg"]["url"], params={"adm4": "35.73.01.1001"}
    )
    steps = [step for day in response.json()["data"][0]["cuaca"] for step in day]
    return {"analysis_date": steps[0]["analysis_date"], "steps": len(steps)}


def newest_granule(http, result, source, now, hours):
    response = get_response(
        http,
        result,
        settings()["cmr"]["granules"],
        params={
            "collection_concept_id": settings()[source]["collection"],
            "temporal": f"{utc_string(now - timedelta(hours=hours))},{utc_string(now)}",
            "sort_key": "-start_date",
            "page_size": 1,
        },
    )
    entries = response.json()["feed"]["entry"]
    if not entries:
        raise ValueError(f"CMR returned no granules in the last {hours} hours")
    result["title"] = entries[0]["title"]
    return entries[0]


@probe
def cmr_imerg(result, http, now):
    entry = newest_granule(http, result, "imerg", now, 12)
    start = datetime.fromisoformat(entry["time_start"])
    end = datetime.fromisoformat(entry["time_end"])
    return {
        "version": entry["title"].rsplit(".", 2)[-2],
        "slot_start": utc_string(start),
        "lag_minutes": round((now - end).total_seconds() / 60, 2),
    }


@probe
def cmr_mergir(result, http, now):
    entry = newest_granule(http, result, "mergir", now, 72)
    start = datetime.fromisoformat(entry["time_start"])
    return {
        "slot_start": utc_string(start),
        "lag_hours": round((now - start).total_seconds() / 3600, 2),
    }


def opendap_crop(result, source, cmr, time_end, axes, default_fill):
    title = cmr.get("title")
    if not title:
        raise ValueError(f"No CMR title available for {source}")
    config = settings()[source]
    constraint = f"{config['variable']}[0:1:{time_end}]"
    for axis in axes:
        first, last = config[f"{axis}_index"]
        constraint += f"[{first}:1:{last}]"
    granule = quote(title, safe=":" if source == "imerg" else "")
    url = f"{config['opendap']}{granule}.dap.nc4?dap4.ce={quote(constraint, safe='/:')}"
    with client(auth=True, timeout=30) as http:
        content = get_response(http, result, url).content
    result["bytes"] = len(content)
    with h5py.File(io.BytesIO(content), "r") as file:
        dataset = file[config["variable"]]
        result["shape"] = list(dataset.shape)
        values = dataset[:]
        fill = dataset.attrs.get("_FillValue", default_fill)
    return values[np.isfinite(values) & (values != fill)]


@probe
def imerg_opendap(result, cmr):
    valid = opendap_crop(result, "imerg", cmr, 0, ("lon", "lat"), -9999.9)
    return {
        "wet_fraction": float((valid >= settings()["imerg"]["wet_mm_h"]).mean())
        if valid.size
        else None
    }


@probe
def mergir_opendap(result, cmr):
    valid = opendap_crop(result, "mergir", cmr, 1, ("lat", "lon"), -9999)
    return {
        "cold_fraction": float((valid < settings()["mergir"]["cold_k"]).mean())
        if valid.size
        else None
    }


@probe
def himawari(result, http, now):
    config = settings()["himawari"]
    scan = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    for minutes_back in range(0, 61, 10):
        candidate = scan - timedelta(minutes=minutes_back)
        prefix = config["prefix"].format(
            yyyy=candidate.strftime("%Y"),
            mm=candidate.strftime("%m"),
            dd=candidate.strftime("%d"),
            HHMM=candidate.strftime("%H%M"),
        )
        response = get_response(
            http, result, config["bucket_url"], params={"list-type": "2", "prefix": prefix}
        )
        objects = {
            item.findtext("{*}Key"): int(item.findtext("{*}Size"))
            for item in ET.fromstring(response.content).findall("{*}Contents")
        }
        sizes = {
            f"{segment:02}": objects.get(
                f"{prefix}HS_H09_{candidate:%Y%m%d_%H%M}_{config['band']}"
                f"_FLDK_R20_S{segment:02}10.DAT.bz2"
            )
            for segment in config["segments"]
        }
        if sizes["07"] is not None:
            return {
                "scan_time": utc_string(candidate),
                "segment_bytes": sizes,
                "lag_minutes": round((now - candidate).total_seconds() / 60, 2),
            }
    raise ValueError("B13 segment 07 was absent across the 60-minute scan lookback")


@probe
def ghcnh(result, http, now):
    year = now.year
    response = http.head(
        "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/"
        f"access/by-year/{year}/psv/GHCNh_IDI0000WARR_{year}.psv"
    )
    result["status"] = response.status_code
    response.raise_for_status()
    length = response.headers.get("content-length")
    return {
        "content_length": int(length) if length is not None else None,
        "last_modified": response.headers.get("last-modified"),
    }


@probe
def metar(result, http):
    response = get_response(
        http,
        result,
        "https://aviationweather.gov/api/data/metar",
        params={"ids": "WARR,WIII,WADD", "format": "json", "hours": 2},
    )
    return {"reports": 0 if response.status_code == 204 else len(response.json())}


@probe
def geoboundaries(result, http):
    response = get_response(
        http, result, "https://www.geoboundaries.org/api/current/gbOpen/IDN/ADM2/"
    )
    boundary = response.json()
    name = "boundaryCanonical" if boundary.get("boundaryCanonical") else "boundaryYearRepresented"
    return {name: boundary[name], "admUnitCount": boundary["admUnitCount"]}


@probe
def wilayah(result, http):
    payload = get_response(http, result, "https://wilayah.id/api/regencies/35.json").json()
    return {"rows": len(payload["data"]), "updated_at": payload["meta"]["updated_at"]}


def main():
    now = datetime.now(UTC).replace(microsecond=0)
    results = {}

    def record(function, *args):
        result = function(*args)
        results[function.__name__] = result
        print(redact(f"{function.__name__} {json.dumps(result)}"), flush=True)

    with client(timeout=30) as http:
        record(bmkg, http)
        record(cmr_imerg, http, now)
        record(imerg_opendap, results["cmr_imerg"])
        record(cmr_mergir, http, now)
        record(mergir_opendap, results["cmr_mergir"])
        record(himawari, http, now)
        record(ghcnh, http, now)
        record(metar, http)
        record(geoboundaries, http)
        record(wilayah, http)

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        redact(json.dumps({"probed_at": utc_string(now), **results}, indent=2)) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

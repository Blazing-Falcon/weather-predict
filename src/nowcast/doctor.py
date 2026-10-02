import base64
import json
import math
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import quote

import httpx
import typer

from nowcast import config, http, paths
from nowcast.log import redact

TIMEOUT = 20.0


def python():
    status = "PASS" if sys.version_info[:2] == (3, 12) else "FAIL"
    return status, platform.python_version()


def java():
    try:
        result = subprocess.run(
            ["java", "-version"], check=False, capture_output=True, text=True, timeout=5
        )
    except OSError:
        return "WARN", "java unavailable (needed for Spark)"
    except subprocess.TimeoutExpired:
        return "WARN", "java -version timed out (needed for Spark)"
    match = re.search(r'(?:openjdk|java) version "([^"]+)"', result.stderr + result.stdout)
    if not match:
        return "WARN", "version unknown (needed for Spark)"
    version = match[1]
    major = re.match(r"\d+", version)
    if major and major[0] in {"17", "21", "25"}:
        return "PASS", version
    return "WARN", f"{version} (needed for Spark)"


def data_root():
    root = paths.data_root()
    try:
        root = paths.ensure_layout()
        with NamedTemporaryFile(dir=root) as probe:
            probe.write(b"nowcast doctor\n")
    except OSError as error:
        return "FAIL", f"{root} ({error.strerror or type(error).__name__})"
    return "PASS", str(root)


def earthdata_token():
    token = config.env("EARTHDATA_TOKEN")
    if not token:
        return "FAIL", "missing"
    try:
        _, payload, _ = token.split(".")
        decoded = base64.b64decode(payload + "=" * (-len(payload) % 4), b"-_", validate=True)
        claims = json.loads(decoded)
        expiry = claims.get("exp") if isinstance(claims, dict) else None
        if type(expiry) not in (int, float):
            return "WARN", "set (expiry unknown)"
        seconds = expiry - datetime.now(UTC).timestamp()
        days = math.floor(seconds / 86400)
    except (ValueError, OverflowError):
        return "WARN", "set (expiry unknown)"
    status = "PASS" if seconds > 7 * 86400 else "WARN"
    return status, f"set (expires in {days} days)"


def newest_granule(request, *, recent=False):
    settings = config.settings()
    params = {
        "collection_concept_id": settings["imerg"]["collection"],
        "sort_key": "-start_date",
        "page_size": 1,
    }
    if recent:
        now = datetime.now(UTC).replace(microsecond=0)
        start = now - timedelta(hours=12)
        params["temporal"] = f"{start:%Y-%m-%dT%H:%M:%SZ},{now:%Y-%m-%dT%H:%M:%SZ}"
    response = request(settings["cmr"]["granules"], params=params)
    if response.status_code != 200:
        return "FAIL", f"HTTP {response.status_code}", None
    entries = response.json()["feed"]["entry"]
    if not isinstance(entries, list):
        return "FAIL", "HTTP 200, expected a granule list", None
    if not entries:
        return "WARN", "HTTP 200, no IMERG granules", None
    return "PASS", "HTTP 200, found an IMERG granule", entries[0]


def cmr(request):
    status, detail, _ = newest_granule(request, recent=True)
    if status != "FAIL":
        detail += " in the last 12 h"
    return status, detail


def gesdisc(request):
    if not config.env("EARTHDATA_TOKEN"):
        return "FAIL", "Earthdata token missing"
    _, detail, granule = newest_granule(request)
    if granule is None:
        return "FAIL", f"cannot select crop (CMR {detail})"
    settings = config.settings()["imerg"]
    constraint = f"{settings['variable']}[0:1:0]"
    for axis in ("lon", "lat"):
        first, last = settings[f"{axis}_index"]
        constraint += f"[{first}:1:{last}]"
    title = quote(granule["title"], safe=":")
    url = f"{settings['opendap']}{title}.dap.nc4?dap4.ce={quote(constraint, safe='/:')}"
    response = request(url, auth=True)
    if response.status_code == 200:
        return "PASS", f"HTTP 200, {len(response.content)} bytes"
    if response.status_code == 403:
        return "FAIL", "HTTP 403, EULA not accepted?"
    return "FAIL", f"HTTP {response.status_code}"


def himawari(request):
    settings = config.settings()["himawari"]
    now = datetime.now(UTC)
    scan = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    for minutes_back in range(0, 61, 10):
        candidate = scan - timedelta(minutes=minutes_back)
        prefix = settings["prefix"].format(
            yyyy=candidate.strftime("%Y"),
            mm=candidate.strftime("%m"),
            dd=candidate.strftime("%d"),
            HHMM=candidate.strftime("%H%M"),
        )
        response = request(settings["bucket_url"], params={"list-type": "2", "prefix": prefix})
        if response.status_code != 200:
            return "FAIL", f"HTTP {response.status_code}"
        keys = ET.fromstring(response.content).findall("{*}Contents/{*}Key")
        if any(f"_{settings['band']}_" in (key.text or "") for key in keys):
            return "PASS", f"{settings['band']} at {candidate:%H:%M} UTC"
    return "FAIL", f"no {settings['band']} keys across the 60 min lookback"


def ghcnh(request):
    year = datetime.now(UTC).year
    response = request(
        "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/"
        f"access/by-year/{year}/psv/GHCNh_IDI0000WARR_{year}.psv",
        method="HEAD",
    )
    return ("PASS" if response.status_code == 200 else "FAIL"), f"HTTP {response.status_code}"


def json_list(response, key=None):
    if response.status_code != 200:
        return "FAIL", f"HTTP {response.status_code}"
    payload = response.json()
    if key is not None:
        payload = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(payload, list):
        return "FAIL", "HTTP 200, expected a JSON list"
    return "PASS", f"HTTP 200, {len(payload)} items"


def bmkg(request):
    return json_list(
        request(config.settings()["bmkg"]["url"], params={"adm4": "35.73.01.1001"}),
        "data",
    )


def metar(request):
    return json_list(
        request(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": "WARR,WIII,WADD", "format": "json", "hours": 2},
        )
    )


def geoboundaries(request):
    response = request("https://www.geoboundaries.org/api/current/gbOpen/IDN/ADM2/")
    return ("PASS" if response.status_code == 200 else "FAIL"), f"HTTP {response.status_code}"


def wilayah(request):
    return json_list(request("https://wilayah.id/api/regencies/35.json"), "data")


LOCAL_CHECKS = (python, java, data_root, earthdata_token)
NETWORK_CHECKS = (bmkg, cmr, gesdisc, himawari, ghcnh, metar, geoboundaries, wilayah)


def run_network_check(check):
    started = monotonic()
    deadline = started + TIMEOUT

    def request(url, *, auth=False, method="GET", **kwargs):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        with http.client(auth=auth, timeout=remaining / 2) as client:
            if method == "GET":
                return http.get(client, url, retries=1, backoff=min(2.0, remaining / 4), **kwargs)
            try:
                response = client.head(url, **kwargs)
            except httpx.TransportError:
                pass
            else:
                if response.status_code != 429 and not 500 <= response.status_code < 600:
                    return response
            return client.head(url, **kwargs)

    try:
        status, detail = check(request)
    except Exception as error:  # noqa: BLE001
        status, detail = "FAIL", type(error).__name__
    elapsed = round((monotonic() - started) * 1000)
    return status, f"{detail} ({elapsed} ms)"


def run_doctor():
    results = []
    deadline = monotonic() + TIMEOUT
    pool = ThreadPoolExecutor(max_workers=max(1, len(NETWORK_CHECKS)))
    try:
        futures = [pool.submit(run_network_check, check) for check in NETWORK_CHECKS]
        for check in LOCAL_CHECKS:
            try:
                status, detail = check()
            except Exception as error:  # noqa: BLE001
                status, detail = "FAIL", type(error).__name__
            results.append((check.__name__, status, detail))
        _, pending = wait(futures, timeout=max(0, deadline - monotonic()))
        for check, future in zip(NETWORK_CHECKS, futures, strict=True):
            if future in pending:
                status, detail = "FAIL", f"timeout ({TIMEOUT * 1000:.0f} ms)"
            else:
                status, detail = future.result()
            results.append((check.__name__, status, detail))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    width = max(len(name) for name, _, _ in results)
    for name, status, detail in results:
        counts[status] += 1
        typer.echo(redact(f"{status:<4}  {name:<{width}}  {detail}"))
    typer.echo(f"{counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail")
    return int(counts["FAIL"] > 0)


def doctor():
    raise typer.Exit(run_doctor())

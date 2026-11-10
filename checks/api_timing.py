"""Measure 20 HTTP requests per Nowcast route against a running server."""

import argparse
import math
from statistics import median
from time import perf_counter

import httpx

REQUESTS = 20


def measure(client, path, expected=200):
    durations = []
    for _ in range(REQUESTS):
        started = perf_counter()
        response = client.get(path)
        durations.append((perf_counter() - started) * 1000)
        if response.status_code != expected:
            raise SystemExit(f"{path}: HTTP {response.status_code}, expected {expected}")
    p95 = sorted(durations)[math.ceil(0.95 * REQUESTS) - 1]
    print(
        f"{path:<43} {expected:>4}  {median(durations):>8.2f}  {p95:>8.2f}  {max(durations):>8.2f}"
    )
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    print(f"{REQUESTS} requests per endpoint (milliseconds)")
    print(f"{'Endpoint':<43} {'HTTP':>4}  {'Median':>8}  {'p95':>8}  {'Max':>8}")
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        meta = measure(client, "/api/meta").json()
        area_id = next((area["area_id"] for area in meta["areas"] if area.get("is_admin")), 0)
        for path in (
            "/api/frames?layer=obs",
            f"/api/areas/{area_id}",
            "/api/alerts",
            "/api/scores?scope=national",
            "/api/model",
            f"/api/history/{area_id}",
            "/api/pipeline",
            "/geo/kabkota.geojson",
            "/geo/neighbours.geojson",
            "/docs",
            "/openapi.json",
        ):
            measure(client, path)
        for layer, extension in (("rain", "png"), ("cloud", "webp")):
            stamps = meta["tile_times"][layer]
            stamp = stamps[-1] if stamps else "20000101T0000Z"
            measure(client, f"/tiles/{layer}/{stamp}.{extension}", 200 if stamps else 404)


if __name__ == "__main__":
    main()

import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from fcntl import LOCK_EX, flock
from pathlib import Path
from typing import Annotated

import typer

from nowcast import paths
from nowcast.features.build import month_ranges
from nowcast.gold.frames import latest_predictions
from nowcast.model.metrics import csi, far, pod
from nowcast.model.score import HALF_HOUR, partition, read_rows, read_slots, save_state

HOUR = timedelta(hours=1)


def gauge_check_report(root: Path, first: datetime, stop: datetime) -> dict:
    silver = root / "silver"
    gauges = {}
    stations = defaultdict(set)
    for month, _ in month_ranges(first, stop + HOUR):
        for source in ("ghcnh", "metar"):
            for row in read_rows(
                partition(silver / "gauge_obs", month, f"part-{source}.parquet"),
                columns=["obs_time", "area_id", "station_id", "is_rain"],
                filters=[("obs_time", ">=", first), ("obs_time", "<", stop + HOUR)],
            ):
                area = row["area_id"]
                if area is None or row["is_rain"] is None:
                    continue
                hour = row["obs_time"].replace(minute=0, second=0, microsecond=0)
                key = area, hour
                gauges[key] = gauges.get(key, False) or row["is_rain"]
                stations[key].add(row["station_id"])

    rain = {
        (row["area_id"], row["slot"]): row
        for row in read_slots(
            silver / "rain_kabkota", first, stop, ["area_id", "slot", "rain_mean_x10", "wet_pct"]
        )
    }
    rules = {"imerg_mean": ("rain_mean_x10", 5), "imerg_wet_pct": ("wet_pct", 50)}
    comparisons = {rule: {} for rule in rules}
    for area, hour in gauges:
        slots = [rain.get((area, hour + offset), {}) for offset in (timedelta(), HALF_HOUR)]
        for rule, (column, threshold) in rules.items():
            values = [slot.get(column) for slot in slots]
            if None not in values:
                comparisons[rule][area, hour] = max(values) >= threshold

    forecasts = {}
    has_prob1 = False
    for row in latest_predictions(root, first, stop):
        t, probability = row["t"], row["prob_pct"]
        if row["lead_h"] != 1 or probability is None:
            continue
        has_prob1 = True
        key = row["area_id"], t.replace(minute=0, second=0, microsecond=0) + HOUR
        if key in gauges and (key not in forecasts or t > forecasts[key][0]):
            forecasts[key] = t, probability >= 50
    if has_prob1:
        comparisons["prob1"] = {key: wet for key, (_, wet) in forecasts.items()}

    report = {}
    for rule, forecast in comparisons.items():
        table = Counter((wet, gauges[key]) for key, wet in forecast.items())
        hits, false_alarms = table[True, True], table[True, False]
        misses, correct_negatives = table[False, True], table[False, False]
        n = len(forecast)
        report[rule] = {
            "hours_compared": n,
            "stations": len({station for key in forecast for station in stations[key]}),
            "areas": len({area for area, _ in forecast}),
            "table": {
                "hits": hits,
                "false_alarms": false_alarms,
                "misses": misses,
                "correct_negatives": correct_negatives,
            },
            "pod": pod(hits, misses),
            "far": far(hits, false_alarms),
            "csi": csi(hits, false_alarms, misses),
            "hit_rate": (hits + correct_negatives) / n if n else None,
        }
    return report


def gauge_check(
    start: Annotated[str, typer.Option(help="First UTC date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option(help="Last UTC date, inclusive (YYYY-MM-DD).")],
) -> None:
    """Compare hourly gauge reports against IMERG and one-hour forecasts."""
    try:
        first, last = (
            datetime.combine(date.fromisoformat(value), time(), UTC) for value in (start, end)
        )
    except ValueError:
        raise typer.BadParameter("Dates must be in YYYY-MM-DD format.") from None
    if first > last:
        raise typer.BadParameter("End date must be on or after the start date.")

    root = paths.data_root()
    report = gauge_check_report(root, first, last + timedelta(days=1))
    state = root / "state" / "gauge_check"
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".lock").open("a") as lock:
        flock(lock, LOCK_EX)
        save_state(state / f"{first:%Y-%m-%d}_{last:%Y-%m-%d}.json", report)
    typer.echo(json.dumps(report, indent=2))

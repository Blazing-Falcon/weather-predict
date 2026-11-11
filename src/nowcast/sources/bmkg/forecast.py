import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import pyarrow as pa
import typer

from nowcast import paths, storage
from nowcast.log import get_logger
from nowcast.sources.bmkg import payload
from nowcast.storage import atomic_write, month_path
from nowcast.timeslots import UTC_US, parse_month

SCHEMA = pa.schema(
    [
        ("analysis_time", UTC_US),
        ("first_seen", UTC_US),
        ("area_id", pa.int16()),
        ("adm4", pa.string()),
        ("valid_time", UTC_US),
        ("lead_h", pa.int16()),
        ("weather", pa.int16()),
        ("tp_x10", pa.int16()),
        ("tcc_pct", pa.uint8()),
        ("t", pa.int8()),
        ("hu_pct", pa.uint8()),
        ("ws_x10", pa.int16()),
        ("wd_deg", pa.int16()),
    ]
)
WEATHER_CODES = {0, 1, 2, 3, 4, 5, 10, 17, 45, 60, 61, 63, 65, 80, 95, 97}
RANGES = {
    "t": (-10, 45),
    "hu": (0, 100),
    "tcc": (0, 100),
    "ws": (0, 200),
    "wd_deg": (0, 360),
    "tp": (0, 500),
    "weather": (-32768, 32767),
}


def forecast(body: str) -> tuple[datetime | None, list]:
    try:
        days = json.loads(body)["data"][0]["cuaca"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None, []
    if not isinstance(days, list) or not all(isinstance(day, list) for day in days):
        return None, []
    steps = [step for day in days for step in day]
    for step in steps:
        try:
            stamp = payload.analysis_time(step["analysis_date"])
        except (ValueError, KeyError, TypeError):
            continue
        return stamp, steps
    return None, steps


def forecast_values(step: dict, lead: float) -> dict:
    if not -12 <= lead <= 96:
        raise ValueError("lead_h out of range")
    if not lead.is_integer():
        raise ValueError("lead_h must be an integer")
    for field, (minimum, maximum) in RANGES.items():
        value = step.get(field)
        if type(value) not in (int, float) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ValueError(f"{field} missing or non-numeric")
        if not minimum <= value <= maximum:
            raise ValueError(f"{field} out of range")
        if field in ("hu", "tcc", "wd_deg", "weather") and value != int(value):
            raise ValueError(f"{field} must be an integer")
    return {
        "lead_h": int(lead),
        "weather": int(step["weather"]),
        "tp_x10": round(step["tp"] * 10),
        "tcc_pct": int(step["tcc"]),
        "t": round(step["t"]),
        "hu_pct": int(step["hu"]),
        "ws_x10": round(step["ws"] * 10),
        "wd_deg": int(step["wd_deg"]),
    }


def read_bronze(bronze: Path, month: str | None) -> tuple[dict, dict]:
    partitions = defaultdict(lambda: defaultdict(dict))
    first_seen = {}
    skipped = 0
    files = sorted(
        path
        for pattern in ("sentinel", "sweep")
        for path in bronze.glob(f"run=*/{pattern}=*.jsonl.gz")
    )
    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for raw in source:
                line = json.loads(raw)
                analysis_time, steps = forecast(line["body"])
                usable = line["http_status"] == 200 and analysis_time is not None
                skipped += not usable
                if analysis_time is None:
                    continue
                run_month = analysis_time.strftime("%Y-%m")
                if month is not None and run_month != month:
                    continue
                fetched_at = datetime.fromisoformat(line["fetched_at"]).astimezone(UTC)
                first_seen[analysis_time] = min(
                    first_seen.get(analysis_time, fetched_at), fetched_at
                )
                if usable:
                    points = partitions[run_month][analysis_time]
                    area_id = line["area_id"]
                    if area_id not in points or fetched_at < points[area_id][0]:
                        points[area_id] = (fetched_at, line["adm4"], steps)
    get_logger(__name__).info("BMKG skipped bronze lines=%d", skipped)
    return partitions, first_seen


def point_rows(analysis_time, seen_at, area_id: int, adm4: str, steps: list) -> tuple[list, list, list]:
    rows = []
    dead_letters = []
    leads = []
    valid_times = set()
    for step in steps:
        try:
            valid_time = datetime.fromisoformat(step["datetime"])
            if valid_time.tzinfo is None:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            reason = "invalid datetime"
        else:
            valid_time = valid_time.astimezone(UTC)
            lead = (valid_time - analysis_time).total_seconds() / 3600
            leads.append(lead)
            try:
                values = forecast_values(step, lead)
            except ValueError as error:
                reason = str(error)
            else:
                assert valid_time not in valid_times, (
                    f"Duplicate BMKG key: {(analysis_time, area_id, valid_time)}"
                )
                valid_times.add(valid_time)
                rows.append({
                    "analysis_time": analysis_time,
                    "first_seen": seen_at,
                    "area_id": area_id,
                    "adm4": adm4,
                    "valid_time": valid_time,
                    **values,
                })
                continue
        dead_letters.append({
            "reason": reason,
            "area_id": area_id,
            "analysis_time": analysis_time.isoformat(),
            "step": step,
        })
    return rows, dead_letters, leads


def run_rows(analysis_time: datetime, seen_at: datetime, points: dict) -> tuple[list, list]:
    rows = []
    dead_letters = []
    rows_per_point = []
    leads = []
    for area_id, (_, adm4, steps) in sorted(points.items()):
        point, letters, point_leads = point_rows(analysis_time, seen_at, area_id, adm4, steps)
        rows.extend(point)
        dead_letters.extend(letters)
        rows_per_point.append(len(point))
        leads.extend(point_leads)
    reasons = Counter(letter["reason"] for letter in dead_letters)
    unknown_weather = Counter(row["weather"] for row in rows if row["weather"] not in WEATHER_CODES)
    get_logger(__name__).info(
        "BMKG run=%s points=%d rows=%d rows_per_point=%s dead_letters=%s "
        "lead_range=%s first_seen=%s unknown_weather=%s",
        analysis_time.isoformat(),
        len(points),
        sum(rows_per_point),
        (min(rows_per_point), max(rows_per_point)),
        dict(sorted(reasons.items())),
        (min(leads), max(leads)) if leads else None,
        seen_at.isoformat(),
        dict(sorted(unknown_weather.items())),
    )
    return rows, dead_letters


def write_month(bronze: Path, silver: Path, month: str, runs: dict, first_seen: dict) -> dict:
    rows = []
    dead_letters = defaultdict(list)
    for analysis_time, points in sorted(runs.items()):
        run, letters = run_rows(analysis_time, first_seen[analysis_time], points)
        rows.extend(run)
        dead_letters[analysis_time.strftime("date=%Y-%m-%d/run=%H.jsonl.gz")].extend(letters)
    target = month_path(silver, parse_month(month), "part.parquet")
    target.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    table = table.sort_by(
        [(key, "ascending") for key in ("area_id", "analysis_time", "valid_time")]
    )
    storage.write_table(table, target)
    report = {"rows": table.num_rows, "bytes": target.stat().st_size}
    for name, letters in sorted(dead_letters.items()):
        target = bronze / "_dlq" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with (
            atomic_write(target) as file,
            gzip.GzipFile(filename="", fileobj=file, mode="wb", mtime=0) as output,
        ):
            for letter in letters:
                output.write((json.dumps(letter, ensure_ascii=False) + "\n").encode("utf-8"))
    return report


def parse(
    month: Annotated[str | None, typer.Option(help="Rebuild only this YYYY-MM partition.")] = None,
) -> dict:
    """Rebuild BMKG forecast partitions from bronze."""
    if month is not None:
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError:
            raise typer.BadParameter("Month must be in YYYY-MM format.") from None
    bronze = paths.zone("bronze") / "bmkg"
    silver = paths.zone("silver") / "bmkg_forecast"
    partitions, first_seen = read_bronze(bronze, month)
    report = {"rows": 0, "bytes": 0}
    for run_month, runs in sorted(partitions.items()):
        written = write_month(bronze, silver, run_month, runs, first_seen)
        report["rows"] += written["rows"]
        report["bytes"] += written["bytes"]
    return report

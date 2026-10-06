import gzip
import json
import math
import os
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from tempfile import NamedTemporaryFile
from typing import Annotated

import pyarrow as pa
import typer

from nowcast import parquet, paths
from nowcast.log import get_logger

SCHEMA = pa.schema(
    [
        ("analysis_time", pa.timestamp("us", tz="UTC")),
        ("first_seen", pa.timestamp("us", tz="UTC")),
        ("area_id", pa.int16()),
        ("adm4", pa.string()),
        ("valid_time", pa.timestamp("us", tz="UTC")),
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
            analysis_time = datetime.fromisoformat(step["analysis_date"])
        except (ValueError, KeyError, TypeError):
            continue
        if analysis_time.tzinfo is None:
            analysis_time = analysis_time.replace(tzinfo=UTC)
        return analysis_time.astimezone(UTC), steps
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


def parse(
    month: Annotated[str | None, typer.Option(help="Rebuild only this YYYY-MM partition.")] = None,
) -> None:
    """Rebuild BMKG forecast partitions from bronze."""
    if month is not None:
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError:
            raise typer.BadParameter("Month must be in YYYY-MM format.") from None

    bronze = paths.zone("bronze") / "bmkg"
    silver = paths.zone("silver") / "bmkg_forecast"
    logger = get_logger(__name__)
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
    logger.info("BMKG skipped bronze lines=%d", skipped)

    for run_month, runs in sorted(partitions.items()):
        rows = []
        dead_letters = defaultdict(list)
        for analysis_time, points in sorted(runs.items()):
            seen_at = first_seen[analysis_time]
            dlq = dead_letters[analysis_time.strftime("date=%Y-%m-%d/run=%H.jsonl.gz")]
            rows_per_point = []
            leads = []
            reasons = Counter()
            unknown_weather = Counter()
            for area_id, (_, adm4, steps) in sorted(points.items()):
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
                            if values["weather"] not in WEATHER_CODES:
                                unknown_weather[values["weather"]] += 1
                            rows.append(
                                {
                                    "analysis_time": analysis_time,
                                    "first_seen": seen_at,
                                    "area_id": area_id,
                                    "adm4": adm4,
                                    "valid_time": valid_time,
                                    **values,
                                }
                            )
                            continue
                    reasons[reason] += 1
                    dlq.append(
                        {
                            "reason": reason,
                            "area_id": area_id,
                            "analysis_time": analysis_time.isoformat(),
                            "step": step,
                        }
                    )
                rows_per_point.append(len(valid_times))
            logger.info(
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

        year, month_number = run_month.split("-")
        target = silver / f"year={year}" / f"month={month_number}" / "part.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=SCHEMA)
        table = table.sort_by(
            [(key, "ascending") for key in ("area_id", "analysis_time", "valid_time")]
        )
        parquet.write_table(table, target)
        for name, letters in sorted(dead_letters.items()):
            target = bronze / "_dlq" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                try:
                    with gzip.GzipFile(filename="", fileobj=temp, mode="wb", mtime=0) as output:
                        for letter in letters:
                            output.write(
                                (json.dumps(letter, ensure_ascii=False) + "\n").encode("utf-8")
                            )
                    temp.close()
                    os.replace(temp.name, target)
                except BaseException:
                    os.unlink(temp.name)
                    raise

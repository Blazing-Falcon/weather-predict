from collections import defaultdict
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, flock
from pathlib import Path
from typing import Annotated

import pyarrow as pa
import typer

from nowcast import config, paths
from nowcast.features.build import CLOUD_PARTS, month_ranges
from nowcast.log import get_logger
from nowcast.model.score import HALF_HOUR, UTC_US, partition, read_rows, read_slots
from nowcast.model.verify import BLOCK_START, bmkg_result
from nowcast.parquet import write_table

SCHEMA = pa.schema(
    [
        ("slot", UTC_US),
        ("area_id", pa.int16()),
        ("layer", pa.dictionary(pa.int8(), pa.string())),
        ("value", pa.int16()),
    ]
)


def latest_predictions(root: Path, first: datetime, stop: datetime) -> list[dict]:
    """Newest model version per slot; hindcasts only fill slots with no live prediction."""
    tables = {}
    for table in ("predictions", "hindcast"):
        rows = []
        for month, _ in month_ranges(first, stop):
            rows += read_rows(
                partition(root / "gold" / table, month),
                columns=["t", "area_id", "lead_h", "model_version", "prob_pct"],
                filters=[("t", ">=", first), ("t", "<", stop)],
            )
        tables[table] = rows
    live = {row["t"] for row in tables["predictions"]}
    rows = tables["predictions"] + [row for row in tables["hindcast"] if row["t"] not in live]
    versions = {}
    for row in rows:
        versions[row["t"]] = max(row["model_version"], versions.get(row["t"], ""))
    return [row for row in rows if row["model_version"] == versions[row["t"]]]


def rebuild(root: Path, now: datetime, days: int) -> tuple[int, int]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first = today - timedelta(days=days - 1)
    stop = today + timedelta(days=1)
    silver = root / "silver"
    target = root / "gold" / "map_frames"
    settings = config.settings()["bmkg"]
    alignment, rain_code = settings["step_alignment"], settings["rain_codes_min"]
    step_offset = BLOCK_START[alignment] * HALF_HOUR
    step_duration = timedelta(hours=3)
    runs = defaultdict(lambda: defaultdict(list))
    for part in (silver / "bmkg_forecast").glob("year=*/month=*/part.parquet"):
        for row in read_rows(
            part,
            columns=["area_id", "analysis_time", "first_seen", "valid_time", "weather"],
            filters=[("first_seen", "<", stop)],
        ):
            runs[row["area_id"]][row["analysis_time"]].append(row)
    ordered_runs = {area: sorted(by_time.items(), reverse=True) for area, by_time in runs.items()}

    frames = defaultdict(dict)
    for row in read_slots(
        silver / "rain_kabkota", first, stop, ["slot", "area_id", "rain_mean_x10"]
    ):
        slot, area, value = row["slot"], row["area_id"], row["rain_mean_x10"]
        if value is not None:
            frames[slot.date()]["obs", slot, area] = value
    for source, part in sorted(CLOUD_PARTS.items()):
        for row in read_slots(
            silver / "cloud_kabkota",
            first,
            stop,
            ["slot", "area_id", "cold_pct"],
            part,
            [("source", "=", source)],
        ):
            slot, area, value = row["slot"], row["area_id"], row["cold_pct"]
            if value is not None:
                frames[slot.date()]["cold", slot, area] = value
    for row in latest_predictions(root, first, stop):
        slot, area, lead, value = row["t"], row["area_id"], row["lead_h"], row["prob_pct"]
        if lead in (1, 2, 3) and value is not None:
            frames[slot.date()][f"prob{lead}", slot, area] = value

    dates = rows = 0
    for day in range(days):
        midnight = first + timedelta(days=day)
        values = frames[midnight.date()]
        for offset in range(48):
            slot = midnight + offset * HALF_HOUR
            if slot > now:
                break
            for area, area_runs in ordered_runs.items():
                wet, analysis = bmkg_result(
                    area_runs, slot, slot, slot + HALF_HOUR, alignment, rain_code
                )
                if wet is not None:
                    values["bmkg", slot, area] = 100 * wet
                    for step in runs[area][analysis]:
                        elapsed = slot - (step["valid_time"] + step_offset)
                        if timedelta(0) <= elapsed < step_duration:
                            values["bmkg_code", slot, area] = step["weather"]
                            values["bmkg_step", slot, area] = elapsed // timedelta(minutes=1)
                            values["bmkg_age", slot, area] = (slot - analysis) // timedelta(hours=1)
                            break
                    prob3 = values.get(("prob3", slot, area))
                    if prob3 is not None:
                        wet3, _ = bmkg_result(
                            area_runs, slot, slot, slot + step_duration, alignment, rain_code
                        )
                        values["disagree", slot, area] = int((prob3 >= 50) != wet3)

        part = target / f"date={midnight:%Y-%m-%d}" / "part.parquet"
        if values or part.exists():
            table = pa.Table.from_pylist(
                [
                    {"slot": slot, "area_id": area, "layer": layer, "value": value}
                    for (layer, slot, area), value in sorted(values.items())
                ],
                schema=SCHEMA,
            )
            part.parent.mkdir(parents=True, exist_ok=True)
            part.with_suffix(".parquet.tmp").unlink(missing_ok=True)
            write_table(table, part)
            dates += 1
            rows += table.num_rows
    return dates, rows


def frames(days: Annotated[int, typer.Option(min=1)] = 7) -> None:
    """Rebuild map frames for recent UTC dates."""
    root = paths.data_root()
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "map_frames.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        dates, rows = rebuild(root, datetime.now(UTC), days)
    get_logger(__name__).info("Map frames dates=%d rows=%d", dates, rows)

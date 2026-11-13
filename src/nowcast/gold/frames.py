from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import pyarrow as pa
import typer

from nowcast import config, paths
from nowcast.cloud import CLOUD_PARTS
from nowcast.locks import file_lock
from nowcast.log import get_logger
from nowcast.model.bmkg_baseline import (
    STEP_BLOCK,
    bmkg_result,
    newest_run,
    read_runs,
    step_at,
)
from nowcast.storage import month_path, read_rows, read_slots, write_table
from nowcast.timeslots import SLOT, UTC_US, month_ranges

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
                month_path(root / "gold" / table, month, "part.parquet"),
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


def collect_values(root, first, stop):
    silver = root / "silver"
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

    return frames


def add_bmkg_layers(values, slot, runs, rain_code):
    for area, area_runs in runs.items():
        wet, analysis = bmkg_result(area_runs, slot, slot, slot + SLOT, rain_code)
        if wet is None:
            continue
        values["bmkg", slot, area] = 100 * wet
        _, _, steps = newest_run(area_runs, slot)
        start, weather = step_at(steps, slot)
        if weather is not None:
            values["bmkg_code", slot, area] = weather
            values["bmkg_step", slot, area] = (slot - start) // timedelta(minutes=1)
            values["bmkg_age", slot, area] = (slot - analysis) // timedelta(hours=1)
        prob3 = values.get(("prob3", slot, area))
        if prob3 is not None:
            wet3, _ = bmkg_result(area_runs, slot, slot, slot + STEP_BLOCK, rain_code)
            values["disagree", slot, area] = int((prob3 >= 50) != wet3)


def write_day(root, midnight, values):
    part = root / "gold" / "map_frames" / f"date={midnight:%Y-%m-%d}" / "part.parquet"
    if not values and not part.exists():
        return 0, 0
    table = pa.Table.from_pylist(
        [
            {"slot": slot, "area_id": area, "layer": layer, "value": value}
            for (layer, slot, area), value in sorted(values.items())
        ],
        schema=SCHEMA,
    )
    part.parent.mkdir(parents=True, exist_ok=True)
    write_table(table, part)
    return 1, table.num_rows


def rebuild(root: Path, now: datetime, days: int) -> tuple[int, int]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first = today - timedelta(days=days - 1)
    stop = today + timedelta(days=1)
    settings = config.settings()["bmkg"]
    runs = read_runs(root, first, stop, settings["step_alignment"])
    frames = collect_values(root, first, stop)
    dates = rows = 0
    for day in range(days):
        midnight = first + timedelta(days=day)
        values = frames[midnight.date()]
        for offset in range(48):
            slot = midnight + offset * SLOT
            if slot > now:
                break
            add_bmkg_layers(values, slot, runs, settings["rain_codes_min"])
        written, count = write_day(root, midnight, values)
        dates += written
        rows += count
    return dates, rows


def frames(days: Annotated[int, typer.Option(min=1)] = 7) -> None:
    """Rebuild map frames for recent UTC dates."""
    root = paths.data_root()
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with file_lock(state, "map_frames.lock"):
        dates, rows = rebuild(root, datetime.now(UTC), days)
    get_logger(__name__).info("Map frames dates=%d rows=%d", dates, rows)

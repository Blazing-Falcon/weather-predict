from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import pyarrow as pa

from nowcast import config, paths, storage
from nowcast.locks import file_lock
from nowcast.log import get_logger
from nowcast.model.bmkg_baseline import bmkg_result, read_runs
from nowcast.model.cards import model_card
from nowcast.model.score import KEY
from nowcast.model.score import SCHEMA as PREDICTION_SCHEMA
from nowcast.storage import read_rows, read_slots
from nowcast.timeslots import LOCAL_OFFSETS, SLOT, UTC_US

SCHEMA = PREDICTION_SCHEMA.append(pa.field("verified_at", UTC_US))
for field, dtype in (
    ("observed", pa.bool_()),
    ("clim_pct", pa.uint8()),
    ("persist_pct", pa.uint8()),
    ("motion_pct", pa.uint8()),
    ("bmkg_rain", pa.bool_()),
    ("bmkg_analysis_time", UTC_US),
):
    SCHEMA = SCHEMA.append(pa.field(field, dtype))


def reference_data(root):
    areas = {
        row["area_id"]: LOCAL_OFFSETS[row["tz"]]
        for row in read_rows(
            root / "ref" / "dim_area.parquet", columns=["area_id", "is_admin", "tz"]
        )
        if row["is_admin"]
    }
    climate = {
        (row["area_id"], row["month"], row["hour_local"]): row
        for row in read_rows(root / "gold" / "climatology" / "part.parquet")
    }
    return areas, climate


def climate_percent(row, references):
    areas, climate = references
    local = row["t"] + timedelta(hours=areas[row["area_id"]])
    return climate.get((row["area_id"], local.month, local.hour), {}).get(f"p{row['lead_h']}")


def fill_climatology(previous, references):
    count = 0
    for row in previous:
        if row["clim_pct"] is None:
            value = climate_percent(row, references())
            if value is not None:
                row["clim_pct"] = value
                count += 1
    return count


def observed_windows(root, pending):
    first = min(row["t"] for row in pending)
    stop = max(row["t"] + timedelta(hours=row["lead_h"]) for row in pending)
    rain = {
        (row["slot"], row["area_id"]): row["rain_mean_x10"]
        for row in read_slots(
            root / "silver" / "rain_kabkota",
            min(first, min(row["imerg_slot"] for row in pending)),
            stop,
            ["slot", "area_id", "rain_mean_x10"],
        )
    }
    ready = []
    for row in pending:
        window = [
            rain.get((row["t"] + i * SLOT, row["area_id"]))
            for i in range(2 * row["lead_h"])
        ]
        if None not in window:
            ready.append((row, max(window)))
    return rain, ready


def motion_values(root, first, stop):
    return {
        (row["slot"], row["area_id"]): max(
            (row[name] for name in ("adv1_pct", "adv2_pct", "adv3_pct") if row[name] is not None),
            default=-1,
        )
        for row in read_slots(
            root / "silver" / "cloud_kabkota", first, stop,
            ["slot", "area_id", "adv1_pct", "adv2_pct", "adv3_pct"],
            "part-himawari.parquet", [("source", "=", 2)],
        )
    }


def verified_rows(root, ready, rain, references, card_for):
    first = min(row["t"] for row, _ in ready)
    stop = max(row["t"] + timedelta(hours=row["lead_h"]) for row, _ in ready)
    settings = config.settings()["bmkg"]
    runs = read_runs(
        root, first, stop, settings["step_alignment"],
        as_of=max(row["issued_at"] for row, _ in ready),
    )
    motion = motion_values(root, first, max(row["t"] for row, _ in ready) + SLOT)
    verified_at = datetime.now(UTC)
    new = []
    for row, peak in ready:
        t, area, lead, version = KEY(row)
        card = card_for(root, version)
        threshold = card["threshold_mm_h"] * 10
        motion_cutoff = card["leads"][str(lead)]["csi_thresholds"]["motion_adv_max_pct"]
        persist = rain.get((row["imerg_slot"], area))
        wet, analysis = bmkg_result(
            runs.get(area, {}), row["issued_at"], t, t + timedelta(hours=lead),
            settings["rain_codes_min"],
        )
        new.append(
            {
                **row,
                "verified_at": verified_at,
                "observed": peak >= threshold,
                "clim_pct": climate_percent(row, references()),
                "persist_pct": 100 * (persist >= threshold) if persist is not None else None,
                "motion_pct": 100 * (motion.get((t, area), -1) >= motion_cutoff),
                "bmkg_rain": wet,
                "bmkg_analysis_time": analysis,
            }
        )
    return new


def verify_pending(root: Path) -> int:
    predictions = root / "gold" / "predictions"
    references = cache(lambda: reference_data(root))
    card_for = cache(model_card)
    count = 0
    for part in sorted(predictions.glob("year=*/month=*/part.parquet")):
        output = root / "gold" / "verification" / part.relative_to(predictions)
        previous = read_rows(output)
        # Climatology depends only on area and local time, so it can be filled later.
        refilled = fill_climatology(previous, references)
        if refilled:
            storage.write_table(pa.Table.from_pylist(previous, schema=SCHEMA), output)
            get_logger(__name__).info("Filled climatology for %d verified rows", refilled)
        done = {KEY(row) for row in previous}
        pending = [row for row in read_rows(part) if KEY(row) not in done]
        if not pending:
            continue
        rain, ready = observed_windows(root, pending)
        if not ready:
            continue
        new = verified_rows(root, ready, rain, references, card_for)
        rows = previous + new
        assert len(rows) == len({KEY(row) for row in rows}), "Duplicate verification keys"
        output.parent.mkdir(parents=True, exist_ok=True)
        storage.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), output)
        count += len(new)
    return count


def verify() -> None:
    """Verify new forecasts when their complete IMERG windows are available."""
    root = paths.data_root()
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with file_lock(state, "verify.lock"):
        count = verify_pending(root)
    get_logger(__name__).info("Verified %d prediction keys", count)

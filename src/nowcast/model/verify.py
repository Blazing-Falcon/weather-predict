from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import pyarrow as pa

from nowcast import config, paths, storage
from nowcast.locks import file_lock
from nowcast.log import get_logger
from nowcast.model.score import KEY, model_card
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

BLOCK_START = {"before": -6, "after": 0, "centred": -3}


def bmkg_result(runs, issued_at, t, end, alignment, rain_code):
    for analysis, steps in runs:
        if steps[0]["first_seen"] <= issued_at:
            offset = BLOCK_START[alignment] * SLOT
            wet = any(
                start < end
                and start + timedelta(hours=3) > t
                and rain_code <= step["weather"] <= 99
                for step in steps
                for start in [step["valid_time"] + offset]
            )
            return wet, analysis
    return None, None


def verify_pending(root: Path) -> int:
    gold = root / "gold"
    silver = root / "silver"
    settings = config.settings()["bmkg"]

    @cache
    def reference_data():
        areas = {
            row["area_id"]: LOCAL_OFFSETS[row["tz"]]
            for row in read_rows(
                root / "ref" / "dim_area.parquet", columns=["area_id", "is_admin", "tz"]
            )
            if row["is_admin"]
        }
        climate = {
            (row["area_id"], row["month"], row["hour_local"]): row
            for row in read_rows(gold / "climatology" / "part.parquet")
        }
        runs = defaultdict(lambda: defaultdict(list))
        for part in (silver / "bmkg_forecast").glob("year=*/month=*/part.parquet"):
            columns = ["area_id", "analysis_time", "first_seen", "valid_time", "weather"]
            for row in read_rows(part, columns=columns):
                runs[row["area_id"]][row["analysis_time"]].append(row)
        return (
            areas,
            climate,
            {area: sorted(by_time.items(), reverse=True) for area, by_time in runs.items()},
        )

    def clim_pct(row):
        areas, climate, _ = reference_data()
        local = row["t"] + timedelta(hours=areas[row["area_id"]])
        return climate.get((row["area_id"], local.month, local.hour), {}).get(f"p{row['lead_h']}")

    card_for = cache(model_card)
    count = 0
    for part in sorted((gold / "predictions").glob("year=*/month=*/part.parquet")):
        output = gold / "verification" / part.relative_to(gold / "predictions")
        previous = read_rows(output)
        # Climatology depends only on area and local time, so rows verified before it existed
        # can take it later.
        refilled = [row for row in previous if row["clim_pct"] is None and clim_pct(row) is not None]
        for row in refilled:
            row["clim_pct"] = clim_pct(row)
        if refilled:
            storage.write_table(pa.Table.from_pylist(previous, schema=SCHEMA), output)
            get_logger(__name__).info("Filled climatology for %d verified rows", len(refilled))
        done = {KEY(row) for row in previous}
        pending = [row for row in read_rows(part) if KEY(row) not in done]
        if not pending:
            continue

        first = min(row["t"] for row in pending)
        stop = max(row["t"] + timedelta(hours=row["lead_h"]) for row in pending)
        rain = {
            (row["slot"], row["area_id"]): row["rain_mean_x10"]
            for row in read_slots(
                silver / "rain_kabkota",
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
        if not ready:
            continue

        _, _, runs = reference_data()
        motion = {
            (row["slot"], row["area_id"]): max(
                (
                    row[name]
                    for name in ["adv1_pct", "adv2_pct", "adv3_pct"]
                    if row[name] is not None
                ),
                default=-1,
            )
            for row in read_slots(
                silver / "cloud_kabkota",
                first,
                max(row["t"] for row, _ in ready) + SLOT,
                ["slot", "area_id", "adv1_pct", "adv2_pct", "adv3_pct"],
                "part-himawari.parquet",
                [("source", "=", 2)],
            )
        }
        verified_at = datetime.now(UTC)
        new = []
        for row, peak in ready:
            t, area, lead, version = KEY(row)
            card = card_for(root, version)
            threshold = card["threshold_mm_h"] * 10
            motion_cutoff = card["leads"][str(lead)]["csi_thresholds"]["motion_adv_max_pct"]
            persist = rain.get((row["imerg_slot"], area))
            bmkg_rain, analysis = bmkg_result(
                runs.get(area, []),
                row["issued_at"],
                t,
                t + timedelta(hours=lead),
                settings["step_alignment"],
                settings["rain_codes_min"],
            )
            new.append(
                {
                    **row,
                    "verified_at": verified_at,
                    "observed": peak >= threshold,
                    "clim_pct": clim_pct(row),
                    "persist_pct": 100 * (persist >= threshold) if persist is not None else None,
                    "motion_pct": 100 * (motion.get((t, area), -1) >= motion_cutoff),
                    "bmkg_rain": bmkg_rain,
                    "bmkg_analysis_time": analysis,
                }
            )
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

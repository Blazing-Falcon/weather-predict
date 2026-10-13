from collections import defaultdict
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, flock
from functools import cache
from pathlib import Path

import pyarrow as pa

from nowcast import config, parquet, paths
from nowcast.features.build import LOCAL_OFFSETS
from nowcast.log import get_logger
from nowcast.model.score import HALF_HOUR, KEY, UTC_US, model_card, read_rows, read_slots
from nowcast.model.score import SCHEMA as PREDICTION_SCHEMA

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
            offset = BLOCK_START[alignment] * HALF_HOUR
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

    card_for = cache(model_card)
    count = 0
    for part in sorted((gold / "predictions").glob("year=*/month=*/part.parquet")):
        output = gold / "verification" / part.relative_to(gold / "predictions")
        previous = read_rows(output)
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
                rain.get((row["t"] + i * HALF_HOUR, row["area_id"]))
                for i in range(2 * row["lead_h"])
            ]
            if None not in window:
                ready.append((row, max(window)))
        if not ready:
            continue

        areas, climate, runs = reference_data()
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
                max(row["t"] for row, _ in ready) + HALF_HOUR,
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
            local = t + timedelta(hours=areas[area])
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
                    "clim_pct": climate.get((area, local.month, local.hour), {}).get(f"p{lead}"),
                    "persist_pct": 100 * (persist >= threshold) if persist is not None else None,
                    "motion_pct": 100 * (motion.get((t, area), -1) >= motion_cutoff),
                    "bmkg_rain": bmkg_rain,
                    "bmkg_analysis_time": analysis,
                }
            )
        rows = previous + new
        assert len(rows) == len({KEY(row) for row in rows}), "Duplicate verification keys"
        output.parent.mkdir(parents=True, exist_ok=True)
        parquet.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), output)
        count += len(new)
    return count


def verify() -> None:
    """Verify new forecasts when their complete IMERG windows are available."""
    root = paths.data_root()
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "verify.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        count = verify_pending(root)
    get_logger(__name__).info("Verified %d prediction keys", count)

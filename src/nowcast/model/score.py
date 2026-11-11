import json
from datetime import UTC, datetime, timedelta
from operator import itemgetter
from pathlib import Path
from typing import Annotated

import lightgbm as lgb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import typer

from nowcast import config, paths, storage
from nowcast.cloud import CLOUD_PARTS
from nowcast.features import build
from nowcast.features.spark import spark_session
from nowcast.locks import directory_lock, file_lock
from nowcast.log import get_logger
from nowcast.storage import month_path, read_rows, read_slots, write_json
from nowcast.timeslots import SLOT, UTC_US

KEY = itemgetter("t", "area_id", "lead_h", "model_version")
SCHEMA = pa.schema(
    [
        ("issued_at", UTC_US),
        ("t", UTC_US),
        ("area_id", pa.int16()),
        ("lead_h", pa.int8()),
        ("prob_pct", pa.uint8()),
        ("model_version", pa.string()),
        ("cloud_slot", UTC_US),
        ("imerg_slot", UTC_US),
    ]
)
RAIN_FEATURES = [
    "rain_lag",
    "wet_lag",
    "rainmax_lag",
    "rain_sum_3h",
    "rain_sum_6h",
    "rain_sum_24h",
    "nb_rain_lag",
]


def model_card(root, version):
    return json.loads((root / "models" / version / "card.json").read_text(encoding="utf-8"))


def live_features(root, pending, lag):
    slots = {build.available_imerg_slot(t, lag) for t in pending}
    rain = root / "silver" / "rain_kabkota"
    available = {
        row["slot"] for row in read_slots(rain, min(slots), max(slots) + SLOT, ["slot"])
    }
    missing = slots - available
    logger = get_logger(__name__)
    for slot in sorted(missing):
        logger.warning(
            "IMERG slot %s is missing at scoring time (configured lag %d min)", slot, lag
        )

    with spark_session(root) as spark:
        rows = build.feature_rows(
            spark,
            root,
            build.admin_areas(spark, root),
            build.read_climatology(spark, root),
            pending[0],
            pending[-1] + SLOT,
            lag,
            1,
            cloud_source=2,
        )
        table = rows.where(rows.t.isin(pending)).select(*build.KEYS, *build.FEATURES).toArrow()
    frame = table.to_pandas(categories=["island"])
    frame.loc[frame["imerg_slot"].isin(missing), RAIN_FEATURES] = np.nan
    return frame


def predict(root: Path, pending: list[datetime]) -> tuple[pa.Table, dict]:
    model = config.settings()["model"]
    version = model["live_version"]
    card = model_card(root, version)
    lag = int(config.env("IMERG_LAG_MINUTES", str(model["imerg_lag_minutes"])))
    frame = live_features(root, pending, lag)
    predictions = []
    directory = root / "models" / version
    for lead, details in card["leads"].items():
        booster = lgb.Booster(model_file=str(directory / f"model_lead{lead}.txt"))
        features = frame.rename(columns={f"clim_p{lead}": "clim_p"})[card["features"]]
        probabilities = booster.predict(features, num_threads=model["lgbm"]["n_jobs"])
        if details["calibration_used"]:
            calibration = json.loads(
                (directory / f"isotonic_lead{lead}.json").read_text(encoding="utf-8")
            )
            probabilities = np.interp(
                probabilities, calibration["thresholds"], calibration["values"]
            )
        rows = frame[build.KEYS].copy()
        rows["lead_h"] = int(lead)
        rows["model_version"] = version
        rows["prob_pct"] = np.rint(probabilities * 100).astype(np.uint8)
        predictions.append(pa.Table.from_pandas(rows, preserve_index=False))

    return pa.concat_tables(predictions), card


def score_pending(root: Path, now: datetime) -> int:
    state = root / "state"
    cursor = state / "score_live.json"
    last = (
        datetime.fromisoformat(json.loads(cursor.read_text(encoding="utf-8"))["last_t"])
        if cursor.exists()
        else datetime.min.replace(tzinfo=UTC)
    )
    cutoff = now - timedelta(hours=3)
    cloud = root / "silver" / "cloud_kabkota"
    slots = {
        row["slot"]
        for row in read_slots(
            cloud,
            cutoff - timedelta(hours=1),
            now + SLOT,
            ["slot"],
            CLOUD_PARTS[2],
            [("source", "=", 2)],
        )
    }
    pending = sorted(
        t
        for t in slots
        if cutoff <= t <= now
        and t > last
        and t.minute in (0, 30)
        and t.second == t.microsecond == 0
        and t - timedelta(hours=1) in slots
    )
    logger = get_logger(__name__)
    if not pending:
        logger.info("No pending Himawari issue slots")
        return 0

    table, card = predict(root, pending)
    count = 0
    window_slots = 2 * max(map(int, card["leads"]))
    # IMERG tick and backfill take this directory lock before publishing rain.
    with directory_lock(state, shared=True):
        rain_slots = {
            row["slot"]
            for row in read_slots(
                root / "silver" / "rain_kabkota",
                pending[0],
                pending[-1] + window_slots * SLOT,
                ["slot"],
            )
        }
        for t in pending:
            part = month_path(root / "gold" / "predictions", t, "part.parquet")
            existing = read_rows(part)
            keys = {KEY(row) for row in existing}
            rows = table.filter(pc.equal(table["t"], pa.scalar(t))).to_pylist()
            new = [row for row in rows if KEY(row) not in keys]
            if len(new) < len(rows):
                logger.info("Kept %d existing prediction keys for t=%s", len(rows) - len(new), t)
            if any(t + i * SLOT in rain_slots for i in range(window_slots)):
                if new:
                    logger.warning("Skipped t=%s because IMERG truth is already in silver", t)
                new = []
            if new:
                issued_at = datetime.now(UTC)
                for row in new:
                    row["issued_at"] = issued_at
                part.parent.mkdir(parents=True, exist_ok=True)
                storage.write_table(pa.Table.from_pylist(existing + new, schema=SCHEMA), part)
                count += len(new)

            attempted = {KEY(row) for row in rows}
            issued = [row for row in existing if KEY(row) in attempted] + new
            if issued:
                issued_at = max(row["issued_at"] for row in issued)
                delay = (issued_at - t).total_seconds() / 60
                health = {
                    "last_t": t.isoformat(),
                    "issued_at": issued_at.isoformat(),
                    "delay_minutes": delay,
                    "rows": len(issued),
                }
                write_json(state / "health" / "score.json", health)
                write_json(cursor, {"last_t": t.isoformat()})
                if new:
                    logger.info(
                        "Scored t=%s issued_at=%s delay=%.2f min rows=%d",
                        t.isoformat(),
                        issued_at.isoformat(),
                        delay,
                        len(new),
                    )
    return count


def score() -> None:
    """Issue forecasts for recent Himawari slots."""
    root = paths.data_root()
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with file_lock(state, "score.lock"):
        score_pending(root, datetime.now(UTC))


def hindcast(
    start: Annotated[str, typer.Option(help="UTC start (ISO).")],
    end: Annotated[str, typer.Option(help="UTC end (ISO).")],
) -> None:
    """Score past Himawari slots into gold/hindcast, kept apart from live predictions."""
    root = paths.data_root()
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    slots = {
        row["slot"]
        for row in read_slots(
            root / "silver" / "cloud_kabkota", first - timedelta(hours=1), last + SLOT,
            ["slot"], CLOUD_PARTS[2], [("source", "=", 2)],
        )
    }
    pending = sorted(
        t for t in slots
        if first <= t <= last and t.minute in (0, 30) and t - timedelta(hours=1) in slots
    )
    if not pending:
        get_logger(__name__).info("No Himawari slots to hindcast")
        return
    table, _ = predict(root, pending)
    issued_at = datetime.now(UTC)
    count = 0
    for t in pending:
        part = month_path(root / "gold" / "hindcast", t, "part.parquet")
        existing = read_rows(part)
        keys = {KEY(row) for row in existing}
        new = [
            row | {"issued_at": issued_at}
            for row in table.filter(pc.equal(table["t"], pa.scalar(t))).to_pylist()
            if KEY(row) not in keys
        ]
        if new:
            part.parent.mkdir(parents=True, exist_ok=True)
            storage.write_table(pa.Table.from_pylist(existing + new, schema=SCHEMA), part)
            count += len(new)
    get_logger(__name__).info("Hindcast slots=%d rows=%d", len(pending), count)

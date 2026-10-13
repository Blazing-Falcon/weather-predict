from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from fcntl import LOCK_EX, flock
from functools import cache
from pathlib import Path
from typing import Annotated

import numpy as np
import pyarrow as pa
import typer

from nowcast import parquet, paths
from nowcast.features.build import date_range
from nowcast.log import get_logger
from nowcast.model import metrics
from nowcast.model.score import model_card, partition, read_rows

SOURCES = {
    "model": "prob_pct",
    "clim": "clim_pct",
    "persist": "persist_pct",
    "motion": "motion_pct",
    "bmkg": "bmkg_rain",
}
SCHEMA = pa.schema(
    [
        ("day", pa.date32()),
        ("scope", pa.string()),
        ("scope_id", pa.string()),
        ("lead_h", pa.int8()),
        ("source", pa.string()),
        ("n", pa.int64()),
        *[(name, pa.float64()) for name in ("brier", "bss", "csi", "pod", "far", "base_rate")],
    ]
)


def write_day(root: Path, day: date) -> int:
    first, stop = date_range(day.isoformat(), day.isoformat())
    gold = root / "gold"
    output = partition(gold / "scores_daily", day)
    rows = read_rows(
        partition(gold / "verification", day), filters=[("t", ">=", first), ("t", "<", stop)]
    )
    groups = defaultdict(list)
    if rows:
        areas = {
            row["area_id"]: row
            for row in read_rows(
                root / "ref" / "dim_area.parquet", columns=["area_id", "island", "prov_code"]
            )
        }
        for row in rows:
            area = areas[row["area_id"]]
            for scope, scope_id in (
                ("national", "ID"),
                ("island", area["island"]),
                ("province", area["prov_code"]),
            ):
                groups[scope, scope_id, row["lead_h"]].append(row)

    card_for = cache(model_card)
    scores = []
    for (scope, scope_id, lead), group in sorted(groups.items()):
        for source, column in SOURCES.items():
            selected = [row for row in group if row[column] is not None]
            if not selected:
                continue
            y = np.array([row["observed"] for row in selected], dtype=float)
            p = np.array([row[column] for row in selected], dtype=float)
            if source != "bmkg":
                p /= 100
            thresholds = np.array(
                [
                    card_for(root, row["model_version"])["leads"][str(lead)]["csi_thresholds"][
                        "probability"
                    ].get(source, 0.5)
                    for row in selected
                ]
            )
            hits, false_alarms, misses = metrics.contingency(y, p, thresholds)
            clim = np.array([row["clim_pct"] for row in selected], dtype=float) / 100
            matched = np.isfinite(clim)
            skill = (
                metrics.bss(
                    metrics.brier(y[matched], p[matched]), metrics.brier(y[matched], clim[matched])
                )
                if matched.any()
                else None
            )
            scores.append(
                {
                    "day": day,
                    "scope": scope,
                    "scope_id": scope_id,
                    "lead_h": lead,
                    "source": source,
                    "n": len(y),
                    "brier": metrics.brier(y, p),
                    "bss": skill,
                    "csi": metrics.csi(hits, false_alarms, misses),
                    "pod": metrics.pod(hits, misses),
                    "far": metrics.far(hits, false_alarms),
                    "base_rate": float(y.mean()),
                }
            )

    if scores or output.is_file():
        previous = [row for row in read_rows(output) if row["day"] != day]
        output.parent.mkdir(parents=True, exist_ok=True)
        parquet.write_table(pa.Table.from_pylist(previous + scores, schema=SCHEMA), output)
    return len(scores)


def daily(
    day: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
) -> None:
    """Replace daily scores for one UTC issue date."""
    issue_day = day.date() if day else datetime.now(UTC).date() - timedelta(days=1)
    root = paths.data_root()
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "daily.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        count = write_day(root, issue_day)
    get_logger(__name__).info("Wrote %d daily score rows for %s", count, issue_day)

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Annotated

import numpy as np
import pyarrow as pa
import typer

from nowcast import paths, storage
from nowcast.locks import file_lock
from nowcast.log import get_logger
from nowcast.model import metrics
from nowcast.model.cards import model_card
from nowcast.storage import month_path, read_rows
from nowcast.timeslots import date_range

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


def score_groups(root, rows):
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

    return groups


def probability_thresholds(root, selected, source, card_for):
    # Binary baselines keep 0.5; a tuned cutoff can only make them always predict rain.
    if source not in {"model", "clim"}:
        return 0.5
    thresholds = []
    for row in selected:
        card = card_for(root, row["model_version"])
        cutoff = card["leads"][str(row["lead_h"])]["csi_thresholds"]["probability"][source]
        thresholds.append(cutoff)
    return np.array(thresholds)



def source_score(root, group, source, card_for):
    column = SOURCES[source]
    selected = [row for row in group if row[column] is not None]
    if not selected:
        return None
    y = np.array([row["observed"] for row in selected], dtype=float)
    p = np.array([row[column] for row in selected], dtype=float)
    if source != "bmkg":
        p /= 100
    thresholds = probability_thresholds(root, selected, source, card_for)
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
    return {
        "n": len(y),
        "brier": metrics.brier(y, p),
        "bss": skill,
        "csi": metrics.csi(hits, false_alarms, misses),
        "pod": metrics.pod(hits, misses),
        "far": metrics.far(hits, false_alarms),
        "base_rate": float(y.mean()),
    }


def write_day(root: Path, day: date) -> int:
    first, stop = date_range(day.isoformat(), day.isoformat())
    gold = root / "gold"
    output = month_path(gold / "scores_daily", day, "part.parquet")
    rows = read_rows(
        month_path(gold / "verification", day, "part.parquet"),
        filters=[("t", ">=", first), ("t", "<", stop)],
    )
    groups = score_groups(root, rows)
    card_for = cache(model_card)
    scores = []
    for (scope, scope_id, lead), group in sorted(groups.items()):
        for source in SOURCES:
            score = source_score(root, group, source, card_for)
            if score is not None:
                scores.append(
                    {
                        "day": day,
                        "scope": scope,
                        "scope_id": scope_id,
                        "lead_h": lead,
                        "source": source,
                        **score,
                    }
                )
    if scores or output.is_file():
        previous = [row for row in read_rows(output) if row["day"] != day]
        output.parent.mkdir(parents=True, exist_ok=True)
        storage.write_table(pa.Table.from_pylist(previous + scores, schema=SCHEMA), output)
    return len(scores)


def daily(
    day: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
) -> None:
    """Replace daily scores for one UTC issue date."""
    issue_day = day.date() if day else datetime.now(UTC).date() - timedelta(days=1)
    root = paths.data_root()
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with file_lock(state, "daily.lock"):
        count = write_day(root, issue_day)
    get_logger(__name__).info("Wrote %d daily score rows for %s", count, issue_day)

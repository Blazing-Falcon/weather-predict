"""Write the real table schemas and sample rows that the "How it works" page shows.

Reads DATA_ROOT and writes two files the page loads:
- web/explain-samples.json: per pipeline stage the schema, row and file counts, and one real row for
  one area at the newest live issue. It also holds the feature rows of every admin area at that
  issue, rebuilt with the live scorer's code and scored again with the live boosters.
- web/explain-membership.json: the IMERG cell weights of every area.

    DATA_ROOT=... uv run python experiments/08_explain_samples.py [--area "Kota Malang"]
"""

import argparse
import gzip
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pyarrow.parquet as pq

from nowcast import config, paths
from nowcast.features import build
from nowcast.model import score
from nowcast.model.verify import BLOCK_START
from nowcast.registry import grids
from nowcast.sources import imerg

WEB = config.REPO_ROOT / "web"


def plain(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 4)
    return value


def read(path, filters=None, newest=None, limit=None):
    found = pq.read_table(path, partitioning=None, filters=filters).to_pylist()
    if newest:
        found.sort(key=lambda row: row[newest], reverse=True)
    return [{key: plain(value) for key, value in row.items()} for row in found[:limit]]


def table(root, directory, pattern, sample, partitioning):
    """Schema, totals over every file of the dataset, and the sample rows."""
    files = sorted((root / directory).glob(pattern))
    meta = [pq.ParquetFile(file).metadata for file in files]
    return {
        "path": f"{directory}/{pattern}",
        "partitioning": partitioning,
        "files": len(files),
        "rows": sum(item.num_rows for item in meta),
        "bytes": sum(file.stat().st_size for file in files),
        "compression": meta[0].row_group(0).column(0).compression,
        "schema": [[field.name, str(field.type)] for field in pq.read_schema(files[0])],
        "sample": sample,
    }


def month_file(root, directory, time, part="part.parquet"):
    return root / directory / f"year={time:%Y}" / f"month={time:%m}" / part


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--area", default="Kota Malang")
    args = parser.parse_args()
    root = paths.data_root()
    model = config.settings()["model"]
    version, lag = model["live_version"], model["imerg_lag_minutes"]

    dim = pq.read_table(root / "ref" / "dim_area.parquet").to_pylist()
    area = next(row for row in dim if row["kemendagri_name"] == args.area)
    area_id = area["area_id"]
    offset = build.LOCAL_OFFSETS[area["tz"]]

    now = datetime.now(UTC)
    predictions_file = month_file(root, "gold/predictions", now)
    live = pq.read_table(predictions_file, filters=[("model_version", "=", version)]).to_pylist()
    t = max(row["t"] for row in live)
    imerg_slot = build.available_imerg_slot(t, lag)
    local = t + timedelta(hours=offset)
    print(f"{args.area} (area {area_id}), issue {t:%Y-%m-%d %H:%M}Z, IMERG slot {imerg_slot:%H:%M}Z")

    stages = {}
    stages["ref_dim_area"] = table(
        root, "ref", "dim_area.parquet", [{k: plain(v) for k, v in area.items()}], "none"
    )
    membership = pq.read_table(root / "ref" / "imerg_membership.parquet").to_pylist()
    own = [row for row in membership if row["area_id"] == area_id]
    stages["ref_imerg_membership"] = table(
        root, "ref", "imerg_membership.parquet",
        [{k: plain(v) for k, v in row.items()} for row in own[:2]], "none",
    )
    stages["ref_neighbours"] = table(
        root, "ref", "neighbours.parquet",
        read(root / "ref" / "neighbours.parquet", [("area_id", "=", area_id)], limit=2), "none",
    )

    # Bronze IMERG crop of the slot the issue uses, and the area mean computed from it here.
    grid = grids.imerg_grid()
    crops = root / "bronze" / "imerg" / f"year={imerg_slot:%Y}" / f"month={imerg_slot:%m}"
    crop = next(crops.glob(f"*.{imerg_slot:%Y%m%d}-S{imerg_slot:%H%M}00-*.nc4"))
    values = imerg.read_crop(crop, grid)
    cells = [[row["cell"], round(row["weight"], 4), round(float(values[row["cell"]]), 3)]
             for row in own]
    valid = [(weight, rain) for _, weight, rain in cells if rain >= 0]
    mean = sum(weight * rain for weight, rain in valid) / sum(weight for weight, _ in valid)
    stages["bronze_imerg"] = {
        "path": "bronze/imerg/year=YYYY/month=MM/<CMR granule title>.nc4",
        "file": crop.name,
        "bytes": crop.stat().st_size,
        "variable": "/Grid/precipitation, float32, [time 1][lon 480][lat 180], mm/hr, fill -9999.9",
        "cells": cells,
        "mean_mm_h": round(mean, 4),
    }

    scan = sorted((root / "bronze" / "himawari").glob("date=*/hour=*"))[-1]
    segments = sorted(scan.glob("*.DAT.bz2"))[-3:]
    stages["bronze_himawari"] = {
        "path": "bronze/himawari/date=YYYY-MM-DD/hour=HH/<segment>.DAT.bz2",
        "files": [[path.name, path.stat().st_size] for path in segments],
    }

    run = sorted((root / "bronze" / "bmkg").glob("run=*"))[-1]
    # BMKG's cache can serve the previous run for a while, so take a body of the folder's own run.
    analysis = f"{run.name.removeprefix('run=')}:00:00"
    record = body = None
    for chunk in sorted(run.glob("sweep=*.jsonl.gz")):
        with gzip.open(chunk, "rt", encoding="utf-8") as file:
            for line in file:
                item = json.loads(line)
                if item["area_id"] == area_id and item["http_status"] == 200:
                    body = json.loads(item["body"])
                    if body["data"][0]["cuaca"][0][0]["analysis_date"] == analysis:
                        record = item
                        break
        if record:
            break
    stages["bronze_bmkg"] = {
        "path": "bronze/bmkg/run=YYYY-MM-DDTHH/sweep=<fetched_at>.jsonl.gz",
        "file": f"{run.name}/{chunk.name}",
        "record": {key: record[key] for key in ("area_id", "adm4", "fetched_at", "http_status")},
        "steps": sum(len(day) for day in body["data"][0]["cuaca"]),
        "step": body["data"][0]["cuaca"][0][0],
    }

    stages["silver_rain"] = table(
        root, "silver/rain_kabkota", "year=*/month=*/part.parquet",
        read(month_file(root, "silver/rain_kabkota", imerg_slot),
             [("slot", "=", imerg_slot), ("area_id", "=", area_id)]),
        "year=YYYY/month=MM",
    )
    stages["silver_cloud"] = table(
        root, "silver/cloud_kabkota", "year=*/month=*/part-*.parquet",
        read(month_file(root, "silver/cloud_kabkota", t, "part-himawari.parquet"),
             [("slot", "=", t), ("area_id", "=", area_id)]),
        "year=YYYY/month=MM, one file per source (mergir, himawari)",
    )
    stages["silver_bmkg"] = table(
        root, "silver/bmkg_forecast", "year=*/month=*/part.parquet",
        bmkg_step(read(month_file(root, "silver/bmkg_forecast", t),
                       [("area_id", "=", area_id), ("first_seen", "<=", t)]), t),
        "year=YYYY/month=MM",
    )
    stages["silver_gauge"] = table(
        root, "silver/gauge_obs", "year=*/month=*/part-*.parquet",
        read(month_file(root, "silver/gauge_obs", now, "part-metar.parquet"),
             [("is_rain", "=", True)], newest="obs_time", limit=1),
        "year=YYYY/month=MM, one file per source (ghcnh, metar)",
    )
    stages["gold_climatology"] = table(
        root, "gold/climatology", "part.parquet",
        read(root / "gold" / "climatology" / "part.parquet",
             [("area_id", "=", area_id), ("month", "=", local.month),
              ("hour_local", "=", local.hour)]),
        "none",
    )

    # Live feature rows for every admin area, then the live boosters on them.
    frame = score.live_features(root, [t], lag)
    card = score.model_card(root, version)
    stored = {(row["area_id"], row["lead_h"]): row["prob_pct"] for row in live if row["t"] == t}
    probabilities, differ = {}, 0
    for lead in card["leads"]:
        booster = lgb.Booster(model_file=str(root / "models" / version / f"model_lead{lead}.txt"))
        inputs = frame.rename(columns={f"clim_p{lead}": "clim_p"})[card["features"]]
        for key, value in zip(frame["area_id"], np.rint(booster.predict(inputs) * 100), strict=True):
            probabilities.setdefault(int(key), []).append(int(value))
            differ += stored[(int(key), int(lead))] != int(value)
    print(f"{len(frame) * len(card['leads'])} probabilities recomputed, {differ} differ from gold")
    own_keys = frame.loc[frame.area_id == area_id].iloc[0]
    stages["features"] = {
        "path": "in memory at scoring time; the training table in tmp/features is deleted after training",
        "keys": {key: plain(own_keys[key]) for key in ("t", "imerg_slot", "cloud_slot")},
        "columns": build.FEATURES,
        "model_columns": card["features"],
        "rows": {int(row["area_id"]): [plain(row[name]) for name in build.FEATURES]
                 for row in frame.to_dict("records")},
        "probabilities": probabilities,
        "check": {"recomputed": len(frame) * len(card["leads"]), "differ_from_stored": differ},
    }

    stages["gold_predictions"] = table(
        root, "gold/predictions", "year=*/month=*/part.parquet",
        read(predictions_file, [("t", "=", t), ("area_id", "=", area_id)]),
        "year=YYYY/month=MM",
    )
    stages["gold_verification"] = table(
        root, "gold/verification", "year=*/month=*/part.parquet",
        read(month_file(root, "gold/verification", now), [("area_id", "=", area_id),
             ("model_version", "=", version)], newest="t", limit=1),
        "year=YYYY/month=MM",
    )
    stages["gold_scores"] = table(
        root, "gold/scores_daily", "year=*/month=*/part.parquet",
        read(month_file(root, "gold/scores_daily", now),
             [("scope", "=", "national"), ("source", "=", "model"), ("lead_h", "=", 3)],
             newest="day", limit=1),
        "year=YYYY/month=MM",
    )
    stages["gold_frames"] = table(
        root, "gold/map_frames", "date=*/part.parquet",
        read(root / "gold" / "map_frames" / f"date={t:%Y-%m-%d}" / "part.parquet",
             [("slot", "=", t), ("area_id", "=", area_id)]),
        "date=YYYY-MM-DD",
    )

    snapshot = {
        "generated_at": plain(now),
        "area_id": area_id,
        "area_name": args.area,
        "issue_t": plain(t),
        "imerg_slot": plain(imerg_slot),
        "model_version": version,
        "stages": stages,
    }
    write(WEB / "explain-samples.json", snapshot)

    weights = {}
    for row in membership:
        weights.setdefault(int(row["area_id"]), []).extend(
            [int(row["cell"]), round(float(row["weight"]), 3)])
    write(WEB / "explain-membership.json", {
        "width": grid.width, "height": grid.height, "step": 0.1,
        "lon0": round(float(grid.lon[0]), 2), "lat0": round(float(grid.lat[0]), 2),
        "areas": weights,
    })


def bmkg_step(rows, t):
    """The step of the newest run seen by t whose 3 h block holds t."""
    newest = max(row["analysis_time"] for row in rows)
    start = BLOCK_START[config.settings()["bmkg"]["step_alignment"]] * timedelta(minutes=30)
    return [
        row for row in rows if row["analysis_time"] == newest
        and timedelta(0) <= t - (datetime.fromisoformat(row["valid_time"]) + start) < timedelta(hours=3)
    ]


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"wrote {path.name}, {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()

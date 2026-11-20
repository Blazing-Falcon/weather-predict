import gzip
import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

from nowcast import config
from nowcast.api.archive import (
    AREAS,
    BMKG,
    CLIMATOLOGY,
    CLOUD,
    FRAMES,
    GAUGE,
    PREDICTIONS,
    RAIN,
    SCORES,
    VERIFICATION,
    read_json,
)
from nowcast.model.cards import model_card
from nowcast.retention import directory_size, disk_usage


def recent_runs(root):
    """The newest run of each job, newest first."""
    try:
        with (root / "state" / "runs.jsonl").open(encoding="utf-8") as stream:
            newest = {
                run["job"]: run
                for run in map(json.loads, (line for line in stream if line.endswith("\n")))
            }
    except FileNotFoundError:
        return []
    return sorted(newest.values(), key=lambda run: run["started"], reverse=True)


def latency_stats(root, now):
    records = defaultdict(list)
    for path in sorted((root / "state").glob("*latency*.jsonl")):
        file_source = path.stem.split("_latency", 1)[0]
        try:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        if line.endswith("\n"):
                            raise
                        break
                    stamp = entry.get("seen_at") or entry.get("ready_at") or entry.get("first_seen")
                    if stamp is not None and entry.get("latency_min") is not None:
                        records[entry.get("source", file_source)].append(
                            (datetime.fromisoformat(stamp), entry["latency_min"])
                        )
        except FileNotFoundError:
            continue
    cutoff = now - timedelta(hours=24)
    result = {}
    for source, samples in records.items():
        samples.sort()
        recent = sorted(latency for stamp, latency in samples if cutoff <= stamp <= now)
        stats = {
            "last": samples[-1][1],
            "last_at": samples[-1][0],
            "median_24h": None,
            "p90_24h": None,
            "samples_24h": len(recent),
        }
        if recent:
            for key, quantile in (("median_24h", 0.5), ("p90_24h", 0.9)):
                index = (len(recent) - 1) * quantile
                lower = int(index)
                upper = min(lower + 1, len(recent) - 1)
                stats[key] = recent[lower] + (recent[upper] - recent[lower]) * (index - lower)
        result[source] = stats
    return result


def pipeline(archive, now):
    root = archive.root
    report = disk_usage()
    policies = config.retention()["datasets"]
    sizes = report["datasets"]
    for name, directory in (
        (CLIMATOLOGY, root / CLIMATOLOGY),
    ):
        if directory.is_dir() and name not in sizes:
            sizes[name] = directory_size(directory)
            sizes["other"] -= sizes[name]
    settings = config.settings()
    processed = {}
    for source, dataset, where in (("imerg", RAIN, ""), ("mergir", CLOUD, "WHERE source = 1")):
        lon_first, lon_last = settings[source]["lon_index"]
        lat_first, lat_last = settings[source]["lat_index"]
        cells = (lon_last - lon_first + 1) * (lat_last - lat_first + 1)
        rows = archive.rows(dataset, f"SELECT count(DISTINCT slot) AS slots FROM data {where}")
        count = rows[0]["slots"] if rows else 0
        processed[source] = {
            "cells_per_grid": cells,
            "slots" if source == "imerg" else "frames": count,
            "values_processed": count * cells,
        }
    processed["silver_bytes"] = sum(
        size for name, size in sizes.items() if name.startswith("silver/")
    )
    storage = {
        name: {"bytes": sizes.get(name, 0), "cap": policies.get(name)}
        for name in sorted(sizes.keys() | policies.keys())
    }
    for node_id, dataset in PIPELINE_DATASETS.items():
        detail = pipeline_node(archive, node_id, sample_rows=False)
        entry = storage.setdefault(dataset, {"bytes": 0, "cap": policies.get(dataset)})
        if dataset == AREAS and (root / dataset).is_file():
            entry["bytes"] = (root / dataset).stat().st_size
        entry["columns"] = detail.get("columns", [])
        files = detail.get("files", [])
        entry["newest"] = detail.get("newest") or (
            files[0]["modified"] if files else detail.get("live", {}).get("created_at")
        )
    return {
        "as_of": now,
        "health": {
            path.stem: data
            for path in sorted((root / "state" / "health").glob("*.json"))
            if (data := read_json(path)) is not None
        },
        "latency_minutes": latency_stats(root, now),
        "storage": storage,
        "storage_total_bytes": report["total_bytes"],
        "storage_budget_gb": report["budget_gb"],
        "processed_vs_stored": processed,
        "runs": recent_runs(root),
        "imerg_backfill": read_json(root / "state" / "imerg_backfill.json") or {},
    }


PIPELINE_DATASETS = {
    "br_bmkg": "bronze/bmkg",
    "br_himawari": "bronze/himawari",
    "br_imerg": "bronze/imerg",
    "br_gauges": "bronze/gauges",
    "br_mergir": "bronze/mergir",
    "br_ref": AREAS,
    "sv_bmkg": BMKG,
    "sv_cloud": CLOUD,
    "sv_rain": RAIN,
    "sv_gauge": GAUGE,
    "gd_frames": FRAMES,
    "gd_tiles": "gold/tiles",
    "gd_pred": PREDICTIONS,
    "gd_verif": VERIFICATION,
    "gd_scores": SCORES,
    "gd_hindcast": "gold/hindcast",
    "gd_clim": CLIMATOLOGY,
    "md_models": "models",
}


def pipeline_node(archive, node_id, sample_rows=True):
    dataset = PIPELINE_DATASETS[node_id]
    if dataset == "models":
        return model_folder(archive.root)
    if dataset.startswith("bronze/") or dataset == "gold/tiles":
        return file_dataset(archive.root / dataset, dataset)
    return parquet_dataset(archive, dataset, sample_rows)


def model_folder(root):
    directory = root / "models"
    version = config.settings()["model"]["live_version"]
    try:
        card = model_card(root, version)
    except FileNotFoundError:
        card = {}
    return {
        "versions": sorted(path.name for path in directory.glob("*") if path.is_dir()),
        "live": {key: card[key] for key in ("version", "created_at") if key in card},
        "test_bss": {
            lead: details.get("metrics", {}).get("test", {}).get("model", {}).get("bss")
            for lead, details in card.get("leads", {}).items()
        },
    }


def file_dataset(directory, dataset):
    files = [
        (path, path.stat())
        for path in directory.rglob("*")
        if path.is_file() and not (dataset == "bronze/bmkg" and "_dlq" in path.parts)
    ]
    files.sort(key=lambda entry: (entry[1].st_mtime, str(entry[0])), reverse=True)
    result = {
        "file_count": len(files),
        "files": [
            {
                "name": path.relative_to(directory).as_posix(),
                "bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            }
            for path, stat in files[:3]
        ],
    }
    preview = next(
        (
            path
            for path, _ in files
            if (
                dataset == "bronze/bmkg"
                and path.name.endswith(".jsonl.gz")
                and "_dlq" not in path.parts
            )
            or (
                dataset == "bronze/gauges"
                and path.suffix == ".json"
                and "metar" in str(path).lower()
            )
        ),
        None,
    )
    if preview:
        if dataset == "bronze/bmkg":
            result["json_preview"] = bmkg_preview(preview)
        elif content := read_json(preview):
            result["json_preview"] = {
                key: content[0].get(key)
                for key in ("icaoId", "reportTime", "wxString", "rawOb", "lat", "lon")
            }
    return result


def bmkg_preview(path):
    with gzip.open(path, "rt") as stream:
        record = json.loads(stream.readline())
    body = json.loads(record["body"])
    steps = [step for point in body["data"] for day in point["cuaca"] for step in day]
    return {
        **{key: record.get(key) for key in ("area_id", "adm4", "fetched_at", "http_status")},
        "lokasi": {
            key: body["lokasi"].get(key)
            for key in ("desa", "kecamatan", "kotkab", "provinsi", "lat", "lon")
        },
        "step_count": len(steps),
        "steps": [
            {key: step.get(key) for key in (
                "local_datetime", "weather", "weather_desc", "tp", "tcc", "t", "hu"
            )}
            for step in steps[:2]
        ],
    }


def parquet_dataset(archive, dataset, sample_rows):
    directory = archive.root / dataset
    files = archive.files(dataset)
    if dataset in (CLOUD, GAUGE):
        suffix = "part-himawari.parquet" if dataset == CLOUD else "part-metar.parquet"
        files = [file for file in files if Path(file).name == suffix]
    if not files:
        return {"columns": [], "newest_partition": None, "rows": 0, "newest": None, "sample": []}
    file = files[-1]
    parquet = pq.ParquetFile(file)
    columns = [
        {"name": field.name, "type": str(field.type)}
        for field in parquet.schema_arrow
        if field.name not in {"year", "month", "date"}
    ]
    time_column = {
        RAIN: "slot",
        CLOUD: "slot",
        FRAMES: "slot",
        BMKG: "analysis_time",
        GAUGE: "obs_time",
        PREDICTIONS: "issued_at",
        VERIFICATION: "issued_at",
        "gold/hindcast": "issued_at",
        SCORES: "day",
    }.get(dataset)
    names = ", ".join('"' + col["name"] + '"' for col in columns)
    order = f'ORDER BY "{time_column}" DESC NULLS LAST' if time_column else ""
    sample = (
        archive.rows(dataset, f"SELECT {names} FROM data {order} LIMIT 3", files=[file])
        if sample_rows
        else []
    )
    newest = sample[0][time_column] if sample and time_column else None
    if not sample_rows and time_column in parquet.schema_arrow.names:
        index = parquet.schema_arrow.names.index(time_column)
        maxima = [
            stats.max
            for group in range(parquet.metadata.num_row_groups)
            if (stats := parquet.metadata.row_group(group).column(index).statistics)
            and stats.has_min_max
        ]
        newest = max(maxima) if maxima else None

    def iso(value):
        if isinstance(value, datetime):
            return (
                value.replace(tzinfo=UTC).isoformat()
                if value.tzinfo is None
                else value.astimezone(UTC).isoformat()
            )
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time(), UTC).isoformat()
        return value

    return {
        "columns": columns,
        "newest_partition": Path(file).relative_to(directory).as_posix()
        if directory.is_dir()
        else file.split("/")[-1],
        "rows": parquet.metadata.num_rows,
        "newest": iso(newest),
        "sample": [{key: iso(value) for key, value in row.items()} for row in sample],
    }

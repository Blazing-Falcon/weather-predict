import csv
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from nowcast import config
from nowcast.retention import _size, disk_usage

DB = duckdb.connect(config={"threads": "2"})
AREA_COUNT = 519
RAIN = "silver/rain_kabkota"
CLOUD = "silver/cloud_kabkota"
BMKG = "silver/bmkg_forecast"
GAUGE = "silver/gauge_obs"
PREDICTIONS = "gold/predictions"
VERIFICATION = "gold/verification"
SCORES = "gold/scores_daily"
FRAMES = "gold/map_frames"
CLIMATOLOGY = "gold/climatology"
AREAS = "ref/dim_area.parquet"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


class Archive:
    def __init__(self, root: Path, connection: duckdb.DuckDBPyConnection):
        self.root = root
        self.connection = connection

    def files(self, dataset, first=None, last=None):
        path = self.root / dataset
        if path.suffix == ".parquet":
            return [str(path)] if path.is_file() else []
        candidates = path.rglob("*.parquet")
        if first is not None:
            # Prune by the on-disk partition before DuckDB reads the Parquet footers.
            if dataset == FRAMES:
                begin, end = (f"date={stamp.date()}" for stamp in (first, last))
                candidates = (
                    file
                    for file in candidates
                    if file.parent == path or begin <= file.parent.name <= end
                )
            else:
                begin, end = (
                    f"year={stamp.year:04d}/month={stamp.month:02d}" for stamp in (first, last)
                )
                candidates = (
                    file
                    for file in candidates
                    if file.parent == path
                    or begin <= file.parent.relative_to(path).as_posix() <= end
                )
        return sorted(str(file) for file in candidates if not file.name.startswith("_"))

    def rows(self, dataset, sql, params=(), *, first=None, last=None, files=None):
        files = self.files(dataset, first, last) if files is None else files
        if not files:
            return []
        query = "WITH data AS (SELECT * FROM read_parquet(?, hive_partitioning=true, union_by_name=true)) "
        try:
            result = self.connection.execute(query + sql, [files, *params])
            columns = [column[0] for column in result.description]
            return [dict(zip(columns, row)) for row in result.fetchall()]
        except duckdb.IOException:
            # Retention can remove a partition between discovery and opening it.
            remaining = [file for file in files if Path(file).is_file()]
            if len(remaining) == len(files):
                raise
            return self.rows(dataset, sql, params, files=remaining)

    def latest(self, dataset, column, where=""):
        months = defaultdict(list)
        for file in self.files(dataset):
            months[str(Path(file).parent)].append(file)
        for month in sorted(months, reverse=True):
            rows = self.rows(
                dataset,
                f"SELECT max({column}) AS latest FROM data {where}",
                files=months[month],
            )
            if rows and rows[0]["latest"] is not None:
                return rows[0]["latest"]
        return None

    def areas(self, area_id=None):
        where = "" if area_id is None else "WHERE area_id = ?"
        params = () if area_id is None else (area_id,)
        return self.rows(AREAS, f"SELECT * FROM data {where} ORDER BY area_id", params)

    def frames(self, layer, first, last):
        rows = self.rows(
            FRAMES,
            "SELECT slot, area_id, value FROM data WHERE layer = ? AND slot BETWEEN ? AND ? "
            "ORDER BY slot, area_id",
            (layer, first, last),
            first=first,
            last=last,
        )
        frames = {}
        for row in rows:
            slot = row["slot"]
            if slot not in frames:
                frames[slot] = [None] * AREA_COUNT
            frames[slot][row["area_id"]] = row["value"]
        return {
            "layer": layer,
            "slots": [slot.isoformat() for slot in frames],
            "values": list(frames.values()),
        }

    def predictions(self, first, last, area_id=None):
        where = "" if area_id is None else "AND area_id = ?"
        params = (first, last) if area_id is None else (first, last, area_id)
        return self.rows(
            PREDICTIONS,
            f""", versions AS (
                SELECT *, max(model_version) OVER (PARTITION BY t) AS newest_version
                FROM data WHERE t BETWEEN ? AND ?
            )
            SELECT t AS slot, issued_at, area_id, lead_h, prob_pct, model_version, cloud_slot
            FROM versions WHERE model_version = newest_version AND lead_h BETWEEN 1 AND 3
            {where} ORDER BY slot, area_id, lead_h""",
            params,
            first=first,
            last=last,
        )

    def bmkg(self, first, last, area_id=None):
        # Runs publish 3-hour steps up to three days ahead.
        start = first - timedelta(days=4)
        where = "" if area_id is None else "AND area_id = ?"
        params = (start, last) if area_id is None else (start, last, area_id)
        if first == last:
            where += (
                " AND first_seen <= ?"
                " QUALIFY analysis_time = max(analysis_time) OVER (PARTITION BY area_id)"
            )
            params = (*params, first)
        rows = self.rows(
            BMKG,
            "SELECT area_id, analysis_time, first_seen, valid_time, weather "
            f"FROM data WHERE analysis_time BETWEEN ? AND ? {where} ORDER BY analysis_time DESC",
            params,
            first=start,
            last=last,
        )
        settings = config.settings()["bmkg"]
        alignment = {"before": -3, "after": 0, "centred": -1.5}[settings["step_alignment"]]
        offset = timedelta(hours=alignment)
        runs = defaultdict(dict)
        for row in rows:
            run = runs[row["area_id"]].setdefault(row["analysis_time"], (row["first_seen"], {}))
            run[1][row["valid_time"] + offset] = row["weather"]
        return runs

    def cloud(self, first, last, area_id=None):
        where = "" if area_id is None else "AND area_id = ?"
        params = (first, last) if area_id is None else (first, last, area_id)
        return self.rows(
            CLOUD,
            "SELECT slot, area_id, source, cold_pct, tb_min FROM data "
            f"WHERE slot BETWEEN ? AND ? {where} ORDER BY slot, source",
            params,
            first=first,
            last=last,
        )

    def scores(self, now, days, scope=None, scope_id=None, lead=None):
        first = date.fromordinal(max(1, now.date().toordinal() - days + 1))
        where = ["day BETWEEN ? AND ?"]
        params = [first, now.date()]
        for column, value in (("scope", scope), ("scope_id", scope_id), ("lead_h", lead)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        return self.rows(
            SCORES,
            "SELECT day, scope, scope_id, lead_h, source, n, brier, bss, csi, pod, far, "
            f"base_rate FROM data WHERE {' AND '.join(where)} "
            "ORDER BY day, scope, scope_id, lead_h, source",
            params,
            first=first,
            last=now.date(),
        )

    def climatology(self, area_id):
        return self.rows(
            CLIMATOLOGY,
            "SELECT month, hour_local, wet_pct, p1, p2, p3 "
            "FROM data WHERE area_id = ? ORDER BY month, hour_local",
            (area_id,),
        )


def weather_at(runs, slot, as_of):
    for analysis, (first_seen, steps) in runs.items():
        if first_seen <= as_of:
            for start, weather in steps.items():
                if start <= slot < start + timedelta(hours=3):
                    return {"weather": weather, "analysis_time": analysis, "first_seen": first_seen}
            break
    return {"weather": None, "analysis_time": None, "first_seen": None}


def bmkg_rain(weather):
    if weather is None:
        return None
    return config.settings()["bmkg"]["rain_codes_min"] <= weather <= 99


def tile_times(root, layer, suffix, first):
    cutoff = first.strftime("%Y%m%dT%H%MZ")
    return sorted(
        path.stem
        for path in (root / "gold" / "tiles" / layer).glob(f"*{suffix}")
        if path.stem >= cutoff
    )


def cards(root):
    result = []
    for path in (root / "models").glob("*/card.json"):
        card = read_json(path)
        if card is not None:
            result.append(card)
    return sorted(
        result,
        key=lambda card: (datetime.fromisoformat(card["created_at"]), card["version"]),
        reverse=True,
    )


def meta(archive, now):
    root = archive.root
    rain = archive.latest(RAIN, "slot", "WHERE rain_mean_x10 IS NOT NULL")
    issue = archive.latest(PREDICTIONS, "t")
    run = archive.latest(BMKG, "analysis_time")
    seen = (
        archive.rows(
            BMKG,
            "SELECT min(first_seen) AS first_seen FROM data WHERE analysis_time = ?",
            (run,),
            first=run,
            last=run,
        )
        if run is not None
        else []
    )
    slots = {
        "imerg": archive.latest(RAIN, "slot"),
        "mergir": archive.latest(CLOUD, "slot", "WHERE source = 1"),
        "himawari": archive.latest(CLOUD, "slot", "WHERE source = 2"),
        "bmkg": run,
        "gauges": archive.latest(GAUGE, "obs_time"),
        "metar": archive.latest(GAUGE, "obs_time", "WHERE source = 'metar'"),
        "ghcnh": archive.latest(GAUGE, "obs_time", "WHERE source = 'ghcnh'"),
        "model": issue,
    }
    areas = archive.areas()
    model_cards = cards(root)
    reporting = {
        row["station_id"]
        for row in archive.rows(
            GAUGE,
            "SELECT DISTINCT station_id FROM data WHERE source = 'metar' AND obs_time >= ?",
            [now - timedelta(hours=24)],
        )
    }
    with (config.CONFIG_DIR / "geo" / "metar_stations.csv").open(encoding="utf-8") as source:
        gauges = [
            {**row, "lat": float(row["lat"]), "lon": float(row["lon"])}
            for row in csv.DictReader(source)
            if row["icao"] in reporting
        ]
    return {
        "as_of": now,
        "latest_slots": slots,
        "bmkg_first_seen": seen[0]["first_seen"] if seen else None,
        "freshness_minutes": {
            source: round((now - slot).total_seconds() / 60, 1) if slot is not None else None
            for source, slot in slots.items()
        },
        "model_version": model_cards[0]["version"] if model_cards else None,
        "tile_bounds": read_json(root / "gold" / "tiles" / "bounds.json"),
        "tile_times": {
            "rain": tile_times(root, "rain", ".png", now - timedelta(days=7)),
            "cloud": tile_times(root, "cloud", ".webp", now - timedelta(days=7)),
        },
        "areas": areas,
        "area_count": len(areas),
        "latest_observed_slot": rain,
        "latest_issue_slot": issue,
        "gauge_stations": gauges,
    }


def area_detail(archive, area_id, area, now):
    first = now - timedelta(hours=48)
    observed = archive.rows(
        RAIN,
        "SELECT slot, rain_mean_x10 FROM data WHERE area_id = ? AND slot BETWEEN ? AND ? "
        "ORDER BY slot",
        (area_id, first, now),
        first=first,
        last=now,
    )
    last = now + timedelta(hours=3)
    runs = archive.bmkg(first, now, area_id).get(area_id, {})
    bmkg = []
    if runs:
        slot = first.replace(minute=30 * (first.minute // 30), second=0, microsecond=0)
        if slot < first:
            slot += timedelta(minutes=30)
        while slot <= last:
            forecast = weather_at(runs, slot, min(slot, now))
            bmkg.append({"slot": slot, **forecast, "rain": bmkg_rain(forecast["weather"])})
            slot += timedelta(minutes=30)
    score_scope, scope_id = "area", str(area_id)
    scores = archive.scores(now, 30, score_scope, scope_id)
    if not scores and area and area["prov_code"] is not None:
        score_scope, scope_id = "province", area["prov_code"]
        scores = archive.scores(now, 30, score_scope, scope_id)
    predictions = archive.predictions(first, now, area_id)
    return {
        "area_id": area_id,
        "area": area,
        "observed": observed,
        "predictions": predictions,
        "bmkg": bmkg,
        "cloud": archive.cloud(first, now, area_id),
        "scores": {"scope": score_scope, "scope_id": scope_id, "rows": scores},
        "climatology": archive.climatology(area_id),
    }


def alerts(archive, threshold, level, island, province):
    issue = archive.latest(PREDICTIONS, "t")
    if issue is None:
        return {"issue_slot": None, "alerts": []}
    predictions = archive.predictions(issue, issue)
    areas = {row["area_id"]: row for row in archive.areas()}
    cloud_slots = [row["cloud_slot"] for row in predictions if row["cloud_slot"] is not None]
    first = min(cloud_slots, default=issue - timedelta(hours=6))
    first = min(first, issue - timedelta(hours=6))
    clouds = archive.cloud(first, issue)
    by_scan = {(row["area_id"], row["slot"]): row for row in clouds}
    latest = {row["area_id"]: row for row in clouds}
    forecasts = archive.bmkg(issue, issue)
    entries = {}
    for prediction in predictions:
        area_id = prediction["area_id"]
        area = areas.get(area_id, {})
        if island and (area.get("island") or "").casefold() != island.casefold():
            continue
        if province and province.casefold() not in {
            (area.get("prov_code") or "").casefold(),
            (area.get("prov_name") or "").casefold(),
        }:
            continue
        if area_id not in entries:
            cloud = by_scan.get((area_id, prediction["cloud_slot"]), latest.get(area_id, {}))
            bmkg = weather_at(forecasts.get(area_id, {}), issue, issue)
            entries[area_id] = {
                "area_id": area_id,
                "name": area.get("geo_name"),
                "province": area.get("prov_name"),
                "province_code": area.get("prov_code"),
                "island": area.get("island"),
                "prob_pct": dict.fromkeys(("1", "2", "3")),
                "tb_min": cloud.get("tb_min"),
                "cold_pct": cloud.get("cold_pct"),
                "cloud_slot": cloud.get("slot"),
                "bmkg": {**bmkg, "rain": bmkg_rain(bmkg["weather"])},
            }
        entries[area_id]["prob_pct"][str(prediction["lead_h"])] = prediction["prob_pct"]

    selected = []
    for entry in entries.values():
        probability = max((p for p in entry["prob_pct"].values() if p is not None), default=-1)
        tb_min = entry["tb_min"]
        rank = (
            3
            if probability >= 85 and tb_min is not None and tb_min <= 203
            else 2
            if probability >= 70
            else 1
            if probability >= threshold
            else 0
        )
        if rank >= level and probability >= 0:
            entry.update(
                level=("None", "Waspada", "Siaga", "Awas")[rank],
                rank=rank,
                max_prob_pct=probability,
            )
            selected.append(entry)
    selected.sort(key=lambda entry: (-entry["rank"], -entry["max_prob_pct"], entry["area_id"]))
    return {"issue_slot": issue, "alerts": selected}


def model(archive, now):
    model_cards = cards(archive.root)
    history = [
        {
            "version": card["version"],
            "created_at": card["created_at"],
            "test_bss": {
                lead: details["metrics"]["test"]["model"]["bss"]
                for lead, details in card["leads"].items()
            },
        }
        for card in model_cards
    ]
    first = now - timedelta(days=30)
    dimension = archive.files(AREAS)
    csi = (
        archive.rows(
            VERIFICATION,
            """, verified AS (
            SELECT *, max(model_version) OVER (PARTITION BY t) AS newest_version
            FROM data WHERE t BETWEEN ? AND ?
        ), by_source AS (
            SELECT lead_h, island, observed, source, wet,
                hour(t) + CASE tz WHEN 'WIB' THEN 7 WHEN 'WITA' THEN 8 WHEN 'WIT' THEN 9 END AS hour
            FROM verified
            JOIN read_parquet(?, hive_partitioning=true, union_by_name=true) USING (area_id)
            CROSS JOIN LATERAL (VALUES
                ('model', prob_pct >= 50), ('motion', motion_pct >= 50),
                ('persist', persist_pct >= 50), ('clim', clim_pct >= 50), ('bmkg', bmkg_rain)
            ) AS forecast(source, wet)
            WHERE model_version = newest_version AND wet IS NOT NULL
        ), local AS (
            SELECT *, hour % 24 AS hour_local FROM by_source
        )
        SELECT lead_h, source, hour_local, island, count(*) AS n,
            count(*) FILTER (WHERE wet AND observed)::DOUBLE
            / nullif(count(*) FILTER (WHERE wet OR observed), 0) AS csi
        FROM local
        GROUP BY GROUPING SETS ((lead_h, source, hour_local), (lead_h, source, island))
        ORDER BY lead_h, source, hour_local, island""",
            (first, now, dimension),
            first=first,
            last=now,
        )
        if dimension
        else []
    )
    return {
        "card": model_cards[0] if model_cards else None,
        "history": history,
        "live_csi": {
            "from": first,
            "to": now,
            "threshold_pct": 50,
            "by_local_hour": [
                {key: value for key, value in row.items() if key != "island"}
                for row in csi
                if row["hour_local"] is not None
            ],
            "by_island": [
                {key: value for key, value in row.items() if key != "hour_local"}
                for row in csi
                if row["island"] is not None
            ],
        },
    }


def history(archive, area_id, area):
    zone = {"WIB": 7, "WITA": 8, "WIT": 9}.get(area["tz"] if area else None, 0)
    totals = archive.rows(
        RAIN,
        """, local AS (
            SELECT slot + ? * INTERVAL '1 hour' AS local_slot, rain_mean_x10
            FROM data WHERE area_id = ? AND rain_mean_x10 IS NOT NULL
        )
        SELECT year(local_slot) AS year, month(local_slot) AS month,
            least(3, (day(local_slot) - 1) // 10 + 1) AS dekad,
            sum(rain_mean_x10) AS rain_x10, count(*) AS slots
        FROM local GROUP BY year, month, dekad ORDER BY year, month, dekad""",
        (zone, area_id),
    )
    monthly = defaultdict(lambda: [0, 0])
    yearly = defaultdict(lambda: [0, 0])
    for row in totals:
        year, month = row["year"], row["month"]
        monthly[year, month][0] += row["rain_x10"]
        monthly[year, month][1] += row["slots"]
        yearly[year][0] += row["rain_x10"]
        yearly[year][1] += row["slots"]

    seasons = {}
    for (year, month), (rain, _) in monthly.items():
        if month >= 9 and rain >= 3000:
            seasons.setdefault(year, {"year": year, "onset_month": month, "last_wet_dekad": None})
    for row in totals:
        year, month = row["year"], row["month"]
        start_year = year if month >= 9 else year - 1
        if start_year in seasons and row["rain_x10"] >= 1000:
            season = seasons[start_year]
            if month < 9 or month >= season["onset_month"]:
                season["last_wet_dekad"] = {"year": year, "month": month, "dekad": row["dekad"]}

    return {
        "area_id": area_id,
        "area": area,
        "timezone": area["tz"] if area and area["tz"] else "UTC",
        "climatology": archive.climatology(area_id),
        "climatology_period": config.settings()["model"]["climatology_period"],
        "monthly": [
            {"year": year, "month": month, "rain_mm": rain / 20, "slots": slots}
            for (year, month), (rain, slots) in monthly.items()
        ],
        "yearly": [
            {"year": year, "rain_mm": rain / 20, "slots": slots}
            for year, (rain, slots) in yearly.items()
        ],
        "wet_season_rule": (
            "Onset is the first calendar month from September through December with at least "
            "150 mm recorded. The last wet dekad is the last observed 1–10, 11–20 or 21–end "
            "period from onset through the following August with at least 50 mm recorded."
        ),
        "wet_season": [
            seasons.get(year, {"year": year, "onset_month": None, "last_wet_dekad": None})
            for year in yearly
        ],
    }


def dagster_runs(root):
    path = root / "dagster" / "history" / "runs.db"
    if not path.is_file():
        return []
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT run_id, pipeline_name AS job, status, create_timestamp AS created_at, "
                "start_time, end_time, end_time - start_time AS duration_seconds "
                "FROM runs ORDER BY id DESC LIMIT 20"
            )
            return [dict(row) for row in rows]
    except sqlite3.OperationalError as error:
        if error.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and (
            path.is_file() and str(error) != "no such table: runs"
        ):
            raise
        return []


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
        ("dagster_runs", root / "dagster" / "history"),
    ):
        if directory.is_dir() and name not in sizes:
            sizes[name] = _size(directory)
            sizes["other"] -= sizes[name]
    if "dagster_compute_logs" not in sizes:
        logs = sum(
            _size(directory) for directory in (root / "dagster" / "storage").glob("*/compute_logs")
        )
        sizes["dagster_compute_logs"] = logs
        sizes["other"] -= logs
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
    return {
        "as_of": now,
        "health": {
            path.stem: data
            for path in sorted((root / "state" / "health").glob("*.json"))
            if (data := read_json(path)) is not None
        },
        "latency_minutes": latency_stats(root, now),
        "storage": {
            name: {"bytes": sizes.get(name, 0), "cap": policies.get(name)}
            for name in sorted(sizes.keys() | policies.keys())
        },
        "storage_total_bytes": report["total_bytes"],
        "storage_budget_gb": report["budget_gb"],
        "processed_vs_stored": processed,
        "dagster_runs": dagster_runs(root),
        "imerg_backfill": read_json(root / "state" / "imerg_backfill.json") or {},
    }

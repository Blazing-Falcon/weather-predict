import csv
from collections import Counter, defaultdict
from datetime import timedelta
from itertools import groupby
from operator import itemgetter

from nowcast import config
from nowcast.api.archive import AREAS, RAIN, VERIFICATION
from nowcast.model.cards import cards
from nowcast.timeslots import LOCAL_OFFSETS


def model(archive, now):
    model_cards = cards(archive.root)
    live = config.settings()["model"]["live_version"]
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
    return {
        "card": next((card for card in model_cards if card["version"] == live), None),
        "history": history,
        "live_csi": live_csi(archive, now),
    }


def live_csi(archive, now):
    offsets = "CASE tz " + " ".join(
        f"WHEN '{zone}' THEN {hours}" for zone, hours in LOCAL_OFFSETS.items()
    ) + " END"
    first = now - timedelta(days=30)
    dimension = archive.files(AREAS)
    csi = (
        archive.rows(
            VERIFICATION,
            f""", verified AS (
            SELECT *, max(model_version) OVER (PARTITION BY t) AS newest_version
            FROM data WHERE t BETWEEN ? AND ?
        ), by_source AS (
            SELECT lead_h, island, observed, source, wet,
                hour(t) + {offsets} AS hour
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
    }


def _read_enso(path):
    with path.open(encoding="utf-8", newline="") as source:
        next(source)
        reader = csv.DictReader(source)
        seasons = reader.fieldnames[1:]
        rows = {int(row["year"]): row for row in reader}

    series = []
    latest_year = None
    for year in range(min(rows), max(rows) + 1):
        row = rows.get(year, {})
        for season in seasons:
            phase = None
            if cell := row.get(season):
                latest_year = year
                value = float(cell)
                if value >= 0.5:
                    phase = "El Niño"
                elif value <= -0.5:
                    phase = "La Niña"
            series.append((phase, year, season))

    phases = {}
    for phase, run in groupby(series, key=itemgetter(0)):
        if phase is None:
            continue
        episode = list(run)
        if len(episode) >= 5:
            counts = Counter(year for _, year, season in episode if season in ("JJA", "JAS", "ASO", "SON"))
            phases.update({year: phase for year, count in counts.items() if count >= 3})
    return phases, latest_year


ENSO_PHASES, ENSO_LATEST_YEAR = _read_enso(config.CONFIG_DIR / "enso" / "roni.csv")


def history(archive, area_id, area):
    zone = LOCAL_OFFSETS.get(area["tz"] if area else None, 0)
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
        "enso": [
            {"year": year, "phase": ENSO_PHASES.get(year), "provisional": year == ENSO_LATEST_YEAR}
            for year in yearly
        ],
        "wet_season": [
            seasons.get(year, {"year": year, "onset_month": None, "last_wet_dekad": None})
            for year in yearly
        ],
    }



import csv
from collections import defaultdict
from datetime import timedelta

from nowcast import config
from nowcast.api.archive import BMKG, CLOUD, FRAMES, GAUGE, PREDICTIONS, RAIN, read_json
from nowcast.model.bmkg_baseline import bmkg_rain, weather_at
from nowcast.model.cards import has_card
from nowcast.timeslots import SLOT


def tile_times(root, layer, suffix, first):
    cutoff = first.strftime("%Y%m%dT%H%MZ")
    return sorted(
        path.stem
        for path in (root / "gold" / "tiles" / layer).glob(f"*{suffix}")
        if path.stem >= cutoff
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
    live = config.settings()["model"]["live_version"]
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
        "model_version": live if has_card(root, live) else None,
        "tile_bounds": read_json(root / "gold" / "tiles" / "bounds.json"),
        "tile_times": {
            "rain": tile_times(root, "rain", ".png", now - timedelta(days=31)),
            "cloud": tile_times(root, "cloud", ".webp", now - timedelta(days=31)),
        },
        "areas": areas,
        "area_count": len(areas),
        "latest_observed_slot": rain,
        "latest_issue_slot": issue,
        "gauge_stations": gauges,
        "frames_updated": max(
            (file.stat().st_mtime for file in (archive.root / FRAMES).glob("date=*/*.parquet")),
            default=None,
        ),
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
            slot += SLOT
        while slot <= last:
            forecast = weather_at(runs, slot, min(slot, now))
            bmkg.append({"slot": slot, **forecast, "rain": bmkg_rain(forecast["weather"])})
            slot += SLOT
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


def latest_issue(archive):
    issue = archive.latest(PREDICTIONS, "t")
    if issue is None:
        return None, [], [], {}
    predictions = archive.predictions(issue, issue)
    cloud_slots = [row["cloud_slot"] for row in predictions if row["cloud_slot"] is not None]
    first = min(min(cloud_slots, default=issue), issue - timedelta(hours=6))
    clouds = archive.cloud(first, issue)
    forecasts = archive.bmkg(issue, issue, end=issue + timedelta(hours=3))
    return issue, predictions, clouds, forecasts


def filter_areas(area_ids, areas, island, province):
    for area_id in area_ids:
        area = areas.get(area_id, {})
        if island and (area.get("island") or "").casefold() != island.casefold():
            continue
        if province and province.casefold() not in {
            (area.get("prov_code") or "").casefold(),
            (area.get("prov_name") or "").casefold(),
        }:
            continue
        yield area_id, area


def alert_entry(area_id, area, predictions, cloud, runs, issue):
    bmkg_3h = None
    if runs:
        (analysis, (_, steps)), = runs.items()
        weather = max((code for code in steps.values() if bmkg_rain(code)), default=None)
        if weather is not None:
            bmkg_3h = {"weather": weather, "analysis_time": analysis}
    bmkg = weather_at(runs, issue, issue)
    probabilities = dict.fromkeys(("1", "2", "3"))
    probabilities.update((str(row["lead_h"]), row["prob_pct"]) for row in predictions)
    return {
        "area_id": area_id,
        "name": area.get("geo_name"),
        "province": area.get("prov_name"),
        "province_code": area.get("prov_code"),
        "island": area.get("island"),
        "prob_pct": probabilities,
        "tb_min": cloud.get("tb_min"),
        "cold_pct": cloud.get("cold_pct"),
        "cloud_slot": cloud.get("slot"),
        "bmkg": {**bmkg, "rain": bmkg_rain(bmkg["weather"])},
        "bmkg_3h": bmkg_3h,
        "max_prob_pct": max((p for p in probabilities.values() if p is not None), default=None),
    }


def rank_model_alerts(entries, threshold, level):
    selected = []
    for entry in entries:
        probability = entry["max_prob_pct"]
        if probability is None:
            continue
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
        if rank >= level:
            entry.update(level=("None", "Waspada", "Siaga", "Awas")[rank], rank=rank)
            selected.append(entry)
    return sorted(selected, key=lambda entry: (-entry["rank"], -entry["max_prob_pct"], entry["area_id"]))


def rank_bmkg_alerts(entries):
    return sorted(
        (entry for entry in entries if entry["bmkg_3h"] is not None),
        key=lambda entry: (
            -entry["bmkg_3h"]["weather"],
            -(entry["max_prob_pct"] if entry["max_prob_pct"] is not None else -1),
            entry["area_id"],
        ),
    )


def alerts(archive, threshold, level, island, province, source):
    issue, predictions, clouds, forecasts = latest_issue(archive)
    if issue is None:
        return {"issue_slot": None, "alerts": []}
    areas = {row["area_id"]: row for row in archive.areas()}
    by_scan = {(row["area_id"], row["slot"]): row for row in clouds}
    latest = {row["area_id"]: row for row in clouds}
    by_area = defaultdict(list)
    for prediction in predictions:
        by_area[prediction["area_id"]].append(prediction)
    entries = []
    area_ids = forecasts if source == "bmkg" else by_area
    for area_id, area in filter_areas(area_ids, areas, island, province):
        area_predictions = by_area.get(area_id, [])
        cloud_slot = area_predictions[0]["cloud_slot"] if area_predictions else None
        cloud = by_scan.get((area_id, cloud_slot), latest.get(area_id, {}))
        entries.append(
            alert_entry(area_id, area, area_predictions, cloud, forecasts.get(area_id, {}), issue)
        )
    if source == "bmkg":
        newest = max((analysis for runs in forecasts.values() for analysis in runs), default=None)
        return {
            "issue_slot": issue,
            "bmkg_analysis_time": newest,
            "alerts": rank_bmkg_alerts(entries),
        }
    return {"issue_slot": issue, "alerts": rank_model_alerts(entries, threshold, level)}

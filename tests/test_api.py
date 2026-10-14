import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from nowcast.api.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def write_rows(root, dataset, rows, stamp=None):
    path = root / dataset
    if path.suffix != ".parquet":
        if stamp is not None:
            part = (
                f"date={stamp.date()}"
                if dataset == "gold/map_frames"
                else (f"year={stamp:%Y}/month={stamp:%m}")
            )
            path /= part
        path /= "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


@pytest.fixture
def archive(tmp_path):
    slot = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    old = slot - timedelta(hours=12)
    areas = [2, 7, 8, 10, 11]
    dimension = [
        {
            "area_id": area,
            "geo_name": f"Area {area}",
            "prov_code": "35",
            "prov_name": "Jawa Timur",
            "island": "Jawa",
            "tz": "WIB",
            "bmkg_adm4": "35.73.01.1001",
        }
        for area in areas
    ]
    write_rows(tmp_path, "ref/dim_area.parquet", dimension)
    frames = defaultdict(list)
    for stamp in (slot, old):
        frames[stamp.date()].extend(
            [
                {"slot": stamp, "area_id": 2, "layer": "obs", "value": 13},
                {"slot": stamp, "area_id": 7, "layer": "obs", "value": 0},
                {"slot": stamp, "area_id": 2, "layer": "prob3", "value": 85},
            ]
        )
    for rows in frames.values():
        write_rows(tmp_path, "gold/map_frames", rows, rows[0]["slot"])
    write_rows(
        tmp_path,
        "silver/rain_kabkota",
        [
            {"slot": slot, "area_id": 2, "rain_mean_x10": 13},
        ],
        slot,
    )
    write_rows(
        tmp_path,
        "silver/cloud_kabkota",
        [
            {
                "slot": slot,
                "area_id": area,
                "source": 2,
                "cold_pct": 33,
                "tb_min": 203 if area == 2 else 204,
            }
            for area in areas
        ],
        slot,
    )
    predictions = [
        {
            "t": slot,
            "issued_at": slot + timedelta(minutes=1),
            "area_id": area,
            "lead_h": lead,
            "prob_pct": probability if lead == 3 else 10,
            "model_version": "new",
            "cloud_slot": slot,
        }
        for area, probability in zip(areas, (85, 99, 70, 50, 49), strict=True)
        for lead in (1, 2, 3)
    ]
    write_rows(
        tmp_path,
        "gold/predictions",
        predictions + [{**predictions[0], "model_version": "earlier", "prob_pct": 100}],
        slot,
    )
    write_rows(
        tmp_path,
        "silver/bmkg_forecast",
        [
            {
                "area_id": 2,
                "analysis_time": slot - timedelta(hours=2),
                "first_seen": slot - timedelta(hours=1),
                "weather": 61,
                "valid_time": slot,
            },
            {
                "area_id": 2,
                "analysis_time": slot - timedelta(hours=1),
                "first_seen": slot + timedelta(minutes=20),
                "weather": 0,
                "valid_time": slot,
            },
        ],
        slot,
    )
    write_rows(
        tmp_path,
        "gold/scores_daily",
        [
            {
                "day": slot.date(),
                "scope": "province",
                "scope_id": "35",
                "lead_h": 3,
                "source": "model",
                "n": 1,
                "brier": 0.12,
                "bss": 0.2,
                "csi": 0.5,
                "pod": 0.6,
                "far": 0.3,
                "base_rate": 0.3,
            }
        ],
        slot,
    )
    write_rows(
        tmp_path,
        "gold/climatology",
        [
            {
                "area_id": 2,
                "month": 1,
                "hour_local": 12,
                "wet_pct": 4.5,
                "p1": 10,
                "p2": 12,
                "p3": 14,
            }
        ],
    )
    return slot, old


def test_frames_and_cache(client, archive, tmp_path):
    slot, old = archive
    live = client.get("/api/frames", params={"layer": "obs"})
    assert live.status_code == 200
    assert live.headers["cache-control"] == "public, max-age=60"
    assert live.headers["content-encoding"] == "gzip"
    body = live.json()
    assert body["layer"] == "obs"
    assert body["slots"] == [old.isoformat(), slot.isoformat()]
    assert len(body["values"][0]) == 519
    assert body["values"][1][:8] == [None, None, 13, None, None, None, None, 0]
    finished = client.get(
        "/api/frames",
        params={
            "layer": "obs",
            "from": old.isoformat(),
            "to": old.isoformat(),
        },
    )
    assert finished.json()["slots"] == [old.isoformat()]
    assert finished.headers["cache-control"] == "public, max-age=86400"
    assert (
        client.get(
            "/api/frames",
            params={
                "from": (slot - timedelta(days=8)).isoformat(),
                "to": slot.isoformat(),
            },
        ).status_code
        == 422
    )

    tile = f"{old:%Y%m%dT%H%MZ}"
    path = tmp_path / "gold" / "tiles" / "rain" / f"{tile}.png"
    assert client.get(f"/tiles/rain/{tile}.png").status_code == 404
    assert client.get("/tiles/cloud/absent.webp").status_code == 404
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    image = client.get(f"/tiles/rain/{tile}.png")
    assert image.status_code == 200
    assert image.headers["cache-control"] == "public, max-age=86400"
    assert client.get("/geo/neighbours.geojson").headers["cache-control"] == "public, max-age=86400"


def test_area_and_alerts(client, archive):
    slot, _ = archive
    response = client.get("/api/areas/2")
    assert response.status_code == 200
    data = response.json()
    assert data["area"]["bmkg_adm4"] == "35.73.01.1001"
    assert data["observed"] == [{"slot": slot.isoformat(), "rain_mean_x10": 13}]
    assert [row["prob_pct"] for row in data["predictions"]] == [10, 10, 85]
    assert next(row for row in data["bmkg"] if row["slot"] == slot.isoformat())["weather"] == 61
    assert data["cloud"][0]["cold_pct"] == 33
    assert data["scores"]["scope"] == "province"
    assert data["scores"]["rows"][0]["csi"] == 0.5
    assert data["climatology"][0]["p3"] == 14
    assert client.get("/api/areas/3").status_code == 404

    response = client.get("/api/alerts")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.json()["issue_slot"] == slot.isoformat()
    rows = response.json()["alerts"]
    assert [(row["area_id"], row["level"]) for row in rows] == [
        (2, "Awas"),
        (7, "Siaga"),
        (8, "Siaga"),
        (10, "Waspada"),
    ]
    assert rows[0]["prob_pct"] == {"1": 10, "2": 10, "3": 85}
    assert rows[0]["tb_min"] == 203
    assert rows[0]["bmkg"]["rain"] is True
    for params, expected in (
        ({"level": "awas", "province": "35"}, [2]),
        ({"level": 2, "province": "jawa timur"}, [2, 7, 8]),
        ({"threshold": 60}, [2, 7, 8]),
        ({"island": "Papua"}, []),
    ):
        body = client.get("/api/alerts", params=params).json()
        assert [row["area_id"] for row in body["alerts"]] == expected

    scores = client.get("/api/scores", params={"scope": "province", "id": "35", "lead": 3})
    assert scores.json() == data["scores"]["rows"]


def test_missing_datasets(client):
    response = client.get("/api/meta")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    meta = response.json()
    assert meta["area_count"] == 0
    assert meta["model_version"] is None
    assert meta["tile_bounds"] is None
    assert meta["bmkg_first_seen"] is None
    assert meta["latest_observed_slot"] is None
    assert meta["latest_issue_slot"] is None
    assert all(value is None for value in meta["freshness_minutes"].values())
    assert client.get("/api/frames").json() == {"layer": "prob3", "slots": [], "values": []}
    assert client.get("/api/areas/2").json()["observed"] == []
    assert client.get("/api/alerts").json() == {"issue_slot": None, "alerts": []}
    assert client.get("/api/model").json()["card"] is None
    assert client.get("/api/history/2").json()["yearly"] == []
    assert client.get("/api/pipeline").json()["dagster_runs"] == []


def test_model_history_and_pipeline(client, archive, tmp_path):
    slot, _ = archive
    write_rows(
        tmp_path,
        "gold/verification",
        [
            {
                "t": slot,
                "area_id": 2,
                "lead_h": 3,
                "model_version": "v2",
                "observed": True,
                "prob_pct": 85,
                "motion_pct": 10,
                "persist_pct": 0,
                "clim_pct": 5,
                "bmkg_rain": True,
            }
        ],
        slot,
    )
    for idx, version in enumerate(("v2", "v1")):
        path = tmp_path / "models" / version / "card.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": version,
                    "created_at": (slot - timedelta(days=idx)).isoformat(),
                    "leads": {"3": {"metrics": {"test": {"model": {"bss": 0.12 - idx}}}}},
                }
            )
        )
    model = client.get("/api/model").json()
    assert model["card"]["version"] == "v2"
    assert [row["test_bss"]["3"] for row in model["history"]] == [0.12, -0.88]
    by_hour = model["live_csi"]["by_local_hour"]
    assert next(row for row in by_hour if row["source"] == "model") == {
        "lead_h": 3,
        "source": "model",
        "hour_local": (slot.hour + 7) % 24,
        "n": 1,
        "csi": 1.0,
    }

    september = datetime(2020, 9, 1, tzinfo=UTC)
    write_rows(
        tmp_path,
        "silver/rain_kabkota",
        [
            {"slot": september, "area_id": 2, "rain_mean_x10": 3000},
        ],
        september,
    )
    history = client.get("/api/history/2").json()
    assert history["monthly"][0] == {"year": 2020, "month": 9, "rain_mm": 150, "slots": 1}
    assert history["yearly"][0]["rain_mm"] == 150
    assert history["wet_season"][0]["onset_month"] == 9
    assert "150 mm" in history["wet_season_rule"]

    health = tmp_path / "state/health"
    health.mkdir(parents=True)
    (health / "imerg.json").write_text('{"new_slots": 1}')
    (health.parent / "imerg_latency.jsonl").write_text(
        "\n".join(
            json.dumps({"seen_at": (slot + timedelta(minutes=i)).isoformat(), "latency_min": i})
            for i in (10, 20, 30)
        )
    )
    runs = tmp_path / "dagster/history/runs.db"
    runs.parent.mkdir(parents=True)
    with closing(sqlite3.connect(runs)) as connection:
        connection.executescript(
            "CREATE TABLE runs (id, run_id, pipeline_name, status, create_timestamp, "
            "start_time, end_time); "
            "INSERT INTO runs VALUES (1, 'r1', 'score_now', 'SUCCESS', '2020-01-01', 1, 3);"
        )
    pipeline = client.get("/api/pipeline").json()
    assert pipeline["health"]["imerg"]["new_slots"] == 1
    assert pipeline["latency_minutes"]["imerg"]["median_24h"] == 20
    assert pipeline["latency_minutes"]["imerg"]["p90_24h"] == 28
    assert pipeline["storage"]["silver/rain_kabkota"]["cap"] == {"keep": "10 years"}
    assert pipeline["processed_vs_stored"]["imerg"]["slots"] == 2
    assert pipeline["processed_vs_stored"]["imerg"]["values_processed"] == 172800
    assert pipeline["dagster_runs"][0]["duration_seconds"] == 2

    openapi = client.get("/openapi.json").json()
    assert len(openapi["paths"]) == 12
    assert all(item["get"]["summary"] for item in openapi["paths"].values())

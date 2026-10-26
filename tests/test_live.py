import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from nowcast import config
from nowcast.cli import app
from nowcast.features.build import available_imerg_slot
from nowcast.features.spark import spark_session
from nowcast.model import score, scores_daily, verify
from nowcast.registry.dim_area import SCHEMA as AREA_SCHEMA
from nowcast.registry.neighbours import SCHEMA as EDGE_SCHEMA
from nowcast.silver.bmkg import SCHEMA as BMKG_SCHEMA
from nowcast.sources.imerg import RAIN_SCHEMA
from nowcast.sources.mergir import CLOUD_SCHEMA

T = datetime(2025, 1, 31, 23, 30, tzinfo=UTC)
LAG = 60
ANCHOR = available_imerg_slot(T, LAG)
HALF = timedelta(minutes=30)
ISSUED = T + timedelta(minutes=20)
VERSION = "20250131T0000Z-test"
CLIM_SCHEMA = pa.schema(
    [
        ("area_id", pa.int16()),
        ("month", pa.int32()),
        ("hour_local", pa.int32()),
        ("wet_pct", pa.float32()),
        *[(f"p{lead}", pa.uint8()) for lead in (1, 2, 3)],
    ]
)


def write(path, rows, schema):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def write_slots(root, name, rows, schema, filename="part.parquet"):
    for month in {row["slot"].replace(day=1) for row in rows}:
        part = score.partition(root / "silver" / name, month, filename)
        write(part, [row for row in rows if row["slot"].month == month.month], schema)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("IMERG_LAG_MINUTES", str(LAG))
    monkeypatch.setitem(config.settings()["model"], "live_version", VERSION)
    (tmp_path / "state").mkdir()
    directory = tmp_path / "models" / VERSION
    directory.mkdir(parents=True)
    card = {
        "features": ["cold_trend_1h", "rain_lag", "rain_sum_3h", "island", "clim_p"],
        "threshold_mm_h": 0.5,
        "leads": {
            str(lead): {
                "calibration_used": lead == 1,
                "csi_thresholds": {
                    "motion_adv_max_pct": 10,
                    "probability": {"model": 0.7, "clim": 0.5, "persist": 1.0, "motion": 1.0},
                },
            }
            for lead in (1, 2)
        },
    }
    (directory / "card.json").write_text(json.dumps(card))
    (directory / "isotonic_lead1.json").write_text(
        json.dumps({"thresholds": [0, 1], "values": [0.2, 0.8]})
    )
    write(
        tmp_path / "ref" / "dim_area.parquet",
        [
            {
                "area_id": 1,
                "is_admin": True,
                "area_km2": 10,
                "island": "Jawa",
                "tz": "WIB",
                "prov_code": "35",
            }
        ],
        AREA_SCHEMA,
    )
    write(tmp_path / "ref" / "neighbours.parquet", [], EDGE_SCHEMA)
    write(
        tmp_path / "gold" / "climatology" / "part.parquet",
        [{"area_id": 1, "month": 2, "hour_local": 6, "wet_pct": 25, "p1": 25, "p2": 50}],
        CLIM_SCHEMA,
    )
    return tmp_path


def rain(slot, value):
    return {
        "slot": slot,
        "area_id": 1,
        "rain_mean_x10": value,
        "rain_max_x10": value,
        "wet_pct": 20,
    }


def cloud(slot, value):
    return {"slot": slot, "area_id": 1, "source": 2, "cold_pct": value, "adv2_pct": 10}


def prediction(t=T, lead=1, probability=80):
    return {
        "t": t,
        "area_id": 1,
        "lead_h": lead,
        "prob_pct": probability,
        "model_version": VERSION,
        "cloud_slot": t,
        "imerg_slot": available_imerg_slot(t, LAG),
        "issued_at": ISSUED,
    }


def test_score_leakage_and_first_write(root, monkeypatch):
    cloud_rows = [cloud(T - 2 * HALF, 10), cloud(T, 20), cloud(T + HALF, 99)]
    rain_rows = [rain(ANCHOR - HALF, 8), rain(ANCHOR, 2), rain(ANCHOR + HALF, 99), rain(T, 99)]

    def seed(cloud_data, rain_data):
        write_slots(root, "cloud_kabkota", cloud_data, CLOUD_SCHEMA, "part-himawari.parquet")
        write_slots(root, "rain_kabkota", rain_data, RAIN_SCHEMA)

    with spark_session(root) as spark:
        monkeypatch.setattr(score, "spark_session", lambda _: nullcontext(spark))
        seed(cloud_rows, rain_rows)
        present = score.live_features(root, [T], LAG)
        seed(cloud_rows[:2], rain_rows[:2])
        truncated = score.live_features(root, [T], LAG)
        seed(cloud_rows[:2], rain_rows[:1])
        missing = score.live_features(root, [T], LAG)
        assert missing[score.RAIN_FEATURES].isna().all().all()
    pd.testing.assert_frame_equal(truncated, present)
    assert truncated.iloc[0]["imerg_slot"] == ANCHOR
    assert truncated.iloc[0]["rain_lag"] == pytest.approx(0.2)
    assert truncated.iloc[0]["cold_trend_1h"] == 10

    predicted = 0.75

    class Booster:
        def __init__(self, *, model_file):
            pass

        def predict(self, frame, *, num_threads):
            assert frame.iloc[0]["cold_trend_1h"] == 10
            return np.full(len(frame), predicted)

    monkeypatch.setattr(score.lgb, "Booster", Booster)
    monkeypatch.setattr(score, "live_features", lambda *args: truncated)
    seed(cloud_rows[:2], rain_rows)
    assert score.score_pending(root, ISSUED) == 0
    seed(cloud_rows[:2], rain_rows[:2])
    assert score.score_pending(root, ISSUED) == 2
    part = score.partition(root / "gold" / "predictions", T)
    first = pq.ParquetFile(part).read()
    assert first.schema == score.SCHEMA
    assert first["prob_pct"].to_pylist() == [65, 75]
    assert score.score_pending(root, ISSUED) == 0
    (root / "state" / "score_live.json").unlink()
    predicted = 0.9
    assert score.score_pending(root, ISSUED) == 0
    assert pq.ParquetFile(part).read().equals(first)
    assert json.loads((root / "state" / "health" / "score.json").read_text())["rows"] == 2


def test_verify_waits_and_keeps_the_first_result(root):
    write(
        score.partition(root / "gold" / "predictions", T),
        [prediction(), prediction(lead=2)],
        score.SCHEMA,
    )
    write_slots(root, "cloud_kabkota", [cloud(T, 10)], CLOUD_SCHEMA, "part-himawari.parquet")
    write_slots(root, "rain_kabkota", [rain(ANCHOR, 5), rain(T, 1), rain(T + HALF, 5)], RAIN_SCHEMA)
    part = score.partition(root / "gold" / "verification", T)
    assert verify.verify_pending(root) == 1
    [first] = score.read_rows(part)
    assert first["observed"] is True
    assert (first["clim_pct"], first["persist_pct"], first["motion_pct"]) == (25, 100, 100)
    assert first["bmkg_rain"] is None
    assert verify.verify_pending(root) == 0
    assert score.read_rows(part) == [first]

    write_slots(
        root,
        "rain_kabkota",
        [rain(T, 0), rain(T + HALF, 0), rain(T + 2 * HALF, 0), rain(T + 3 * HALF, 0)],
        RAIN_SCHEMA,
    )
    assert verify.verify_pending(root) == 1
    [original, second] = score.read_rows(part)
    assert original == first
    assert second["observed"] is False
    assert second["persist_pct"] is None
    assert len({score.KEY(row) for row in (original, second)}) == 2


@pytest.mark.parametrize(
    ("alignment", "wet"), [("before", True), ("after", False), ("centred", True)]
)
def test_bmkg_as_of_issue(root, monkeypatch, alignment, wet):
    monkeypatch.setitem(config.settings()["bmkg"], "step_alignment", alignment)
    write(score.partition(root / "gold" / "predictions", T), [prediction()], score.SCHEMA)
    write_slots(root, "rain_kabkota", [rain(T, 2), rain(T + HALF, 2)], RAIN_SCHEMA)
    newest_available = T - 4 * HALF
    rows = [
        {
            "analysis_time": analysis,
            "first_seen": seen,
            "area_id": 1,
            "weather": code,
            "valid_time": valid,
        }
        for analysis, seen, valid, code in [
            (T - 2 * HALF, ISSUED + HALF, T, 95),
            (newest_available, ISSUED, T + 2 * HALF, 60),
            (newest_available, ISSUED, T - 4 * HALF, 0),
            (T - 8 * HALF, ISSUED - HALF, T, 0),
        ]
    ]
    write(score.partition(root / "silver" / "bmkg_forecast", T), rows, BMKG_SCHEMA)
    assert verify.verify_pending(root) == 1
    [row] = score.read_rows(score.partition(root / "gold" / "verification", T))
    assert row["bmkg_rain"] is wet
    assert row["bmkg_analysis_time"] == newest_available


def test_daily_scores(root):
    rows = [
        prediction(t, probability=p)
        | {
            "verified_at": ISSUED,
            "observed": wet,
            "clim_pct": 25,
            "persist_pct": None,
            "motion_pct": 0,
            "bmkg_rain": bmkg,
        }
        for t, p, wet, bmkg in [(T - HALF, 80, True, True), (T, 60, False, None)]
    ]
    write(score.partition(root / "gold" / "verification", T), rows, verify.SCHEMA)
    runner = CliRunner()
    result = runner.invoke(app, ["live", "daily", "--day", T.date().isoformat()])
    assert result.exit_code == 0, result.exception
    output = score.partition(root / "gold" / "scores_daily", T)
    first = score.read_rows(output)
    assert len(first) == 12
    by_scope = {(r["scope"], r["scope_id"], r["source"]): r for r in first}
    model = by_scope["national", "ID", "model"]
    assert model["brier"] == pytest.approx((0.2**2 + 0.6**2) / 2)
    assert model["bss"] == pytest.approx(1 - 0.2 / 0.3125)
    assert (model["n"], model["csi"], model["pod"], model["far"], model["base_rate"]) == (
        2,
        1,
        1,
        0,
        0.5,
    )
    assert by_scope["island", "Jawa", "bmkg"]["n"] == 1
    assert by_scope["province", "35", "clim"]["bss"] == 0
    assert scores_daily.write_day(root, T.date()) == 12
    assert score.read_rows(output) == first

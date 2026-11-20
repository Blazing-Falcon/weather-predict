import shutil
import subprocess
import sys
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nowcast.cloud import CLOUD_PARTS, CLOUD_SCHEMA
from nowcast.features import live
from nowcast.model import score
from nowcast.timeslots import SLOT, UTC_US


def test_score_import_without_pyspark():
    subprocess.run([
        sys.executable, "-c",
        ("import sys; import nowcast.model.score; "
        "assert not any(n == 'pyspark' or n.startswith('pyspark.') for n in sys.modules)"),
    ], check=True)


def test_live_feature_parity(tmp_path):
    pytest.importorskip("pyspark", reason="Spark feature parity requires pyspark")
    if shutil.which("java") is None:
        pytest.skip("Spark feature parity requires Java")
    from nowcast.features import rows
    from nowcast.features.spark import spark_session

    assert live.KEYS == rows.KEYS
    assert live.FEATURES == rows.FEATURES
    first = datetime(2025, 1, 31, tzinfo=UTC)
    pending = [first + i * SLOT for i in (47, 48, 50, 51, 70, 95)]
    lag = 61

    def write(path, data, schema=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(data, schema=schema), path)

    write(tmp_path / "ref" / "dim_area.parquet", [
        {"area_id": area, "area_km2": 12.345, "island": island,
         "tz": tz, "is_admin": area != 5}
        for area, island, tz in [(1, "Jawa", "WIB"), (2, "Bali", "WITA"),
                                 (3, "Papua", "WIT"), (4, None, "WIB"), (5, "Jawa", "WIB")]
    ], pa.schema([("area_id", pa.int16()), ("area_km2", pa.float64()),
                  ("island", pa.string()), ("tz", pa.string()), ("is_admin", pa.bool_())]))
    write(tmp_path / "ref" / "neighbours.parquet", [
        {"area_id": a, "neighbour_id": b} for a, b in ((1, 2), (1, 3), (2, 1), (3, 1))
    ], pa.schema([("area_id", pa.int16()), ("neighbour_id", pa.int16())]))
    write(tmp_path / "gold" / "climatology" / "part.parquet", [
        {"area_id": a, "month": m, "hour_local": h, "wet_pct": 33.3,
         "p1": 17, "p2": None, "p3": 39}
        for a in (1, 2) for m in (1, 2) for h in range(24)
    ], pa.schema([("area_id", pa.int16()), ("month", pa.int32()), ("hour_local", pa.int32()),
                  ("wet_pct", pa.float32()), *[(f"p{i}", pa.uint8()) for i in (1, 2, 3)]]))
    rain_schema = pa.schema([("slot", UTC_US), ("area_id", pa.int16()),
                            *[(c, pa.int16()) for c in
                              ("rain_mean_x10", "rain_max_x10", "wet_pct")]])
    for month, indices in ((1, range(48)), (2, range(48, 96))):
        rain = [
            {"slot": first + i * SLOT, "area_id": a,
             "rain_mean_x10": (i * a) % 53 if a == 1 else 0,
             "rain_max_x10": i * 2, "wet_pct": None if i % 7 == 0 else 31}
            for i in indices for a in (1, 2) if i != 47 and (i, a) != (44, 1)
        ]
        cloud = [
            {"slot": first + i * SLOT, "area_id": a, "source": 2 if a != 5 else 1,
             "cold_pct": (i + a) % 100, "tb_min": 220, "tb_mean_x10": 2327,
             "adv1_pct": None, "adv2_pct": 19 if i % 3 else None,
             "adv3_pct": 7, "flow_kmh": 20}
            for i in indices for a in (1, 2, 3, 5) if i != 46 and (i, a) != (48, 2)
        ]
        base = tmp_path / "silver"
        write(base / "rain_kabkota" / "year=2025" / f"month={month:02}" / "part.parquet",
              rain, rain_schema)
        write(base / "cloud_kabkota" / "year=2025" / f"month={month:02}" / CLOUD_PARTS[2],
              cloud, CLOUD_SCHEMA)

    actual = live.live_features(tmp_path, pending, lag)
    with spark_session(tmp_path) as spark:
        expected = rows.feature_rows(
            spark, tmp_path, rows.admin_areas(spark, tmp_path),
            rows.read_climatology(spark, tmp_path), pending[0], pending[-1] + SLOT, lag, 1,
            cloud_source=2,
        ).where("t in (" + ",".join(
            f"timestamp'{t:%Y-%m-%d %H:%M:%S}'" for t in pending
        ) + ")").select(*rows.KEYS, *rows.FEATURES).toArrow().to_pandas(categories=["island"])
    expected["island"] = expected["island"].cat.reorder_categories(actual.island.cat.categories)
    pd.testing.assert_frame_equal(
        actual.sort_values(live.KEYS).reset_index(drop=True),
        expected.sort_values(live.KEYS).reset_index(drop=True),
        atol=1e-9, rtol=0,
    )
    masked = score.live_features(tmp_path, pending, lag)
    missing = masked.imerg_slot == first + 47 * SLOT
    assert missing.any()
    assert np.isnan(masked.loc[missing, score.RAIN_FEATURES]).all().all()

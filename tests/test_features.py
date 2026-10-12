from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from math import sin, tau

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

from nowcast import config
from nowcast.features import build, climatology
from nowcast.features.spark import spark_session
from nowcast.registry.dim_area import SCHEMA as AREA_SCHEMA
from nowcast.registry.neighbours import SCHEMA as EDGE_SCHEMA
from nowcast.sources.imerg import RAIN_SCHEMA
from nowcast.sources.mergir import CLOUD_SCHEMA

FIRST = datetime(2025, 1, 31, tzinfo=UTC)
SECOND = FIRST + timedelta(days=1)
HALF_HOUR = timedelta(minutes=30)
ISSUE = FIRST + 16 * HALF_HOUR
LAG = 61


def write_rows(path, schema, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


@pytest.fixture(scope="module")
def silver(tmp_path_factory):
    root = tmp_path_factory.mktemp("feature_data")
    write_rows(
        root / "ref" / "dim_area.parquet",
        AREA_SCHEMA,
        [
            {
                "area_id": area,
                "is_admin": area < 3,
                "area_km2": 12.5,
                "island": "Jawa",
                "tz": "WIB" if area == 1 else "WITA",
            }
            for area in (1, 2, 3)
        ],
    )
    write_rows(
        root / "ref" / "neighbours.parquet",
        EDGE_SCHEMA,
        [
            {"area_id": 1, "neighbour_id": 2},
            {"area_id": 2, "neighbour_id": 1},
        ],
    )
    rain = []
    cloud = []
    for index in range(96):
        for area in (1, 2):
            slot = FIRST + index * HALF_HOUR
            if (area, index) != (1, 22):
                mean = {13: 4, 17: 10, 21: 12, 48: 20}.get(index, 0) if area == 1 else 20
                rain.append(
                    {
                        "slot": slot,
                        "area_id": area,
                        "rain_mean_x10": mean,
                        "rain_max_x10": mean * 2,
                        "wet_pct": 31,
                        "product_version": "V07B",
                    }
                )
            cloud.append(
                {
                    "slot": slot,
                    "area_id": area,
                    "source": 1,
                    "cold_pct": (index + area * 3) % 100,
                    "tb_min": 220,
                    "tb_mean_x10": 2325,
                    "adv1_pct": 5,
                    "adv2_pct": 9,
                    "adv3_pct": 7,
                    "flow_kmh": 20,
                }
            )
    cloud.append({"slot": ISSUE, "area_id": 1, "source": 2, "cold_pct": 99})
    rain_dir = root / "silver" / "rain_kabkota" / "year=2025"
    cloud_dir = root / "silver" / "cloud_kabkota" / "year=2025"
    for day in (FIRST, SECOND):
        write_rows(
            rain_dir / f"month={day:%m}" / "part.parquet",
            RAIN_SCHEMA,
            [row for row in rain if row["slot"].date() == day.date()],
        )
        write_rows(
            cloud_dir / f"month={day:%m}" / "part-mergir.parquet",
            CLOUD_SCHEMA,
            [row for row in cloud if row["slot"].date() == day.date()],
        )
    return (
        root,
        rain_dir / "month=01" / "part.parquet",
        cloud_dir / "month=01" / "part-mergir.parquet",
        [row for row in rain if row["slot"] < SECOND],
        [row for row in cloud if row["slot"] < SECOND],
    )


@pytest.fixture(scope="module")
def spark(silver):
    with spark_session(silver[0]) as session:
        yield session


def test_available_imerg_slot():
    assert build.available_imerg_slot(ISSUE, 360) == FIRST + 4 * HALF_HOUR
    assert build.available_imerg_slot(ISSUE, LAG) == FIRST + 13 * HALF_HOUR


def test_issue_time_features(spark, silver, monkeypatch):
    root, rain_part, cloud_part, rain, cloud = silver
    monkeypatch.setenv("DATA_ROOT", str(root))
    monkeypatch.setenv("IMERG_LAG_MINUTES", str(LAG))
    monkeypatch.setitem(config.settings()["model"], "climatology_period", ["2025-01-31"] * 2)
    monkeypatch.setattr(climatology, "spark_session", lambda _: nullcontext(spark))
    monkeypatch.setattr(build, "spark_session", lambda _: nullcontext(spark))

    climatology.climatology()
    clim_part = root / "gold" / "climatology" / "part.parquet"
    clim = pq.read_table(clim_part)
    assert all(clim.schema.field(f"p{lead}").type == pa.uint8() for lead in build.LEADS)
    assert clim.schema.field("n").type == pa.int32()
    climate_cells = {(r["area_id"], r["month"], r["hour_local"]): r for r in clim.to_pylist()}
    assert climate_cells[1, 1, 15]["p1"] == 100
    assert climate_cells[1, 1, 15]["n"] == 2
    assert climate_cells[2, 2, 7]["n"] == 2
    assert climate_cells[2, 2, 7]["p2"] is None

    assert climate_cells[1, 2, 6]["p1"] == 0

    out = root / "features"
    build.build("2025-01-31", "2025-02-01", out)
    table = ds.dataset(out, format="parquet", partitioning="hive").to_table()
    rows = {(r["t"], r["area_id"]): r for r in table.to_pylist()}
    assert len(rows) == (96 - 5) * 2 == table.num_rows
    assert {area for _, area in rows} == {1, 2}
    assert set(table["date"].to_pylist()) == {"2025-01-31", "2025-02-01"}
    assert all(
        r["cloud_slot"] == t and r["imerg_slot"] <= t - timedelta(minutes=LAG)
        for (t, _), r in rows.items()
    )
    assert all(table.schema.field(name).type == pa.int8() for name in build.TARGETS)
    assert all(
        table.schema.field(name).type == pa.float32() for name in build.FEATURES if name != "island"
    )
    expected = rows[ISSUE, 1]
    assert [expected[name] for name in build.TARGETS] == [1, 1, 1]
    assert [rows[ISSUE + 2 * HALF_HOUR, 1][name] for name in build.TARGETS] == [0, 1, None]
    assert [rows[ISSUE + 4 * HALF_HOUR, 1][name] for name in build.TARGETS] == [1, None, None]
    assert rows[SECOND - HALF_HOUR, 1]["y1"] == 1
    assert rows[SECOND, 1]["cold_trend_1h"] == 2
    assert rows[SECOND, 1]["nb_rain_lag"] == 2
    assert rows[FIRST, 1]["rain_lag"] is None
    assert rows[FIRST, 1]["cold_trend_1h"] is None
    assert expected["rain_lag"] == pytest.approx(0.4)
    assert expected["rainmax_lag"] == pytest.approx(0.8)
    assert expected["nb_rain_lag"] == 2
    assert expected["rain_sum_3h"] == pytest.approx(0.2)
    assert expected["cold_pct"] == 19
    assert expected["nb_cold_pct"] == 22
    assert expected["cold_trend_1h"] == 2
    assert expected["tb_mean"] == 232.5
    assert expected["adv_max_pct"] == 9
    assert expected["hour_sin"] == pytest.approx(sin(tau * 15 / 24))
    assert expected["clim_wet_pct"] == 31
    assert expected["clim_p1"] == 100

    areas = build.admin_areas(spark, root).where("area_id = 1")
    climate = spark.read.parquet(str(clim_part)).selectExpr(
        "area_id",
        "month",
        "hour_local",
        "wet_pct as clim_wet_pct",
        "p1 as clim_p1",
        "p2 as clim_p2",
        "p3 as clim_p3",
    )

    def issue_features():
        return (
            build.feature_rows(
                spark,
                root,
                areas,
                climate,
                ISSUE,
                ISSUE + HALF_HOUR,
                LAG,
                1,
            )
            .select(*build.FEATURES)
            .first()
            .asDict()
        )

    expected_features = {name: expected[name] for name in build.FEATURES}
    write_rows(
        rain_part,
        RAIN_SCHEMA,
        [
            row | {"rain_mean_x10": 999, "rain_max_x10": 999, "wet_pct": 99}
            if row["slot"] > ISSUE - timedelta(minutes=LAG)
            else row
            for row in rain
        ],
    )
    assert issue_features() == expected_features

    write_rows(rain_part, RAIN_SCHEMA, rain)
    write_rows(
        cloud_part,
        CLOUD_SCHEMA,
        [
            row | {"cold_pct": 98, "tb_min": 250, "adv1_pct": 99} if row["slot"] > ISSUE else row
            for row in cloud
        ],
    )
    assert issue_features() == expected_features

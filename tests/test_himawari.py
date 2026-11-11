from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyproj import CRS

from nowcast import config
from nowcast.cloud import CLOUD_PARTS, CLOUD_SCHEMA, CloudMotion, cloud_rows
from nowcast.registry.grids import Grid, mergir_grid
from nowcast.sources.himawari import (
    _check_coverage,
    block_mean,
    is_model_slot,
    merge_scan,
    pixel_index,
)
from nowcast.storage import write_table
from nowcast.zonal import Zonal


def test_pixel_index_and_block_mean():
    grid = Grid("test", [100., 101.], [-1., 0.])
    lon = np.array([[99.5, 100.2, 100.5, 101.2], [99.8, 100.1, 101.5, np.inf]])
    lat = np.array([[-1.4, -1.2, -1., -1.], [-0.5, 0.2, -0.2, 0.]])
    tb = np.array([[200., 220., 230., np.nan], [250., 270., 300., 310.]])

    index = pixel_index(lon, lat, grid)
    np.testing.assert_array_equal(index, [[0, 0, 1, 1], [2, 2, -1, -1]])
    np.testing.assert_allclose(block_mean(tb, index, grid), [[210, 230], [260, np.nan]])


def test_coverage_across_antimeridian_and_missing_segment():
    area = SimpleNamespace(
        crs=CRS.from_dict({"proj": "geos", "lon_0": 140.7, "h": 35785863, "ellps": "WGS84"}),
        area_extent=(-5500000, -2200000, 5500000, 1100000),
        projection_x_coords=np.linspace(-5499000, 5499000, 5500),
    )
    grid = mergir_grid()
    _check_coverage(area, grid, [5, 6, 7])

    area.area_extent = (-5500000, -2200000, 5500000, 0)
    with pytest.raises(ValueError, match="additional segments required: 05"):
        _check_coverage(area, grid, [6, 7])


def test_model_slots_and_motion_gap():
    scan = datetime(2026, 11, 19, 12, tzinfo=UTC)
    assert [
        is_model_slot(scan + timedelta(minutes=minute)) for minute in range(0, 60, 10)
    ] == [True, False, False, True, False, False]

    grid = Grid("test", np.linspace(100, 102, 48), np.linspace(-1, 1, 48))
    zonal = Zonal(pa.table({"area_id": [7], "cell": [1000], "weight": [1.0]}), 48**2)
    settings = config.settings()["mergir"]
    motion = CloudMotion(grid, settings["flow"])
    tb = np.full((48, 48), 220, dtype=np.float32)
    motion_columns = ["adv1_pct", "adv2_pct", "adv3_pct", "flow_kmh"]

    adjacent = cloud_rows(
        scan, tb, (scan - timedelta(minutes=30), tb), zonal, motion, settings["cold_k"]
    )
    assert adjacent.select(motion_columns).to_pylist() == [
        {"adv1_pct": 100, "adv2_pct": 100, "adv3_pct": 100, "flow_kmh": 0}
    ]
    assert adjacent["source"].to_pylist() == [2]
    assert adjacent["tb_mean_x10"].to_pylist() == [2200]

    gap = cloud_rows(
        scan, tb, (scan - timedelta(minutes=60), tb), zonal, motion, settings["cold_k"]
    )
    assert all(gap[name].null_count == 1 for name in motion_columns)


def test_merge_replaces_only_matching_keys(tmp_path):
    scan = datetime(2026, 11, 19, 12, tzinfo=UTC)
    later = scan + timedelta(minutes=30)
    row = {"slot": scan, "area_id": 1, "source": 2, "tb_min": 220}
    mergir = tmp_path / "part-mergir.parquet"
    write_table(pa.Table.from_pylist([dict(row, source=1)], schema=CLOUD_SCHEMA), mergir)
    original = mergir.read_bytes()

    part = tmp_path / CLOUD_PARTS[2]
    merge_scan(part, pa.Table.from_pylist([row, dict(row, area_id=2)], schema=CLOUD_SCHEMA), scan)
    merge_scan(part, pa.Table.from_pylist([dict(row, slot=later)], schema=CLOUD_SCHEMA), later)
    merge_scan(part, pa.Table.from_pylist([dict(row, tb_min=210)], schema=CLOUD_SCHEMA), scan)

    table = pq.ParquetFile(part).read()
    assert table.schema == CLOUD_SCHEMA
    assert table.select(["slot", "area_id", "tb_min"]).to_pylist() == [
        {"slot": scan, "area_id": 1, "tb_min": 210},
        {"slot": later, "area_id": 1, "tb_min": 220},
        {"slot": scan, "area_id": 2, "tb_min": 220},
    ]
    assert mergir.read_bytes() == original

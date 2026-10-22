import json
from datetime import UTC, datetime, timedelta
from threading import Event

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nowcast import config
from nowcast.cloud.flow import CloudMotion
from nowcast.registry.grids import Grid
from nowcast.sources import mergir
from nowcast.sources.mergir import PART, Granule, read_crop, write_month
from nowcast.zonal import Zonal


def test_write_month_is_byte_identical_and_motion_requires_previous_slot(tmp_path):
    grid = Grid("synthetic", np.arange(64) * 0.036, np.arange(64) * 0.036)
    motion = CloudMotion(grid, config.settings()["mergir"]["flow"])
    cold = np.full((64, 64), 220, dtype=np.int16)
    warm = cold + 60
    for frame in (cold, warm):
        frame.ravel()[0] = 230
    start = datetime(2025, 1, 1, tzinfo=UTC)
    granules = [
        Granule(start + timedelta(hours=hour), f"GPM_MERGIR.1:merg_20250101{hour:02}_4km-pixel.nc4")
        for hour in (0, 2)
    ]
    bronze, silver = tmp_path / "bronze", tmp_path / "silver"
    bronze.mkdir()
    for granule in granules:
        with h5py.File(bronze / granule.title, "w") as file:
            file["Tb"] = np.stack([cold, warm])

    zonal = Zonal(pa.table({
        "area_id": [7, 4], "cell": [1, 0], "weight": [1, 1],
    }), cold.size)
    stats, last = write_month(granules, bronze, silver, grid, zonal, motion)
    first = {path.name: path.read_bytes() for path in silver.iterdir()}
    assert stats["frames"] == 4
    assert last[0] == start + timedelta(hours=2, minutes=30)
    np.testing.assert_array_equal(last[1], warm)
    table = pq.read_table(silver / PART)
    assert table["area_id"].to_pylist() == [4] * 4 + [7] * 4
    assert table["source"].to_pylist() == [1] * 8
    assert table["tb_min"].to_pylist() == [230] * 4 + [220, 280] * 2
    assert table["cold_pct"].to_pylist() == [100] * 4 + [100, 0] * 2
    for column in ("adv1_pct", "adv2_pct", "adv3_pct"):
        assert [value is None for value in table[column].to_pylist()] == [True, False] * 4
    assert [value is None for value in table["flow_kmh"].to_pylist()] == [
        True, False, True, False, True, True, True, True,
    ]

    write_month(granules, bronze, silver, grid, zonal, motion)
    assert {path.name: path.read_bytes() for path in silver.iterdir()} == first

    write_month(
        granules, bronze, silver, grid, zonal, motion, (start - timedelta(minutes=30), cold),
    )
    seeded = pq.read_table(silver / PART)
    assert seeded["adv1_pct"][0].as_py() == 100
    assert seeded["adv1_pct"][4].as_py() == 100
    assert seeded["adv1_pct"][2].as_py() is None


def test_rejects_disagreeing_time_coordinates(tmp_path):
    grid = Grid("synthetic", np.arange(2), np.arange(2))
    crop = tmp_path / "crop.nc4"
    slot = datetime(2025, 1, 1, tzinfo=UTC)
    with h5py.File(crop, "w") as file:
        file["Tb"] = np.full((2, 2, 2), 220, dtype=np.int16)
        file["time"] = [0, 30]
        file["time"].attrs["units"] = "minutes since 2025-01-01T00:00:00Z"

    read_crop(crop, grid, verify_slot=slot)
    with pytest.raises(ValueError, match="disagrees with HH:00/HH:30"):
        read_crop(crop, grid, verify_slot=slot + timedelta(hours=1))


@pytest.mark.parametrize("download_fails", [False, True])
def test_backfill_prefetches_next_pending_month(tmp_path, monkeypatch, download_fails):
    jan, feb, mar = [datetime(2025, month, 1, tzinfo=UTC) for month in (1, 2, 3)]
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("NASA_PARALLEL", "2")
    monkeypatch.setattr(
        mergir, "mergir_grid", lambda: Grid("synthetic", np.arange(2), np.arange(2))
    )
    ref = tmp_path / "ref"
    ref.mkdir()
    pq.write_table(
        pa.table({"area_id": [1], "cell": [0], "weight": [1]}),
        ref / "mergir_membership.parquet",
    )
    state_path = tmp_path / "mergir_backfill.json"
    state_path.write_text(json.dumps({"2025-02": {"done": True}}))
    previous_months, downloads, writes = [], [], []
    march_started = Event()
    last_slot = feb - mergir.SLOT_LENGTH

    def download_month(granules, bronze, grid, parallel):
        assert parallel == 2
        month = granules[0].slot
        downloads.append(month)
        if month == mar:
            march_started.set()
            if download_fails:
                raise mergir.EarthdataForbidden("synthetic failure")
        bronze.mkdir(parents=True)
        (bronze / granules[0].title).touch()

    def previous_frame(month, carry, bronze, grid):
        previous_months.append(month)
        if month == mar:
            with np.load(carry) as last:
                assert last["slot"].item() == last_slot.isoformat()

    def write_month(granules, bronze, silver, grid, zonal, motion, previous):
        month = granules[0].slot
        if month == jan:
            assert march_started.wait(timeout=5)
        assert (bronze / granules[0].title).exists()
        writes.append(month)
        silver.mkdir(parents=True)
        pq.write_table(pa.table({"bytes": [1]}), silver / "_manifest-mergir.parquet")
        stats = {"frames": 2, "cold_speed_kmh_median": None, "silver_bytes": 1}
        return stats, (last_slot, np.zeros((2, 2))) if month == jan else None

    monkeypatch.setattr(
        mergir.cmr, "granules",
        lambda collection, start, end: [{"title": f"GPM_MERGIR.1:merg_{start:%Y%m%d%H}_4km-pixel.nc4"}],
    )
    monkeypatch.setattr(mergir, "download_month", download_month)
    monkeypatch.setattr(mergir, "_previous_frame", previous_frame)
    monkeypatch.setattr(mergir, "write_month", write_month)

    if download_fails:
        with pytest.raises(mergir.EarthdataForbidden, match="synthetic failure"):
            mergir._backfill(jan, mar, False, state_path)
    else:
        assert list(mergir._backfill(jan, mar, False, state_path)) == ["2025-01", "2025-03"]

    assert downloads == [jan, mar]
    assert writes == previous_months == ([jan] if download_fails else [jan, mar])
    state = json.loads(state_path.read_text())
    assert state["2025-01"]["done"]
    assert state["2025-02"] == {"done": True}
    assert ("2025-03" in state) == (not download_fails)
    assert not list((tmp_path / "bronze").rglob("*.nc4"))

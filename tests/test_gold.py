from datetime import UTC, datetime, timedelta

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from nowcast import config
from nowcast.cloud import CLOUD_PARTS, CLOUD_SCHEMA
from nowcast.gold import frames, tiles
from nowcast.gold.gauge_check import gauge_check_report
from nowcast.model import score
from nowcast.registry.grids import Grid, imerg_grid
from nowcast.sources import gauges, himawari, imerg
from nowcast.sources.bmkg import forecast as bmkg
from nowcast.storage import month_path

T = datetime(2025, 1, 2, 10, tzinfo=UTC)
HALF = timedelta(minutes=30)
HOUR = timedelta(hours=1)


def write(directory, rows, schema, filename="part.parquet"):
    part = month_path(directory, T, filename)
    part.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), part)


def test_rain_tile_orientation(tmp_path):
    grid = imerg_grid()
    crop = tmp_path / "crop.nc4"
    values = np.zeros((1, grid.width, grid.height), dtype=np.float32)
    values[0, 0, -1] = 1.2
    values[0, -1, -1] = 28
    values[0, 0, 0] = 1.9
    values[0, -1, 0] = -9999
    with h5py.File(crop, "w") as file:
        file[config.settings()["imerg"]["variable"]] = values

    target = tmp_path / "rain.png"
    tiles.write_rain(target, imerg.read_crop(crop, grid), grid)
    with Image.open(target) as image:
        assert (image.size, image.mode) == ((480, 180), "L")
        pixels = np.asarray(image)
        assert pixels[[0, 0, -1, -1], [0, -1, 0, -1]].tolist() == [12, 255, 19, 0]


def test_cloud_tile_values():
    tb = np.array([300, 288.15, 240.65, 193.15, 175, np.nan, -9999, np.inf])
    assert tiles.cloud_pixels(tb).tolist() == [0, 0, 141, 255, 255, 0, 0, 0]


def test_live_cloud_tiles_include_non_model_scans(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    pq.write_table(
        pa.table({"area_id": [1], "cell": [0], "weight": [1.0]}),
        ref / "mergir_membership.parquet",
    )
    segment = tmp_path / "segment"
    segment.touch()
    off_slot = T + timedelta(minutes=10)
    calls = []

    def decode(files, index_path, grid, source):
        calls.append(files)
        return np.full((grid.height, grid.width), 220.0)

    monkeypatch.setattr(
        himawari,
        "_ready_scans",
        lambda *_: [(scan, scan, {"segment": 0}) for scan in (T, off_slot)],
    )
    monkeypatch.setattr(himawari, "_download", lambda *_: segment)
    monkeypatch.setattr(himawari, "decode", decode)
    monkeypatch.setattr(himawari, "mergir_grid", lambda: Grid("test", [100, 101], [0, 1]))
    himawari._tick(state, off_slot)

    assert calls == [[segment], [segment]]
    cloud = tmp_path / "silver" / "cloud_kabkota"
    assert pq.ParquetFile(month_path(cloud, T, CLOUD_PARTS[2])).read()["slot"].to_pylist() == [
        T
    ]
    assert sorted(path.name for path in (tmp_path / "gold" / "tiles" / "cloud").iterdir()) == [
        "20250102T1000Z.webp",
        "20250102T1010Z.webp",
    ]


def test_frames_rebuild_whole_dates_and_compare_bmkg_window(tmp_path, monkeypatch):
    monkeypatch.setitem(config.settings()["bmkg"], "step_alignment", "after")
    silver = tmp_path / "silver"
    rain = silver / "rain_kabkota"
    previous = T - timedelta(days=1)
    write(
        rain,
        [
            {"slot": previous, "area_id": 1, "rain_mean_x10": 7},
            {"slot": T, "area_id": 1, "rain_mean_x10": 8},
        ],
        imerg.RAIN_SCHEMA,
    )
    for source, part, value in ((1, "part-mergir.parquet", 10), (2, "part-himawari.parquet", 20)):
        write(
            silver / "cloud_kabkota",
            [
                {"slot": T, "area_id": 1, "source": source, "cold_pct": value},
                {
                    "slot": T,
                    "area_id": 2,
                    "source": source,
                    "cold_pct": 30 if source == 1 else None,
                },
            ],
            CLOUD_SCHEMA,
            part,
        )

    start = T - 2 * HOUR
    write(
        silver / "bmkg_forecast",
        [
            {
                "area_id": area,
                "analysis_time": start,
                "first_seen": start,
                "valid_time": valid,
                "weather": weather,
            }
            for area in (1, 2)
            for valid, weather in ((start, 0), (T + HOUR, 60))
        ]
        + [
            {
                "area_id": 1,
                "analysis_time": T,
                "first_seen": T + timedelta(minutes=20),
                "valid_time": T,
                "weather": 60,
            },
        ],
        bmkg.SCHEMA,
    )
    write(
        tmp_path / "gold" / "predictions",
        [
            {"t": T, "area_id": area, "lead_h": 3, "model_version": version, "prob_pct": pct}
            for area, version, pct in (
                (1, "20250101T0000Z-test", 80),
                (1, "20250102T0000Z-test", 45),
                (2, "20250102T0000Z-test", 60),
                (3, "20250102T0000Z-test", 60),
            )
        ],
        score.SCHEMA,
    )

    now = T + 4 * HOUR
    result = frames.rebuild(tmp_path, now, 2)
    directory = tmp_path / "gold" / "map_frames"
    part = directory / "date=2025-01-02" / "part.parquet"
    earlier = directory / "date=2025-01-01" / "part.parquet"
    before = part.read_bytes()
    data = pq.ParquetFile(part).read()
    assert data.schema == frames.SCHEMA
    rows = data.to_pylist()
    keys = [(row["layer"], row["slot"], row["area_id"]) for row in rows]
    assert keys == sorted(set(keys))
    values = {(row["layer"], row["area_id"]): row["value"] for row in rows if row["slot"] == T}
    assert values == {
        ("obs", 1): 8,
        ("cold", 1): 20,
        ("cold", 2): 30,
        ("bmkg", 1): 0,
        ("bmkg", 2): 0,
        ("bmkg_code", 1): 0,
        ("bmkg_code", 2): 0,
        ("bmkg_step", 1): 120,
        ("bmkg_step", 2): 120,
        ("bmkg_age", 1): 2,
        ("bmkg_age", 2): 2,
        ("prob3", 1): 45,
        ("prob3", 2): 60,
        ("prob3", 3): 60,
        ("disagree", 1): 1,
        ("disagree", 2): 0,
    }
    next_slot = {
        row["layer"]: row["value"] for row in rows
        if row["slot"] == T + HALF and row["area_id"] == 1
    }
    assert next_slot == {"bmkg": 100, "bmkg_code": 60, "bmkg_step": 30, "bmkg_age": 0}
    assert {row["layer"]: row["value"] for row in rows
            if row["slot"] == now and row["area_id"] == 1} == {"bmkg": 0}
    assert frames.rebuild(tmp_path, now, 2) == result
    assert part.read_bytes() == before
    older_bytes = earlier.read_bytes()
    write(rain, [], imerg.RAIN_SCHEMA)
    assert frames.rebuild(tmp_path, now, 1)[0] == 1
    assert earlier.read_bytes() == older_bytes
    assert "obs" not in pq.ParquetFile(part).read()["layer"].to_pylist()


def test_gauge_check_contingency(tmp_path):
    silver = tmp_path / "silver"
    write(
        silver / "gauge_obs",
        [
            {"area_id": 1, "station_id": station, "obs_time": T + offset, "is_rain": wet}
            for station, offset, wet in (
                ("one", timedelta(), False),
                ("two", HALF, True),
                ("one", HOUR, False),
                ("one", 2 * HOUR, True),
                ("one", 3 * HOUR, False),
                ("one", 4 * HOUR, True),
            )
        ],
        gauges.SCHEMA,
        "part-ghcnh.parquet",
    )
    write(
        silver / "rain_kabkota",
        [
            {
                "area_id": 1,
                "slot": T + hour * HOUR + offset,
                "rain_mean_x10": mean if offset else 0,
                "wet_pct": wet_pct if offset else 0,
            }
            for hour, (mean, wet_pct) in enumerate(((5, 50), (6, 20), (4, 55), (1, 10)))
            for offset in (timedelta(), HALF)
        ],
        imerg.RAIN_SCHEMA,
    )
    write(
        tmp_path / "gold" / "predictions",
        [
            {"area_id": 1, "t": T + offset, "lead_h": 1, "model_version": "v", "prob_pct": pct}
            for offset, pct in ((timedelta(), 90), (HALF, 40), (HOUR, 50))
        ],
        score.SCHEMA,
    )

    report = gauge_check_report(tmp_path, T, T + 5 * HOUR)
    assert report["imerg_mean"] == {
        "hours_compared": 4,
        "stations": 2,
        "areas": 1,
        "table": {"hits": 1, "false_alarms": 1, "misses": 1, "correct_negatives": 1},
        "pod": 0.5,
        "far": 0.5,
        "csi": 1 / 3,
        "hit_rate": 0.5,
    }
    assert report["imerg_wet_pct"]["table"] == {
        "hits": 2,
        "false_alarms": 0,
        "misses": 0,
        "correct_negatives": 2,
    }
    assert report["prob1"]["table"] == {
        "hits": 1,
        "false_alarms": 0,
        "misses": 0,
        "correct_negatives": 1,
    }

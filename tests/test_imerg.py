import json
from datetime import UTC, datetime, timedelta

import h5py
import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from nowcast.cli import app
from nowcast.registry.grids import imerg_grid
from nowcast.sources import cmr, imerg
from nowcast.zonal import Zonal

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2025, 2, 1, tzinfo=UTC)
V07B = "GPM_3IMERGHHE.07:3B-HHR-E.MS.MRG.3IMERG.20250101-S000000-E002959.0000.V07B.HDF5"
V07C = V07B.replace("V07B", "V07C")


@pytest.mark.parametrize("title,version", [(V07B, "V07B"), (V07C, "V07C")])
def test_parse_title(title, version):
    assert imerg.parse_title(title) == imerg.Granule(START, title, version)


def test_select_highest_version():
    for titles in ([V07B, V07C], [V07C, V07B]):
        assert imerg.select_granules([{"title": title} for title in titles], START, END) == [
            imerg.Granule(START, V07C, "V07C")
        ]


def test_cmr_search_after(monkeypatch):
    entry = {"title": V07C, "time_start": START.isoformat(), "time_end": START.isoformat()}
    pages = iter(
        [
            (None, [entry], "first"),
            ("first", [entry], "second"),
            ("second", [], None),
        ]
    )

    def respond(request):
        cursor, entries, next_cursor = next(pages)
        assert request.url.params["page_size"] == "2000"
        assert request.url.params["collection_concept_id"] == "collection"
        assert request.url.params["temporal"] == "2025-01-01T00:00:00Z,2025-02-01T00:00:00Z"
        assert request.headers.get("CMR-Search-After") == cursor
        headers = {"CMR-Search-After": next_cursor} if next_cursor else {}
        return httpx.Response(200, headers=headers, json={"feed": {"entry": entries}})

    monkeypatch.setattr(
        cmr.http, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond))
    )
    assert cmr.granules("collection", START, END) == [entry, entry]
    assert next(pages, None) is None


@pytest.fixture
def sample(tmp_path):
    grid = imerg_grid()
    membership = pa.table(
        {
            "area_id": [9, 4, 4, 4],
            "cell": [481, 480, 2, 481],
            "weight": [1, 1, 0.5, 1],
        }
    )
    path = tmp_path / f"{V07C}.nc4"
    values = np.full((1, 480, 180), -9999.9, dtype=np.float32)
    values[0, 0, 1] = 3
    values[0, 2, 0] = 0
    with h5py.File(path, "w") as file:
        file["Grid/precipitation"] = values
    return grid, membership, path


def test_write_month(sample, tmp_path):
    grid, membership, crop = sample
    archive = tmp_path / "silver"
    zonal = Zonal(membership, grid.width * grid.height)
    granules = [imerg.parse_title(V07C)]
    stats = imerg.write_month(granules, crop.parent, archive, grid, zonal)
    part = archive / "part.parquet"
    manifest = archive / "_manifest.parquet"

    rain = pq.read_table(part)
    assert rain.schema == imerg.RAIN_SCHEMA
    assert rain.to_pydict() == {
        "slot": [START, START],
        "area_id": [4, 9],
        "rain_mean_x10": [20, None],
        "rain_max_x10": [30, None],
        "wet_pct": [67, None],
        "product_version": ["V07C", "V07C"],
    }
    assert pq.read_table(manifest).to_pydict() == {
        "slot": [START],
        "title": [V07C],
        "product_version": ["V07C"],
        "fetched_at": [datetime.fromtimestamp(crop.stat().st_mtime, UTC)],
        "bytes": [crop.stat().st_size],
    }
    assert stats == {
        "box_wet_pct": 50,
        "invalid_cell_pct": 100 * (480 * 180 - 2) / (480 * 180),
        "silver_bytes": part.stat().st_size,
        "rows": 2,
        "null_area_slots": 1,
    }
    outputs = part.read_bytes(), manifest.read_bytes()
    imerg.write_month(granules, crop.parent, archive, grid, zonal)
    assert (part.read_bytes(), manifest.read_bytes()) == outputs


def test_download_and_skip(sample, tmp_path):
    grid, _, crop = sample
    path = tmp_path / "download.nc4"
    path.write_bytes(b"incomplete")
    content = crop.read_bytes()
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.raw_path.endswith(
            b".dap.nc4?dap4.ce=/Grid/precipitation%5B0:1:0%5D%5B2740:1:3219%5D%5B785:1:964%5D"
        )
        assert V07C in request.url.path
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert imerg.download(client, V07C, path, grid) == len(content)
        assert imerg.download(client, V07C, path, grid) == 0
    assert len(requests) == 1
    assert path.read_bytes() == content
    assert not path.with_suffix(".nc4.tmp").exists()


def test_backfill_403_then_resume_and_skip(monkeypatch, sample, tmp_path):
    _, membership, crop = sample
    content = crop.read_bytes()
    root = tmp_path / "data"
    ref = root / "ref"
    ref.mkdir(parents=True)
    pq.write_table(membership, ref / "imerg_membership.parquet")
    monkeypatch.setenv("DATA_ROOT", str(root))
    monkeypatch.setenv("NASA_PARALLEL", "1")
    second_title = V07C.replace("S000000-E002959.0000", "S003000-E005959.0030")
    entries = [{"title": V07C}, {"title": second_title}]

    def list_granules(collection, start, end):
        assert start == START and end == END - timedelta(seconds=1)
        return entries

    status = 403
    requests = []

    def respond(request):
        requests.append(request)
        if second_title in request.url.path:
            return httpx.Response(status, content=content)
        return httpx.Response(200, content=content)

    def client(auth):
        assert auth
        return httpx.Client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(cmr, "granules", list_granules)
    monkeypatch.setattr(imerg.http, "client", client)
    runner = CliRunner()
    command = ["imerg", "backfill", "--start", "2025-01", "--end", "2025-01"]
    result = runner.invoke(app, command)
    assert result.exit_code == 1
    assert "Earthdata 403: EULA not accepted or token invalid" in result.output
    bronze = root / "bronze/imerg/year=2025/month=01"
    assert (bronze / f"{V07C}.nc4").read_bytes() == content
    state_path = root / "state/imerg_backfill.json"
    assert not state_path.exists()

    status = 200
    result = runner.invoke(app, command)
    assert result.exit_code == 0, result.output
    assert len(requests) == 3
    assert not list(bronze.iterdir())
    state = json.loads(state_path.read_bytes())["2025-01"]
    assert state["done"] and state["slots"] == 2 and state["expected"] == 1488
    assert len(state["missing_slots"]) == 1486
    assert state["versions"] == {"V07C": 2}
    assert runner.invoke(app, command).exit_code == 0
    assert len(requests) == 3


def test_merge_month(sample, tmp_path):
    grid, membership, crop = sample
    archive = tmp_path / "silver"
    zonal = Zonal(membership, grid.width * grid.height)
    slots = [START + i * imerg.SLOT_LENGTH for i in range(3)]
    a, b, newer_b, c = [
        imerg.Granule(slot, f"{slot:%H%M}-{version}", version)
        for slot, version in zip([slots[0], slots[1], slots[1], slots[2]],
                                 ["V07B", "V07B", "V07C", "V07C"], strict=True)
    ]
    for granule in (a, b, newer_b, c):
        (crop.parent / f"{granule.title}.nc4").hardlink_to(crop)
    imerg.write_month([a, b], crop.parent, archive, grid, zonal)
    for _ in range(2):
        imerg.write_month([newer_b, c], crop.parent, archive, grid, zonal, merge=True)
    rain = pq.read_table(archive / "part.parquet")
    assert rain.select(["slot", "area_id", "product_version"]).to_pydict() == {
        "slot": slots * 2,
        "area_id": [4] * 3 + [9] * 3,
        "product_version": ["V07B", "V07C", "V07C"] * 2,
    }
    manifest = pq.read_table(archive / "_manifest.parquet")
    assert manifest["slot"].to_pylist() == slots
    assert manifest["title"].to_pylist() == [a.title, newer_b.title, c.title]
    assert manifest["product_version"].to_pylist() == ["V07B", "V07C", "V07C"]


@pytest.fixture
def live(monkeypatch, sample, tmp_path):
    _, membership, crop = sample
    root = tmp_path / "data"
    ref = root / "ref"
    ref.mkdir(parents=True)
    pq.write_table(membership, ref / "imerg_membership.parquet")
    monkeypatch.setenv("DATA_ROOT", str(root))
    state = root / "state"
    state.mkdir()
    cursor = state / "imerg_live.json"
    now = END + timedelta(hours=4)
    previous = END - timedelta(hours=1)
    cursor.write_text(json.dumps({"last_slot": previous.isoformat(), "processed": 3}))
    titles = [
        V07C.replace("20250101-S000000-E002959.0000", "20250131-S230000-E232959.1380"),
        V07C.replace("20250101-S000000-E002959.0000", "20250131-S233000-E235959.1410"),
        V07C.replace("20250101", "20250201"),
    ]
    searches = []

    def list_granules(collection, start, end):
        searches.append((start, end))
        return [{"title": title} for title in titles]

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return now

    content = crop.read_bytes()
    monkeypatch.setattr(imerg, "datetime", Clock)
    monkeypatch.setattr(cmr, "granules", list_granules)
    monkeypatch.setattr(imerg.http, "client", lambda auth: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    ))
    return root, cursor, previous, now, titles, searches


def test_tick_month_boundary(live):
    root, cursor, previous, now, titles, searches = live
    runner = CliRunner()
    result = runner.invoke(app, ["imerg", "tick"])
    assert result.exit_code == 0, result.output
    # The slot at the cursor is missing from silver, so the lookback fetches it again.
    assert searches == [(now - imerg.LIVE_LOOKBACK, now)]
    for month, slots, month_titles in zip(
        (1, 2), ([previous, END - imerg.SLOT_LENGTH], [END]), (titles[:2], titles[2:]), strict=True
    ):
        partition = f"year=2025/month={month:02d}"
        silver = root / "silver/rain_kabkota" / partition
        assert pq.ParquetFile(silver / "part.parquet").read()["slot"].to_pylist() == slots * 2
        manifest = pq.ParquetFile(silver / "_manifest.parquet").read()
        assert manifest["title"].to_pylist() == month_titles
        for title in month_titles:
            assert (root / "bronze/imerg" / partition / f"{title}.nc4").exists()
    assert json.loads(cursor.read_text()) == {"last_slot": END.isoformat(), "processed": 6}
    latency = root / "state/imerg_latency.jsonl"
    lines = [json.loads(line) for line in latency.read_text().splitlines()]
    assert lines[-1] == {
        "slot": END.isoformat(), "title": titles[-1], "version": "V07C",
        "seen_at": now.isoformat(), "latency_min": 210,
    }
    health = root / "state/health/imerg.json"
    assert json.loads(health.read_text()) == {
        "last_tick": now.isoformat(), "last_slot": END.isoformat(), "new_slots": 3,
        "latency_min_last": 210, "latency_min_p90_24h": 264,
    }
    assert runner.invoke(app, ["imerg", "tick"]).exit_code == 0
    assert len(latency.read_text().splitlines()) == 3
    assert json.loads(health.read_text())["new_slots"] == 0


def test_tick_cursor_waits_for_writer(live, monkeypatch):
    root, cursor, previous, _, _, _ = live
    write_table = imerg.write_table

    def fail_manifest(table, path):
        if path.name == "_manifest.parquet" and path.parent.name == "month=02":
            raise OSError("manifest write failed")
        write_table(table, path)

    monkeypatch.setattr(imerg, "write_table", fail_manifest)
    runner = CliRunner()
    result = runner.invoke(app, ["imerg", "tick"])
    assert result.exit_code == 1
    assert "manifest write failed" in result.output
    assert json.loads(cursor.read_text()) == {"last_slot": previous.isoformat(), "processed": 3}
    assert not (root / "state/imerg_latency.jsonl").exists()
    monkeypatch.setattr(imerg, "write_table", write_table)
    result = runner.invoke(app, ["imerg", "tick"])
    assert result.exit_code == 0, result.output
    assert json.loads(cursor.read_text()) == {"last_slot": END.isoformat(), "processed": 5}


def test_gaps(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return START + timedelta(days=1)

    monkeypatch.setattr(imerg, "datetime", Clock)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    part = tmp_path / "silver/rain_kabkota/year=2025/month=01/part.parquet"
    part.parent.mkdir(parents=True)
    slots = [START + i * imerg.SLOT_LENGTH for i in range(8)]
    present = [slots[i] for i in (0, 2, 6, 7)]
    count = len(present) * 514
    pq.write_table(pa.table({
        "slot": np.repeat(present, 514).tolist(),
        "rain_mean_x10": [None] + [0] * (count - 1),
        "product_version": ["V07B"] * 514 + ["V07C"] * (count - 514),
    }), part)
    result = CliRunner().invoke(app, ["imerg", "gaps", "--month", "2025-01"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert json.loads((tmp_path / "state/imerg_gaps/2025-01.json").read_text()) == report
    assert report == {
        "month": "2025-01", "expected_slots": 8, "present_slots": 4,
        "missing_slots": [slots[i].isoformat() for i in (1, 3, 4, 5)],
        "missing_runs": [
            {"start": slots[1].isoformat(), "end": slots[1].isoformat(), "slots": 1},
            {"start": slots[3].isoformat(), "end": slots[5].isoformat(), "slots": 3},
        ],
        "rows_per_slot_min": 514, "rows_per_slot_max": 514,
        "versions": {"V07B": 1, "V07C": 3}, "null_area_slots": 1,
    }

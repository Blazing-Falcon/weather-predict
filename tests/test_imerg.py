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

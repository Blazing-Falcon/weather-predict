import json
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from nowcast import config, http
from nowcast.cli import app
from nowcast.registry import bmkg_points

DISTRICTS = ("11.01.01", "11.01.02", "11.01.03")
VILLAGES = ("11.01.01.2003", "11.01.01.2001", "11.01.01.2002", "11.01.01.2004")


@pytest.fixture
def clock(monkeypatch):
    now = 0.0

    def monotonic():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(bmkg_points, "monotonic", monotonic)
    monkeypatch.setattr(bmkg_points, "sleep", sleep)
    monkeypatch.setattr(http.time, "sleep", sleep)
    return monotonic


@pytest.fixture
def run(tmp_path, monkeypatch, clock):
    geo = tmp_path / "geo"
    geo.mkdir()
    (geo / "kecamatan.csv").write_text(
        "kecamatan_code,kecamatan_name,kabkota_code\n"
        + "".join(f"{code},Kecamatan,11.01\n" for code in DISTRICTS),
        encoding="utf-8",
    )
    (geo / "kabkota_codes.csv").write_text(
        "kemendagri_code,area_id,is_admin\n11.01,23,1\n11.01,99,0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        config, "settings", lambda: {"bmkg": {"url": "https://bmkg.test/forecast", "max_rpm": 55}}
    )
    monkeypatch.setenv("BMKG_MAX_RPM", "30")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    path = tmp_path / "state" / "registry" / "bmkg_candidates.jsonl"

    def invoke(handler, limit):
        monkeypatch.setattr(
            http, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
        )
        result = CliRunner().invoke(app, ["registry", "bmkg-candidates", "--limit", str(limit)])
        assert result.exit_code == 0, (result.output, result.exception)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    return invoke, path


def villages(*codes):
    return httpx.Response(200, json={"data": [{"code": code, "name": "Wilayah"} for code in codes]})


def forecast():
    return httpx.Response(
        200,
        json={
            "lokasi": {"desa": "BMKG", "lat": 2.95, "lon": 97.49},
            "data": [{"cuaca": [[{"analysis_date": "2026-11-19T12:00:00"}]]}],
        },
    )


def test_second_village_accepted(run):
    invoke, _ = run
    handler = Mock(
        side_effect=[
            villages(*VILLAGES),
            httpx.Response(200, json={"error": "Data cuaca tidak ditemukan"}),
            forecast(),
        ]
    )
    [record] = invoke(handler, 1)

    requests = [call.args[0] for call in handler.call_args_list]
    assert str(requests[0].url) == f"https://wilayah.id/api/villages/{DISTRICTS[0]}.json"
    assert [request.url.params["adm4"] for request in requests[1:]] == list(VILLAGES[:2])
    assert record.pop("fetched_at").endswith("Z")
    assert record == {
        "kecamatan_code": DISTRICTS[0],
        "kabkota_code": "11.01",
        "area_id": 23,
        "adm4": VILLAGES[1],
        "desa": "BMKG",
        "lat": 2.95,
        "lon": 97.49,
        "analysis_date": "2026-11-19T12:00:00Z",
        "tries": 2,
    }


def test_all_three_villages_fail(run):
    invoke, _ = run
    handler = Mock(
        side_effect=[
            villages(*VILLAGES),
            httpx.Response(400),
            httpx.Response(401),
            httpx.Response(404),
        ]
    )
    [record] = invoke(handler, 1)

    assert handler.call_count == 4
    assert record["adm4"] is record["lat"] is record["lon"] is None
    assert record["tries"] == 3
    assert record["error"] == 404


def test_resume_uses_latest_record_and_recovers_partial_line(run):
    invoke, path = run
    path.parent.mkdir(parents=True)
    history = [
        {"kecamatan_code": DISTRICTS[0], "adm4": None},
        {"kecamatan_code": DISTRICTS[0], "adm4": VILLAGES[0]},
        {"kecamatan_code": DISTRICTS[1], "adm4": "11.01.02.2001"},
        {"kecamatan_code": DISTRICTS[1], "adm4": None},
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in history) + '{"kecamatan_code":',
        encoding="utf-8",
    )
    handler = Mock(side_effect=[villages("11.01.02.2002"), forecast()])
    records = invoke(handler, 1)

    assert handler.call_count == 2
    assert handler.call_args_list[0].args[0].url.path == f"/api/villages/{DISTRICTS[1]}.json"
    assert records[:-1] == history
    assert records[-1]["kecamatan_code"] == DISTRICTS[1]
    assert records[-1]["adm4"] == "11.01.02.2002"


def test_rate_limits_include_http_retries(run, clock):
    invoke, _ = run
    responses = iter(
        [
            villages(),
            villages(),
            villages("11.01.03.2001", "11.01.03.2002"),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(503),
            httpx.Response(404),
            forecast(),
        ]
    )
    times = {"wilayah.id": [], "bmkg.test": []}

    def handler(request):
        times[request.url.host].append(clock())
        return next(responses)

    records = invoke(handler, 3)

    assert times["wilayah.id"] == pytest.approx([0, 0.2, 0.4])
    assert times["bmkg.test"] == pytest.approx([0.4, 2.4, 6.4, 8.4])
    assert records[-1]["tries"] == 2

import fcntl
import gzip
import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from nowcast import http
from nowcast.cli import app
from nowcast.sources import bmkg

RUN = "2026-11-19T12:00:00Z"
OLD_RUN = "2026-11-19T00:00:00Z"
PICKS = ["35.73.01.1001", "73.71.01.1001", "91.71.01.1001", "11.01.01.1001"]
RAW = (
    '\n  {"data": [{"cuaca": [[{}, {"analysis_date": "2026-11-19 12:00:00"},'
    ' {"analysis_date": "2026-11-19 00:00:00"}]]}], "note": "hujan é"}\r\n'
)


def read_gzip(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BMKG_MAX_RPM", "55")
    (tmp_path / "ref").mkdir()
    (tmp_path / "state").mkdir()
    start = datetime(2026, 11, 19, 12, 15, tzinfo=UTC)
    calls = []
    responses = {}
    clock = SimpleNamespace(elapsed=0.0)

    def advance(seconds):
        clock.elapsed += seconds

    def now():
        return (start + timedelta(seconds=clock.elapsed)).isoformat().replace("+00:00", "Z")

    def handle(request):
        adm4 = request.url.params["adm4"]
        calls.append((adm4, clock.elapsed))
        status, body = responses[adm4].pop(0) if responses.get(adm4) else (200, RAW)
        return httpx.Response(status, content=body.encode("utf-8"), headers={"Retry-After": "0"})

    def write_picks(extra=()):
        rows = [
            {"area_id": index, "kemendagri_code": adm4[:5], "bmkg_adm4": adm4, "is_admin": True}
            for index, adm4 in enumerate(PICKS, start=1)
        ]
        pq.write_table(
            pa.Table.from_pylist(rows + list(extra)), tmp_path / "ref" / "dim_area.parquet"
        )

    write_picks()
    monkeypatch.setattr(bmkg, "utc_now", now)
    monkeypatch.setattr(http.time, "monotonic", lambda: clock.elapsed)
    monkeypatch.setattr(http.time, "sleep", advance)
    monkeypatch.setattr(http, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    return SimpleNamespace(
        root=tmp_path,
        calls=calls,
        responses=responses,
        advance=advance,
        write_picks=write_picks,
        state=lambda: json.loads((tmp_path / "state" / "bmkg.json").read_text()),
        health=lambda: json.loads((tmp_path / "state" / "health" / "bmkg.json").read_text()),
        bronze=tmp_path / "bronze" / "bmkg" / "run=2026-11-19T12",
    )


def test_new_run_sweeps_all_picks_and_preserves_raw_body(setup):
    setup.write_picks(
        [
            {"area_id": 8, "kemendagri_code": "22.10", "bmkg_adm4": None, "is_admin": True},
            {"area_id": 9, "kemendagri_code": "22.11", "bmkg_adm4": "other", "is_admin": False},
        ]
    )
    assert CliRunner().invoke(app, ["bmkg", "tick"]).exit_code == 0
    assert [adm4 for adm4, _ in setup.calls] == PICKS[:3] + PICKS
    progress = setup.state()["runs"][RUN]
    first_seen = "2026-11-19T12:15:00Z"
    assert progress == {
        "first_seen": first_seen,
        "done": [1, 2, 3, 4],
        "missing": [],
        "complete": True,
        "last_sweep": "2026-11-19T12:15:02.181818Z",
    }
    sentinel = read_gzip(setup.bronze / "sentinel=20261119T121500Z.jsonl.gz")
    assert sentinel == [
        {
            "area_id": 1,
            "adm4": PICKS[0],
            "fetched_at": first_seen,
            "http_status": 200,
            "body": RAW,
        }
    ]
    (chunk,) = setup.bronze.glob("sweep=*-1.jsonl.gz")
    lines = read_gzip(chunk)
    assert [line["area_id"] for line in lines] == [1, 2, 3, 4]
    assert all(line["body"].encode("utf-8") == RAW.encode("utf-8") for line in lines)
    assert setup.health() == {
        "last_tick": lines[-1]["fetched_at"],
        "last_run": RUN,
        "points_ok": 4,
        "points_failed": 0,
        "runs_incomplete": 0,
    }


def test_retries_only_the_point_with_an_old_run(setup):
    setup.responses[PICKS[-1]] = [(200, RAW.replace("12:00:00", "00:00:00"))]
    bmkg.tick()
    assert setup.state()["runs"][RUN]["done"] == [1, 2, 3]
    assert setup.health()["points_failed"] == 1
    assert setup.health()["runs_incomplete"] == 1
    setup.advance(15 * 60)
    setup.calls.clear()

    bmkg.tick()

    assert [adm4 for adm4, _ in setup.calls] == PICKS[:3] + PICKS[-1:]
    assert setup.state()["runs"][RUN]["complete"]
    assert setup.health()["points_ok"] == 1


def test_sweep_credits_response_to_its_own_run(setup):
    old_body = RAW.replace("12:00:00", "00:00:00")
    setup.responses[PICKS[1]] = [(200, old_body)]
    setup.responses[PICKS[-1]] = [(200, old_body)]

    bmkg.tick()

    runs = setup.state()["runs"]
    assert runs[RUN]["done"] == [1, 2, 3]
    assert not runs[RUN]["complete"]
    assert runs[OLD_RUN]["done"] == [4]
    assert setup.health()["points_failed"] == 1
    first_seen = runs[OLD_RUN]["first_seen"]
    old_bronze = setup.bronze.with_name("run=2026-11-19T00")
    (sentinel,) = read_gzip(old_bronze / "sentinel=20261119T121501.090909Z.jsonl.gz")
    assert sentinel["area_id"] == 2
    assert sentinel["fetched_at"] == first_seen
    assert setup.health()["last_run"] == RUN


def test_never_sweeps_older_incomplete_runs(setup):
    setup.responses[PICKS[1]] = [(200, RAW.replace("12:00:00", "00:00:00"))]
    bmkg.tick()

    assert [adm4 for adm4, _ in setup.calls] == PICKS[:3] + PICKS
    runs = setup.state()["runs"]
    assert runs[RUN]["complete"]
    assert not runs[OLD_RUN]["complete"]
    assert runs[OLD_RUN]["last_sweep"] is None
    setup.advance(15 * 60)
    setup.calls.clear()

    bmkg.tick()

    assert [adm4 for adm4, _ in setup.calls] == PICKS[:3]
    assert not setup.state()["runs"][OLD_RUN]["complete"]


def test_expired_run_closes_with_missing_points(setup, monkeypatch):
    logger = SimpleNamespace(warning=lambda message, *args: warnings.append(message % args))
    warnings = []
    monkeypatch.setattr(bmkg, "get_logger", lambda _: logger)
    setup.responses[PICKS[-1]] = [(404, "missing")]
    bmkg.tick()
    setup.advance(6 * 60 * 60)
    setup.calls.clear()

    bmkg.tick()

    assert [adm4 for adm4, _ in setup.calls] == PICKS[:3]
    progress = setup.state()["runs"][RUN]
    assert progress["complete"]
    assert progress["missing"] == [4]
    assert warnings == [f"BMKG run {RUN} expired; missing areas [4]"]


def test_tick_with_lock_held_makes_no_requests(setup):
    with (setup.root / "state" / "bmkg.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(app, ["bmkg", "tick"])

    assert result.exit_code == 0
    assert setup.calls == []
    assert not (setup.root / "state" / "bmkg.json").exists()


def test_rate_limit_covers_sentinels_and_retries(setup):
    setup.responses[PICKS[0]] = [(200, RAW), (429, "retry this")]
    bmkg.tick()

    assert len(setup.calls) == 8
    times = [elapsed for _, elapsed in setup.calls]
    assert all(b - a >= 60 / 55 - 1e-12 for a, b in pairwise(times))
    (chunk,) = setup.bronze.glob("sweep=*-1.jsonl.gz")
    lines = read_gzip(chunk)
    assert [(line["http_status"], line["body"]) for line in lines[:2]] == [
        (429, "retry this"),
        (200, RAW),
    ]
    assert setup.health()["points_ok"] == 4


def test_sweep_checkpoints_closed_chunks(setup, monkeypatch):
    setup.write_picks(
        {
            "area_id": index,
            "kemendagri_code": f"99.{index}",
            "bmkg_adm4": f"99.{index}.01.1001",
            "is_admin": True,
        }
        for index in range(5, 52)
    )
    get = http.get

    def inspect_checkpoint(client, url, **kwargs):
        if kwargs["params"]["adm4"] == "99.51.01.1001":
            (first_chunk,) = setup.bronze.glob("sweep=*-1.jsonl.gz")
            assert len(read_gzip(first_chunk)) == 50
            assert setup.state()["runs"][RUN]["done"] == list(range(1, 51))
        return get(client, url, **kwargs)

    monkeypatch.setattr(http, "get", inspect_checkpoint)
    bmkg.tick()

    (last_chunk,) = setup.bronze.glob("sweep=*-2.jsonl.gz")
    assert [line["area_id"] for line in read_gzip(last_chunk)] == [51]
    assert setup.state()["runs"][RUN]["complete"]


@pytest.mark.parametrize("readable", [True, False])
def test_resume_after_chunk_publish_before_checkpoint(setup, monkeypatch, readable):
    save = bmkg.write_json

    def interrupt(path, data, **options):
        if data["runs"].get(RUN, {}).get("done"):
            raise InterruptedError
        save(path, data, **options)

    with monkeypatch.context() as patch:
        patch.setattr(bmkg, "write_json", interrupt)
        with pytest.raises(InterruptedError):
            bmkg.tick()
    assert setup.state()["runs"][RUN]["done"] == []
    (chunk,) = setup.bronze.glob("sweep=*.jsonl.gz")
    if not readable:
        chunk.write_bytes(b"partial")
    setup.advance(15 * 60)
    setup.calls.clear()

    bmkg.tick()

    assert [adm4 for adm4, _ in setup.calls] == PICKS[:3] + ([] if readable else PICKS)
    assert setup.state()["runs"][RUN]["complete"]

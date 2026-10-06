import gzip
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from nowcast.cli import app
from nowcast.silver import bmkg

FIXTURES = Path(__file__).parent / "fixtures"
MALANG = "bmkg_35.73.01.1001_kota_malang.json"
RUN = datetime(2026, 11, 19, 12, tzinfo=UTC)
SILVER = "silver/bmkg_forecast/year=2026/month=11/part.parquet"
DLQ = "bronze/bmkg/_dlq/date=2026-11-19/run=12.jsonl.gz"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    return tmp_path


def line(body, *, area_id=7, fetched_at="2026-11-19T12:30:00Z", status=200):
    return {
        "area_id": area_id,
        "adm4": "35.73.01.1001",
        "fetched_at": fetched_at,
        "http_status": status,
        "body": body if isinstance(body, str) else json.dumps(body),
    }


def archive(root, *lines, name="sweep=sample.jsonl.gz", hour="2026-11-19T12"):
    path = root / "bronze" / "bmkg" / f"run={hour}" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for record in lines:
            stream.write(json.dumps(record) + "\n")


def parse(*options):
    return CliRunner().invoke(app, ["bmkg", "parse", *options], catch_exceptions=False)


def test_malang_forecast_types_and_rounding(root, caplog, monkeypatch):
    monkeypatch.setattr(bmkg, "get_logger", logging.getLogger)
    body = json.loads((FIXTURES / MALANG).read_text())
    body["data"][0]["cuaca"][0][0].update(
        tp=1.26, ws=4.24, weather=99, datetime="2026-11-19T00:00:00Z"
    )
    archive(root, line(body))

    with caplog.at_level(logging.INFO, logger=bmkg.__name__):
        assert parse().exit_code == 0
    table = pq.ParquetFile(root / SILVER).read()
    assert table.num_rows == 17
    assert [(field.name, str(field.type)) for field in table.schema] == [
        ("analysis_time", "timestamp[us, tz=UTC]"),
        ("first_seen", "timestamp[us, tz=UTC]"),
        ("area_id", "int16"),
        ("adm4", "string"),
        ("valid_time", "timestamp[us, tz=UTC]"),
        ("lead_h", "int16"),
        ("weather", "int16"),
        ("tp_x10", "int16"),
        ("tcc_pct", "uint8"),
        ("t", "int8"),
        ("hu_pct", "uint8"),
        ("ws_x10", "int16"),
        ("wd_deg", "int16"),
    ]
    first = table.to_pylist()[0]
    assert first["analysis_time"] == RUN
    assert first["area_id"] == 7
    assert first["adm4"] == "35.73.01.1001"
    assert first["valid_time"] == datetime(2026, 11, 19, tzinfo=UTC)
    assert (first["lead_h"], first["tp_x10"], first["ws_x10"], first["weather"]) == (
        -12,
        13,
        42,
        99,
    )
    assert "points=1 rows=17 rows_per_point=(17, 17)" in caplog.text
    assert "lead_range=(-12.0, 57.0)" in caplog.text
    assert "unknown_weather={99: 1}" in caplog.text


@pytest.mark.parametrize(("sentinel_status", "areas"), [(200, [7, 8]), (503, [7])])
def test_keeps_earliest_point_and_uses_sentinel_first_seen(root, sentinel_status, areas):
    original = (FIXTURES / MALANG).read_text()
    later = json.loads(original)
    later["data"][0]["cuaca"][0][0]["t"] = 42
    archive(root, line(later), line(original, fetched_at="2026-11-19T12:20:00Z"))
    archive(
        root,
        line(original, area_id=8, fetched_at="2026-11-19T12:15:00Z", status=sentinel_status),
        name="sentinel=sample.jsonl.gz",
    )

    assert parse().exit_code == 0
    rows = pq.ParquetFile(root / SILVER).read().to_pylist()
    assert [row["area_id"] for row in rows] == [area for area in areas for _ in range(17)]
    assert rows[0]["t"] == 20
    assert {row["first_seen"] for row in rows} == {datetime(2026, 11, 19, 12, 15, tzinfo=UTC)}


def test_dead_letter_and_byte_identical_rebuild(root):
    body = json.loads((FIXTURES / MALANG).read_text())
    invalid = body["data"][0]["cuaca"][0][0]
    invalid["hu"] = 150
    archive(root, line(body))

    assert parse().exit_code == 0
    assert pq.ParquetFile(root / SILVER).metadata.num_rows == 16
    with gzip.open(root / DLQ, "rt", encoding="utf-8") as stream:
        assert [json.loads(raw) for raw in stream] == [
            {
                "reason": "hu out of range",
                "area_id": 7,
                "analysis_time": RUN.isoformat(),
                "step": invalid,
            }
        ]
    before = [(root / name).read_bytes() for name in (SILVER, DLQ)]
    assert parse().exit_code == 0
    assert [(root / name).read_bytes() for name in (SILVER, DLQ)] == before


def test_skips_failed_requests_and_invalid_body(root, caplog, monkeypatch):
    monkeypatch.setattr(bmkg, "get_logger", logging.getLogger)
    error = (FIXTURES / "bmkg_error_unknown_code.json").read_text()
    archive(root, line(error), line("not JSON"), line(error, status=404))

    with caplog.at_level(logging.INFO, logger=bmkg.__name__):
        assert parse().exit_code == 0
    assert "skipped bronze lines=3" in caplog.text
    assert not (root / SILVER).exists()
    assert not (root / "bronze/bmkg/_dlq").exists()


def test_month_uses_response_run_and_rebuilds_whole_partition(root):
    november = (FIXTURES / MALANG).read_text()
    december = november.replace("2026-11-", "2026-12-")
    archive(root, line(december, area_id=8), line(november), hour="2026-11-19T12")
    archive(root, line(november, area_id=9), hour="2026-12-19T12")
    december_path = root / SILVER.replace("month=11", "month=12")

    assert parse("--month", "2026-11").exit_code == 0
    assert not december_path.exists()
    assert pq.ParquetFile(root / SILVER).read()["area_id"].to_pylist() == [7] * 17 + [9] * 17
    before = (root / SILVER).read_bytes()
    assert parse().exit_code == 0
    assert pq.ParquetFile(december_path).metadata.num_rows == 17
    assert (root / SILVER).read_bytes() == before


def test_duplicate_valid_time_fails_before_publishing(root):
    body = json.loads((FIXTURES / MALANG).read_text())
    steps = body["data"][0]["cuaca"][0]
    steps.append(steps[0])
    archive(root, line(body))

    with pytest.raises(AssertionError, match="Duplicate BMKG key"):
        parse()
    assert not (root / SILVER).exists()

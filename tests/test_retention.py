import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nowcast.cli import app

NOW = datetime(2026, 3, 1, 3, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    return tmp_path


def write(path: Path, *, mtime=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def run_retention(root, *args, now=NOW):
    result = runner.invoke(app, ["retention", "--now", now.isoformat(), *args])
    assert result.exit_code == 0, (result.output, result.exception)
    return json.loads((root / "state/health/retention.json").read_text())


def test_month_partitions(root):
    rain = root / "silver/rain_kabkota"
    old = write(rain / "year=2010/month=01/part.parquet")
    at_cutoff = write(rain / "year=2016/month=02/part.parquet")
    after_cutoff = write(rain / "year=2016/month=03/part.parquet")
    current = write(rain / "year=2026/month=03/part.parquet", mtime=NOW - timedelta(days=4000))

    report = run_retention(root, now=NOW.replace(hour=0))

    assert not old.parent.exists()
    assert not at_cutoff.parent.exists()
    assert after_cutoff.exists()
    assert current.exists()
    assert report["datasets"]["silver/rain_kabkota"]["bytes_freed"] == 8


def test_month_partitions_with_short_window(root):
    imerg = root / "bronze/imerg"
    month = imerg / "year=2026/month=02"
    old = write(month / "old.nc4", mtime=NOW - timedelta(days=8))
    recent = write(month / "recent.nc4", mtime=NOW - timedelta(days=1))
    empty = write(imerg / "year=2026/month=01/stale.nc4", mtime=NOW - timedelta(days=8)).parent

    report = run_retention(root)

    assert not old.exists()
    assert recent.exists()
    assert not empty.exists()
    assert report["datasets"]["bronze/imerg"] == {"deleted_count": 3, "bytes_freed": 8}


def test_run_and_hour_partitions(root):
    bmkg = root / "bronze/bmkg"
    run = write(bmkg / "run=2025-03-01T03/points.jsonl.gz")
    later_run = write(bmkg / "run=2025-03-01T04/points.jsonl.gz")
    himawari = root / "bronze/himawari/date=2026-02-28"
    hour = write(himawari / "hour=20/scan.dat")
    later_hour = write(himawari / "hour=21/scan.dat", mtime=NOW - timedelta(days=1))

    run_retention(root)

    assert not run.parent.exists()
    assert later_run.exists()
    assert not hour.parent.exists()
    assert later_hour.exists()


def test_calendar_cutoff_clamps_month_end(root, monkeypatch):
    monkeypatch.setattr(
        "nowcast.config.retention",
        lambda: {"datasets": {"gold/predictions": {"keep": "1 month"}}},
    )
    predictions = root / "gold/predictions"
    expired = write(predictions / "date=2026-02-27/part.parquet")
    kept = write(predictions / "date=2026-02-28/part.parquet", mtime=NOW - timedelta(days=60))
    long_month = write(
        predictions / "year=2026/month=03/part.parquet", mtime=NOW - timedelta(days=60)
    )

    run_retention(root, now=datetime(2026, 3, 31, tzinfo=UTC))

    assert not expired.parent.exists()
    assert kept.exists()
    assert long_month.exists()


def test_models_keeps_five_newest_folders(root):
    models = root / "models"
    for day in range(7):
        folder = write(models / f"v{day}/model", mtime=NOW).parent
        stamp = (NOW - timedelta(days=day)).timestamp()
        os.utime(folder, (stamp, stamp))
    metadata = write(models / "metadata.json", mtime=NOW - timedelta(days=30))

    report = run_retention(root)

    assert {path.name for path in models.iterdir() if path.is_dir()} == {f"v{i}" for i in range(5)}
    assert metadata.exists()
    assert report["datasets"]["models"] == {"deleted_count": 2, "bytes_freed": 8}


@pytest.mark.parametrize("dry_run", [False, True])
def test_models_keeps_older_live_version(root, monkeypatch, dry_run):
    monkeypatch.setattr("nowcast.config.settings", lambda: {"model": {"live_version": "v6"}})
    models = root / "models"
    for day in range(7):
        folder = write(models / f"v{day}/model").parent
        stamp = (NOW - timedelta(days=day)).timestamp()
        os.utime(folder, (stamp, stamp))

    report = run_retention(root, *(["--dry-run"] if dry_run else []))

    kept = {path.name for path in models.iterdir()}
    assert kept == (
        {f"v{i}" for i in range(7)} if dry_run else {"v0", "v1", "v2", "v3", "v4", "v6"}
    )
    assert report["datasets"]["models"] == {"deleted_count": 1, "bytes_freed": 4}


def test_mtime_and_independent_nested_policy(root):
    tiles = root / "gold/tiles/rain"
    old = write(tiles / "old.png", mtime=NOW - timedelta(days=31))
    recent = write(tiles / "recent.png", mtime=NOW - timedelta(days=30))
    dlq = write(root / "bronze/bmkg/_dlq/date=2025-11-29/records.jsonl.gz")

    report = run_retention(root)

    assert not old.exists()
    assert recent.exists()
    assert not dlq.parent.exists()
    assert report["datasets"]["bronze/bmkg"] == {"deleted_count": 0, "bytes_freed": 0}
    assert report["datasets"]["bronze/bmkg/_dlq"] == {"deleted_count": 1, "bytes_freed": 4}


def test_dry_run(root):
    partition = write(root / "silver/rain_kabkota/year=2010/month=01/part.parquet")
    unpartitioned = write(root / "gold/tiles/rain/old.png", mtime=NOW - timedelta(days=31))

    preview = run_retention(root, "--dry-run")

    assert partition.exists()
    assert unpartitioned.exists()
    assert preview["dry_run"] is True
    assert preview["run_at"] == NOW.isoformat()
    assert preview["datasets"]["silver/rain_kabkota"]["bytes_freed"] == 4
    assert preview["datasets"] == run_retention(root)["datasets"]


def test_disk_budget_and_dataset_sizes(root):
    write(root / "bronze/bmkg/run=2026-03-01T03/points.jsonl.gz")
    write(root / "bronze/bmkg/_dlq/date=2026-03-01/records.jsonl.gz")
    write(root / "ref/boundaries.json")
    write(root / "state/last_run.json")
    write(root / "bronze/unconfigured/data")
    write(root / "tmp/download")

    result = runner.invoke(app, ["disk", "--budget-gb", "0.00000001"])

    assert result.exit_code == 1, (result.output, result.exception)
    assert "FAIL disk budget exceeded" in result.output
    report_path = root / "state/health/disk.json"
    report = json.loads(report_path.read_text())
    assert report["datasets"] == dict.fromkeys(
        ["bronze/bmkg", "bronze/bmkg/_dlq", "ref", "state", "other"], 4
    )
    assert report["total_bytes"] == 20
    assert report["budget_exceeded"]

    result = runner.invoke(app, ["disk", "--budget-gb", "1"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "GB of 1 GB" in result.output
    assert not json.loads(report_path.read_text())["budget_exceeded"]

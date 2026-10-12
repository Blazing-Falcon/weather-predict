from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from dagster import (
    AssetKey,
    DagsterEventType,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
)

spec = spec_from_file_location(
    "definitions", Path(__file__).resolve().parents[1] / "orchestration" / "definitions.py",
)
definitions = module_from_spec(spec)
spec.loader.exec_module(definitions)
MONTHS = definitions.MONTHS
defs = definitions.defs


def test_definitions():
    Definitions.validate_loadable(defs)
    repository = defs.get_repository_def()
    graph = defs.resolve_asset_graph()

    assert {key.to_user_string() for key in graph.get_all_asset_keys()} == {
        "area_registry", "bmkg_raw", "imerg_crops", "mergir_crops", "himawari_scans",
        "gauge_reports", "bmkg_forecast", "rain_kabkota", "cloud_kabkota", "gauge_obs",
        "ops_health",
    }
    assert graph.get(AssetKey("bmkg_forecast")).parent_keys == {AssetKey("bmkg_raw")}
    assert graph.get(AssetKey("rain_kabkota")).parent_keys == {
        AssetKey("imerg_crops"), AssetKey("area_registry"),
    }
    assert graph.get(AssetKey("cloud_kabkota")).parent_keys == {
        AssetKey("mergir_crops"), AssetKey("himawari_scans"), AssetKey("area_registry"),
    }
    assert {(key.asset_key.to_user_string(), key.name) for key in graph.asset_check_keys} == {
        ("ops_health", "disk_budget"), ("rain_kabkota", "rain_rows_per_slot"),
    }

    assert {job.name for job in repository.get_all_jobs() if not job.name.startswith("__")} == {
        "bmkg_job", "imerg_live_job", "himawari_live_job", "metar_job", "retention_job",
        "dagster_cleanup_job", "imerg_backfill_job", "mergir_backfill_job", "ghcnh_backfill_job",
        "disk_check_job",
    }
    for name in ("imerg_backfill_job", "mergir_backfill_job", "ghcnh_backfill_job"):
        assert defs.resolve_job_def(name).partitions_def == MONTHS
    for name in ("imerg_backfill_job", "mergir_backfill_job"):
        assert defs.resolve_job_def(name).tags["pool"] == "nasa"

    assert {s.name: s.minimum_interval_seconds for s in repository.sensor_defs} == {
        "bmkg_sensor": 900, "imerg_sensor": 600, "himawari_sensor": 300,
    }
    assert {s.name: s.cron_schedule for s in repository.schedule_defs} == {
        "gauges_schedule": "20 * * * *", "retention_schedule": "30 1 * * *",
        "dagster_cleanup_schedule": "0 2 * * 0",
    }
    assert all(s.default_status == DefaultSensorStatus.STOPPED for s in repository.sensor_defs)
    assert all(s.default_status == DefaultScheduleStatus.STOPPED for s in repository.schedule_defs)
    assert MONTHS.get_last_partition_key() == datetime.now(UTC).strftime("%Y-%m-01")


def test_cloud_job_records_each_provider_for_its_partition():
    current = MONTHS.get_last_partition_key()
    previous = MONTHS.get_partition_keys()[-2]
    with patch.object(definitions.himawari, "tick", return_value={
        previous[:7]: {"slots": 1, "rows": 514, "bronze_bytes": 20, "silver_bytes": 10},
    }):
        result = defs.resolve_job_def("himawari_live_job").execute_in_process(partition_key=current)
    assert {(event.asset_key.to_user_string(), event.partition)
            for event in result.get_asset_materialization_events()} == {
        ("himawari_scans", current), ("cloud_kabkota", current),
        ("himawari_scans", previous), ("cloud_kabkota", previous),
    }

    with patch.object(definitions.mergir, "backfill", return_value={}) as backfill:
        result = defs.resolve_job_def("mergir_backfill_job").execute_in_process(partition_key=previous)
    backfill.assert_called_once_with(start=previous[:7], end=previous[:7])
    assert {event.asset_key.to_user_string() for event in result.get_asset_materialization_events()} == {
        "mergir_crops", "cloud_kabkota",
    }


def test_disk_budget_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DISK_BUDGET_GB", "0")
    (tmp_path / "data.bin").write_bytes(b"x")
    result = defs.resolve_job_def("disk_check_job").execute_in_process(raise_on_error=False)
    assert not result.success
    check, = result.filter_events(lambda event: event.event_type == DagsterEventType.ASSET_CHECK_EVALUATION)
    assert not check.event_specific_data.passed
    assert check.event_specific_data.severity.value == "ERROR"

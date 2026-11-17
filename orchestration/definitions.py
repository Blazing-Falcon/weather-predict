from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter

import pyarrow.parquet as pq
from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    AssetKey,
    AssetMaterialization,
    AssetSelection,
    AssetSpec,
    Config,
    DagsterRunStatus,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
    MaterializeResult,
    MonthlyPartitionsDefinition,
    OpExecutionContext,
    RunRequest,
    RunsFilter,
    ScheduleEvaluationContext,
    SkipReason,
    asset,
    asset_check,
    define_asset_job,
    job,
    multi_asset,
    op,
    schedule,
    sensor,
)

from nowcast import config, paths
from nowcast.registry import boundaries, dim_area, membership, neighbours
from nowcast.retention import disk_usage, retention
from nowcast.sources import gauges, himawari, imerg, mergir
from nowcast.sources.bmkg import archive as bmkg
from nowcast.sources.bmkg import forecast
from nowcast.storage import month_path

# Include the open UTC month for live data.
MONTHS = MonthlyPartitionsDefinition(start_date="2016-01-01", timezone="UTC", end_offset=1)
NASA = {"pool": "nasa"}


@asset(group_name="ref")
def area_registry() -> MaterializeResult:
    """Area dimension, grid memberships, neighbours and recorded BMKG picks."""
    started = perf_counter()
    ref = paths.zone("ref")
    boundaries.download(force=False)
    # Rebuilding the dimension discards any BMKG picks already recorded in it.
    if not (ref / "dim_area.parquet").exists():
        dim_area.build()
    membership.build("all")
    neighbours.build()
    tables = ("dim_area", "imerg_membership", "mergir_membership", "neighbours")
    metadata = {}
    for name in tables:
        part = ref / f"{name}.parquet"
        metadata[name] = pq.ParquetFile(part).metadata.num_rows
    picks = pq.read_table(
        ref / "dim_area.parquet", columns=["bmkg_adm4"], filters=[("is_admin", "=", True)],
    )["bmkg_adm4"]
    return MaterializeResult(metadata={
        **metadata,
        "bmkg_picks": len(picks) - picks.null_count,
        "bytes": sum((ref / f"{name}.parquet").stat().st_size for name in tables),
        "elapsed_seconds": perf_counter() - started,
    })


@asset(group_name="bronze", deps=[area_registry])
def bmkg_raw() -> MaterializeResult:
    """Archived BMKG forecast responses."""
    started = perf_counter()
    return MaterializeResult(metadata={**bmkg.tick(), "elapsed_seconds": perf_counter() - started})


@asset(group_name="silver", deps=[bmkg_raw])
def bmkg_forecast() -> MaterializeResult:
    """Parsed BMKG forecasts for the current UTC month."""
    started = perf_counter()
    report = forecast.parse(month=datetime.now(UTC).strftime("%Y-%m"))
    return MaterializeResult(metadata={**report, "elapsed_seconds": perf_counter() - started})


def recorded_partitions(
    context: AssetExecutionContext,
    bronze: str,
    silver: str,
    reports: dict,
    elapsed: float,
    *,
    live: bool,
    count: str = "slots",
):
    for month in sorted(reports.keys() | {context.partition_key[:7]}):
        report = reports.get(month, {})
        for name in (bronze, silver):
            key = AssetKey(name)
            if key not in context.selected_asset_keys:
                continue
            if name == bronze:
                metadata = {count: report.get(count, 0), "bytes": report.get("bronze_bytes", 0)}
                latency = report.get("bronze_latency_min", report.get("latency_min"))
            else:
                metadata = {"rows": report.get("rows", 0), "bytes": report.get("silver_bytes", 0)}
                latency = report.get("latency_min")
            metadata["elapsed_seconds"] = elapsed
            if live:
                metadata["latency_min"] = latency
            partition = f"{month}-01"
            if partition == context.partition_key:
                yield MaterializeResult(asset_key=key, metadata=metadata)
            elif report.get(count if name == bronze else "rows", 0) or metadata["bytes"]:
                context.log_event(AssetMaterialization(
                    asset_key=key, partition=partition, metadata=metadata,
                ))


class BackfillConfig(Config):
    backfill: bool = False


@multi_asset(
    specs=[
        AssetSpec("imerg_crops", group_name="bronze", partitions_def=MONTHS),
        AssetSpec(
            "rain_kabkota", group_name="silver", partitions_def=MONTHS,
            deps=["imerg_crops", "area_registry"],
        ),
    ],
)
def imerg_assets(context: AssetExecutionContext, config: BackfillConfig):
    started = perf_counter()
    month = context.partition_key[:7]
    reports = imerg.backfill(start=month, end=month) if config.backfill else imerg.tick()
    yield from recorded_partitions(
        context, "imerg_crops", "rain_kabkota", reports, perf_counter() - started,
        live=not config.backfill,
    )


@multi_asset(
    can_subset=True,
    specs=[
        AssetSpec("mergir_crops", group_name="bronze", partitions_def=MONTHS, skippable=True),
        AssetSpec("himawari_scans", group_name="bronze", partitions_def=MONTHS, skippable=True),
        AssetSpec(
            "cloud_kabkota", group_name="silver", partitions_def=MONTHS, skippable=True,
            deps=["mergir_crops", "himawari_scans", "area_registry"],
        ),
    ],
)
def cloud_assets(context: AssetExecutionContext):
    started = perf_counter()
    month = context.partition_key[:7]
    selected = {key.to_user_string() for key in context.selected_asset_keys}
    providers = selected & {"mergir_crops", "himawari_scans"}
    backfill = (
        "mergir_crops" in providers if len(providers) == 1
        else month != datetime.now(UTC).strftime("%Y-%m")
    )
    if backfill:
        reports = mergir.backfill(start=month, end=month)
        bronze = "mergir_crops"
    else:
        reports = himawari.tick()
        bronze = "himawari_scans"
    yield from recorded_partitions(
        context, bronze, "cloud_kabkota", reports, perf_counter() - started,
        live=not backfill,
    )


@multi_asset(
    specs=[
        AssetSpec("gauge_reports", group_name="bronze", partitions_def=MONTHS),
        AssetSpec(
            "gauge_obs", group_name="silver", partitions_def=MONTHS,
            deps=["gauge_reports", "area_registry"],
        ),
    ],
)
def gauge_assets(context: AssetExecutionContext, config: BackfillConfig):
    started = perf_counter()
    if config.backfill:
        year, month, _ = context.partition_key.split("-")
        reports = gauges.ghcnh(year=int(year), month=int(month))
    else:
        reports = gauges.metar()
    yield from recorded_partitions(
        context, "gauge_reports", "gauge_obs", reports, perf_counter() - started,
        live=not config.backfill, count="reports",
    )


@asset_check(asset="rain_kabkota")
def rain_rows_per_slot() -> AssetCheckResult:
    month = MONTHS.get_last_partition_key()
    stamp = datetime.strptime(month, "%Y-%m-%d")
    part = month_path(paths.zone("silver") / "rain_kabkota", stamp)
    part /= "part.parquet"
    counts = []
    if part.is_file():
        slots = pq.ParquetFile(part).read(columns=["slot"])
        counts = slots.group_by("slot").aggregate([("slot", "count")])["slot_count"].to_pylist()
    return AssetCheckResult(
        passed=bool(counts) and min(counts) == max(counts) == 514,
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "partition": month,
            "slots": len(counts),
            "min_rows_per_slot": min(counts, default=0),
            "max_rows_per_slot": max(counts, default=0),
            "expected_rows_per_slot": 514,
        },
    )


@asset(group_name="ops")
def ops_health() -> MaterializeResult:
    """Disk usage for the nowcast data root."""
    started = perf_counter()
    report = disk_usage()
    return MaterializeResult(metadata={
        "bytes": report["total_bytes"], "elapsed_seconds": perf_counter() - started,
    })


@asset_check(asset=ops_health, blocking=True)
def disk_budget() -> AssetCheckResult:
    override = config.env("DISK_BUDGET_GB")
    report = disk_usage(float(override) if override is not None else None)
    return AssetCheckResult(
        passed=not report["budget_exceeded"],
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "bytes": report["total_bytes"],
            "budget_gb": report["budget_gb"],
            "datasets": report["datasets"],
        },
    )


bmkg_job = define_asset_job("bmkg_job", selection=["bmkg_raw", "bmkg_forecast"])
rain = AssetSelection.assets("imerg_crops", "rain_kabkota")
imerg_live_job = define_asset_job("imerg_live_job", selection=rain)
imerg_backfill_job = define_asset_job(
    "imerg_backfill_job", selection=rain.without_checks(), tags=NASA,
    config={"ops": {"imerg_assets": {"config": {"backfill": True}}}},
)
himawari_live_job = define_asset_job(
    "himawari_live_job", selection=["himawari_scans", "cloud_kabkota"],
)
mergir_backfill_job = define_asset_job(
    "mergir_backfill_job", selection=["mergir_crops", "cloud_kabkota"], tags=NASA,
)
metar_job = define_asset_job("metar_job", selection=["gauge_reports", "gauge_obs"])
ghcnh_backfill_job = define_asset_job(
    "ghcnh_backfill_job", selection=["gauge_reports", "gauge_obs"],
    config={"ops": {"gauge_assets": {"config": {"backfill": True}}}},
)
disk_check_job = define_asset_job("disk_check_job", selection=AssetSelection.checks(disk_budget))


@sensor(job=bmkg_job, minimum_interval_seconds=900, default_status=DefaultSensorStatus.STOPPED)
def bmkg_sensor():
    now = datetime.now(UTC)
    return RunRequest(run_key=now.replace(
        minute=now.minute // 15 * 15, second=0, microsecond=0,
    ).isoformat())


@sensor(job=imerg_live_job, minimum_interval_seconds=600, default_status=DefaultSensorStatus.STOPPED)
def imerg_sensor():
    now = datetime.now(UTC)
    granule = imerg.newest_granule(now)
    if granule is None:
        return SkipReason("No IMERG granule newer than the cursor")
    return RunRequest(run_key=granule.title, partition_key=f"{now:%Y-%m}-01")


@sensor(
    job=himawari_live_job, minimum_interval_seconds=300, default_status=DefaultSensorStatus.STOPPED,
)
def himawari_sensor():
    now = datetime.now(UTC)
    scan = himawari.newest_scan(now)
    if scan is None:
        return SkipReason("No complete Himawari scan newer than the cursor")
    return RunRequest(run_key=scan.isoformat(), partition_key=f"{now:%Y-%m}-01")


@schedule(
    job=metar_job, cron_schedule="20 * * * *", execution_timezone="UTC",
    default_status=DefaultScheduleStatus.STOPPED,
)
def gauges_schedule(context: ScheduleEvaluationContext):
    return RunRequest(partition_key=f"{context.scheduled_execution_time:%Y-%m}-01")


class RetentionConfig(Config):
    dry_run: bool = False


@op
def expire_data(config: RetentionConfig):
    retention(dry_run=config.dry_run)


@job
def retention_job():
    expire_data()


@schedule(
    job=retention_job, cron_schedule="30 1 * * *", execution_timezone="UTC",
    default_status=DefaultScheduleStatus.STOPPED,
)
def retention_schedule():
    return RunRequest()


@op
def expire_dagster_history(context: OpExecutionContext):
    cutoff = datetime.now(UTC) - timedelta(days=30)
    instance = context.instance
    records = instance.get_run_records(filters=RunsFilter(
        created_before=cutoff,
        statuses=[DagsterRunStatus.SUCCESS, DagsterRunStatus.FAILURE, DagsterRunStatus.CANCELED],
    ))
    for record in records:
        instance.delete_run(record.dagster_run.run_id)

    log_root = Path(instance.storage_directory())
    for log_dir in log_root.glob("*/compute_logs"):
        run = instance.get_run_by_id(log_dir.parent.name)
        if run is not None and not run.is_finished:
            continue
        for log in log_dir.iterdir():
            if log.is_file() and log.stat().st_mtime < cutoff.timestamp():
                log.unlink(missing_ok=True)
    context.log.info("Deleted %d Dagster runs created before %s", len(records), cutoff.isoformat())


@job
def dagster_cleanup_job():
    expire_dagster_history()


@schedule(
    job=dagster_cleanup_job, cron_schedule="0 2 * * 0", execution_timezone="UTC",
    default_status=DefaultScheduleStatus.STOPPED,
)
def dagster_cleanup_schedule():
    return RunRequest()


defs = Definitions(
    assets=[area_registry, bmkg_raw, bmkg_forecast, imerg_assets, cloud_assets, gauge_assets,
            ops_health],
    asset_checks=[disk_budget, rain_rows_per_slot],
    jobs=[bmkg_job, imerg_live_job, himawari_live_job, metar_job, retention_job, dagster_cleanup_job,
          imerg_backfill_job, mergir_backfill_job, ghcnh_backfill_job, disk_check_job],
    sensors=[bmkg_sensor, imerg_sensor, himawari_sensor],
    schedules=[gauges_schedule, retention_schedule, dagster_cleanup_schedule],
)

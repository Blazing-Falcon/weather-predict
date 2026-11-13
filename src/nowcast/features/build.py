import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Annotated

import typer
from pyspark.sql import Observation
from pyspark.sql import functions as F

from nowcast import config, paths
from nowcast.features.rows import (
    FEATURES,
    KEYS,
    LEADS,
    TARGETS,
    admin_areas,
    feature_rows,
    read_climatology,
    slot_number,
)
from nowcast.features.spark import spark_session
from nowcast.log import get_logger
from nowcast.timeslots import SLOT, SLOT_SECONDS, date_range, month_ranges, next_month


def sample_month_days(month, first, stop, fraction, seed):
    day = max(month, first).date()
    days = [
        day + timedelta(days=i)
        for i in range((min(next_month(month), stop).date() - day).days)
    ]
    excluded = set()
    if days and fraction < 1:
        k = max(1, round(fraction * len(days)))
        chosen = random.Random(f"{seed}-{month:%Y-%m}").sample(days, k)
        excluded = set(days).difference(chosen)
    return days, excluded


def build(
    start: Annotated[str, typer.Option()],
    end: Annotated[str, typer.Option()],
    out: Annotated[Path | None, typer.Option()] = None,
    cloud_source: Annotated[int, typer.Option(min=1, max=2, help="1 = MERGIR, 2 = Himawari.")] = 1,
) -> None:
    try:
        first, stop = date_range(start, end)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    model = config.settings()["model"]
    sampling = model["sample_days"]
    sample_start, sample_stop = date_range(*model["split"][sampling["split"]])
    fraction = sampling["fraction"]
    if not 0 <= fraction <= 1:
        raise typer.BadParameter("Sample fraction must be between 0 and 1")
    lag = int(config.env("IMERG_LAG_MINUTES", str(model["imerg_lag_minutes"])))
    every, remainder = divmod(model["issue_every_minutes"], 30)
    if every <= 0 or remainder or lag < 0:
        raise typer.BadParameter(
            "Issue interval must be a positive multiple of 30 and lag nonnegative"
        )

    root = paths.data_root()
    output = (out or root / "tmp" / "features" / f"{start}_{end}").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    logger = get_logger(__name__)
    started = monotonic()
    metrics = {}
    kept_days = total_days = 0
    with spark_session(root) as spark, TemporaryDirectory(dir=output.parent) as temp:
        rain_dir = root / "silver" / "rain_kabkota"
        newest = None
        for part in sorted(rain_dir.glob("year=*/month=*/part.parquet"), reverse=True):
            newest = spark.read.parquet(str(part)).select(F.max(F.unix_seconds("slot"))).first()[0]
            if newest is not None:
                break
        if newest is None:
            raise ValueError(f"No IMERG slots in {rain_dir}")
        areas = admin_areas(spark, root)
        climatology = read_climatology(spark, root)
        staged = Path(temp) / "features"
        available_stop = datetime.fromtimestamp(newest - (2 * max(LEADS) - 2) * SLOT_SECONDS, UTC)
        for month_first, month_stop in month_ranges(first, min(stop, available_stop)):
            month = month_first.replace(day=1)
            advance = (slot_number(first) - slot_number(month_first)) % every
            month_first += timedelta(seconds=advance * SLOT_SECONDS)
            if month_first >= month_stop:
                continue
            days, excluded_days = sample_month_days(
                month, sample_start, sample_stop, fraction, sampling["seed"]
            )
            issue_days = {
                datetime.fromtimestamp(slot * SLOT_SECONDS, UTC).date()
                for slot in range(slot_number(month_first), slot_number(month_stop), every)
            }
            sampled_issue_days = issue_days.intersection(days)
            total_days += len(sampled_issue_days)
            kept_days += len(sampled_issue_days - excluded_days)
            if issue_days <= excluded_days:
                continue
            rows = feature_rows(
                spark,
                root,
                areas,
                climatology,
                month_first,
                month_stop,
                lag,
                every,
                cloud_source,
                excluded_days,
            )
            stats = Observation()
            observed = rows.observe(
                stats,
                F.count(F.lit(1)).alias("rows"),
                *[F.count(name).alias(name) for name in FEATURES + TARGETS],
                *[F.coalesce(F.sum(name), F.lit(0)).alias(f"{name}_positive") for name in TARGETS],
            )
            observed.repartition("date").write.mode("append").partitionBy("date").parquet(
                str(staged)
            )
            for name, value in stats.get.items():
                metrics[name] = metrics.get(name, 0) + value
        if not metrics:
            rows = feature_rows(
                spark, root, areas, climatology, first, first + SLOT, lag, every,
                cloud_source,
            )
            rows.limit(0).write.parquet(str(staged))
        previous = Path(temp) / "previous"
        if output.exists():
            output.rename(previous)
        try:
            staged.rename(output)
        except OSError:
            if previous.exists():
                previous.rename(output)
            raise
        memory = spark.sparkContext._jvm.java.lang.management.ManagementFactory
        peak_heap = sum(
            pool.getPeakUsage().getUsed()
            for pool in memory.getMemoryPoolMXBeans()
            if pool.getType().name() == "HEAP"
        )
    count = metrics.get("rows", 0)
    logger.info("Sampled split %s kept %d of %d issue days", sampling["split"], kept_days, total_days)
    logger.info("Wrote %s, %d rows, %d columns", output, count, len(KEYS + FEATURES + TARGETS) + 1)
    logger.info(
        "Feature null share %s",
        {name: round(1 - metrics[name] / count, 5) if count else None for name in FEATURES},
    )
    logger.info(
        "Target positive rate %s",
        {
            name: round(metrics[f"{name}_positive"] / metrics[name], 5)
            if metrics.get(name)
            else None
            for name in TARGETS
        },
    )
    logger.info(
        "Runtime %.1f s, driver peak heap pool total %.0f MiB",
        monotonic() - started,
        peak_heap / 2**20,
    )

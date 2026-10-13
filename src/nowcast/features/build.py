import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Annotated

import typer
from pyspark.sql import Column, DataFrame, Observation, SparkSession, Window
from pyspark.sql import functions as F

from nowcast import config, paths
from nowcast.features.spark import spark_session
from nowcast.log import get_logger

SLOT_SECONDS = 30 * 60
RAIN_SCHEMA = (
    "slot timestamp, area_id smallint, rain_mean_x10 smallint, rain_max_x10 smallint, "
    "wet_pct smallint"
)
CLOUD_SCHEMA = (
    "slot timestamp, area_id smallint, source smallint, cold_pct smallint, tb_min smallint, "
    "tb_mean_x10 smallint, adv1_pct smallint, adv2_pct smallint, adv3_pct smallint, flow_kmh smallint"
)
CLIM_KEYS = ["area_id", "month", "hour_local"]
CLOUD_PARTS = {1: "part-mergir.parquet", 2: "part-himawari.parquet"}
LOCAL_OFFSETS = {"WIB": 7, "WITA": 8, "WIT": 9}
LEADS = (1, 2, 3)
TARGETS = [f"y{lead}" for lead in LEADS]
KEYS = ["t", "area_id", "imerg_slot", "cloud_slot"]
FEATURES = [
    "cold_pct",
    "tb_min",
    "tb_mean",
    "cold_trend_1h",
    "nb_cold_pct",
    "adv1_pct",
    "adv2_pct",
    "adv3_pct",
    "adv_max_pct",
    "flow_kmh",
    "rain_lag",
    "wet_lag",
    "rainmax_lag",
    "rain_sum_3h",
    "rain_sum_6h",
    "rain_sum_24h",
    "nb_rain_lag",
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
    "area_km2",
    "island",
    "clim_wet_pct",
    "clim_p1",
    "clim_p2",
    "clim_p3",
]


def available_imerg_slot(t: datetime, lag: int) -> datetime:
    cutoff = t.astimezone(UTC) - timedelta(minutes=lag)
    return cutoff.replace(minute=cutoff.minute // 30 * 30, second=0, microsecond=0)


def date_range(start: str, end: str) -> tuple[datetime, datetime]:
    first, last = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) for day in (start, end))
    if first > last:
        raise ValueError("Start date must be on or before end date")
    return first, last + timedelta(days=1)


def month_ranges(first: datetime, stop: datetime):
    while first < stop:
        month_start = datetime(first.year, first.month, 1, tzinfo=UTC)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        yield first, min(stop, next_month)
        first = next_month


def slot_number(time: datetime) -> int:
    return int(time.timestamp()) // SLOT_SECONDS


def admin_areas(spark: SparkSession, root: Path) -> DataFrame:
    return (
        spark.read.parquet(str(root / "ref" / "dim_area.parquet"))
        .where("is_admin")
        .select("area_id", "area_km2", "island", "tz")
    )


def read_slots(
    spark: SparkSession, directory: Path, part: str, schema: str, first: int, last: int
) -> DataFrame:
    start = datetime.fromtimestamp(first * SLOT_SECONDS, UTC)
    stop = datetime.fromtimestamp((last + 1) * SLOT_SECONDS, UTC)
    files = [
        directory / f"year={month:%Y}" / f"month={month:%m}" / part
        for month, _ in month_ranges(start, stop)
    ]
    files = [str(file) for file in files if file.is_file()]
    if not files:
        return spark.createDataFrame([], schema).withColumn("slot", F.lit(None).cast("long"))
    return (
        spark.read.schema(schema)
        .parquet(*files)
        .where(F.col("slot").between(start, stop - timedelta(minutes=30)))
        .withColumn("slot", (F.unix_seconds("slot") / SLOT_SECONDS).cast("long"))
    )


def local_time(slot: str) -> Column:
    zone = F.create_map(*(F.lit(value) for pair in LOCAL_OFFSETS.items() for value in pair))
    return F.timestamp_seconds(F.col(slot) * SLOT_SECONDS + zone[F.col("tz")] * 3600)


def target_columns() -> list[Column]:
    threshold = config.settings()["target"]["threshold_mm_h"] * 10
    slots = Window.partitionBy("area_id").orderBy("slot")
    rain = F.col("rain_mean_x10")
    return [
        F.when(
            F.count(rain).over(slots.rangeBetween(0, 2 * lead - 1)) == 2 * lead,
            F.max(rain).over(slots.rangeBetween(0, 2 * lead - 1)) >= threshold,
        )
        .cast("tinyint")
        .alias(f"y{lead}")
        for lead in LEADS
    ]


def feature_rows(
    spark: SparkSession,
    root: Path,
    areas: DataFrame,
    climatology: DataFrame,
    first: datetime,
    stop: datetime,
    lag: int,
    every: int,
    cloud_source: int = 1,
) -> DataFrame:
    start, end = slot_number(first), slot_number(stop) - 1
    offset = start - slot_number(available_imerg_slot(first, lag))
    issues = spark.range(start, end + 1, every).select(
        F.col("id").alias("t"), (F.col("id") - offset).alias("imerg_slot")
    )
    grid = issues.crossJoin(F.broadcast(areas))
    rain_first = min(start, start - offset - 47)
    rain_last = max(end + 2 * max(LEADS) - 1, end - offset)
    rain = read_slots(
        spark, root / "silver" / "rain_kabkota", "part.parquet", RAIN_SCHEMA, rain_first, rain_last
    )
    targets = rain.select(F.col("slot").alias("t"), "area_id", *target_columns())
    anchors = (
        spark.range(start - offset, end - offset + 1)
        .select(F.col("id").alias("slot"))
        .crossJoin(F.broadcast(areas.select("area_id")))
    )
    # Only slots in the lookback outside the anchor grid need to come from the raw input.
    past_rain = rain.where(F.col("slot") <= end - offset)
    history = anchors.join(past_rain, ["slot", "area_id"], "left").unionByName(
        past_rain.where(F.col("slot") < start - offset)
    )
    weather_window = Window.partitionBy("area_id").orderBy("slot")
    history = history.select(
        F.col("slot").alias("imerg_slot"),
        "area_id",
        (F.col("rain_mean_x10") / 10).alias("rain_lag"),
        F.col("wet_pct").alias("wet_lag"),
        (F.col("rain_max_x10") / 10).alias("rainmax_lag"),
        *[
            (F.sum("rain_mean_x10").over(weather_window.rangeBetween(1 - 2 * hours, 0)) / 20).alias(
                f"rain_sum_{hours}h"
            )
            for hours in (3, 6, 24)
        ],
    ).where(F.col("imerg_slot") >= start - offset)

    cloud = read_slots(
        spark,
        root / "silver" / "cloud_kabkota",
        CLOUD_PARTS[cloud_source],
        CLOUD_SCHEMA,
        start - 2,
        end,
    ).where(F.col("source") == cloud_source)
    cloud_now = cloud.select(
        F.col("slot").alias("t"),
        "area_id",
        "cold_pct",
        "tb_min",
        (F.col("tb_mean_x10") / 10).alias("tb_mean"),
        "adv1_pct",
        "adv2_pct",
        "adv3_pct",
        F.greatest("adv1_pct", "adv2_pct", "adv3_pct").alias("adv_max_pct"),
        "flow_kmh",
        (F.col("cold_pct") - F.max("cold_pct").over(weather_window.rangeBetween(-2, -2))).alias(
            "cold_trend_1h"
        ),
    ).where(F.col("t") >= start)
    edges = F.broadcast(
        spark.read.parquet(str(root / "ref" / "neighbours.parquet")).select(
            "area_id", "neighbour_id"
        )
    )
    nb_cloud = (
        cloud.where(F.col("slot") >= start)
        .select(F.col("slot").alias("t"), F.col("area_id").alias("neighbour_id"), "cold_pct")
        .join(edges, "neighbour_id")
        .groupBy("t", "area_id")
        .agg(F.avg("cold_pct").alias("nb_cold_pct"))
    )
    nb_rain = (
        past_rain.where(F.col("slot") >= start - offset)
        .select(
            F.col("slot").alias("imerg_slot"),
            F.col("area_id").alias("neighbour_id"),
            "rain_mean_x10",
        )
        .join(edges, "neighbour_id")
        .groupBy("imerg_slot", "area_id")
        .agg((F.avg("rain_mean_x10") / 10).alias("nb_rain_lag"))
    )
    rows = (
        grid.join(history, ["imerg_slot", "area_id"], "left")
        .join(nb_rain, ["imerg_slot", "area_id"], "left")
        .join(cloud_now, ["t", "area_id"], "left")
        .join(nb_cloud, ["t", "area_id"], "left")
        .join(targets, ["t", "area_id"], "left")
        .withColumn("local", local_time("t"))
        .withColumn("month", F.month("local"))
        .withColumn("hour_local", F.hour("local"))
        .join(F.broadcast(climatology), CLIM_KEYS, "left")
    )
    hour_angle = (F.col("hour_local") + F.minute("local") / 60) * (math.tau / 24)
    year_angle = (F.dayofyear("local") - 1) * (math.tau / 365.25)
    rows = rows.withColumns(
        {
            "hour_sin": F.sin(hour_angle),
            "hour_cos": F.cos(hour_angle),
            "doy_sin": F.sin(year_angle),
            "doy_cos": F.cos(year_angle),
            "cloud_slot": F.timestamp_seconds(F.col("t") * SLOT_SECONDS),
            **{key: F.timestamp_seconds(F.col(key) * SLOT_SECONDS) for key in ("t", "imerg_slot")},
        }
    )
    return rows.select(
        *KEYS,
        *[
            F.col(name).cast("string" if name == "island" else "float").alias(name)
            for name in FEATURES
        ],
        *TARGETS,
        F.to_date("t").alias("date"),
    )


def read_climatology(spark: SparkSession, root: Path) -> DataFrame:
    return spark.read.parquet(str(root / "gold" / "climatology" / "part.parquet")).select(
        *CLIM_KEYS,
        *[F.col(name).alias(f"clim_{name}") for name in ("wet_pct", "p1", "p2", "p3")],
    )


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
            advance = (slot_number(first) - slot_number(month_first)) % every
            month_first += timedelta(seconds=advance * SLOT_SECONDS)
            if month_first >= month_stop:
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
                spark, root, areas, climatology, first, first + timedelta(minutes=30), lag, every,
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

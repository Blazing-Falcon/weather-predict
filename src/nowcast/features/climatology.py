from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import pyarrow as pa
from pyspark.sql import functions as F

from nowcast import config, paths
from nowcast.features.build import (
    CLIM_KEYS,
    LEADS,
    RAIN_SCHEMA,
    admin_areas,
    local_time,
    read_slots,
    slot_number,
    target_columns,
)
from nowcast.features.spark import spark_session
from nowcast.log import get_logger
from nowcast.storage import write_table
from nowcast.timeslots import date_range, month_ranges


def climatology() -> None:
    first, stop = date_range(*config.settings()["model"]["climatology_period"])
    every = config.settings()["model"]["issue_every_minutes"] // 30
    root = paths.data_root()
    output = root / "gold" / "climatology" / "part.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    with spark_session(root) as spark, TemporaryDirectory(dir=output.parent) as temp:
        # p1, p2 and p3 measure forecast events. Wet share measures spatial coverage.
        issue = (F.col("slot") - slot_number(first)) % every == 0
        values = {
            "wet_pct": F.col("wet_pct"),
            "rain_mean_x10": F.col("rain_mean_x10"),
            **{f"p{lead}": F.when(issue, F.col(f"y{lead}")) * 100 for lead in LEADS},
        }
        partials = str(Path(temp) / "monthly")
        areas = F.broadcast(admin_areas(spark, root).select("area_id", "tz"))
        for month_first, month_stop in month_ranges(first, stop):
            month_end = slot_number(month_stop) - 1
            rain = read_slots(
                spark,
                root / "silver" / "rain_kabkota",
                "part.parquet",
                RAIN_SCHEMA,
                slot_number(month_first),
                min(slot_number(stop) - 1, month_end + 2 * max(LEADS) - 1),
            )
            rows = (
                rain.select("slot", "area_id", "wet_pct", "rain_mean_x10", *target_columns())
                .where(F.col("slot") <= month_end)
                .join(areas, "area_id")
                .withColumn("local", local_time("slot"))
                .withColumn("month", F.month("local"))
                .withColumn("hour_local", F.hour("local"))
            )
            rows.groupBy(*CLIM_KEYS).agg(
                *[F.sum(col).alias(f"{name}_sum") for name, col in values.items()],
                *[F.count(col).alias(f"{name}_count") for name, col in values.items()],
            ).write.mode("append").parquet(partials)

        totals = (
            spark.read.parquet(partials)
            .groupBy(*CLIM_KEYS)
            .agg(
                *[
                    F.sum(f"{name}_{stat}").alias(f"{name}_{stat}")
                    for name in values
                    for stat in ("sum", "count")
                ]
            )
        )
        means = {
            name: F.try_divide(F.col(f"{name}_sum"), F.col(f"{name}_count")) for name in values
        }
        table = totals.select(
            *CLIM_KEYS,
            means["wet_pct"].cast("float").alias("wet_pct"),
            means["rain_mean_x10"].cast("float").alias("rain_mean_x10"),
            *[F.round(means[f"p{lead}"]).cast("short").alias(f"p{lead}") for lead in LEADS],
            F.col("rain_mean_x10_count").cast("int").alias("n"),
        ).toArrow()
        for lead in LEADS:
            name = f"p{lead}"
            index = table.schema.get_field_index(name)
            table = table.set_column(index, name, table[name].cast(pa.uint8()))
        write_table(table, output)
    get_logger(__name__).info(
        "Wrote %s, %d rows in %.1f s", output, table.num_rows, monotonic() - started
    )

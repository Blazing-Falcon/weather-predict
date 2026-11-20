import math
from datetime import UTC, datetime, timedelta

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from nowcast.cloud import CLOUD_PARTS
from nowcast.storage import month_path
from nowcast.timeslots import LOCAL_OFFSETS, SLOT, SLOT_SECONDS, UTC_US, month_ranges

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


def live_features(root, pending, lag):
    """Compute the Spark feature contract for pending live issue slots."""
    first, last = min(pending), max(pending)
    anchor = available_imerg_slot(first, lag)
    end_anchor = available_imerg_slot(last, lag)
    tables = {}
    for name, part, start, stop, columns in (
        ("rain", "part.parquet", min(first, anchor - 47 * SLOT), end_anchor + SLOT,
         ["rain_mean_x10", "rain_max_x10", "wet_pct"]),
        ("cloud", CLOUD_PARTS[2], first - 2 * SLOT, last + SLOT,
         ["source", "cold_pct", "tb_min", "tb_mean_x10", "adv1_pct", "adv2_pct",
          "adv3_pct", "flow_kmh"]),
    ):
        schema = pa.schema([("slot", UTC_US), ("area_id", pa.int16()),
                            *[(column, pa.int16()) for column in columns]])
        parts = []
        for month, _ in month_ranges(start, stop):
            path = month_path(root / "silver" / f"{name}_kabkota", month, part)
            if path.is_file():
                parts.append(pq.read_table(
                    path, partitioning=None, columns=schema.names,
                    filters=[("slot", ">=", start), ("slot", "<", stop)],
                ).cast(schema))
        tables[name + "_input"] = pa.concat_tables(parts) if parts else schema.empty_table()
    tables["issues"] = pa.table({
        "t": pa.array([int(t.timestamp()) // SLOT_SECONDS for t in pending], pa.int64()),
        "imerg_slot": pa.array([
            int(available_imerg_slot(t, lag).timestamp()) // SLOT_SECONDS for t in pending
        ], pa.int64()),
    })
    tables["areas"] = pq.read_table(root / "ref" / "dim_area.parquet", columns=[
        "area_id", "area_km2", "island", "tz", "is_admin",
    ])
    tables["edges"] = pq.read_table(root / "ref" / "neighbours.parquet", columns=[
        "area_id", "neighbour_id",
    ])
    tables["climate"] = pq.read_table(root / "gold" / "climatology" / "part.parquet")
    zone = "CASE tz " + " ".join(
        f"WHEN '{name}' THEN {offset}" for name, offset in LOCAL_OFFSETS.items()
    ) + " END"
    with duckdb.connect(config={"threads": 1}) as connection:
        for name, table in tables.items():
            connection.register(name, table)
        table = connection.sql(f"""
            WITH rain AS (
                SELECT floor(epoch(slot) / {SLOT_SECONDS})::BIGINT AS slot,
                       * EXCLUDE(slot) FROM rain_input
            ), cloud AS (
                SELECT floor(epoch(slot) / {SLOT_SECONDS})::BIGINT AS slot,
                       * EXCLUDE(slot) FROM cloud_input WHERE source = 2
            ), anchors AS (
                SELECT DISTINCT imerg_slot AS slot, area_id
                FROM issues CROSS JOIN areas WHERE is_admin
            ), history AS (
                SELECT slot, area_id, rain_mean_x10, rain_max_x10, wet_pct
                FROM anchors LEFT JOIN rain USING(slot, area_id)
                UNION ALL
                SELECT * FROM rain WHERE slot NOT IN (SELECT imerg_slot FROM issues)
            ), rolling AS (
                SELECT slot AS imerg_slot, area_id,
                       rain_mean_x10 / 10.0 AS rain_lag, wet_pct AS wet_lag,
                       rain_max_x10 / 10.0 AS rainmax_lag,
                       sum(rain_mean_x10) OVER (
                         PARTITION BY area_id ORDER BY slot
                         RANGE BETWEEN 5 PRECEDING AND CURRENT ROW) / 20.0 AS rain_sum_3h,
                       sum(rain_mean_x10) OVER (
                         PARTITION BY area_id ORDER BY slot
                         RANGE BETWEEN 11 PRECEDING AND CURRENT ROW) / 20.0 AS rain_sum_6h,
                       sum(rain_mean_x10) OVER (
                         PARTITION BY area_id ORDER BY slot
                         RANGE BETWEEN 47 PRECEDING AND CURRENT ROW) / 20.0 AS rain_sum_24h
                FROM history
            ), cloud_now AS (
                SELECT slot AS t, area_id, cold_pct, tb_min, tb_mean_x10 / 10.0 AS tb_mean,
                       adv1_pct, adv2_pct, adv3_pct,
                       greatest(adv1_pct, adv2_pct, adv3_pct) AS adv_max_pct, flow_kmh,
                       cold_pct - max(cold_pct) OVER (
                         PARTITION BY area_id ORDER BY slot
                         RANGE BETWEEN 2 PRECEDING AND 2 PRECEDING) AS cold_trend_1h
                FROM cloud
            ), nb_cloud AS (
                SELECT slot AS t, edges.area_id, avg(cold_pct) AS nb_cold_pct
                FROM cloud JOIN edges ON cloud.area_id = edges.neighbour_id
                GROUP BY slot, edges.area_id
            ), nb_rain AS (
                SELECT slot AS imerg_slot, edges.area_id,
                       avg(rain_mean_x10) / 10.0 AS nb_rain_lag
                FROM rain JOIN edges ON rain.area_id = edges.neighbour_id
                GROUP BY slot, edges.area_id
            ), grid AS (
                SELECT *, make_timestamp(t * {SLOT_SECONDS} * 1000000)
                       + ({zone}) * INTERVAL '1 hour' AS local
                FROM issues CROSS JOIN areas WHERE is_admin
            )
            SELECT grid.t, grid.area_id, grid.imerg_slot, grid.t AS cloud_slot,
                   cold_pct, tb_min, tb_mean, cold_trend_1h, nb_cold_pct,
                   adv1_pct, adv2_pct, adv3_pct, adv_max_pct, flow_kmh,
                   rain_lag, wet_lag, rainmax_lag, rain_sum_3h, rain_sum_6h,
                   rain_sum_24h, nb_rain_lag,
                   (hour(local) + minute(local) / 60.0) * {math.tau / 24} AS hour_angle,
                   (dayofyear(local) - 1) * {math.tau / 365.25} AS year_angle,
                   area_km2, island, climate.wet_pct AS clim_wet_pct,
                   p1 AS clim_p1, p2 AS clim_p2, p3 AS clim_p3
            FROM grid LEFT JOIN rolling USING(imerg_slot, area_id)
            LEFT JOIN nb_rain USING(imerg_slot, area_id)
            LEFT JOIN cloud_now USING(t, area_id)
            LEFT JOIN nb_cloud USING(t, area_id)
            LEFT JOIN climate ON grid.area_id = climate.area_id
                AND month(local) = climate.month AND hour(local) = climate.hour_local
        """).to_arrow_table()
    frame = table.to_pandas()
    for prefix, angle in (("hour", "hour_angle"), ("doy", "year_angle")):
        frame[prefix + "_sin"] = frame[angle].map(math.sin)
        frame[prefix + "_cos"] = frame[angle].map(math.cos)
    for key in ("t", "imerg_slot", "cloud_slot"):
        frame[key] = pa.array(frame[key] * SLOT_SECONDS * 1000000, UTC_US).to_pandas()
    for name in FEATURES:
        frame[name] = frame[name].astype("category" if name == "island" else "float32")
    return frame[KEYS + FEATURES]

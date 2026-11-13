import json
from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import duckdb

from nowcast import config
from nowcast.model.bmkg_baseline import STEP_BLOCK, step_offset
from nowcast.storage import month_path

AREA_COUNT = 519
RAIN = "silver/rain_kabkota"
CLOUD = "silver/cloud_kabkota"
BMKG = "silver/bmkg_forecast"
GAUGE = "silver/gauge_obs"
PREDICTIONS = "gold/predictions"
VERIFICATION = "gold/verification"
SCORES = "gold/scores_daily"
FRAMES = "gold/map_frames"
CLIMATOLOGY = "gold/climatology"
AREAS = "ref/dim_area.parquet"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


@lru_cache(maxsize=1)
def database(root: Path):
    connection = duckdb.connect(config={"threads": "2"})
    connection.execute("SET allowed_directories = ?", [[str(root.resolve())]])
    connection.execute("SET enable_external_access = false")
    return connection


class Archive:
    def __init__(self, root: Path, connection: duckdb.DuckDBPyConnection):
        self.root = root
        self.connection = connection

    def files(self, dataset, first=None, last=None):
        path = self.root / dataset
        if path.suffix == ".parquet":
            return [str(path)] if path.is_file() else []
        candidates = path.rglob("*.parquet")
        if first is not None:
            # Prune by the on-disk partition before DuckDB reads the Parquet footers.
            if dataset == FRAMES:
                begin, end = (f"date={stamp.date()}" for stamp in (first, last))
                candidates = (
                    file
                    for file in candidates
                    if file.parent == path or begin <= file.parent.name <= end
                )
            else:
                begin, end = (
                    month_path(Path(), stamp).as_posix() for stamp in (first, last)
                )
                candidates = (
                    file
                    for file in candidates
                    if file.parent == path
                    or begin <= file.parent.relative_to(path).as_posix() <= end
                )
        return sorted(str(file) for file in candidates if not file.name.startswith("_"))

    def rows(self, dataset, sql, params=(), *, first=None, last=None, files=None):
        files = self.files(dataset, first, last) if files is None else files
        if not files:
            return []
        query = "WITH data AS (SELECT * FROM read_parquet(?, hive_partitioning=true, union_by_name=true)) "
        try:
            result = self.connection.execute(query + sql, [files, *params])
            columns = [column[0] for column in result.description]
            return [dict(zip(columns, row)) for row in result.fetchall()]
        except duckdb.IOException:
            # Retention can remove a partition between discovery and opening it.
            remaining = [file for file in files if Path(file).is_file()]
            if len(remaining) == len(files):
                raise
            return self.rows(dataset, sql, params, files=remaining)

    def latest(self, dataset, column, where=""):
        months = defaultdict(list)
        for file in self.files(dataset):
            months[str(Path(file).parent)].append(file)
        for month in sorted(months, reverse=True):
            rows = self.rows(
                dataset,
                f"SELECT max({column}) AS latest FROM data {where}",
                files=months[month],
            )
            if rows and rows[0]["latest"] is not None:
                return rows[0]["latest"]
        return None

    def areas(self, area_id=None):
        where = "" if area_id is None else "WHERE area_id = ?"
        params = () if area_id is None else (area_id,)
        return self.rows(AREAS, f"SELECT * FROM data {where} ORDER BY area_id", params)

    def frames(self, layer, first, last):
        # One value list per slot, indexed by area_id, with nulls for areas without a value.
        rows = self.rows(
            FRAMES,
            f"""SELECT strftime(s.slot AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S+00:00') AS slot,
                list(d.value ORDER BY a.area_id) AS "values"
            FROM (SELECT DISTINCT slot FROM data WHERE layer = ? AND slot BETWEEN ? AND ?) s
            CROSS JOIN range({AREA_COUNT}) a(area_id)
            LEFT JOIN data d ON d.layer = ? AND d.slot = s.slot AND d.area_id = a.area_id
            GROUP BY s.slot ORDER BY s.slot""",
            (layer, first, last, layer),
            first=first,
            last=last,
        )
        return {
            "layer": layer,
            "slots": [row["slot"] for row in rows],
            "values": [row["values"] for row in rows],
        }

    def predictions(self, first, last, area_id=None):
        where = "" if area_id is None else "AND area_id = ?"
        params = (first, last) if area_id is None else (first, last, area_id)
        return self.rows(
            PREDICTIONS,
            f""", versions AS (
                SELECT *, max(model_version) OVER (PARTITION BY t) AS newest_version
                FROM data WHERE t BETWEEN ? AND ?
            )
            SELECT t AS slot, issued_at, area_id, lead_h, prob_pct, model_version, cloud_slot
            FROM versions WHERE model_version = newest_version AND lead_h BETWEEN 1 AND 3
            {where} ORDER BY slot, area_id, lead_h""",
            params,
            first=first,
            last=last,
        )

    def bmkg(self, first, last, area_id=None, *, end=None):
        start = first - timedelta(days=4)
        settings = config.settings()["bmkg"]
        offset = step_offset(settings["step_alignment"])
        where = "" if area_id is None else "AND area_id = ?"
        params = (start, last) if area_id is None else (start, last, area_id)
        sql = (
            "SELECT area_id, analysis_time, first_seen, valid_time, weather "
            f"FROM data WHERE analysis_time BETWEEN ? AND ? {where}"
        )
        if first == last:
            # Pick the newest run before filtering its steps to the requested window.
            upper = "<=" if end is None else "<"
            sql = (
                f"SELECT * FROM ({sql} AND first_seen <= ?"
                " QUALIFY analysis_time = max(analysis_time) OVER (PARTITION BY area_id))"
                f" WHERE valid_time + ? {upper} ? AND valid_time + ? > ?"
            )
            params = (*params, first, offset, end or first, offset, first - STEP_BLOCK)
        rows = self.rows(BMKG, sql + " ORDER BY analysis_time DESC", params, first=start, last=last)
        runs = defaultdict(dict)
        for row in rows:
            run = runs[row["area_id"]].setdefault(row["analysis_time"], (row["first_seen"], {}))
            run[1][row["valid_time"] + offset] = row["weather"]
        return runs

    def cloud(self, first, last, area_id=None):
        where = "" if area_id is None else "AND area_id = ?"
        params = (first, last) if area_id is None else (first, last, area_id)
        return self.rows(
            CLOUD,
            "SELECT slot, area_id, source, cold_pct, tb_min FROM data "
            f"WHERE slot BETWEEN ? AND ? {where} ORDER BY slot, source",
            params,
            first=first,
            last=last,
        )

    def scores(self, now, days, scope=None, scope_id=None, lead=None):
        first = date.fromordinal(max(1, now.date().toordinal() - days + 1))
        where = ["day BETWEEN ? AND ?"]
        params = [first, now.date()]
        for column, value in (("scope", scope), ("scope_id", scope_id), ("lead_h", lead)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        return self.rows(
            SCORES,
            "SELECT day, scope, scope_id, lead_h, source, n, brier, bss, csi, pod, far, "
            f"base_rate FROM data WHERE {' AND '.join(where)} "
            "ORDER BY day, scope, scope_id, lead_h, source",
            params,
            first=first,
            last=now.date(),
        )

    def climatology(self, area_id):
        return self.rows(
            CLIMATOLOGY,
            "SELECT month, hour_local, wet_pct, p1, p2, p3 "
            "FROM data WHERE area_id = ? ORDER BY month, hour_local",
            (area_id,),
        )



from collections import defaultdict
from datetime import timedelta

from nowcast import config
from nowcast.storage import month_path, read_rows
from nowcast.timeslots import month_ranges

STEP_BLOCK = timedelta(hours=3)
MAX_LEAD = timedelta(hours=96)
RUN_LOOKBACK = MAX_LEAD + STEP_BLOCK
STEP_OFFSETS = {"before": -STEP_BLOCK, "after": timedelta(0), "centred": -STEP_BLOCK / 2}


def step_offset(alignment):
    return STEP_OFFSETS[alignment]


def bmkg_rain(weather, rain_code=None):
    if weather is None:
        return None
    if rain_code is None:
        rain_code = config.settings()["bmkg"]["rain_codes_min"]
    return rain_code <= weather <= 99


def read_runs(root, first, stop, alignment, *, as_of=None):
    start = first - RUN_LOOKBACK
    last = max(stop, as_of or stop)
    runs = defaultdict(dict)
    offset = step_offset(alignment)
    for month, _ in month_ranges(start, last + timedelta(microseconds=1)):
        part = month_path(root / "silver" / "bmkg_forecast", month, "part.parquet")
        for row in read_rows(
            part,
            columns=["area_id", "analysis_time", "first_seen", "valid_time", "weather"],
            filters=[("analysis_time", ">=", start), ("analysis_time", "<=", last)],
        ):
            run = runs[row["area_id"]].setdefault(row["analysis_time"], (row["first_seen"], {}))
            run[1][row["valid_time"] + offset] = row["weather"]
    return {area: dict(sorted(by_time.items(), reverse=True)) for area, by_time in runs.items()}


def newest_run(runs, as_of):
    for analysis, (first_seen, steps) in runs.items():
        if first_seen <= as_of:
            return analysis, first_seen, steps
    return None, None, {}


def step_at(steps, slot):
    for start, weather in steps.items():
        if start <= slot < start + STEP_BLOCK:
            return start, weather
    return None, None


def weather_at(runs, slot, as_of):
    analysis, first_seen, steps = newest_run(runs, as_of)
    _, weather = step_at(steps, slot)
    if weather is not None:
        return {"weather": weather, "analysis_time": analysis, "first_seen": first_seen}
    return {"weather": None, "analysis_time": None, "first_seen": None}


def bmkg_result(runs, issued_at, t, end, rain_code):
    analysis, _, steps = newest_run(runs, issued_at)
    covered = [weather for start, weather in steps.items() if start < end and start + STEP_BLOCK > t]
    if not covered:
        return None, None
    return any(bmkg_rain(weather, rain_code) for weather in covered), analysis

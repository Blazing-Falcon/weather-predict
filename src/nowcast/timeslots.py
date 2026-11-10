from datetime import UTC, datetime, timedelta

import pyarrow as pa

SLOT = timedelta(minutes=30)
SLOT_SECONDS = int(SLOT.total_seconds())
HOUR = timedelta(hours=1)
UTC_US = pa.timestamp("us", tz="UTC")
LOCAL_OFFSETS = {"WIB": 7, "WITA": 8, "WIT": 9}


def next_month(month: datetime) -> datetime:
    return (month + timedelta(days=32)).replace(day=1)


def parse_month(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC)


def utc_text(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def utc_now() -> str:
    return utc_text(datetime.now(UTC))


def date_range(start: str, end: str) -> tuple[datetime, datetime]:
    first, last = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) for day in (start, end))
    if first > last:
        raise ValueError("Start date must be on or before end date")
    return first, last + timedelta(days=1)


def month_ranges(first: datetime, stop: datetime):
    while first < stop:
        month_start = datetime(first.year, first.month, 1, tzinfo=UTC)
        following = next_month(month_start)
        yield first, min(stop, following)
        first = following


def slot_range(start: datetime, end: datetime) -> list[datetime]:
    return [start + i * SLOT for i in range((end - start) // SLOT)]

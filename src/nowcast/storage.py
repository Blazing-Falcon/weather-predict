import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from nowcast.timeslots import month_ranges

# Temp files are created as 0600; published files get the mode a plain open() would give.
UMASK = os.umask(0)
os.umask(UMASK)


@contextmanager
def atomic_write(path: Path):
    """Yield a binary temp file and publish it after the writer succeeds."""
    with NamedTemporaryFile(dir=path.parent, prefix=".atomic-", suffix=".tmp", delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), 0o666 & ~UMASK)
            yield file
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temp.unlink(missing_ok=True)


def write_bytes(path: Path, content: bytes) -> None:
    with atomic_write(path) as file:
        file.write(content)


def write_json(path: Path, values: dict, *, indent: int | None = 2, allow_nan: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes(path, (json.dumps(values, indent=indent, allow_nan=allow_nan) + "\n").encode("utf-8"))


def write_table(table: pa.Table, path: Path) -> None:
    with atomic_write(path) as file:
        pq.write_table(table, file, compression="zstd")


def month_path(directory: Path, month: date | datetime, part: str | None = None) -> Path:
    path = directory / f"year={month:%Y}" / f"month={month:%m}"
    return path / part if part else path


def read_rows(path, **options):
    return pq.read_table(path, partitioning=None, **options).to_pylist() if path.is_file() else []


def read_slots(directory, first, stop, columns, part="part.parquet", filters=()):
    for month, _ in month_ranges(first, stop):
        yield from read_rows(
            month_path(directory, month, part),
            columns=columns,
            filters=[("slot", ">=", first), ("slot", "<", stop), *filters],
        )


def scaled(values: NDArray, scale: int, dtype: type) -> pa.Array:
    missing = np.isnan(values)
    limits = np.iinfo(dtype)
    numbers = np.clip(np.rint(np.where(missing, 0, values) * scale), limits.min, limits.max)
    return pa.array(numbers.astype(dtype), mask=missing)

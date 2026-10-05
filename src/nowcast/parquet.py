import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def write_table(table: pa.Table, path: Path) -> None:
    temp = path.with_name(f"{path.name}.tmp")
    target = temp.open("xb")
    try:
        with target:
            pq.write_table(table, target, compression="zstd")
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

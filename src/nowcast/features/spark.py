import shutil
from contextlib import contextmanager
from pathlib import Path

from pyspark.sql import SparkSession

from nowcast.locks import directory_lock


@contextmanager
def spark_session(root: Path):
    scratch = root / "tmp" / "spark"
    scratch.mkdir(parents=True, exist_ok=True)
    with directory_lock(scratch):
        spark = None
        try:
            spark = (
                SparkSession.builder.master("local[3]")
                .appName("nowcast features")
                .config("spark.driver.memory", "3g")
                .config("spark.sql.shuffle.partitions", "16")
                .config("spark.local.dir", str(scratch))
                .config("spark.sql.session.timeZone", "UTC")
                .config("spark.sql.parquet.compression.codec", "zstd")
                .config("spark.sql.parquet.outputTimestampType", "TIMESTAMP_MICROS")
                .config("spark.ui.enabled", "false")
                .config("spark.ui.showConsoleProgress", "false")
                .getOrCreate()
            )
            spark.sparkContext.setLogLevel("WARN")
            yield spark
        finally:
            try:
                if spark is not None:
                    spark.stop()
            finally:
                for entry in scratch.iterdir():
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()

"""Compare an isolated live window with the training feature implementation."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True

import numpy as np
import pyarrow.parquet as pq

from nowcast import config
from nowcast.cloud import CLOUD_PARTS
from nowcast.features.live import FEATURES, KEYS, available_imerg_slot, live_features
from nowcast.model.score import issue_slots
from nowcast.timeslots import SLOT, month_ranges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--slots", type=int, default=12)
    args = parser.parse_args()
    if args.slots < 1:
        parser.error("--slots must be positive")
    source = args.source.resolve()
    model = config.settings()["model"]
    lag = int(config.env("IMERG_LAG_MINUTES", str(model["imerg_lag_minutes"])))
    pending = []
    for part in sorted((source / "silver" / "cloud_kabkota").glob(
        f"year=*/month=*/{CLOUD_PARTS[2]}"
    ), reverse=True):
        slots = pq.read_table(part, partitioning=None, columns=["slot"],
                              filters=[("source", "=", 2)])["slot"].to_pylist()
        if slots:
            pending = sorted(set(pending) | set(issue_slots(source, min(slots), max(slots))))
        if len(pending) >= args.slots:
            break
    pending = pending[-args.slots:]
    if not pending:
        parser.error("source has no eligible Himawari issue slots")
    scratch_parent = source / "tmp" / "parity"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="features-", dir=scratch_parent) as directory:
        root = Path(directory)
        for name in ("ref", "gold/climatology"):
            shutil.copytree(source / name, root / name)
        version = model["live_version"]
        model_dir = root / "models" / version
        model_dir.mkdir(parents=True)
        for part in (source / "models" / version).iterdir():
            if part.name == "card.json" or part.match("model_lead*.txt") or part.match(
                "isotonic_lead*.json"
            ):
                shutil.copy2(part, model_dir / part.name)
        first, last = pending[0], pending[-1]
        for name, part, start, stop in (
            ("rain_kabkota", "part.parquet",
             min(first, available_imerg_slot(first, lag) - 47 * SLOT),
             max(last + 6 * SLOT, available_imerg_slot(last, lag) + SLOT)),
            ("cloud_kabkota", CLOUD_PARTS[2], first - 2 * SLOT, last + SLOT),
        ):
            for month, _ in month_ranges(start, stop):
                relative = Path("silver") / name / f"year={month:%Y}" / f"month={month:%m}" / part
                if (source / relative).is_file():
                    (root / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / relative, root / relative)
        print(f"issue_slots={len(pending)} first={first.isoformat()} last={last.isoformat()}",
              flush=True)
        env = os.environ | {
            "TMPDIR": str(root), "PYTHONDONTWRITEBYTECODE": "1",
            "JAVA_TOOL_OPTIONS": os.environ.get("JAVA_TOOL_OPTIONS", "")
            + f" -Djava.io.tmpdir={root} -XX:-UsePerfData",
        }
        subprocess.run([
            "/usr/bin/time", "-f", "peak_rss_kb=%M wall_seconds=%e",
            sys.executable, "-c",
            ("import sys, time; from pathlib import Path; from datetime import datetime; "
            "from nowcast.model.score import predict; start=time.perf_counter(); "
            "table, _ = predict(Path(sys.argv[1]), [datetime.fromisoformat(sys.argv[2])]); "
            "print(f'prediction_rows={len(table)} feature_prediction_seconds={time.perf_counter()-start:.6f}',"
            " flush=True); assert not any(n.startswith('pyspark') for n in sys.modules)"),
            str(root), last.isoformat(),
        ], check=True, env=env, cwd=root)
        actual = live_features(root, pending, lag).sort_values(KEYS).reset_index(drop=True)
        # JVM, Spark warehouse, and temporary files must remain inside the copied root.
        previous = Path.cwd()
        previous_tmp = tempfile.tempdir
        previous_env = {key: os.environ.get(key) for key in ("TMPDIR", "JAVA_TOOL_OPTIONS")}
        os.chdir(root)
        tempfile.tempdir = str(root)
        os.environ.update({key: env[key] for key in previous_env})
        try:
            from nowcast.features.rows import admin_areas, feature_rows, read_climatology
            from nowcast.features.spark import spark_session

            with spark_session(root) as spark:
                rows = feature_rows(
                    spark, root, admin_areas(spark, root), read_climatology(spark, root),
                    first, last + SLOT, lag, 1, cloud_source=2,
                )
                expected = rows.where(rows.t.isin(pending)).select(*KEYS, *FEATURES).toArrow()
            expected = expected.to_pandas(categories=["island"]).sort_values(KEYS)
            expected = expected.reset_index(drop=True)
        finally:
            os.chdir(previous)
            tempfile.tempdir = previous_tmp
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        left = Counter(actual[KEYS].itertuples(index=False, name=None))
        right = Counter(expected[KEYS].itertuples(index=False, name=None))
        key_mismatches = sum((left - right).values()) + sum((right - left).values())
        print(f"row_key_mismatches={key_mismatches}")
        failed = bool(key_mismatches)
        if not key_mismatches:
            for feature in FEATURES:
                a, b = actual[feature], expected[feature]
                nulls = int((a.isna() != b.isna()).sum())
                valid = a.notna() & b.notna()
                if feature == "island":
                    difference = int((a[valid].astype(str) != b[valid].astype(str)).sum())
                else:
                    difference = float(np.max(np.abs(
                        a[valid].to_numpy(dtype=float) - b[valid].to_numpy(dtype=float)
                    ), initial=0))
                dtype_match = a.dtype == b.dtype
                print(f"{feature}: max_abs_diff={difference:.12g} "
                      f"nan_mismatches={nulls} dtype_match={dtype_match}")
                failed |= difference > 1e-9 or nulls != 0 or not dtype_match
        print("FAIL" if failed else "PASS", flush=True)
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())

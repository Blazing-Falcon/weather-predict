import json
import re
from calendar import monthrange
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from math import isfinite
from os import stat_result
from pathlib import Path
from shutil import rmtree
from stat import S_ISDIR, S_ISREG
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer

from nowcast import config, paths
from nowcast.log import get_logger

PARTITIONS = (
    (r"run=(\d{4}-\d{2}-\d{2}T\d{2})", "%Y-%m-%dT%H", "run"),
    (r"date=(\d{4}-\d{2}-\d{2}/hour=\d{2})", "%Y-%m-%d/hour=%H", "hour"),
    (r"date=(\d{4}-\d{2}-\d{2})", "%Y-%m-%d", "day"),
    (r"year=(\d{4}/month=\d{2})", "%Y/month=%m", "month"),
    (r"year=(\d{4})", "%Y", "year"),
)
DAGSTER_DATASETS = {"dagster_runs", "dagster_compute_logs"}


def _shift(stamp: datetime, count: int, unit: str) -> datetime:
    if unit in {"hour", "day"}:
        return stamp + timedelta(**{f"{unit}s": count})
    months = count * 12 if unit == "year" else count
    year, month = divmod(stamp.year * 12 + stamp.month - 1 + months, 12)
    day = min(stamp.day, monthrange(year, month + 1)[1])
    return stamp.replace(year=year, month=month + 1, day=day)


def _partition(directory: Path) -> tuple[datetime, datetime] | None:
    for pattern, fmt, unit in PARTITIONS:
        name = directory.name
        if unit in {"hour", "month"}:
            name = f"{directory.parent.name}/{name}"
        if match := re.fullmatch(pattern, name):
            start = datetime.strptime(match[1], fmt).replace(tzinfo=UTC)
            return start, start if unit == "run" else _shift(start, 1, unit)
    return None


def _write_health(root: Path, name: str, report: dict) -> None:
    target = root / "state" / "health" / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, delete_on_close=False
    ) as temp:
        json.dump(report, temp, indent=2)
        temp.write("\n")
        temp.close()
        Path(temp.name).replace(target)


def _size(directory: Path) -> int:
    return sum(
        stat.st_size
        for parent, _, files in directory.walk(on_error=_raise_walk_error)
        for name in files
        if (stat := _entry_stat(parent / name)) is not None and S_ISREG(stat.st_mode)
    )


def _raise_walk_error(error: OSError) -> None:
    raise error


def _entry_stat(entry: Path) -> stat_result | None:
    try:
        return entry.lstat()
    except FileNotFoundError:
        return None


def _expire(
    dataset: Path,
    nested_datasets: set[Path],
    now: datetime,
    window: str,
    delete: Callable[[Path, int, Callable[[], None]], None],
) -> None:
    amount, unit = window.split()
    count, unit = int(amount), unit.removesuffix("s")
    cutoff = _shift(now, -count, unit)
    cutoff_mtime = cutoff.timestamp()

    def visit(directory: Path, use_mtime: bool) -> tuple[int, bool] | None:
        if directory in nested_datasets:
            return 0, True

        bounds = _partition(directory) if directory != dataset else None
        expired = fallback = False
        if bounds is not None:
            start, end = bounds
            fallback = start < _shift(end, -count, unit)
            use_mtime = fallback
            expired = not fallback and end <= cutoff

        remaining = []
        for entry in directory.iterdir():
            stat = _entry_stat(entry)
            if stat is None:
                continue
            if S_ISDIR(stat.st_mode):
                if (result := visit(entry, use_mtime)) is not None:
                    remaining.append(result)
            elif S_ISREG(stat.st_mode):
                if use_mtime and stat.st_mtime <= cutoff_mtime:
                    delete(entry, stat.st_size, entry.unlink)
                else:
                    remaining.append((stat.st_size, False))
            else:
                remaining.append((0, False))

        size = sum(size for size, _ in remaining)
        kept_partition = any(kept for _, kept in remaining)
        # A retained inner partition takes precedence over its enclosing partition.
        if expired and not kept_partition:
            delete(directory, size, partial(rmtree, directory))
        elif fallback and not remaining:
            delete(directory, 0, directory.rmdir)
        else:
            return size, kept_partition or bounds is not None
        return None

    visit(dataset, use_mtime=True)


def retention(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    now: Annotated[str | None, typer.Option(help="UTC run time in ISO format.")] = None,
) -> None:
    """Delete data past its retention window."""
    if now is None:
        stamp = datetime.now(UTC)
    else:
        try:
            stamp = datetime.fromisoformat(now)
        except ValueError as error:
            raise typer.BadParameter(
                "Expected an ISO date or timestamp.", param_hint="--now"
            ) from error
        stamp = (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).astimezone(UTC)

    root = paths.data_root().resolve()
    datasets = config.retention()["datasets"]
    dataset_paths = {root / name for name in datasets}
    logger = get_logger(__name__)

    def delete(stats: dict, path: Path, size: int, remove: Callable[[], None]) -> None:
        if not dry_run:
            remove()
        logger.info("%s %s (%d bytes)", "Would delete" if dry_run else "Deleted", path, size)
        stats["deleted_count"] += 1
        stats["bytes_freed"] += size

    results = {}
    for name, policy in datasets.items():
        dataset = root / name
        stats = {"deleted_count": 0, "bytes_freed": 0}
        if name in DAGSTER_DATASETS:
            logger.info("%s: handled by the Dagster cleanup job", name)
            stats["skipped"] = True
        elif dataset.is_dir() and dataset.resolve() == dataset:
            if "keep_versions" in policy:
                versions = sorted(
                    (
                        entry
                        for entry in dataset.iterdir()
                        if entry.is_dir() and not entry.is_symlink()
                    ),
                    key=lambda entry: (entry.stat().st_mtime_ns, entry.name),
                    reverse=True,
                )
                for version in versions[policy["keep_versions"] :]:
                    delete(stats, version, _size(version), partial(rmtree, version))
            else:
                _expire(
                    dataset,
                    dataset_paths - {dataset},
                    stamp,
                    policy["keep"],
                    partial(delete, stats),
                )

        logger.info(
            "%s: %s %d, %s %d bytes",
            name,
            "would delete" if dry_run else "deleted",
            stats["deleted_count"],
            "would free" if dry_run else "freed",
            stats["bytes_freed"],
        )
        results[name] = stats

    _write_health(
        root,
        "retention",
        {
            "run_at": stamp.isoformat(),
            "dry_run": dry_run,
            "datasets": results,
        },
    )


def disk(
    budget_gb: Annotated[float | None, typer.Option(help="Disk budget in GB.")] = None,
) -> None:
    """Measure dataset sizes and check the disk budget."""
    report = disk_usage(budget_gb)
    sizes = report["datasets"]
    width = max(len(name) for name in sizes)
    typer.echo(f"{'Dataset':<{width}}  {'Size (MB)':>10}")
    for name, size in sizes.items():
        typer.echo(f"{name:<{width}}  {size / 1_000_000:10.3f}")
    typer.echo(f"TOTAL {report['total_bytes'] / 1_000_000_000:.6f} GB of {report['budget_gb']:g} GB")
    _write_health(paths.data_root(), "disk", report)
    if report["budget_exceeded"]:
        typer.echo("FAIL disk budget exceeded")
        raise typer.Exit(1)


def disk_usage(budget_gb: float | None = None) -> dict:
    root = paths.data_root()
    settings = config.retention()
    budget = settings["budget_gb"] if budget_gb is None else budget_gb
    if not isfinite(budget) or budget < 0:
        raise typer.BadParameter(
            "Expected a finite, non-negative budget.", param_hint="--budget-gb"
        )

    buckets = {root / name: name for name in settings["datasets"] if (root / name).is_dir()}
    buckets.update({root / name: name for name in ("ref", "state")})
    sizes = dict.fromkeys([*buckets.values(), "other"], 0)
    if root.is_dir():
        for directory, subdirs, files in root.walk(on_error=_raise_walk_error):
            if directory == root and "tmp" in subdirs:
                subdirs.remove("tmp")
            bucket = next(
                (
                    buckets[parent]
                    for parent in (directory, *directory.parents)
                    if parent in buckets
                ),
                "other",
            )
            for name in files:
                if (stat := _entry_stat(directory / name)) is not None and S_ISREG(stat.st_mode):
                    sizes[bucket] += stat.st_size

    total = sum(sizes.values())
    return {
        "run_at": datetime.now(UTC).isoformat(),
        "datasets": sizes,
        "total_bytes": total,
        "budget_gb": budget,
        "budget_exceeded": total / 1_000_000_000 > budget,
    }

import stat
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nowcast import storage
from nowcast.storage import atomic_write, write_table


def test_stale_temps_do_not_block_write(tmp_path):
    path = tmp_path / "part.parquet"
    stale = [tmp_path / "part.parquet.tmp", tmp_path / ".atomic-crashed.tmp"]
    for temp in stale:
        temp.write_bytes(b"partial")
    table = pa.table({"value": [1, 2]})

    write_table(table, path)

    assert pq.ParquetFile(path).read() == table
    assert set(tmp_path.iterdir()) == {path, *stale}
    assert all(temp.read_bytes() == b"partial" for temp in stale)


def test_failed_write_preserves_target_and_removes_temp(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"old")

    with pytest.raises(RuntimeError, match="failed"), atomic_write(path) as file:
        file.write(b"partial")
        raise RuntimeError("failed")

    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_successful_write_publishes_and_removes_temp(tmp_path):
    path = tmp_path / "frame.npz"
    path.write_bytes(b"old")

    with atomic_write(path) as file:
        np.savez(file, value=[1, 2])
        assert path.read_bytes() == b"old"

    with np.load(path) as saved:
        assert saved["value"].tolist() == [1, 2]
    assert list(tmp_path.iterdir()) == [path]


def test_published_file_has_umask_mode(tmp_path):
    plain, published = tmp_path / "plain.json", tmp_path / "published.json"
    plain.write_bytes(b"{}")

    storage.write_bytes(published, b"{}")

    assert stat.S_IMODE(published.stat().st_mode) == stat.S_IMODE(plain.stat().st_mode)


@pytest.mark.parametrize("pattern", [
    "*.parquet", "*.nc4", "*.json", "*.jsonl.gz", "sweep=*.jsonl.gz",
    "sentinel=*.jsonl.gz", "*.png", "*.webp", "*.npz",
    "HS_H09_*_FLDK_R20_S*.DAT.bz2",
])
def test_temp_is_invisible_to_readers(tmp_path, pattern):
    directory = tmp_path / "date=2025-01-01"
    directory.mkdir()
    path = directory / "part.parquet"

    with atomic_write(path) as file:
        assert Path(file.name).parent == directory
        assert not list(directory.glob(pattern))
        assert not list(tmp_path.rglob("*.parquet"))
        assert not list(tmp_path.glob("date=*/*.parquet"))


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_failed_publication_preserves_target_and_removes_temp(tmp_path, monkeypatch, operation):
    path = tmp_path / "state.json"
    path.write_bytes(b"old")

    def fail(*args):
        raise OSError("failed")

    monkeypatch.setattr(storage.os, operation, fail)
    with pytest.raises(OSError, match="failed"), atomic_write(path) as file:
        file.write(b"new")

    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_syncs_file_before_rename_and_folder_after(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    events = []
    fsync, replace = storage.os.fsync, storage.os.replace

    def sync(fd):
        folder = stat.S_ISDIR(storage.os.fstat(fd).st_mode)
        events.append("folder" if folder else "file")
        if not folder:
            [temp] = tmp_path.iterdir()
            assert temp.read_bytes() == b"new"
        fsync(fd)

    def publish(source, target):
        events.append("rename")
        replace(source, target)

    monkeypatch.setattr(storage.os, "fsync", sync)
    monkeypatch.setattr(storage.os, "replace", publish)
    with atomic_write(path) as file:
        file.write(b"new")

    assert events == ["file", "rename", "folder"]
    assert path.read_bytes() == b"new"

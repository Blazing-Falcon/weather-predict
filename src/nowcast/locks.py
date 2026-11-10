import os
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, flock
from pathlib import Path


@contextmanager
def file_lock(folder: Path, name: str, *, shared: bool = False, blocking: bool = True):
    """Hold a named file lock and yield whether it was acquired."""
    with (folder / name).open("a") as file:
        try:
            flock(file, (LOCK_SH if shared else LOCK_EX) | (0 if blocking else LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True


@contextmanager
def directory_lock(folder: Path, *, shared: bool = False, blocking: bool = True):
    """Hold a folder lock and yield whether it was acquired."""
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            flock(directory, (LOCK_SH if shared else LOCK_EX) | (0 if blocking else LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(directory)

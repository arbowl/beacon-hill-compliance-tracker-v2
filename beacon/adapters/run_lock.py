"""Exclusive per-data-directory lock, released by the OS if the process dies."""

import os
import sys
from pathlib import Path
from types import TracebackType


class RunLockedError(RuntimeError):
    """Another beacon process holds the data directory."""


class RunLock:
    """Context manager holding an exclusive lock on data_dir/.lock."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / ".lock"
        self._fd: int | None = None

    def __enter__(self) -> "RunLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            raise RunLockedError(
                f"Another beacon run is using {self._path.parent}"
            ) from error
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

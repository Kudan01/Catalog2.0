from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class ScanLockError(RuntimeError):
    """Raised when another scan write operation owns the process lock."""


class ScanProcessLock:
    """
    Hold a cross-process lock for scan staging or activation.

    The operating system releases the lock automatically if the process exits
    unexpectedly. The small lock file is removed after a normal exit.
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._file: BinaryIO | None = None

    def __enter__(self) -> "ScanProcessLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+b")

        try:
            _ensure_lock_byte(lock_file)
            _lock_file_nonblocking(lock_file)
        except OSError as exc:
            lock_file.close()
            raise ScanLockError(
                "Jiná zapisující operace scanu už běží. Operace nebyla spuštěna."
            ) from exc

        self._file = lock_file
        _write_lock_metadata(lock_file)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        lock_file = self._file
        self._file = None

        if lock_file is None:
            return

        try:
            _unlock_file(lock_file)
        finally:
            lock_file.close()

        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            # A stale but unlocked file is harmless. The next scan can reuse it.
            pass


def scan_lock_path(db_path: Path) -> Path:
    """Return the technical process-lock path next to catalog.db."""
    return db_path.with_name(".catalog_scan.lock")


def _ensure_lock_byte(lock_file: BinaryIO) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)


def _write_lock_metadata(lock_file: BinaryIO) -> None:
    text = f"pid={os.getpid()} started_at={time.time():.6f}\n".encode("ascii")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(text)
    lock_file.flush()
    os.fsync(lock_file.fileno())
    lock_file.seek(0)


def _lock_file_nonblocking(lock_file: BinaryIO) -> None:
    lock_file.seek(0)

    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(lock_file: BinaryIO) -> None:
    lock_file.seek(0)

    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass

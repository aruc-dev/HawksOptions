"""Cross-process file locking helpers for shared state files.

The HawksOptions schedulers run on overlapping cadences (1-min, 5-min,
30-min, hourly). They all read or mutate the same JSON / CSV state
files. Without locking those reads and writes can interleave and corrupt
the file. This module provides a small, dependency-free locking helper
backed by ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows.

Usage:

    from core.file_lock import locked_open

    with locked_open(path, "r", lock="shared") as handle:
        data = handle.read()

    with locked_open(path, "w", lock="exclusive") as handle:
        handle.write(payload)

The helper falls back to a no-op lock if neither ``fcntl`` nor
``msvcrt`` is importable, so unit tests on minimal environments still
work. The lock is held for the duration of the ``with`` block.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import threading
from pathlib import Path
from typing import IO, Iterator, Literal

try:  # POSIX
    import fcntl  # type: ignore[import-not-found]

    _HAS_FCNTL = True
except Exception:  # pragma: no cover - Windows or restricted env
    _HAS_FCNTL = False

try:  # Windows fallback
    import msvcrt  # type: ignore[import-not-found]

    _HAS_MSVCRT = True
except Exception:
    _HAS_MSVCRT = False


LockMode = Literal["shared", "exclusive"]


def _acquire(handle: IO, mode: LockMode) -> None:
    if _HAS_FCNTL:
        flag = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), flag)
        return
    if _HAS_MSVCRT:  # pragma: no cover - Windows path
        # msvcrt has no shared lock primitive; fall back to exclusive.
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            pass
        return
    # No locking primitives available — best-effort no-op.


def _release(handle: IO) -> None:
    if _HAS_FCNTL:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        return
    if _HAS_MSVCRT:  # pragma: no cover - Windows path
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextlib.contextmanager
def locked_open(
    path: Path | str,
    mode: str = "r",
    *,
    lock: LockMode = "exclusive",
    encoding: str | None = "utf-8",
    newline: str | None = None,
) -> Iterator[IO]:
    """Open ``path`` with an advisory file lock held for the body."""
    path = Path(path)
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        path.parent.mkdir(parents=True, exist_ok=True)
    open_kwargs: dict = {}
    if "b" not in mode:
        open_kwargs["encoding"] = encoding
        if newline is not None:
            open_kwargs["newline"] = newline
    elif newline is not None:
        # Binary mode does not accept newline.
        pass
    handle = open(path, mode, **open_kwargs)
    try:
        _acquire(handle, lock)
        yield handle
    finally:
        try:
            _release(handle)
        finally:
            handle.close()


def atomic_write_text(path: Path | str, payload: str, *, encoding: str = "utf-8") -> None:
    """Write ``payload`` to ``path`` atomically while holding a lock.

    Uses a per-writer tmp file in the same directory + ``os.replace``
    for the rename, so readers either see the old or the new file but
    never a half-written one. The tmp filename includes the pid, the
    thread id, and a random suffix so two concurrent writers cannot
    fight over the same scratch file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    tmp = path.with_suffix(path.suffix + suffix)
    try:
        with locked_open(tmp, "w", lock="exclusive", encoding=encoding) as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - some filesystems do not support fsync
                pass
        os.replace(tmp, path)
    finally:
        # Best-effort cleanup if os.replace failed for some reason.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

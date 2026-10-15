"""Exclusive ownership of one profile by one live backend.

The lock is an operating-system byte-range lock (Windows) or flock (elsewhere)
on `<profile>/backend.lock`. The OS drops it when the owning process ends, by
normal exit or by being killed, so a lock *file* left behind proves nothing:
only a held lock does, and nobody ever deletes the file to "recover".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

# Fixed exit code and copy: the host maps this exit to "Profile already open",
# never to a crash, so it does not restart or retry by itself.
PROFILE_IN_USE_EXIT_CODE = 3
PROFILE_IN_USE_MESSAGE = "Profile already open: another PotatoCS window is using this profile."
LOCK_FILE_NAME = "backend.lock"


class ProfileInUseError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(PROFILE_IN_USE_MESSAGE)


class ProfileOwnership:
    """Held for the backend's whole lifetime; acquire before touching the profile."""

    def __init__(self, profile_dir: str | Path):
        path = Path(profile_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._file: BinaryIO = open(path / LOCK_FILE_NAME, "a+b")  # never truncates
        try:
            _lock(self._file)
        except OSError as exc:
            self._file.close()
            raise ProfileInUseError() from exc

    def release(self) -> None:
        if self._file.closed:
            return
        try:
            _unlock(self._file)
        finally:
            self._file.close()


if os.name == "nt":
    import msvcrt

    def _lock(file: BinaryIO) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)  # one attempt, no waiting

    def _unlock(file: BinaryIO) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(file: BinaryIO) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(file: BinaryIO) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)

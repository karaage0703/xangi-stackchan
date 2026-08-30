import fcntl
import hashlib
from pathlib import Path
from typing import IO


class DeviceBusyError(RuntimeError):
    pass


class DeviceLock:
    """Process-wide advisory lock shared by standalone and managed modes."""

    def __init__(self, target: str, lock_dir: Path | None = None):
        digest = hashlib.sha256(target.encode()).hexdigest()[:16]
        root = lock_dir or (Path.home() / ".xangi" / "xangi-stackchan" / "locks")
        self.path = root / f"{digest}.lock"
        self.target = target
        self._file: IO[str] | None = None

    def acquire(self) -> "DeviceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise DeviceBusyError(
                f"Stack-chan device is already in use: {self.target}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(self.target + "\n")
        handle.flush()
        self._file = handle
        return self

    def release(self) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None

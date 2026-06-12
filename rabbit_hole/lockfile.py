import msvcrt
from pathlib import Path
from typing import IO


class PipelineLockError(RuntimeError):
    pass


def acquire_lock(lock_path: str | Path) -> IO[str]:
    """Take an exclusive OS-level lock on `lock_path` and return the open handle.

    Hold the handle for the lifetime of the run; the OS releases the lock when
    the handle closes OR the process dies, so a crashed run can never leave a
    stale lock. Raises PipelineLockError if another process holds it.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a")
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as e:
        handle.close()
        raise PipelineLockError(f"another run holds {lock_path}") from e
    return handle

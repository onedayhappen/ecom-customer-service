"""跨平台进程文件锁，供 JSON 状态的多 worker 访问使用。"""

from __future__ import annotations

import os
import time
from pathlib import Path


class ProcessFileLock:
    """使用标准库实现的排他锁，不引入平台专用第三方依赖。"""

    def __init__(self, path: str | Path, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = max(0.1, float(timeout))
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        # Lock the byte range directly, including an empty file. Writing an
        # initialization byte before locking races with another Windows owner:
        # that owner's byte-range lock can reject the write/flush itself.
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(f"获取文件锁超时: {self.path}")
                time.sleep(0.02)

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


__all__ = ["ProcessFileLock"]

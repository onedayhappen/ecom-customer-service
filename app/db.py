"""共享 PostgreSQL 连接池与一次性建表守卫（进程内单例）。

动机：原实现中每次 load/save 会话或长期记忆都会新建一个 psycopg 连接，
并反复执行 ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX`` 等 DDL。
高并发下「TCP 握手 + 认证」与 DDL 元数据锁会成为吞吐瓶颈，本模块解决两件事：

1. **连接池**（:class:`PgConnectionPool`）：min-max 弹性、空闲队列复用、
   取用前健康检查（SELECT 1）、等待超时保护。不引入第三方依赖，
   基于 psycopg3 官方驱动实现，与原有 ``with conn`` 事务语义完全兼容。
2. **一次性建表守卫**（:func:`ensure_schema_once`）：同一进程内每个
   (dsn, key) 只执行一次 DDL，避免每次请求都拿元数据锁。

未配置 PostgreSQL / 未安装 psycopg 时自动降级为直连或 None，
行为与旧版一致，不影响本地 JSON 存储与离线测试。
"""

from __future__ import annotations

import atexit
import queue
import threading
import time
from typing import Callable, Optional

try:
    import psycopg
except ImportError:  # pragma: no cover - 未安装 psycopg 的环境
    psycopg = None  # type: ignore[assignment]

DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 10
DEFAULT_POOL_TIMEOUT = 5.0
DEFAULT_CONNECT_TIMEOUT = 5.0

_POOLS: dict[str, "PgConnectionPool"] = {}
_POOLS_LOCK = threading.Lock()
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_ENSURED: set[tuple[str, str]] = set()


class PoolTimeout(RuntimeError):
    """连接池在超时时间内无法提供可用连接。"""


def _new_psycopg_connection(dsn: str, connect_timeout: float):
    return psycopg.connect(dsn, connect_timeout=connect_timeout)


class PgConnectionPool:
    """进程内 PostgreSQL 连接池。

    - ``min_size`` 个连接在启动时预建，随负载弹性增长到 ``max_size``；
    - 取用前做健康检查，失效连接被丢弃并补建；
    - 等待超过 ``timeout`` 秒抛 :class:`PoolTimeout`，防止请求无限挂起。
    """

    def __init__(
        self,
        dsn: str,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        timeout: float = DEFAULT_POOL_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ):
        self._dsn = dsn
        self._max_size = max(1, int(max_size))
        self._timeout = max(0.1, float(timeout))
        self._connect_timeout = max(0.1, float(connect_timeout))
        self._idle: "queue.Queue" = queue.Queue(maxsize=self._max_size)
        self._count_lock = threading.Lock()
        self._created = 0
        self._closed = False
        # 预建 min_size 条连接；失败静默，等待按需补建。
        for _ in range(max(0, int(min_size))):
            try:
                self._idle.put_nowait(self._new_connection())
            except Exception:
                break

    # -- 对外接口 ----------------------------------------------------------

    def connection(self) -> "_PooledConnection":
        """返回可用连接；以 ``with conn`` 使用，退出时自动提交/回滚并归还。"""
        from app.runtime.metrics import POOL_WAIT, POOL_TIMEOUTS
        started = time.monotonic()
        try:
            return _PooledConnection(self, self._acquire())
        except PoolTimeout:
            POOL_TIMEOUTS.inc()
            raise
        finally:
            POOL_WAIT.observe(time.monotonic() - started)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            self._discard(conn)

    @property
    def created_count(self) -> int:
        return self._created

    @property
    def idle_count(self) -> int:
        return self._idle.qsize()

    @property
    def max_size(self) -> int:
        return self._max_size

    # -- 内部实现 ----------------------------------------------------------

    def _new_connection(self):
        """新建一条连接并计入总数；超过上限返回 None。"""
        with self._count_lock:
            if self._created >= self._max_size:
                return None
            self._created += 1
        try:
            return _new_psycopg_connection(self._dsn, self._connect_timeout)
        except Exception:
            with self._count_lock:
                self._created = max(0, self._created - 1)
            raise

    @staticmethod
    def _healthy(conn) -> bool:
        if conn is None or conn.closed:
            return False
        try:
            conn.execute("SELECT 1")
            # 探测会开启一个事务；rollback 清理，保持连接空闲态归还。
            conn.rollback()
            return True
        except Exception:
            return False

    def _acquire(self):
        deadline = time.monotonic() + self._timeout
        while not self._closed:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = None
            if conn is not None:
                if self._healthy(conn):
                    return conn
                self._discard(conn)
                continue
            # 池空：尝试新建；已达上限则短暂让出后重试。
            try:
                conn = self._new_connection()
            except Exception:
                conn = None
            if conn is not None:
                return conn
            if time.monotonic() >= deadline:
                raise PoolTimeout(
                    f"PostgreSQL 连接池等待超时（{self._timeout:.1f}s，"
                    f"最大 {self._max_size} 条连接）"
                )
            time.sleep(0.01)
        raise PoolTimeout("PostgreSQL 连接池已关闭")

    def _discard(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass
        with self._count_lock:
            self._created = max(0, self._created - 1)

    def _release(self, conn, healthy: bool) -> None:
        if self._closed:
            self._discard(conn)
            return
        if not healthy or not self._healthy(conn):
            self._discard(conn)
            return
        try:
            self._idle.put_nowait(conn)
        except queue.Full:
            self._discard(conn)


class _PooledConnection:
    """包装真实连接：``with`` 语义 = commit/rollback + 归还连接池。

    原仓库代码形如 ``with self._connect() as conn, conn.cursor() as cur:``，
    本包装保证：``__enter__`` 返回真实连接；``__exit__`` 无异常时 commit、
    有异常时 rollback，并把连接归还（健康）或丢弃（断连）。
    """

    __slots__ = ("_pool", "_conn")

    def __init__(self, pool: PgConnectionPool, conn):
        self._pool = pool
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        conn = self._conn
        healthy = not conn.closed
        try:
            if not conn.closed:
                if exc_type is None:
                    conn.commit()
                else:
                    conn.rollback()
        except Exception:
            healthy = False
            raise
        finally:
            self._pool._release(conn, healthy=healthy)
        return False


def get_connection_pool(
    dsn: str,
    min_size: int = DEFAULT_MIN_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
    timeout: float = DEFAULT_POOL_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> Optional[PgConnectionPool]:
    """按 DSN 返回进程级共享连接池；psycopg 不可用或 dsn 为空时返回 None。"""
    if psycopg is None or not dsn:
        return None
    global _POOLS
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = PgConnectionPool(
                dsn,
                min_size=min_size,
                max_size=max_size,
                timeout=timeout,
                connect_timeout=connect_timeout,
            )
            _POOLS[dsn] = pool
        return pool


def ensure_schema_once(dsn: str, key: str, ddl_fn: Callable[[object], None]) -> None:
    """同一进程内每个 (dsn, key) 只执行一次 DDL。

    ``ddl_fn(cur)`` 接收一个 psycopg cursor，在其中执行建表/建索引语句。
    使用独立直连执行（一次性成本），避免占用池连接；失败时回滚且不记录，
    允许下次重试（例如 PostgreSQL 尚未就绪的启动阶段）。
    """
    if psycopg is None or not dsn:
        return
    with _SCHEMA_LOCK:
        if (dsn, key) in _SCHEMA_ENSURED:
            return
        conn = _new_psycopg_connection(dsn, DEFAULT_CONNECT_TIMEOUT)
        try:
            with conn:
                with conn.cursor() as cur:
                    ddl_fn(cur)
            _SCHEMA_ENSURED.add((dsn, key))
        finally:
            conn.close()


def close_pools() -> None:
    """关闭全部连接池（进程退出 / lifespan shutdown 时调用）。"""
    global _POOLS
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


atexit.register(close_pools)

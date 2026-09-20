"""fork 子任务隔离（Harness 核心能力之三）：每条用例独立子进程 + 父进程超时 kill。

解决的问题：单条 Agent 测试用例触发死循环 / 卡死时，主线程被永久阻塞，
后面所有测试用例跑不完。

方案：每一条测试用例 fork 出独立子进程运行完整 Agent 会话；父进程设置
超时时间；子进程死循环 / hung 住时，父进程超时直接 kill 子进程，记录失败，
继续跑下一条用例。

隔离收益：

1. 测试用例之间进程隔离，内存、状态互不干扰；
2. 单个用例死循环不会污染整个测试套件；
3. 采集子进程资源指标（CPU、内存），发现 Agent 内存泄漏。

跨平台说明：**Windows 没有 ``os.fork()``**，本模块用 ``multiprocessing``
实现同等效果——Unix 默认 ``fork``（真 fork，子进程继承父进程内存映像）；
Windows 自动降级 ``spawn``（独立 Python 进程重新导入目标模块）。两者都是
**进程级隔离**（不是线程），父进程可 ``terminate()`` / ``kill()`` 杀掉
卡死的子进程，达到与 fork 完全相同的「单用例死循环不堵死套件」目标。

目标函数定位：``entry`` 用 ``"module.path:function_name"`` 字符串定位
（如 ``"app.harness.test_procs:normal_case"``），spawn 模式下子进程按
模块路径重新导入，不依赖 picklable 函数对象，也避免把测试模块卷进子进程。
"""

from __future__ import annotations

import importlib
import multiprocessing
import queue
import time
from dataclasses import dataclass, field
from typing import Any, Optional

#: 子进程目标函数定位格式："module.path:function_name"
Entry = str


@dataclass
class ForkResult:
    """一条用例的子进程执行结果。"""

    entry: Entry
    ok: bool = False
    timed_out: bool = False
    error: str | None = None
    duration_ms: float = 0.0
    resources: dict[str, Any] = field(default_factory=dict)
    value: Any = None


class ForkRunner:
    """子进程隔离执行器：单条用例一个进程，超时 kill，可批量跑。

    :param timeout_s: 单条用例超时阈值（秒），超时即 kill 子进程。
    :param context_method: multiprocessing 启动方式：None 用平台默认
        （Unix=fork / Windows=spawn）；可显式 "fork" / "spawn" / "forkserver"。
    """

    def __init__(self, timeout_s: float = 30.0, context_method: str | None = None):
        self.timeout_s = max(0.5, float(timeout_s))
        self.ctx = multiprocessing.get_context(context_method) if context_method else multiprocessing.get_context()
        self.spawn_mode = self.ctx.get_start_method() != "fork"

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def run_case(
        self,
        entry: Entry,
        args: tuple = (),
        kwargs: dict | None = None,
    ) -> ForkResult:
        """fork 一个子进程跑 ``entry(*args, **kwargs)``，超时 kill。

        ``entry`` 形如 ``"app.harness.test_procs:normal_case"``：
        子进程内 ``importlib`` 定位并调用；返回值经队列回传（可 picklable）。
        """
        result_queue: "multiprocessing.Queue" = self.ctx.Queue()
        process = self.ctx.Process(
            target=_child_main,
            args=(entry, tuple(args), kwargs or {}, result_queue),
            daemon=True,
            name=f"harness-case-{entry}",
        )
        start = time.perf_counter()
        process.start()
        timed_out = False
        payload: Any = None
        try:
            payload = result_queue.get(timeout=self.timeout_s)
        except queue.Empty:
            timed_out = True
            self._kill(process)
        duration_ms = (time.perf_counter() - start) * 1000.0

        if timed_out:
            return ForkResult(
                entry=entry, ok=False, timed_out=True,
                error=f"用例超时（>{self.timeout_s:.1f}s），已 kill 子进程（疑似死循环/卡死）",
                duration_ms=duration_ms,
            )
        if payload is None or not isinstance(payload, dict):
            self._kill(process)  # 进程意外退出但没回传结果
            return ForkResult(
                entry=entry, ok=False,
                error="子进程未返回结果（异常退出）", duration_ms=duration_ms,
            )
        return ForkResult(
            entry=entry,
            ok=bool(payload.get("ok")),
            error=payload.get("error"),
            duration_ms=payload.get("duration_ms", duration_ms),
            resources=payload.get("resources") or {},
            value=payload.get("value"),
        )

    def run_cases(self, entries: list[tuple[Entry, tuple, dict | None]]) -> list[ForkResult]:
        """批量跑：逐条 fork，单条超时/失败不阻塞后续用例。"""
        return [self.run_case(entry, args, kwargs) for entry, args, kwargs in entries]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _kill(self, process: "multiprocessing.Process") -> None:
        try:
            process.terminate()
            process.join(timeout=5.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=2.0)
        except Exception:  # noqa: BLE001 —— 清理失败不影响主流程
            pass


# ---------------------------------------------------------------------------
# 子进程入口（模块级：spawn 模式可安全导入）
# ---------------------------------------------------------------------------


def _child_main(entry: Entry, args: tuple, kwargs: dict, result_queue: "multiprocessing.Queue") -> None:
    """子进程执行体：定位目标函数 → 调用 → 回传 (结果, 资源指标)。"""
    start = time.perf_counter()
    try:
        module_path, _, func_name = entry.partition(":")
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        value = func(*args, **kwargs)
        result_queue.put({
            "ok": True,
            "value": value,
            "duration_ms": (time.perf_counter() - start) * 1000.0,
            "resources": _collect_resources(),
        })
    except BaseException as exc:  # noqa: BLE001 —— 子进程内兜底回传错误
        result_queue.put({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": (time.perf_counter() - start) * 1000.0,
            "resources": _collect_resources(),
        })


def _collect_resources() -> dict[str, Any]:
    """采集子进程资源指标（best-effort）。

    Unix：``resource.getrusage(RUSAGE_SELF)``（cpu user/sys + maxrss）；
    Windows：优先 psutil；未安装时使用系统进程 API 获取工作集大小。
    """
    try:  # Unix
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {
            "cpu_user_s": round(usage.ru_utime, 3),
            "cpu_sys_s": round(usage.ru_stime, 3),
            "maxrss_kb": usage.ru_maxrss,
        }
    except (ImportError, AttributeError):
        pass
    try:  # Windows / psutil
        import psutil

        proc = psutil.Process()
        mem = proc.memory_info()
        cpu = proc.cpu_times()
        return {
            "cpu_user_s": round(cpu.user, 3),
            "cpu_sys_s": round(cpu.system, 3),
            "rss_kb": round(mem.rss / 1024.0, 1),
        }
    except Exception:  # noqa: BLE001
        # psutil is optional.  Keep the Windows runner observable without
        # making it a hard dependency: the platform API exposes the current
        # process working set directly.
        try:
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
            get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
            get_memory.restype = wintypes.BOOL
            if get_memory(process, ctypes.byref(counters), counters.cb):
                return {"rss_kb": round(counters.WorkingSetSize / 1024.0, 1)}
        except Exception:  # noqa: BLE001
            pass
        return {}


def collect_resources() -> dict[str, Any]:
    """给当前进程采集资源（Unix 用 RUSAGE_CHILDREN 看累计子进程开销）。"""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return {
            "children_cpu_user_s": round(usage.ru_utime, 3),
            "children_cpu_sys_s": round(usage.ru_stime, 3),
            "children_maxrss_kb": usage.ru_maxrss,
        }
    except (ImportError, AttributeError):
        return _collect_resources()

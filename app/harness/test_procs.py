"""ForkRunner 示例子进程入口（跨平台安全：spawn 模式可被独立导入）。

这些函数是 :class:`~app.harness.fork_runner.ForkRunner` 的 ``entry``
目标（形如 ``"app.harness.test_procs:normal_case"``），用于演示与测试
「进程隔离 + 超时 kill + 资源采集」。真实用例可按相同模式在任意
``app`` 模块下注册入口函数。
"""

from __future__ import annotations

import time


def normal_case(duration_s: float = 0.05) -> str:
    """正常用例：短暂执行后返回结果（验证子进程正常回传）。"""
    time.sleep(duration_s)
    return "case-ok"


def infinite_loop() -> None:
    """死循环用例：永不返回（验证父进程超时 kill）。"""
    while True:  # noqa: BLE001
        time.sleep(0.01)


def hung_io() -> None:
    """卡死用例：长时间阻塞，模拟 hung 住（超过父进程超时即被杀）。"""
    time.sleep(3600)


def memory_allocator(mb: int = 8) -> str:
    """内存占用用例：分配 ``mb`` MB 列表后返回（验证资源指标采集）。"""
    chunk = 1024 * 1024 // 8  # 8 字节/元素
    data = [0.0] * (mb * chunk)
    time.sleep(0.02)
    return f"allocated-{len(data)}"


def cpu_burn(duration_s: float = 0.1) -> str:
    """CPU 消耗用例：忙等 ``duration_s`` 秒（验证 cpu_time 采集）。"""
    end = time.perf_counter() + duration_s
    while time.perf_counter() < end:  # noqa: BLE001
        _ = sum(range(1000))
    return "burned"


def raise_error() -> None:
    """异常用例：抛错（验证子进程错误回传）。"""
    raise RuntimeError("case boom")

# 文件说明：声明执行与测试辅助包，组织调用限额、故障注入和进程隔离。
"""Harness 工程（第13期）：Agent 测试执行框架。

三大核心能力：

1. **异常注入**（:mod:`app.harness.faults`）：向 MCP 工具层动态注入故障
   （超时 / 连接失败 / 抖动 / 非法 JSON / 缺字段 / 错误业务码 / 脏数据 /
   429 限流 / 延迟），自动复现线上抖动，把异常分支纳入自动化回归。
2. **收敛提示**（:mod:`app.harness.convergence`）：检测 ReAct 无限循环、
   工具重复调用，测试时判失败、生产时追加收敛提示终止循环。
3. **fork 子任务隔离**（:mod:`app.harness.fork_runner`）：每条用例独立子进程
   运行，父进程超时 kill，防止单条用例死循环堵死整个测试套件。

事项归属与执行评测分别使用 scripts/eval_association.py 和 scripts/eval_task_execution.py。
"""

from app.harness.convergence import (
    CONVERGENCE_PROMPT,
    CONVERGENCE_REPLY,
    ConvergenceDetector,
    ConvergenceReason,
    ConvergenceReport,
)
from app.harness.faults import FaultInjector, FaultKind, FaultSpec
from app.harness.fork_runner import ForkResult, ForkRunner

__all__ = [
    "CONVERGENCE_PROMPT",
    "CONVERGENCE_REPLY",
    "ConvergenceDetector",
    "ConvergenceReason",
    "ConvergenceReport",
    "FaultInjector",
    "FaultKind",
    "FaultSpec",
    "ForkResult",
    "ForkRunner",
]

"""评估沙箱：隔离、可复现地重跑测试集，并采集运行全过程（新链路版）。

新链路评测：每轮输入 → 规则意图识别（app.intent.recognize）→ AgentEngine
（DataPlan 装配 + dispatch 执行）→ 响应。沙箱做三件事：

1. 隔离：每条用例独立运行，关闭记忆读写与 MCP（保证可复现且不污染评分）；
   只用本地 mock 工具，让幻觉检测有确定的 ground truth。
2. 插桩：给共享 OpenAI client 的 completions.create、注入的 ToolRunner /
   ReactExecutor 打补丁，采集 LLM 调用（token / 工具 / 延迟）与工具返回。
3. 执行：顺序跑完用例的多轮输入，把过程与结果填进 RunTrace 返回。

关键：所有补丁在 finally 中还原，避免影响下一条用例。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from app.agents.engine import AgentEngine
from app.agents.response import CustomerServiceResponse
from app.config.settings import settings
from app.data_plan.enums import Intent
from app.evaluation.dataset import EvalCase
from app.evaluation.trace import LLMCallRecord, RunTrace, ToolObservation
from app.execution import ReactExecutor, ToolRunner


class Sandbox:
    """Agent 评估沙箱：构建隔离环境、插桩采集、跑用例产出 RunTrace。"""

    def __init__(self, client=None, model: str = "", tmp_root: str | None = None):
        self.client = client
        self.model = model
        self.tmp_root = Path(tmp_root) if tmp_root else Path(tempfile.mkdtemp(prefix="eval_sandbox_"))
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    def run(self, case: EvalCase) -> RunTrace:
        """跑一条用例，返回采集到的运行轨迹。"""
        trace = RunTrace(case_id=case.id, turns=list(case.turns))

        patches: list[tuple] = []  # (obj, attr, original) 供还原
        try:
            # LLM 调用插桩（共享 client，插一次即可）
            self._patch_client(trace, patches)

            final_resp: CustomerServiceResponse | None = None
            last_intent: str | None = None
            last_execution: str | None = None
            # 共享 Agent 离线评测：原话保留，不进行强制查询改写。
            context: list[dict] = []

            for turn in case.turns:
                from app.intent import recognize
                context.append({'role': 'user', 'content': turn})
                intent = recognize(turn, messages=context)
                trace.rewrites.append(dict(raw_input=turn, rewritten_input=turn,
                                           source='conversation', clarification=None, order_candidates=[]))
                engine = AgentEngine(intent, client=self.client, model=self.model)

                result = engine.execute(
                    turn,
                    messages=context,
                    runner=ToolRunner(execute=self._wrapped_execute(trace)),
                    react_executor=self._make_react_executor(trace),
                )
                context.append({"role": "assistant", "content": result.reply})

                final_resp = CustomerServiceResponse(
                    intent=intent,
                    confidence=0.95,
                    reply=result.reply,
                    requires_human=result.needs_human,
                    follow_up_question=(
                        "请确认是否继续该操作？" if result.needs_confirmation else None
                    ),
                )
                last_intent = intent.value
                last_execution = engine.plan.execution.value

            trace.final_response = final_resp
            trace.intent = last_intent
            trace.execution = last_execution

        except Exception as e:  # noqa: BLE001 —— 单条用例异常不应中断整轮评估
            trace.error = f"{type(e).__name__}: {e}"
        finally:
            for obj, attr, original in patches:
                setattr(obj, attr, original)

        return trace

    # ---------- 插桩 ----------
    def _patch_client(self, trace: RunTrace, patches: list[tuple]) -> None:
        """给共享 OpenAI client 的 completions.create 打补丁（ReactExecutor 使用）。"""
        if self.client is None:
            return
        completions = self.client.chat.completions
        patches.append((completions, "create", completions.create))
        completions.create = self._wrap_create(completions.create, trace)

    def _make_react_executor(self, trace: RunTrace) -> ReactExecutor | None:
        if self.client is None:
            return None
        return ReactExecutor(
            client=self.client,
            model=self.model,
            temperature=0.0,
            execute=self._wrapped_execute(trace),
        )

    def _wrapped_execute(self, trace: RunTrace):
        """包装真实工具执行：记录 ToolObservation（幻觉检测的 ground truth）。"""
        from app.tools.registry import execute_tool

        def wrapper(name: str, arguments: dict) -> str:
            # These new tools must not read/write a live user's cross-session
            # drafts when an evaluation is invoked with an existing identity.
            result_str = json.dumps({'success': False, 'error_code': 'evidence_unavailable',
                                     'error': '评测沙箱不读取或修改真实对话草稿'}, ensure_ascii=False) if name in {
                                         'read_case_memory', 'save_case_context'} else execute_tool(name, arguments)
            trace.tool_observations.append(
                ToolObservation(name=name, arguments=dict(arguments), result=result_str)
            )
            return result_str
        return wrapper

    def _wrap_create(self, original, trace: RunTrace):
        def wrapper(*args, **kwargs):
            start = time.time()
            response = original(*args, **kwargs)
            latency_ms = (time.time() - start) * 1000
            self._record_llm_call(trace, response, latency_ms)
            return response
        return wrapper

    @staticmethod
    def _record_llm_call(trace: RunTrace, response, latency_ms: float) -> None:
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or 0

        tool_calls: list[dict] = []
        try:
            message = response.choices[0].message
            for tc in (getattr(message, "tool_calls", None) or []):
                tool_calls.append({
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                })
        except (AttributeError, IndexError):
            pass

        model = getattr(response, "model", "") or ""
        trace.llm_calls.append(LLMCallRecord(
            purpose="react",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
        ))

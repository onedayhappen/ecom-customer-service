"""Single-task harness around the existing Agent/ReAct loop."""
from contextlib import nullcontext
import json
import threading
import time
from types import SimpleNamespace

from pydantic import ValidationError

from app.data_plan.builder import build_task_plan
from app.data_plan.enums import Execution
from app.execution.react import ReactExecutor
from app.execution.types import ExecutionResult
from app.harness.bounded_call import CallCapacityExceeded, bounded_calls
from app.harness.convergence import ConvergenceDetector
from app.runtime.budgets import agent_partition
from app.runtime.outbox import clock
from app.session.context_budget import estimate_request_tokens
from app.session.request_budget import ContextBudgetExceeded, request_input_limit
from app.support.store import SupportError
from app.tasks.execution_models import ProgressInput, ReadTaskContext, TASK_PROMPT, TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.execution_updates import ReplanExecution, refresh, save_usage
from app.tasks.runtime import task_scope
from app.tools.request_context import reset_current_user_id, set_current_user_id
from app.observability.interactions import observed_execution, observed_tool, call_model, record
from app.prompts.context import PROGRESS_NOTE, TASK_CONTEXT_NOTE


class ExecutionStopped(RuntimeError):
    pass


def _failure(code):
    return {"success": False, "error_code": "validation_error", "error": code}


class _ModelClient:
    def __init__(self, context, client):
        self.context = context
        self.client = client.bind_scene("task_execution") if hasattr(client, "bind_scene") else client
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **params):
        ctx = self.context
        ctx.checkpoint()
        from app.observability.llm_client import LlmClient
        # LlmClient accounts for every actual provider call, including calls
        # made inside read tools. Injected test/raw clients use this boundary.
        if not isinstance(self.client, LlmClient):
            ctx.model_request(params)
        try:
            result = bounded_calls.run(lambda: call_model(self.client, params), ctx.call_timeout())
        except (SupportError, ExecutionStopped):
            raise
        except TimeoutError:
            raise ExecutionStopped("model_timeout") from None
        except CallCapacityExceeded:
            raise ExecutionStopped("call_capacity") from None
        except ContextBudgetExceeded:
            raise ExecutionStopped("context_budget") from None
        except Exception:
            raise ExecutionStopped("model_unavailable") from None
        ctx.checkpoint()
        ctx.check()
        return result


class _TaskContext:
    def __init__(self, runs, request, reservation, tools):
        self.runs, self.request, self.tools = runs, request, tools
        self.record, self.payload = reservation["record"], reservation["payload"]
        self.deadline = time.monotonic() + request.budget.seconds
        usage = self.payload.get("usage", {})
        self.model_calls = usage.get("model_calls", 0)
        self.tool_calls = usage.get("tool_calls", 0)
        self.progress_calls = usage.get("progress_calls", 0)
        self.budget_lock = threading.Lock()
        self.receipts = []
        self.validation_errors = []
        self.historical = runs.evidence(self.record)
        self.operation_id = None
        from app.memory.task_context import TaskMemory
        self.memory = TaskMemory(runs.store, runs.user)
        self.allowed_tools = frozenset()

    def checkpoint(self):
        """Wait without DB locks; association continues in its separate pool."""
        waiting = False
        while True:
            if time.monotonic() >= self.deadline:
                raise ExecutionStopped("time_budget")
            update = refresh(self.runs, self.request, self.payload)
            if update is not None:
                break
            if not waiting:
                record("execution.waiting_input", task_id=self.record.task_id)
                waiting = True
            time.sleep(min(.05, max(0, self.deadline - time.monotonic())))
        if waiting:
            record("execution.input_ready", task_id=self.record.task_id)
        current, payload, changed = update
        if changed:
            self.record, self.payload = current, payload
            from app.tasks.models import MessageSource
            self.request = self.request.model_copy(update={
                "source": MessageSource.model_validate(payload["execution_source"])})
            self.operation_id = None
            self.historical = self.runs.evidence(current)
            self.receipts = []
            from app.memory.task_context import TaskMemory
            self.memory = TaskMemory(self.runs.store, self.runs.user)
            raise ReplanExecution()

    def persist_usage(self):
        save_usage(self.runs, self.request, self.payload, dict(
            model_calls=self.model_calls, tool_calls=self.tool_calls, progress_calls=self.progress_calls))

    def call_timeout(self):
        return min(self.request.budget.call_seconds, max(0, self.deadline - time.monotonic()))

    def check(self):
        if time.monotonic() >= self.deadline:
            raise ExecutionStopped("time_budget")
        with self.runs.store.transaction(read_only=True) as tx:
            self.runs.check(tx, self.request, self.payload)
            self.evidence_time = clock(tx)
        if not self.memory.current():
            raise ExecutionStopped("memory_context_changed")

    def model_request(self, params):
        self.check()
        with self.budget_lock:
            if self.model_calls >= self.request.budget.model_calls:
                raise ExecutionStopped("model_call_budget")
            self.model_calls += 1
            self.persist_usage()
        output_key = "max_completion_tokens" if "max_completion_tokens" in params else "max_tokens"
        params[output_key] = min(int(params.get(output_key) or self.request.budget.output_tokens), self.request.budget.output_tokens)
        params["timeout"] = self.call_timeout()
        if estimate_request_tokens(params) > min(self.request.budget.input_tokens, request_input_limit(params)):
            raise ExecutionStopped("context_budget")

    def messages(self):
        from app.memory.task_context import task_history
        from app.memory.source_policy import safe_context
        history = task_history(self.runs, self.record)
        self.memory.watch_sources(history['sources'] + history['historical_sources'])
        return [{"role": "user", "content": json.dumps({
            "task": safe_context(self.record.model_dump(exclude={"user_id", "created_at", "updated_at", "storage_mode"})),
            **history, "recent_tool_evidence": safe_context(self.historical),
            "instruction": TASK_CONTEXT_NOTE,
        }, ensure_ascii=False)}]

    def read_progress(self, arguments):
        try:
            query = ReadTaskContext.model_validate(arguments)
        except ValidationError:
            return _failure("invalid_progress_query")
        self.check()
        if query.order_id and query.order_id != self.record.details.get("order_id"):
            return _failure("order_outside_task")
        from app.memory.task_context import task_history
        from app.memory.source_policy import safe_context
        history = task_history(self.runs, self.record)
        self.memory.watch_sources(history['sources'] + history['historical_sources'])
        return {"success": True, "task": safe_context(self.record.model_dump()),
                **history,
                "recent_tool_evidence": safe_context(self.historical),
                "evidence": [e for e in self.receipts if e["result"].get("success")],
                "note": PROGRESS_NOTE}

    def save_progress(self, arguments):
        try:
            progress = ProgressInput.model_validate(arguments)
        except ValidationError as exc:
            self.validation_errors.extend({"field": str(e["loc"][0]) if e["loc"] and e["loc"][0] in ProgressInput.model_fields else "progress",
                                           "message": e["msg"]}
                                          for e in exc.errors(include_input=False)[:5])
            return _failure("invalid_progress")
        if progress.state == "waiting_external" and not any(
                e["event_id"] == progress.evidence_id and e["result"].get("success") for e in self.receipts):
            return _failure("external_wait_requires_current_evidence")
        self.check()
        self.progress_calls += 1
        self.persist_usage()
        self.record, self.payload = self.runs.save_progress(self.request, self.payload, progress, self.progress_calls)
        return {"success": True, "task": self.record.model_dump(), "note": "只保存事项进度，未办理业务操作。"}

    @observed_tool
    def tool(self, name, arguments):
        self.checkpoint()
        self.check()
        if name not in self.allowed_tools:
            return json.dumps(_failure("tool_not_allowed"))
        if self.tool_calls >= self.request.budget.tool_calls:
            raise ExecutionStopped("tool_call_budget")
        from app.tools.catalog import BY_NAME
        from app.services.commerce.common import dumps
        from app.observability.audit import mask_sensitive
        self.tool_calls += 1
        self.persist_usage()
        spec = BY_NAME[name]
        # Read calls may run with a deadline; writes/preparations must never be detached.
        # Persist inside the bounded worker so a timed-out read is still recorded
        # when it eventually returns. Never treat that late result as current.
        admitted = self.record
        admitted_payload = json.loads(json.dumps(self.payload))
        call_index = self.tool_calls
        def invoke():
            raw = self.tools(name, arguments)
            result = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(result, dict):
                result = _failure("invalid_tool_result")
            if name not in {"ask_user", "handoff_to_human"}:
                try:
                    evidence = self.runs.save_tool(self.request, admitted_payload, call_index,
                        name, mask_sensitive(arguments), result)
                except SupportError:
                    evidence = self.runs.save_late_tool(self.request, admitted_payload, admitted,
                        call_index, name, mask_sensitive(arguments), result)
                return result, evidence
            return result, None
        result, evidence = bounded_calls.run(invoke, self.call_timeout()) if not spec.task_only else invoke()
        if evidence is not None and not evidence.get("late"):
            self.receipts.append(evidence)
        self.checkpoint()
        return dumps(result)


class TaskExecutor:
    def __init__(self, store, authenticated_user_id, *, tools=None, scope=None):
        self.runs = TaskRuns(store, authenticated_user_id, scope=scope)
        if tools is None:
            from app.tools.registry import execute_tool
            tools = execute_tool
        self.tools = tools

    def run(self, request, engine):
        request = TaskExecutionInput.model_validate(request)
        reservation, replay = self.runs.begin(request)
        if replay is not None:
            return replay
        return self.run_reserved(request, engine, reservation)

    @observed_execution
    def run_reserved(self, request, engine, reservation):
        """Internal Worker continuation of a DB reservation; not an HTTP input."""
        context = _TaskContext(self.runs, request, reservation, self.tools)
        from app.tools.selection import for_task
        from app.tools.registry import TOOL_DEFINITIONS
        context.allowed_tools = for_task(engine.intent, context.record.goal)
        plan = build_task_plan(engine.intent, allowed_tools=context.allowed_tools)
        executor = ReactExecutor(_ModelClient(context, engine.client), engine.model,
            system_prompt=TASK_PROMPT,
            execute=context.tool,
            checkpoint=context.checkpoint,
            convergence_detector=ConvergenceDetector(max_steps=request.budget.model_calls + 1, max_same_tool_calls=3))
        identity = set_current_user_id(self.runs.user)
        result, stop_reason = None, None
        try:
            # Scheduled runs already own all three database quotas, which survive
            # Redis loss. Direct compatibility calls retain their existing lease.
            partition = nullcontext() if context.payload.get("schedule") else agent_partition(self.runs.user)
            with task_scope(context), partition:
                try:
                    result = self._react(context, executor, plan, TOOL_DEFINITIONS, request)
                except ExecutionStopped as exc:
                    stop_reason = str(exc)
                    has_evidence = any(e["result"].get("success") for e in context.receipts + context.historical)
                    result = ExecutionResult(plan.intent, Execution.REACT, reply=(
                        "已取得当前事项的查询结果，结果已保存，可继续核实处理。" if has_evidence
                        else "暂时无法取得可核实的信息，请稍后重试。"))
                if result.question:
                    record("execution.question", output=result.question)
                    saved = context.save_progress({"expected_version": context.record.version, "state": "waiting_user",
                                                   "question": result.question})
                    if not saved.get("success"):
                        stop_reason = "invalid_question"
                        result = ExecutionResult(plan.intent, Execution.REACT, reply="需要进一步核实当前需求，请联系人工客服。",
                                                 needs_human=True)
                state = context.record.status if context.record.status in {"waiting_user", "waiting_external"} else (
                    "needs_human" if result.needs_human else "needs_confirmation" if context.operation_id
                    else "deferred" if stop_reason else "ready")
                record("react.result", output=result, stop_reason=stop_reason)
                return self.runs.finish(request, context.payload, {
                    "state": state, "reply": result.reply, "task_version": context.record.version,
                    "question": context.record.waiting_for if context.record.status == "waiting_user" else None,
                    "waiting_for": context.record.waiting_for, "needs_human": result.needs_human,
                    "reason": stop_reason, "model_calls": context.model_calls, "tool_calls": context.tool_calls,
                    "validation_errors": context.validation_errors,
                    **({"operation_id": context.operation_id} if context.operation_id else {}),
                    "evidence_ids": list(dict.fromkeys(e["event_id"] for e in context.receipts + context.historical)),
                })
        except (SupportError, ExecutionStopped):
            return self.runs.finish(request, context.payload, {"state": "interrupted", "reply": "",
                "reason": "execution_no_longer_current", "needs_human": False}, interrupted=True)
        except Exception as exc:
            # No provider/tool exception text is stored or shown to customers.
            record("execution.exception", status="error", error={"type": type(exc).__name__, "message": str(exc)})
            return self.runs.finish(request, context.payload, {"state": "deferred", "reply": "暂时无法继续处理，请稍后重试。",
                "reason": "execution_unavailable", "needs_human": False}, interrupted=True)
        finally:
            reset_current_user_id(identity)

    @staticmethod
    def _react(context, executor, plan, definitions, request):
        """Rebuild only after a committed update; counters and deadline survive."""
        while True:
            try:
                context.checkpoint()
                if context.model_calls >= request.budget.model_calls:
                    raise ExecutionStopped("model_call_budget")
                # Rebuild visible schemas and execution fence after goal updates.
                from app.tools.selection import for_task, task_profiles
                context.allowed_tools = for_task(plan.intent, context.record.goal)
                plan = build_task_plan(plan.intent, allowed_tools=context.allowed_tools)
                record("execution.tools_selected", task_id=context.record.task_id,
                       task_version=context.record.version,
                       output=dict(profiles=sorted(task_profiles(plan.intent, context.record.goal)),
                                   tools=sorted(context.allowed_tools), count=len(context.allowed_tools)))
                result = executor.run(context.messages(), plan, max_steps=request.budget.model_calls,
                    tool_definitions=[d for d in definitions if d["function"]["name"] in context.allowed_tools],
                    historical_evidence=[e["result"] for e in context.historical])
                context.checkpoint()
                return result
            except ReplanExecution:
                record("execution.replan", task_id=context.record.task_id,
                       output={"task_version": context.record.version,
                               "model_calls": context.model_calls, "tool_calls": context.tool_calls})

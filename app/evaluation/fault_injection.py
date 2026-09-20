"""故障注入评测执行器：A/B/C 三组系统对照（第三阶段）。

三组系统看到**完全相同的用户输入和模型工具调用意图**（案例脚本），
差异只在脚手架的可靠机制——因此系统间指标差异可归因于机制而非模型：

- **System A（纯 ReAct 基线）**：模型请求什么就执行什么。无确认门控、
  无幂等、无 Case 绑定、无审计。代表"灵活但不可控"。
- **System B（Workflow + 确认门控）**：写操作需要用户确认话术后才执行，
  但确认是**动作级、文本式**的：不绑定参数、不幂等、无审计事件流。
  证明确定性门控的收益，也暴露文本确认的漏洞（夹带参数、重复执行）。
- **System C（完整可靠执行系统）**：直接驱动真实组件——
  :class:`CaseManager`（Case 绑定 + 审计）、:class:`MutationGuard`
  （参数绑定令牌 + 幂等 + unknown 锁定）、:class:`ReActPolicy`
  （失败分类 + 透明重试 + 重复拦截 + 冲突即停）+ 确定性订单消歧。

运行方式::

    python -m app.evaluation.fault_injection            # 跑三组对照并输出报告
    python -m app.evaluation.fault_injection --json data/evaluation/fi_report.json

评测不依赖 LLM：模型行为由案例脚本给定，确定性组件决定执行结果。
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.data_plan import Intent
from app.evaluation.fault_cases import CATEGORY_LABELS, FiCase, FiTurn, build_cases
from app.memory.case_manager import CaseManager
from app.reliability import MutationGuard
from app.reliability.failure_policy import (
    Action as PolicyAction,
    FailureClassifier,
    ReActPolicy,
)

MUTATING_TOOLS = frozenset({"cancel_order", "apply_refund", "update_address"})
INTENT_BY_TOOL = {
    "cancel_order": Intent.ORDER_CANCEL.value,
    "apply_refund": Intent.AFTER_SALE_REQUEST.value,
    "update_address": Intent.ADDRESS_CHANGE.value,
}
#: 各写操作允许的前置订单状态（消歧候选过滤：不可操作状态不进入候选集）
ACTIONABLE_STATUS = {
    "cancel_order": {"pending"},
    "apply_refund": {"pending", "shipped", "delivered"},
    "update_address": {"pending"},
}
#: 写操作的展示名（确认话术使用，避免把工具名直接暴露给用户）
TOOL_LABELS = {
    "cancel_order": "取消订单",
    "apply_refund": "申请退款",
    "update_address": "修改收货地址",
}
#: 写动作 -> 达成业务终态
SUCCESS_FINAL_STATE = {
    "cancel_order": "cancelled",
    "apply_refund": "refund_processing",
    "update_address": "address_changed",
}
ORDER_ID_RE = re.compile(r"ORD-\d{8}-\d{3}", re.IGNORECASE)

AUDIT_EVENT_TYPES = frozenset({
    "intent_detected", "entity_resolved", "confirmation_requested",
    "confirmation_verified", "mutation_started", "mutation_succeeded",
    "mutation_failed", "mutation_unknown", "mutation_replayed",
    "mutation_rejected", "handoff", "order_selection_requested",
    "order_selected", "order_selection_expired",
})


# ---------------------------------------------------------------------------
# 隔离的 mock 订单服务（带故障注入）
# ---------------------------------------------------------------------------

class MockOrderEnvironment:
    """每条案例独立的订单服务副本：与 app/tools 相同的业务规则 + 故障注入。"""

    def __init__(self, user_id: str, case: FiCase):
        from app.tools.mock_data import ORDERS
        self.user_id = user_id
        self.orders = copy.deepcopy(ORDERS)
        for order_id, override in (case.orders_override or {}).items():
            if order_id in self.orders:
                self.orders[order_id].update(override)
        self.faults = {tool: dict(spec) for tool, spec in (case.faults or {}).items()}
        self.write_calls: list[dict[str, Any]] = []  # 下游写调用记录（重复执行率的依据）

    def _fault(self, tool: str) -> dict[str, Any] | None:
        spec = self.faults.get(tool)
        if spec and spec.get("times", 0) > 0:
            spec["times"] -= 1
            return {"success": False, "error_code": spec["error_code"], "error": spec["error"]}
        return None

    def user_orders(self) -> list[dict[str, Any]]:
        fault = self._fault("list_user_orders")
        if fault:
            return fault
        orders = [o for o in self.orders.values() if o["user_id"] == self.user_id]
        return {"success": True, "orders": [
            {"order_id": o["order_id"], "status": o["status"],
             "items_summary": "、".join(i["name"] for i in o["items"]),
             "created_at": o["created_at"]}
            for o in orders
        ]}

    def query_order(self, order_id: str) -> dict[str, Any]:
        fault = self._fault("query_order")
        if fault:
            return fault
        order = self.orders.get(order_id)
        if not order:
            return {"success": False, "error_code": "evidence_unavailable",
                    "error": f"未找到订单 {order_id}"}
        if order["user_id"] != self.user_id:
            return {"success": False, "error_code": "business_rejection", "error": "无权访问该订单"}
        return {"success": True, "order": order}

    def query_logistics(self, order_id: str) -> dict[str, Any]:
        fault = self._fault("query_logistics")
        if fault:
            return fault
        order = self.orders.get(order_id)
        if not order:
            return {"success": False, "error_code": "evidence_unavailable",
                    "error": f"未找到订单 {order_id} 的物流信息"}
        if order["user_id"] != self.user_id:
            return {"success": False, "error_code": "business_rejection", "error": "无权访问该物流信息"}
        status_label = _status_label(order["status"])
        return {"success": True, "logistics": {
            "order_id": order_id, "status": status_label,
            "tracking_no": f"SF{order_id[-3:]}7890123",
            "detail": f"包裹当前状态：{status_label}，最近更新于今天上午。",
        }}

    def execute(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """写操作入口（A/B/C 的下游调用都经过这里，用于统一计量）。"""
        self.write_calls.append({"tool": tool, "arguments": dict(arguments)})
        fault = self._fault(tool)
        if fault:
            return fault
        order_id = str(arguments.get("order_id") or "")
        order = self.orders.get(order_id)
        if not order:
            return {"success": False, "error_code": "evidence_unavailable",
                    "error": f"未找到订单 {order_id}"}
        if order["user_id"] != self.user_id:
            return {"success": False, "error_code": "business_rejection", "error": "无权操作该订单"}
        if tool == "cancel_order":
            if order["status"] != "pending":
                return {"success": False, "error_code": "business_rejection",
                        "error": f"订单 {order_id} 当前状态 {order['status']}，不可取消"}
            order["status"] = "cancelled"
            return {"success": True, "message": f"订单 {order_id} 已取消"}
        if tool == "apply_refund":
            if order["status"] not in {"pending", "shipped", "delivered"}:
                return {"success": False, "error_code": "business_rejection",
                        "error": f"订单 {order_id} 当前状态 {order['status']}，无法申请退款"}
            order["status"] = "refund_processing"
            return {"success": True, "message": f"订单 {order_id} 退款申请已提交"}
        if tool == "update_address":
            if order["status"] != "pending":
                return {"success": False, "error_code": "business_rejection",
                        "error": f"订单 {order_id} 当前状态 {order['status']}，无法修改地址"}
            order["address"] = arguments.get("new_address", "")
            return {"success": True, "message": f"订单 {order_id} 收货地址已更新"}
        return {"success": False, "error_code": "validation_error", "error": f"未知写工具 {tool}"}

    def order_status(self, order_id: str) -> str | None:
        order = self.orders.get(order_id)
        return order["status"] if order else None


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------

@dataclass
class ExecutedMutation:
    tool: str
    order_id: str
    arguments: dict[str, Any]
    turn_index: int
    #: 执行时是否已有针对**这些参数**的已完成用户确认
    confirmed_for_args: bool
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    system: str
    case_id: str
    category: str
    #: 实际到达下游的写调用（env.write_calls 镜像）
    downstream_calls: list[dict[str, Any]] = field(default_factory=list)
    executed: list[ExecutedMutation] = field(default_factory=list)
    confirmation_requested: bool = False
    #: 需要显式确认的写意图在执行前是否请求过确认（参数级）
    audit_events: list[str] = field(default_factory=list)
    needs_human: bool = False
    replies: list[str] = field(default_factory=list)
    final_order_status: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# System A：纯 ReAct 基线
# ---------------------------------------------------------------------------

class SystemA:
    """模型请求即执行：无门控、无幂等、无 Case、无审计。"""

    name = "A_pure_react"

    def run(self, case: FiCase) -> RunRecord:
        env = MockOrderEnvironment(case.user_id, case)
        record = RunRecord(system=self.name, case_id=case.id, category=case.category)
        for i, turn in enumerate(case.turns):
            for call in turn.calls:
                if call.tool in MUTATING_TOOLS:
                    args = dict(call.arguments)
                    result = env.execute(call.tool, args)
                    record.executed.append(ExecutedMutation(
                        tool=call.tool, order_id=str(args.get("order_id", "")),
                        arguments=args, turn_index=i,
                        confirmed_for_args=False, result=result,
                    ))
                elif call.tool == "query_order":
                    env.query_order(str(call.arguments.get("order_id", "")))
                elif call.tool == "query_logistics":
                    env.query_logistics(str(call.arguments.get("order_id", "")))
                elif call.tool == "list_user_orders":
                    env.user_orders()
        _finalize_status(case, env, record)
        return record


# ---------------------------------------------------------------------------
# System B：Workflow + 确认门控（动作级、文本式确认）
# ---------------------------------------------------------------------------

class SystemB:
    """确定性 Workflow + 确认门控，但确认不绑定参数、不幂等、无审计。"""

    name = "B_workflow_confirm"

    def run(self, case: FiCase) -> RunRecord:
        env = MockOrderEnvironment(case.user_id, case)
        record = RunRecord(system=self.name, case_id=case.id, category=case.category)
        #: action -> 用户已看到并确认过的参数（动作级确认的"已确认参数"）
        confirmed_actions: dict[str, dict[str, Any] | None] = {}
        pending: dict[str, dict[str, Any]] = {}  # action -> 系统展示给用户的参数

        for i, turn in enumerate(case.turns):
            for call in turn.calls:
                if call.tool not in MUTATING_TOOLS:
                    if call.tool == "query_order":
                        env.query_order(str(call.arguments.get("order_id", "")))
                    elif call.tool == "query_logistics":
                        env.query_logistics(str(call.arguments.get("order_id", "")))
                    elif call.tool == "list_user_orders":
                        env.user_orders()
                    continue
                action, args = call.tool, dict(call.arguments)
                order_id = str(args.get("order_id", ""))
                if turn.confirms and pending.get(action):
                    # 文本确认放行：执行模型**本轮**的参数（未与展示参数比对）
                    shown = pending.pop(action)
                    confirmed_actions[action] = shown
                    result = env.execute(action, args)
                    record.executed.append(ExecutedMutation(
                        tool=action, order_id=order_id, arguments=args, turn_index=i,
                        confirmed_for_args=(shown is not None and shown == args),
                        result=result,
                    ))
                else:
                    # Workflow 槽位校验：文本中无订单号则索要，不猜测
                    ids = ORDER_ID_RE.findall(turn.user)
                    if not ids:
                        record.replies.append("请提供订单号，我帮您处理。")
                        continue
                    record.confirmation_requested = True
                    pending[action] = args
                    record.replies.append(f"确认对订单 {order_id} 执行 {action} 吗？")

        _finalize_status(case, env, record)
        return record


# ---------------------------------------------------------------------------
# System C：完整可靠执行系统（真实组件）
# ---------------------------------------------------------------------------

class SystemC:
    """Workflow/ReAct + 确认令牌 + Case 绑定 + 消歧 + 幂等 + 审计 + 失败策略。

    与生产 ``main.py`` 相同的控制语义，评测中不依赖 LLM：

    - **确认轮令牌优先路由**（对应 ``MutationGuard.peek_confirmation``）：用户
      回复"确认"时路由回已签发令牌绑定的**原始参数**，而非本轮模型参数——
      确认轮甚至不需要重新做订单绑定。
    - **用户改参 vs 模型篡参**：参数新值出现在用户话术中 → 用户修改，旧令牌
      作废并重新确认；新值不在话术中 → 模型篡改，按被确认的原始参数执行。
    - **确定性订单绑定**：文本订单号 > 本轮明确选择 > 修正流模型订单（仅在
      存在未消费令牌时，且只用于发起**新的确认**）> 待确认令牌订单 > 唯一
      可操作候选 > 澄清。模型猜测的订单永远不能直接触达下游。
    - **幂等门控**：``succeeded`` 重放首次权威结果；``unknown`` /
      ``in_progress(stale)`` 锁定转人工，绝不自动重试写操作。
    - **冲突一次即停**：写前校验/下游返回冲突 → 立即转人工，并封禁同单
      同工具的后续写意图。
    - 伪造/过期令牌 → 拒绝并自动重新签发确认；崩溃模拟在流内写入
      backdated ``in_progress`` 记录（无终态、无下游调用）。
    """

    name = "C_reliable"

    def __init__(self, storage_root: str | None = None):
        self.storage_root = storage_root

    def run(self, case: FiCase) -> RunRecord:
        tmp = Path(tempfile.mkdtemp(prefix=f"fi_{case.id}_", dir=self.storage_root))
        try:
            return self._run(case, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run(self, case: FiCase, tmp: Path) -> RunRecord:
        cm = CaseManager(case.user_id, storage_dir=str(tmp))
        guard = MutationGuard(cm, session_id=f"fi-{case.id}")
        policy = ReActPolicy(mutating_tools=MUTATING_TOOLS)
        classifier = FailureClassifier()
        env = MockOrderEnvironment(case.user_id, case)
        record = RunRecord(system=self.name, case_id=case.id, category=case.category)

        #: (intent, order_id) -> 令牌记录；与 guard 每 Case 单一 pending_mutation 对齐，
        #: 同一 Case 重复签发会覆盖旧令牌（旧令牌字符串随之失效）。
        tokens: dict[tuple[str, str], dict[str, Any]] = {}
        pending_selection = False
        conflict_blocked: set[tuple[str, str]] = set()
        last_ask_turn = -1

        def audit(event: str, payload: dict | None = None, case_id: str = "") -> None:
            cm.record_audit(event, guard.session_id, case_id, payload or {})

        def order_summary(order_id: str) -> str:
            o = env.orders.get(order_id) or {}
            items = "、".join(i["name"] for i in o.get("items", []))
            return f"{order_id}（{items}，{_status_label(o.get('status', ''))}）"

        def locked_reply(tool: str, order_id: str, status: str) -> None:
            audit("handoff", {"tool": tool, "order_id": order_id, "idempotency_status": status})
            record.needs_human = True
            record.replies.append("该操作执行状态未知，为避免重复执行已锁定，请联系人工客服核实。")

        def verify_bind(tool: str, order_id: str | None) -> tuple[str | None, str]:
            """订单查询 + 可操作状态校验；返回 (bound, reason)。"""
            if not order_id:
                return None, "no_order"
            check = env.query_order(order_id)
            if classifier.classify(check).value == "evidence_conflict":
                audit("handoff", {"reason": "order_verification_conflict", "order_id": order_id})
                record.needs_human = True
                record.replies.append("多源数据冲突，已转人工核实。")
                return None, "conflict"
            if not check.get("success"):
                record.replies.append(check.get("error", "订单校验失败"))
                return None, "verification_failed"
            status = (check.get("order") or {}).get("status", "")
            if status not in ACTIONABLE_STATUS.get(tool, set()):
                record.replies.append(f"订单 {order_id} 当前「{_status_label(status)}」，无法执行该操作。")
                return None, "not_actionable"
            return order_id, "verified"

        def bind_order(tool: str, user_text: str, model_order: str,
                       turn_index: int, turn_selected: str | None) -> tuple[str | None, str]:
            """确定性订单绑定。模型猜测的订单永远不会直接作为绑定结果触达下游。"""
            ids = sorted({m.upper() for m in ORDER_ID_RE.findall(user_text)})
            if ids:
                pick = model_order.upper() if model_order and model_order.upper() in ids else ids[0]
                bound, reason = verify_bind(tool, pick)
                return bound, ("explicit_id" if bound else reason)
            if turn_selected:
                bound, _ = verify_bind(tool, turn_selected)
                return bound, ("user_selected" if bound else "selection_invalid")
            unconsumed = [t for t in tokens.values() if not t["consumed"]]
            if unconsumed:
                # 修正流：用户在已发起确认的请求上改目标/改动作。模型提取的新
                # 订单只用于发起一次**新的确认**——是否执行仍由用户确认决定，
                # 因此模型猜测不构成越权风险。
                if model_order:
                    bound, _ = verify_bind(tool, model_order.upper())
                    if bound:
                        return bound, "amendment_model_order"
                for t in sorted(unconsumed, key=lambda t: (t["tool"] != tool, -t["turn"])):
                    bound, _ = verify_bind(tool, str(t["args"].get("order_id") or ""))
                    if bound:
                        return bound, "pending_token_order"
            if model_order:
                # 延续流：模型订单与已消费令牌（已执行/已拒绝）的订单一致——
                # 只做幂等查询与重放，不产生新的执行路径。
                for t in tokens.values():
                    if (t["consumed"] and t["tool"] == tool
                            and str(t["args"].get("order_id", "")).upper() == model_order.upper()):
                        return model_order.upper(), "continuation"
            listing = env.user_orders()
            if classifier.classify(listing).value == "evidence_conflict":
                audit("handoff", {"reason": "candidate_listing_conflict"})
                record.needs_human = True
                record.replies.append("多源数据冲突，已转人工核实。")
                return None, "conflict"
            if not listing.get("success"):
                record.replies.append("暂时无法查询您的订单列表，请提供准确订单号。")
                return None, "listing_failed"
            actionable = [
                o for o in listing.get("orders", [])
                if o.get("status") in ACTIONABLE_STATUS.get(tool, set())
            ]
            if len(actionable) == 1:
                only = actionable[0]["order_id"]
                record.replies.append(f"您当前可操作的订单只有一个：{order_summary(only)}。")
                return only, "single_candidate"
            if not actionable:
                record.replies.append("您当前没有可执行该操作的订单。")
                return None, "no_candidate"
            if last_ask_turn == turn_index:
                return None, "asked_this_turn"  # 同一轮已澄清过，不再重复提问
            cm.request_order_selection(
                guard.session_id, "请选择要操作的订单", INTENT_BY_TOOL[tool],
                [{"order_id": o["order_id"], "items_summary": o["items_summary"],
                  "status": o["status"]} for o in actionable],
            )
            record.replies.append(
                "您有多笔可操作订单，请回复完整订单号或序号选择：\n"
                + "\n".join(f"{j}. {order_summary(o['order_id'])}" for j, o in enumerate(actionable, 1))
            )
            return None, "ask_selection"

        def issue_confirmation(intent: str, tool: str, args: dict[str, Any],
                               turn_index: int) -> None:
            order_id = str(args.get("order_id", ""))
            token = guard.request_confirmation(intent, tool, args)
            case_rec = cm.find_case_for_mutation(intent, order_id)
            tokens[(intent, order_id.upper())] = {
                "tool": tool, "intent": intent, "args": dict(args), "token": token,
                "turn": turn_index, "consumed": False,
                "case_id": case_rec.case_id if case_rec else "",
            }
            record.confirmation_requested = True
            record.replies.append(
                f"确将对订单 {order_summary(order_id)} 执行「{TOOL_LABELS.get(tool, tool)}」，请回复「确认」。"
            )

        def find_confirm_token(tool: str, model_args: dict[str, Any],
                               turn_index: int) -> tuple[dict[str, Any] | None, str | None]:
            """找本轮可消费的确认令牌：必须由**更早轮次**签发且未消费。"""
            cands = [t for t in tokens.values()
                     if not t["consumed"] and t["tool"] == tool and t["turn"] < turn_index]
            if not cands:
                return None, None
            canonical = guard._canonical(model_args)  # noqa: SLF001
            exact = [t for t in cands if guard._canonical(t["args"]) == canonical]
            if exact:
                return exact[-1], "exact"
            return cands[-1], "changed"

        def do_execute(token_rec: dict[str, Any], exec_args: dict[str, Any],
                       turn_index: int, crash: bool = False) -> None:
            token_rec["consumed"] = True
            intent, tool = token_rec["intent"], token_rec["tool"]
            if crash:
                _simulate_crash_in_flow(cm, guard, intent, tool, exec_args)
                record.replies.append("（系统正在处理，暂无响应）")
                return
            outcome = guard.execute(intent, tool, exec_args, token_rec["token"], env.execute)
            result = outcome.result
            order_id = str(exec_args.get("order_id", ""))
            category = classifier.classify(result).value
            if outcome.replayed:
                audit("mutation_replayed", {"tool": tool, "order_id": order_id})
                record.replies.append("该操作此前已成功执行，无需重复提交。")
                record.executed.append(ExecutedMutation(
                    tool=tool, order_id=order_id, arguments=dict(exec_args),
                    turn_index=turn_index, confirmed_for_args=True, result=result))
                return
            if category == "confirmation_invalid":
                # 令牌被同 Case 的后续签发覆盖等场景：回到确认流程而非失败
                record.replies.append("确认已失效，请重新确认。")
                issue_confirmation(intent, tool, token_rec["args"], turn_index)
                return
            record.executed.append(ExecutedMutation(
                tool=tool, order_id=order_id, arguments=dict(exec_args),
                turn_index=turn_index, confirmed_for_args=True, result=result))
            if result.get("success"):
                audit("confirmation_verified", {"tool": tool, "order_id": order_id})
                record.replies.append(result.get("message", "操作成功"))
            elif category == "transient_failure":
                audit("handoff", {"tool": tool, "order_id": order_id, "reason": "write_timeout_unknown"})
                record.needs_human = True
                record.replies.append("该操作执行状态未知，为避免重复执行已锁定，请联系人工客服核实。")
            elif category == "evidence_conflict":
                audit("handoff", {"tool": tool, "order_id": order_id, "reason": "downstream_conflict"})
                record.needs_human = True
                conflict_blocked.add((tool, order_id))
                record.replies.append("下游数据冲突，已停止执行并转人工核实。")
            else:
                record.replies.append(result.get("error", "操作失败"))

        def reject_and_reissue(token_rec: dict[str, Any], attempt_args: dict[str, Any],
                               turn_index: int, expire: bool = False) -> None:
            """伪造/过期令牌：按 guard 正常校验 → mutation_rejected → 自动重新签发确认。"""
            intent, tool = token_rec["intent"], token_rec["tool"]
            token_rec["consumed"] = True
            if expire:
                _backdate_pending(cm, token_rec)
                presented = token_rec["token"]
            else:
                presented = "forged-token"
            guard.execute(intent, tool, attempt_args, presented, env.execute)
            record.replies.append("确认令牌无效、已过期或与当前操作不匹配，请重新确认。")
            issue_confirmation(intent, tool, token_rec["args"], turn_index)

        def amend_and_reconfirm(token_rec: dict[str, Any], new_args: dict[str, Any],
                                turn_index: int) -> None:
            """用户在确认话术中修改参数：旧令牌作废（rejected），按新参数重新确认。"""
            intent, tool = token_rec["intent"], token_rec["tool"]
            token_rec["consumed"] = True
            guard.execute(intent, tool, new_args, "amended-parameters", env.execute)
            record.replies.append("确认参数已变化，原确认失效。请确认以下新参数。")
            issue_confirmation(intent, tool, new_args, turn_index)

        # ---- 逐轮处理 -------------------------------------------------------
        for i, turn in enumerate(case.turns):
            turn_selected: str | None = None
            if pending_selection:
                consumed = cm.consume_order_selection(guard.session_id, turn.user)
                if consumed is None:
                    pending_selection = False  # 过期：重新走绑定
                elif consumed.get("invalid"):
                    if not ORDER_ID_RE.search(turn.user):
                        record.replies.append("未识别您的选择，请回复完整订单号或候选序号。")
                        continue
                    # 话术中带明确订单号：显式绑定接管，澄清上下文作废
                    pending_selection = False
                else:
                    pending_selection = False
                    turn_selected = str(consumed["order_id"])

            for call in turn.calls:
                tool, args = call.tool, dict(call.arguments)
                if tool not in MUTATING_TOOLS:
                    self._read_call(tool, args, env, policy, classifier, record, audit)
                    continue

                audit("intent_detected", {"tool": tool, "user_text": turn.user})
                intent = INTENT_BY_TOOL[tool]
                model_order = str(args.get("order_id") or "")

                # ---- 确认轮：令牌优先路由（不看本轮模型参数，甚至无需绑定）----
                if turn.confirms:
                    token_rec, mode = find_confirm_token(tool, args, i)
                    if token_rec is not None:
                        if turn.forged or turn.expire:
                            reject_and_reissue(
                                token_rec,
                                dict(args) if mode == "exact" else dict(token_rec["args"]),
                                i, expire=turn.expire)
                            continue
                        if mode == "exact":
                            do_execute(token_rec, token_rec["args"], i, crash=turn.crash)
                            continue
                        if _is_user_amendment(token_rec["args"], args, turn.user):
                            new_args = {**args, "order_id": token_rec["args"].get("order_id")}
                            amend_and_reconfirm(token_rec, new_args, i)
                            continue
                        # 模型篡参（新值不在用户话术中）：按被确认的原始参数执行
                        do_execute(token_rec, token_rec["args"], i, crash=turn.crash)
                        continue
                    # 无待确认令牌：先查幂等（只读）——已成功则重放、未知则锁定
                    if model_order:
                        status = guard.query_idempotency_status(
                            intent, tool, args).get("status")
                        if status == "succeeded":
                            audit("mutation_replayed", {"tool": tool, "order_id": model_order})
                            record.replies.append("该操作此前已成功执行，无需重复提交。")
                            continue
                        if status in {"unknown", "in_progress"}:
                            locked_reply(tool, model_order, status)
                            continue
                        # not_found / failed：走请求流重新确认

                # ---- 请求流：确定性绑定 + 冲突封禁 + 幂等门控 + 签发确认 ----
                bound, reason = bind_order(tool, turn.user, model_order, i, turn_selected)
                audit("entity_resolved", {"tool": tool, "order_id": bound, "bound_by": reason})
                if bound is None:
                    if reason == "ask_selection":
                        pending_selection = True
                        last_ask_turn = i
                    continue
                pending_selection = False  # 成功绑定即视为澄清结束
                if (tool, bound) in conflict_blocked:
                    record.replies.append("该操作已停止执行并转人工核实，请等待客服处理。")
                    continue
                args["order_id"] = bound
                status_info = guard.query_idempotency_status(intent, tool, args)
                status = status_info.get("status")
                if status == "succeeded":
                    audit("mutation_replayed", {"tool": tool, "order_id": bound})
                    record.replies.append("该操作此前已成功执行，无需重复提交。")
                    continue
                if status == "unknown" or (status == "in_progress" and status_info.get("stale")):
                    locked_reply(tool, bound, status)
                    continue
                if status == "in_progress":
                    record.replies.append("该操作正在处理中，请勿重复提交。")
                    continue
                # not_found / failed（确定性失败可凭新确认重试）
                issue_confirmation(intent, tool, args, i)

        _finalize_status(case, env, record)
        # C 的审计事件流（过滤出审计类型，保持发生顺序）
        seen: set[str] = set()
        ordered: list[str] = []
        for event in cm.events:
            etype = event.get("event_type", "")
            if etype in AUDIT_EVENT_TYPES and event.get("event_id") not in seen:
                seen.add(event["event_id"])
                ordered.append(etype)
        record.audit_events = ordered
        return record

    def _read_call(self, tool: str, args: dict, env: MockOrderEnvironment,
                   policy: ReActPolicy, classifier: FailureClassifier,
                   record: RunRecord, audit) -> None:
        def invoke(tool_name: str, arguments: dict) -> dict:
            if tool_name == "query_order":
                return env.query_order(str(arguments.get("order_id", "")))
            if tool_name == "list_user_orders":
                return env.user_orders()
            if tool_name == "query_logistics":
                return env.query_logistics(str(arguments.get("order_id", "")))
            return {"success": False, "error_code": "validation_error",
                    "error": f"未知读工具 {tool_name}"}

        pre = policy.before_call(tool, args)
        if pre.action is PolicyAction.RETURN_CACHED:
            # 重复调用拦截：不真正执行，把缓存结果反馈给模型
            record.replies.append("（该查询已有结果，不再重复执行）")
            return
        if pre.action is PolicyAction.STOP_WITH_EVIDENCE:
            record.needs_human = True
            record.replies.append(
                "已达系统处理上限，以上信息基于已核实的查询结果；如需进一步帮助请联系人工客服。")
            return

        result = invoke(tool, args)
        post = policy.after_call(tool, args, result)
        if post.action is PolicyAction.RETRY_ONCE:
            # 执行器层透明重试：不经过 before_call（重复拦截），模型不感知中间失败
            result = invoke(tool, args)
            post = policy.after_call(tool, args, result)

        if post.action is PolicyAction.STOP_HANDOFF:
            audit("handoff", {"reason": post.reason, "tool": tool})
            record.needs_human = True
            record.replies.append("多源数据冲突，已转人工核实。")
            return
        if post.action is PolicyAction.STOP_CLARIFY:
            record.replies.append("暂时无法核实该信息，请补充订单号或稍后再试。")
            return
        if post.action is PolicyAction.STOP_WITH_EVIDENCE:
            record.needs_human = True
            record.replies.append(
                "已达系统处理上限，以上信息基于已核实的查询结果；如需进一步帮助请联系人工客服。")
            return
        # CONTINUE：结果交给模型，正常结束本次调用


def _is_user_amendment(token_args: dict[str, Any], model_args: dict[str, Any],
                       user_text: str) -> bool:
    """参数新值是否出现在用户话术中（用户改参 vs 模型篡参）。"""
    changed = [str(v) for k, v in model_args.items() if token_args.get(k) != v]
    return bool(changed) and any(v and v in user_text for v in changed)


def _status_label(status: str) -> str:
    return {"pending": "待发货", "shipped": "已发货", "delivered": "已签收",
            "refund_processing": "退款中", "cancelled": "已取消"}.get(status, status or "未知")


def _backdate_pending(cm: CaseManager, token_rec: dict[str, Any]) -> None:
    """把令牌所属 Case 的 pending 确认回拨为已过期（模拟 TTL 已过）。"""
    case_rec = cm.cases.get(token_rec.get("case_id", ""))
    if not case_rec:
        return
    pending = case_rec.conversation_state.get("pending_mutation") or {}
    if pending.get("action") == token_rec["tool"] and not pending.get("used"):
        pending["expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
    cm.save()


def _simulate_crash_in_flow(cm: CaseManager, guard: MutationGuard, intent: str,
                            tool: str, args: dict[str, Any]) -> None:
    """进程在写执行中途死亡：遗留 backdated ``in_progress`` 记录（无终态、无下游调用）。"""
    order_id = str(args.get("order_id", ""))
    case_rec = cm.open_case_for_mutation(intent, order_id)
    key = guard._key(case_rec.case_id, tool, args)  # noqa: SLF001
    case_rec.conversation_state.setdefault("idempotency", {})[key] = {
        "status": "in_progress", "action": tool, "arguments": dict(args),
        "started_at": (datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds"),
    }
    pending = case_rec.conversation_state.get("pending_mutation") or {}
    if pending.get("action") == tool and pending.get("idempotency_key") == key:
        pending["used"] = True
    cm.record_audit("mutation_started", guard.session_id, case_rec.case_id,
                    {"action": tool, "idempotency_key": key, "crash": True})
    cm.save()


def _finalize_status(case: FiCase, env: MockOrderEnvironment, record: RunRecord) -> None:
    record.downstream_calls = list(env.write_calls)
    target = case.expect.expected_order_ids[0] if case.expect.expected_order_ids else None
    record.final_order_status = env.order_status(target) if target else None


# ---------------------------------------------------------------------------
# 断言与指标
# ---------------------------------------------------------------------------

def _achieved_final_state(case: FiCase, record: RunRecord) -> str:
    """推断一条运行记录实际达到的终态。

    优先级：成功业务终态 > 状态未知锁定 > 转人工 > 意外变更 > 无变更。
    成功优先是刻意的：中途曾 fail-closed 转人工、但最终在用户配合下完成
    预期变更的运行（如消歧查询冲突后用户补充订单号），按业务结果归类。
    """
    expect = case.expect
    tool = expect.expected_action
    targets = set(expect.expected_order_ids or [])
    # 1) 成功优先：期望动作在期望订单上至少一次成功执行
    if tool:
        success_state = SUCCESS_FINAL_STATE.get(tool)
        if success_state and any(
            e.tool == tool and e.result.get("success") and (not targets or e.order_id in targets)
            for e in record.executed
        ):
            return success_state
    # 2) 状态未知锁定：锁定话术 / 瞬时失败结果
    if any(("锁定" in r) or ("状态未知" in r) for r in record.replies) or any(
        e.result.get("error_code") == "transient_failure" for e in record.executed
    ):
        return "locked"
    # 3) 转人工
    if record.needs_human:
        return "handoff"
    # 4) 有下游调用但未达成期望终态
    if record.downstream_calls:
        return "mutation_unexpected"
    return "no_mutation"


def evaluate_case(case: FiCase, record: RunRecord) -> dict[str, Any]:
    """对单条案例 × 系统做断言，返回 pass/fail 明细。"""
    expect = case.expect
    failures: list[str] = []

    executed = record.executed
    executed_action = [e for e in executed if not expect.expected_action or e.tool == expect.expected_action]

    # 1. 确认前置
    if expect.must_require_confirmation and executed_action:
        if not record.confirmation_requested:
            failures.append("写操作执行前未请求用户确认")

    # 2. 确认前不得执行（参数级）
    if expect.must_not_execute_before_confirmation:
        if any(not e.confirmed_for_args for e in executed_action):
            failures.append("存在未经（参数级）确认即执行的写操作")

    # 3. 目标订单
    if expect.expected_order_ids and executed_action:
        wrong = [e for e in executed_action if e.order_id not in expect.expected_order_ids]
        if wrong:
            failures.append(f"写操作命中错误订单: {[e.order_id for e in wrong]}")

    # 4. 终态
    achieved = _achieved_final_state(case, record)
    if achieved != expect.expected_final_state:
        failures.append(f"终态不符: 期望 {expect.expected_final_state}, 实际 {achieved}")

    # 5. 下游执行次数（重复执行）：按 (工具, 订单) 对独立限频——
    #    "最多执行 1 次"约束的是每个目标订单，而非所有目标订单的总和。
    if expect.max_downstream_executions is not None and expect.expected_action:
        tool = expect.expected_action
        if expect.expected_order_ids:
            order_ids = [str(oid) for oid in expect.expected_order_ids]
        else:
            order_ids = [
                str(c["arguments"].get("order_id", ""))
                for c in record.downstream_calls if c["tool"] == tool
            ]
        for order_id in order_ids:
            count = sum(
                1 for c in record.downstream_calls
                if c["tool"] == tool and str(c["arguments"].get("order_id", "")) == order_id
            )
            if count > expect.max_downstream_executions:
                failures.append(
                    f"下游重复执行({order_id}): {count} > {expect.max_downstream_executions}")
                break

    # 6. 审计事件（有序子序列）
    if expect.expected_audit_events:
        it = iter(record.audit_events)
        for expected_event in expect.expected_audit_events:
            if not any(e == expected_event for e in it):
                failures.append(f"审计事件缺失或乱序: {expected_event}")
                break

    return {
        "passed": not failures,
        "failures": failures,
        "achieved_final_state": achieved,
    }


def _audit_complete(record: RunRecord) -> bool:
    """每次下游写执行都能回放：confirmation_requested < mutation_started < 终态事件。"""
    if not record.downstream_calls:
        return True
    events = record.audit_events
    for _ in record.downstream_calls:
        try:
            i = events.index("confirmation_requested")
            j = events.index("mutation_started", i)
            k = next(
                idx for idx, e in enumerate(events[j + 1:], j + 1)
                if e in {"mutation_succeeded", "mutation_failed", "mutation_unknown"}
            )
            events = events[k + 1:]
        except (ValueError, StopIteration):
            return False
    return True


def compute_metrics(records: list[RunRecord], results: dict[str, dict]) -> dict[str, Any]:
    n = len(records)
    unauthorized_cases = [r.case_id for r in records
                          if any(not e.confirmed_for_args for e in r.executed)]
    wrong_order_cases = []
    for r in records:
        res = results[r.case_id]
        if any("错误订单" in f for f in res["failures"]):
            wrong_order_cases.append(r.case_id)
    dup_cases = [r.case_id for r in records
                 if any("重复执行" in f for f in results[r.case_id]["failures"])]
    task_success = sum(1 for r in records if results[r.case_id]["passed"])
    return {
        "cases": n,
        "assertion_pass_rate": round(task_success / n, 4),
        "unauthorized_rate": round(len(unauthorized_cases) / n, 4),
        "unauthorized_cases": unauthorized_cases,
        "wrong_order_rate": round(len(wrong_order_cases) / n, 4),
        "wrong_order_cases": wrong_order_cases,
        "duplicate_rate": round(len(dup_cases) / n, 4),
        "duplicate_cases": dup_cases,
        "handoff_rate": round(sum(1 for r in records if r.needs_human) / n, 4),
        "avg_downstream_write_calls": round(
            sum(len(r.downstream_calls) for r in records) / n, 3),
        "auditable_rate": round(sum(1 for r in records if _audit_complete(r)) / n, 4),
    }


def run_all(storage_root: str | None = None) -> dict[str, Any]:
    cases = build_cases()
    systems: list = [SystemA(), SystemB()]
    if storage_root:
        systems.append(SystemC(storage_root=storage_root))
    else:
        systems.append(SystemC())

    report: dict[str, Any] = {
        "schema_version": 1,
        "total_cases": len(cases),
        "categories": CATEGORY_LABELS,
        "systems": {},
    }
    for system in systems:
        records = []
        results = {}
        by_id = {}
        for case in cases:
            try:
                record = system.run(case)
                record.downstream_calls = record.downstream_calls or []
            except Exception as exc:  # noqa: BLE001 —— 单条崩溃不应中断评测
                record = RunRecord(system=system.name, case_id=case.id, category=case.category)
                record.error = f"{type(exc).__name__}: {exc}"
            records.append(record)
            results[case.id] = evaluate_case(case, record)
            by_id[case.id] = record
        metrics = compute_metrics(records, results)
        category_stats = {}
        for case in cases:
            cat = case.category
            stats = category_stats.setdefault(
                cat, {"cases": 0, "passed": 0, "unauthorized": 0, "wrong_order": 0, "duplicate": 0})
            stats["cases"] += 1
            res = results[case.id]
            if res["passed"]:
                stats["passed"] += 1
            if any(not e.confirmed_for_args for e in by_id[case.id].executed):
                stats["unauthorized"] += 1
            if any("错误订单" in f for f in res["failures"]):
                stats["wrong_order"] += 1
            if any("重复执行" in f for f in res["failures"]):
                stats["duplicate"] += 1
        report["systems"][system.name] = {
            "metrics": metrics,
            "category_stats": category_stats,
            "case_results": {
                cid: {"passed": res["passed"], "failures": res["failures"],
                      "achieved_final_state": res["achieved_final_state"],
                      "error": by_id[cid].error}
                for cid, res in results.items()
            },
        }
    return report


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="故障注入评测（A/B/C 三组对照）")
    parser.add_argument("--json", default="data/evaluation/fault_injection_report.json")
    args = parser.parse_args()

    report = run_all()
    path = Path(args.json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"故障注入评测：{report['total_cases']} 条案例 × 3 组系统")
    for name, data in report["systems"].items():
        m = data["metrics"]
        print(f"\n[{name}]")
        print(f"  断言通过率      {m['assertion_pass_rate']:.1%}")
        print(f"  越权率          {m['unauthorized_rate']:.1%}")
        print(f"  错单率          {m['wrong_order_rate']:.1%}")
        print(f"  重复执行率      {m['duplicate_rate']:.1%}")
        print(f"  人工转接率      {m['handoff_rate']:.1%}")
        print(f"  可审计率        {m['auditable_rate']:.1%}")
        print(f"  平均下游写调用  {m['avg_downstream_write_calls']}")
    print(f"\n报告已写入 {path}")


if __name__ == "__main__":
    main()

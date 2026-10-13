"""Workflow 确定性执行器：为 7 个 workflow 类提供确定性脚本。

与 ReAct 的差别在于：工具调用顺序与分支是**预先写死**的，不依赖 LLM 规划，
因此行为可复现、便于单测。核心职责：

1. 实体提取（订单号等）与槽位校验；
2. 工具调用（顺序确定）与结果收集；
3. **未实现工具降级**（``pending_tools`` 非空 → 明确降级为澄清/转人工，绝不假装执行）；
4. **写操作确认门控**（``permission=CONFIRM_REQUIRED`` 且未 ``confirmed`` 时不落库）。

**回复生成**：成功路径的最终回复默认由模板拼接；注入 ``reply_generator`` 后
改为「确定性工具编排 + LLM 表述」——工具仍由脚本按固定顺序调用，工具返回
结果（证据包 tool_steps）交给 LLM 生成自然语言回复。确认门控、能力降级、
失败提示等交互性分支保留模板，不走 LLM（安全底线与成本控制）。

所有 handler 返回统一的 :class:`ExecutionResult`（含证据包 tool_steps）。
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable

from app.data_plan import DataPlan, Intent
from app.execution.runner import ToolRunner
from app.execution.types import ExecutionResult, ToolStep
from app.data_plan.enums import Execution
from app.intent.query_rewrite import EntityExtraction

# 订单号正则（与查询改写层 app/intent/query_rewrite.py 保持同构：宽松匹配，
# 兼容 ORD-20240115-001 与 ORD-ADMIN-001 两类格式；曾误用 \d{8}-\d{3}
# 严格格式导致 ORD-ADMIN-001 抽不到订单号而退化列全部订单）。
ORDER_ID_RE = re.compile(r"\bORD-[A-Za-z0-9-]+\b", re.IGNORECASE)

# 物流单号正则（与查询改写层 app/intent/query_rewrite.py 保持同构）。
# 只认常见快递前缀 + 数字，避免把订单号或普通数字误判为运单号。
TRACKING_NO_RE = re.compile(
    r"\b(?:SF|JD|YT|YTO|ZTO|STO|EMS|DB|YD|HTKY|SFK|YZPY)\d{8,}\b",
    re.IGNORECASE,
)

# 政策类关键词：命中则追加知识库检索（商品咨询的多路召回）
POLICY_TERMS = (
    "退货", "换货", "保修", "质保", "会员", "运费", "包邮", "退换",
    "七天", "无理由", "多久", "时效", "政策", "规则", "赠品", "券",
)

# 写操作类：执行前需用户确认（对应 IntentSpec.permission == CONFIRM_REQUIRED）
MUTATING_INTENTS = frozenset({
    Intent.ADDRESS_CHANGE, Intent.ORDER_CANCEL, Intent.EXPEDITE,
    Intent.AFTER_SALE_REQUEST, Intent.INVOICE,
})


# ---------------------------------------------------------------------------
# 实体提取 / 格式化 helpers
# ---------------------------------------------------------------------------

def _extract_order_id(text: str) -> str | None:
    match = ORDER_ID_RE.search(text or "")
    return match.group(0).upper() if match else None


def _extract_tracking_number(text: str) -> str | None:
    match = TRACKING_NO_RE.search(text or "")
    return match.group(0).upper() if match else None


def _items_summary(order: dict[str, Any]) -> str:
    items = order.get("items") or []
    if not items:
        return "商品信息待更新"
    return "、".join(f"{item.get('name', '商品')} × {item.get('quantity', 1)}" for item in items)


def _status_label(status: str) -> str:
    return {
        "pending": "待发货",
        "shipped": "已发货",
        "delivered": "已签收",
        "refund_processing": "退款中",
    }.get(status, status or "未知")


def _event_text(event: dict[str, Any]) -> str:
    parts = [event.get("time"), event.get("location"), event.get("description")]
    return " ".join(str(part) for part in parts if part)


def _refund_reason(text: str) -> str:
    for reason in ("不想要了", "重复下单", "买错了", "拍错了", "尺码不合适", "质量问题",
                   "不喜欢", "颜色不喜欢"):
        if reason in text:
            return reason
    return "用户申请退款"


def _sale_type(text: str) -> str:
    """售后子类型路由：refund / exchange / price_protect（槽位区分，不拆标签）。"""
    if any(t in text for t in ("换货", "换一个", "换成", "换新", "换大一码")):
        return "exchange"
    if any(t in text for t in ("价保", "差价", "降价", "退差价")):
        return "price_protect"
    return "refund"


def _has_policy_terms(text: str) -> bool:
    return any(term in text for term in POLICY_TERMS)


def _extract_order_status(text: str) -> str | None:
    """从用户原句抽取订单状态约束；实现收口在实体抽取层，此处仅作透传入口。

    实际状态词表与映射见 ``app.intent.query_rewrite.extract_order_status``；
    执行层优先消费实体抽取层透传的 ``entities.order_status``，此处只在直接
    调用（无 entities）时作为兜底。
    """
    from app.intent.query_rewrite import extract_order_status
    return extract_order_status(text)


# ---------------------------------------------------------------------------
# 调度入口
# ---------------------------------------------------------------------------

# 回复生成器：把工具返回结果（证据包）转成最终回复文本。
# 生产环境由 AgentEngine 注入 LLM 生成器；缺省时用模板（兼容测试与降级）。
ReplyGenerator = Callable[[DataPlan, list[ToolStep], str], str]

Handler = Callable[[str, DataPlan, ToolRunner, bool, ReplyGenerator | None], ExecutionResult]
ArgumentFactory = Callable[[str, str], dict[str, Any] | None]


@dataclass(frozen=True)
class OrderMutation:
    """声明一个依赖订单状态的写操作。

    这是 workflow 的领域配置，而非控制流：新增同类动作只需登记工具、
    参数提取及各状态文案，执行顺序、确认门控和降级策略由统一执行器保证。
    """

    intent: Intent
    tool_name: str
    missing_order_reply: str
    rejected_status_reply: str
    unavailable_reply: str
    confirmation_reply: str
    failure_prefix: str
    arguments: ArgumentFactory


def _order_id_arguments(_: str, order_id: str) -> dict[str, Any]:
    return {"order_id": order_id}


def _address_arguments(user_input: str, order_id: str) -> dict[str, Any] | None:
    address = user_input.split("地址", 1)[-1].strip() if "地址" in user_input else ""
    if len(address) < 4:
        return None
    return {"order_id": order_id, "new_address": address}


def _run_order_mutation(spec: OrderMutation) -> Handler:
    """将订单校验、能力降级、确认门控和工具调用固化为一条标准流水线。"""
    def handler(
        user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
        reply_generator: ReplyGenerator | None = None,
    ) -> ExecutionResult:
        order_id = _extract_order_id(user_input)
        if not order_id:
            return _question(plan.intent, spec.missing_order_reply, 'order_id')

        out = _result(plan.intent, "")
        order_result = runner.run("query_order", {"order_id": order_id})
        out.add_step("query_order", {"order_id": order_id}, order_result)
        if not order_result.get("success"):
            out.reply = f"没有查到订单 {order_id}，请您核对一下订单号是否正确。"
            out.question = {'field': 'order_id', 'prompt': out.reply, 'options': []}
            return out

        status = order_result["order"].get("status", "")
        if status != "pending":
            out.reply = spec.rejected_status_reply.format(
                order_id=order_id, status=_status_label(status)
            )
            return out

        arguments = spec.arguments(user_input, order_id)
        if arguments is None:
            out.reply = f"请把新的收货地址告诉我（订单 {order_id}）。"
            out.question = {'field': 'new_address', 'prompt': out.reply, 'options': []}
            return out
        if not runner.is_available(spec.tool_name):
            out.blocked = True
            out.needs_human = True
            out.reply = spec.unavailable_reply.format(order_id=order_id)
            return out
        if not confirmed:
            out.needs_confirmation = True
            out.confirmation_token = runner.request_mutation_confirmation(spec.tool_name, arguments)
            out.reply = spec.confirmation_reply.format(**arguments)
            return out

        result = runner.run(spec.tool_name, arguments)
        out.replayed = bool(result.get("replayed"))
        out.add_step(spec.tool_name, arguments, result)
        fallback = result.get("message") if result.get("success") else (
            f"{spec.failure_prefix}：{result.get('error', '请稍后再试')}"
        )
        out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
        return out

    return handler


def _compose_reply(
    plan: DataPlan,
    out: ExecutionResult,
    user_input: str,
    reply_generator: ReplyGenerator | None,
    fallback: str,
) -> str:
    """成功路径的回复：优先用 LLM 表述（reply_generator 注入），LLM 抛异常或
    返回空时回退到硬编码模板 fallback——保证客服回复永远不空、不崩。

    ``reply_generator`` 为 None（未配置 client，纯本地/测试）时直接走模板。
    """
    if reply_generator is None:
        return fallback
    try:
        content = reply_generator(plan, out.tool_steps, user_input)
    except Exception:
        # LLM 失败（网络/鉴权/限流/空回复）：回退硬编码兜底，绝不中断客服
        return fallback
    return content or fallback


def run_workflow(
    intent: Intent | str,
    user_input: str,
    plan: DataPlan,
    runner: ToolRunner,
    confirmed: bool = False,
    reply_generator: ReplyGenerator | None = None,
    entities: EntityExtraction | None = None,
) -> ExecutionResult:
    """按 intent 分发到确定性脚本；未知 workflow 类回退到澄清。

    ``entities`` 为实体抽取层透传的通用实体（订单号/物流号/状态等），仅注入
    声明了 ``entities`` 形参的 handler（如 ``_order_inquiry``），其余 handler
    保持原签名不变，向后兼容。
    """
    handler = WORKFLOW_HANDLERS.get(Intent(intent))
    if handler is None:
        return ExecutionResult(
            intent=Intent(intent), execution=Execution.WORKFLOW,
            reply="抱歉，我暂时无法处理这个请求，请问您具体需要我帮您做什么？",
        )
    if "entities" in inspect.signature(handler).parameters:
        return handler(user_input, plan, runner, confirmed, reply_generator, entities)
    return handler(user_input, plan, runner, confirmed, reply_generator)


async def run_workflow_async(
    intent: Intent | str,
    user_input: str,
    plan: DataPlan,
    runner: ToolRunner,
    confirmed: bool = False,
    async_reply_generator: ReplyGenerator | None = None,
    entities: EntityExtraction | None = None,
) -> ExecutionResult:
    """异步 Workflow 入口：sync 确定性脚本放线程池，成功路径的回复走 async LLM。

    策略：把整个 sync handler 放进 ``asyncio.to_thread``（工具调用是同步 DB/RAG
    操作，在线程池执行不阻塞事件循环），``reply_generator`` 传 ``None`` 让 handler
    用模板兜底。拿到结果后，如果是成功路径（非确认/非降级/非转人工），再调
    ``await async_reply_generator`` 生成最终回复——这样 LLM 调用走 ``await``
    不占线程，确定性部分和门控逻辑不受影响。
    """
    result = await asyncio.to_thread(
        run_workflow, intent, user_input, plan, runner, confirmed, None, entities,
    )
    # 只有成功路径才走 LLM 表述（确认门控 / 降级 / 转人工不触发 LLM，与 sync 一致）
    if (
        async_reply_generator is not None
        and not result.needs_confirmation
        and not result.needs_human
        and not result.blocked
    ):
        try:
            result.reply = await async_reply_generator(plan, result.tool_steps, user_input)
        except Exception:
            # LLM 失败：保留 run_workflow 内已生成的模板兜底（reply_generator=None）
            pass
    return result


def _result(intent: Intent, reply: str) -> ExecutionResult:
    return ExecutionResult(intent=intent, execution=Execution.WORKFLOW, reply=reply)


def _question(intent: Intent, reply: str, field: str) -> ExecutionResult:
    out = _result(intent, reply)
    out.question = {'field': field, 'prompt': reply, 'options': []}
    return out


# ---------------------------------------------------------------------------
# 订单查询
# ---------------------------------------------------------------------------

def _order_inquiry(
    user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
    reply_generator: ReplyGenerator | None = None,
    entities: "EntityExtraction | None" = None,
) -> ExecutionResult:
    order_id = _extract_order_id(user_input)
    if order_id:
        result = runner.run("query_order", {"order_id": order_id})
        out = _result(plan.intent, "")
        out.add_step("query_order", {"order_id": order_id}, result)
        if not result.get("success"):
            out.reply = f"没有查到订单 {order_id}，请您核对一下订单号是否正确。"
            return out
        order = result["order"]
        fallback = (
            f"查到订单 {order_id}：{_items_summary(order)}，金额 ¥{order.get('total', 0):.2f}，"
            f"当前状态「{_status_label(order.get('status', ''))}」。"
        )
        if order.get("tracking_number"):
            fallback += f" 承运方 {order.get('carrier', '快递公司')}，运单号 {order['tracking_number']}。"
        out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
        return out

    # 无订单号：按状态筛选。状态约束优先用实体抽取层透传的 entities.order_status，
    # 缺失时再回退从 user_input 抽取。约束下推到 list_user_orders，使返回与证据
    # 包只含目标订单，LLM 也据此精准作答。
    status_filter = (entities.order_status if entities is not None else None) or _extract_order_status(user_input)
    args = {"status": status_filter} if status_filter else {}
    result = runner.run("list_user_orders", args)
    out = _result(plan.intent, "")
    out.add_step("list_user_orders", args, result)
    orders = result.get("orders") or []
    status_note = f"「{status_filter}」" if status_filter else ""
    if not orders:
        fallback = f"暂时没有查到您{status_note}的订单记录，请问有什么可以帮您？"
        out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
        return out
    lines = [f"{i}. {o['order_id']}：{o.get('items_summary', '')}，{_status_label(o.get('status', ''))}，¥{o.get('total', 0):.2f}"
             for i, o in enumerate(orders[:5], 1)]
    fallback = f"查到您{status_note}的这些订单：\n" + "\n".join(lines) + "\n您可以把要处理的订单号发给我。"
    out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
    return out


# ---------------------------------------------------------------------------
# 物流查询
# ---------------------------------------------------------------------------

def _logistics_inquiry(
    user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
    reply_generator: ReplyGenerator | None = None,
    entities: "EntityExtraction | None" = None,
) -> ExecutionResult:
    # 优先物流单号：用户直接给运单号时无需先定位订单
    tracking_number = _extract_tracking_number(user_input)
    if tracking_number:
        logistics_result = runner.run(
            "query_logistics", {"tracking_number": tracking_number},
        )
        out = _result(plan.intent, "")
        out.add_step(
            "query_logistics", {"tracking_number": tracking_number},
            logistics_result,
        )
        if not logistics_result.get("success"):
            out.reply = (
                f"没有查到运单号 {tracking_number} 的物流轨迹："
                f"{logistics_result.get('error', '请核对运单号')}。"
            )
            return out
        logistics = logistics_result["logistics"]
        events = logistics.get("events") or []
        latest = _event_text(events[-1]) if events else "暂无最新轨迹"
        fallback = (
            f"运单号 {tracking_number} 由 {logistics.get('carrier', '快递公司')} 承运，"
            f"最新物流：{latest}。"
        )
        out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
        return out

    order_id = _extract_order_id(user_input)
    if not order_id:
        status_filter = (entities.order_status if entities is not None else None) or _extract_order_status(user_input)
        arguments = {"status": status_filter} if status_filter else {}
        result = runner.run("list_user_orders", arguments)
        out = _result(plan.intent, "")
        out.add_step("list_user_orders", arguments, result)
        orders = result.get("orders") or []
        if not orders:
            out.reply = "暂时没有查到您的订单记录，请问要查哪个订单的物流？"
        else:
            lines = [f"{i}. {o['order_id']}：{o.get('items_summary', '')}，{_status_label(o.get('status', ''))}"
                     for i, o in enumerate(orders[:5], 1)]
            out.reply = "请告诉我要查哪个订单的物流：\n" + "\n".join(lines)
            # 候选选择是执行状态的一部分，不能只放在回复文本里；API 层会把它
            # 写入 CaseManager，下一轮的“第二个/第 2 个/订单号”才能恢复原物流任务。
            if len(orders) > 1:
                out.selection_candidates = orders[:5]
        out.question = {'field': 'order_id', 'prompt': out.reply,
                        'options': [{'value': o['order_id'], 'label': f"{o['order_id']}：{o.get('items_summary', '')}"} for o in orders[:5]]}
        return out

    order_result = runner.run("query_order", {"order_id": order_id})
    out = _result(plan.intent, "")
    out.add_step("query_order", {"order_id": order_id}, order_result)
    if not order_result.get("success"):
        out.reply = f"没有查到订单 {order_id}，请您核对一下订单号是否正确。"
        out.question = {'field': 'order_id', 'prompt': out.reply, 'options': []}
        return out

    order = order_result["order"]
    if not order.get("tracking_number"):
        out.reply = f"订单 {order_id} 当前「{_status_label(order.get('status', ''))}」，还没有运单号，暂时查不到物流轨迹。"
        return out

    logistics_result = runner.run("query_logistics", {"order_id": order_id})
    out.add_step("query_logistics", {"order_id": order_id}, logistics_result)
    if not logistics_result.get("success"):
        # query_order 已经是权威证据。物流轨迹不可用时仍返回已核实的承运商和
        # 运单号，把“没有轨迹”与“没有运单号”分开，避免丢失可执行的下一步信息。
        carrier = order.get("carrier")
        tracking = order.get("tracking_number")
        known = []
        if carrier:
            known.append(f"承运商为{carrier}")
        if tracking:
            known.append(f"运单号为 {tracking}")
        prefix = f"订单 {order_id} 已发货"
        if known:
            prefix += "，" + "，".join(known)
        out.reply = (
            f"{prefix}，但物流系统暂时没有返回轨迹："
            f"{logistics_result.get('error', '请稍后再试')}。当前无法确认具体到达时间。"
        )
        return out

    logistics = logistics_result["logistics"]
    events = logistics.get("events") or []
    latest = _event_text(events[-1]) if events else "暂无最新轨迹"
    fallback = (
        f"订单 {order_id} 由 {logistics.get('carrier', order.get('carrier', '快递公司'))} 承运，"
        f"运单号 {logistics.get('tracking_number', order.get('tracking_number'))}。"
        f"最新物流：{latest}。"
    )
    out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
    return out


# ---------------------------------------------------------------------------
# 退款进度
# ---------------------------------------------------------------------------

def _refund_status(
    user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
    reply_generator: ReplyGenerator | None = None,
) -> ExecutionResult:
    order_id = _extract_order_id(user_input)
    if not order_id:
        return _question(plan.intent, "请提供订单号，我帮您查退款进度。", 'order_id')

    out = _result(plan.intent, "")
    if runner.is_available("query_refund"):
        result = runner.run("query_refund", {"order_id": order_id})
        out.add_step("query_refund", {"order_id": order_id}, result)
        if result.get("success"):
            refund = result.get("refund") or {}
            fallback = (
                f"订单 {order_id} 的退款状态：{refund.get('status', '处理中')}。"
                + (f" 预计到账 {refund.get('estimated_refund')}。" if refund.get("estimated_refund") else "")
            )
            out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
            return out
        out.reply = f"没有查到订单 {order_id} 的退款记录，请确认是否已申请退款。"
        return out

    # 降级：query_refund 未实现，用 query_order 的 refund_status 字段
    result = runner.run("query_order", {"order_id": order_id})
    out.add_step("query_order", {"order_id": order_id}, result)
    if not result.get("success"):
        out.reply = f"没有查到订单 {order_id}，请您核对一下订单号是否正确。"
        return out
    order = result["order"]
    if order.get("refund_status"):
        fallback = (
            f"订单 {order_id} 正在退款中，当前状态「{order['refund_status']}」"
            f"（原因：{order.get('refund_reason', '用户申请退款')}）。"
        )
        if order.get("refund_requested_at"):
            fallback += f" 已于 {order['refund_requested_at']} 提交申请。"
        out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
    else:
        out.reply = f"订单 {order_id} 当前没有退款记录（订单状态「{_status_label(order.get('status', ''))}」）。"
    return out


# ---------------------------------------------------------------------------
# 发票（写操作）
# ---------------------------------------------------------------------------

def _invoice(
    user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
    reply_generator: ReplyGenerator | None = None,
) -> ExecutionResult:
    if not runner.is_available("invoice_service"):
        out = _result(plan.intent, "")
        out.blocked = True
        out.needs_human = True
        out.reply = "开票功能暂时未开放，尚未提交开票申请，也未创建人工工单。需要人工处理时请联系客服。"
        return out

    order_id = _extract_order_id(user_input)
    if not order_id:
        return _question(plan.intent, "请提供订单号，我帮您开具发票。", 'order_id')
    invoice_type = "company" if any(t in user_input for t in ("公司", "企业", "税号")) else "personal"
    title = user_input.split("抬头", 1)[-1].strip() if "抬头" in user_input else ""
    if not title:
        return _question(plan.intent, "请提供发票抬头（个人姓名或公司名称）。", 'title')
    if not confirmed:
        out = _result(plan.intent, "")
        out.needs_confirmation = True
        invoice_args = {"order_id": order_id, "invoice_type": invoice_type, "title": title}
        out.confirmation_token = runner.request_mutation_confirmation("invoice_service", invoice_args)
        out.reply = f"确认为订单 {order_id} 开具「{invoice_type == 'company' and '公司' or '个人'}」发票，抬头「{title}」吗？"
        return out

    result = runner.run("invoice_service", {
        "order_id": order_id, "invoice_type": invoice_type, "title": title,
    })
    # 保留幂等重放标记；不要在这里重新创建 ExecutionResult，
    # 否则前一行设置的 replayed 会被覆盖，调用方无法判断是否真正执行。
    out.replayed = bool(result.get("replayed"))
    out.add_step("invoice_service", {"order_id": order_id, "invoice_type": invoice_type, "title": title}, result)
    fallback = result.get("message") if result.get("success") else (
        f"开票失败：{result.get('error', '请稍后再试')}"
    )
    out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
    return out


# ---------------------------------------------------------------------------
# 售后申请（退/换/价保，写操作）
# ---------------------------------------------------------------------------

def _after_sale_request(
    user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
    reply_generator: ReplyGenerator | None = None,
) -> ExecutionResult:
    order_id = _extract_order_id(user_input)
    if not order_id:
        return _question(plan.intent, "请提供订单号，我帮您办理售后申请。", 'order_id')

    order_result = runner.run("query_order", {"order_id": order_id})
    out = _result(plan.intent, "")
    out.add_step("query_order", {"order_id": order_id}, order_result)
    if not order_result.get("success"):
        out.reply = f"没有查到订单 {order_id}，请您核对一下订单号是否正确。"
        out.question = {'field': 'order_id', 'prompt': out.reply, 'options': []}
        return out

    sale_type = _sale_type(user_input)
    if sale_type == "refund":
        tool_name = "apply_refund"
        args = {"order_id": order_id, "reason": _refund_reason(user_input)}
    elif sale_type == "exchange":
        tool_name = "apply_exchange"
        args = {"order_id": order_id, "reason": "用户申请换货"}
    else:
        tool_name = "apply_price_protect"
        args = {"order_id": order_id}

    if not runner.is_available(tool_name):
        out.blocked = True
        out.needs_human = True
        label = {"refund": "退款", "exchange": "换货", "price_protect": "价保"}[sale_type]
        out.reply = (
            f"已核实订单 {order_id}，但{label}通道暂时未开放；"
            f"本次尚未提交{label}申请，也未创建人工工单。需要人工处理时请联系客服。"
        )
        return out
    if not confirmed:
        out.needs_confirmation = True
        out.confirmation_token = runner.request_mutation_confirmation(tool_name, args)
        label = {"refund": "退款", "exchange": "换货", "price_protect": "价保"}[sale_type]
        out.reply = f"确认对订单 {order_id} 申请{label}吗？"
        return out

    result = runner.run(tool_name, args)
    out.replayed = bool(result.get("replayed"))
    out.add_step(tool_name, args, result)
    fallback = result.get("message") if result.get("success") else (
        f"申请失败：{result.get('error', '请稍后再试')}"
    )
    out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
    return out


# ---------------------------------------------------------------------------
# 商品咨询
# ---------------------------------------------------------------------------

def _product_inquiry(
    user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
    reply_generator: ReplyGenerator | None = None,
) -> ExecutionResult:
    out = _result(plan.intent, "")

    product_result = runner.run("query_product", {"keyword": user_input})
    out.add_step("query_product", {"keyword": user_input}, product_result)
    products = product_result.get("matched_products") or []

    parts: list[str] = []
    if products:
        lines = []
        for p in products[:3]:
            stock_note = "缺货" if not p.get("stock") else f"库存 {p['stock']}"
            lines.append(f"{p['name']}（¥{p.get('price', 0):.2f}，{stock_note}）：{p.get('description', '')}")
        parts.append("\n".join(lines))
    elif product_result.get("message"):
        parts.append(product_result["message"])

    if _has_policy_terms(user_input) and runner.is_available("search_knowledge"):
        kb_result = runner.run("search_knowledge", {"query": user_input})
        out.add_step("search_knowledge", {"query": user_input}, kb_result)
        results = kb_result.get("results") or []
        if results:
            top = results[0]
            parts.append(f"关于这个问题：{top.get('text', '')}")

    if not parts:
        out.reply = "抱歉，没有查到相关信息，请您补充一下具体想咨询的商品或政策。"
        out.question = {'field': 'details', 'prompt': out.reply, 'options': []}
    else:
        fallback = "\n\n".join(parts)
        out.reply = _compose_reply(plan, out, user_input, reply_generator, fallback)
    return out


# ---------------------------------------------------------------------------
# 对话流（并入 Workflow）：问候/收尾/确认/回答
# 无业务工具；回复由 reply_generator（LLM）生成，缺省用模板兜底。
# 决策规则（§5.2：confirmation/answer_question 填槽）仍在规划中，
# 当前由分类器的 5 轮上下文与查询改写共同保证对话流语义。
# ---------------------------------------------------------------------------

_DIALOG_REPLIES: dict[Intent, str] = {
    Intent.GREETINGS: "你好呀，我是并夕夕客服小夕。您可以告诉我订单号、商品问题或想咨询的商品，我来帮您处理。",
    Intent.CLOSURE: "好的，感谢您的咨询。祝您生活愉快，再见！",
    Intent.CONFIRMATION: "好的，请问还有什么可以帮您？",
    Intent.ANSWER_QUESTION: "好的，已收到，请问还有什么可以帮您？",
}


def _make_dialog_handler(intent: Intent) -> Handler:
    def handler(
        user_input: str, plan: DataPlan, runner: ToolRunner, confirmed: bool,
        reply_generator: ReplyGenerator | None = None,
    ) -> ExecutionResult:
        out = _result(intent, "")
        out.reply = _compose_reply(
            plan, out, user_input, reply_generator, _DIALOG_REPLIES[intent],
        )
        return out

    return handler


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

ORDER_MUTATIONS: tuple[OrderMutation, ...] = (
    OrderMutation(
        intent=Intent.ADDRESS_CHANGE,
        tool_name="update_address",
        missing_order_reply="请提供订单号，我帮您修改收货地址。",
        rejected_status_reply="订单 {order_id} 当前「{status}」，已无法修改收货地址。",
        unavailable_reply=(
            "已核实订单 {order_id} 尚未发货，可以修改地址。"
            "不过改址功能暂时未开放；本次尚未提交修改，也未创建人工工单。需要人工处理时请联系客服。"
        ),
        confirmation_reply="确认将订单 {order_id} 的收货地址改为「{new_address}」吗？",
        failure_prefix="地址修改失败",
        arguments=_address_arguments,
    ),
    OrderMutation(
        intent=Intent.ORDER_CANCEL,
        tool_name="cancel_order",
        missing_order_reply="请提供订单号，我帮您取消订单。",
        rejected_status_reply="订单 {order_id} 当前「{status}」，已无法取消。",
        unavailable_reply=(
            "已核实订单 {order_id} 尚未发货，可以取消，但取消功能暂时未开放；"
            "本次尚未提交取消申请，也未创建人工工单。需要人工处理时请联系客服。"
        ),
        confirmation_reply="确认取消订单 {order_id} 吗？取消后如需退款将按原路退回。",
        failure_prefix="取消失败",
        arguments=_order_id_arguments,
    ),
    OrderMutation(
        intent=Intent.EXPEDITE,
        tool_name="expedite_logistics",
        missing_order_reply="请提供订单号，我帮您催一下发货。",
        rejected_status_reply="订单 {order_id} 当前「{status}」，已发货/签收，无法再加急。",
        unavailable_reply=(
            "已核实订单 {order_id} 尚未发货，但加急功能暂时未开放；"
            "本次尚未提交加急申请，也未创建人工工单。需要人工处理时请联系客服。"
        ),
        confirmation_reply="确认为订单 {order_id} 申请加急发货吗？",
        failure_prefix="加急失败",
        arguments=_order_id_arguments,
    ),
)

WORKFLOW_HANDLERS: dict[Intent, Handler] = {
    Intent.ORDER_INQUIRY: _order_inquiry,
    Intent.LOGISTICS_INQUIRY: _logistics_inquiry,
    Intent.REFUND_STATUS: _refund_status,
    Intent.INVOICE: _invoice,
    Intent.AFTER_SALE_REQUEST: _after_sale_request,
    Intent.PRODUCT_INQUIRY: _product_inquiry,
}
# 对话流并入 Workflow：问候/收尾/确认/回答（无业务工具，模型生成/模板兜底）
for _dialog_intent in (
    Intent.GREETINGS, Intent.CLOSURE, Intent.CONFIRMATION, Intent.ANSWER_QUESTION,
):
    WORKFLOW_HANDLERS[_dialog_intent] = _make_dialog_handler(_dialog_intent)
WORKFLOW_HANDLERS.update({spec.intent: _run_order_mutation(spec) for spec in ORDER_MUTATIONS})

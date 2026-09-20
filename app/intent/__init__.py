"""意图识别层（微调分类器优先，规则回退）。

对外统一入口 :func:`recognize` 返回 :class:`app.data_plan.enums.Intent`。
新链路：意图识别 → IntentSpec → Data Plan → dispatch 执行。

识别策略（fail-safe）：
1. 启用微调分类器且模型可用 → 用微调模型（判断末轮用户意图，多轮上下文）；
2. 模型输出解析失败 / 依赖缺失 / 推理异常 / 未启用 → 回退规则识别，
   保证客服链路任何情况下都能给出确定性的意图。
"""

from __future__ import annotations

from app.data_plan.enums import Intent
from app.intent.classifier import get_classifier
from app.intent.query_rewrite import extract_order_status
from app.intent.rules import has_order_id
from app.intent.rules import active_user_text, recognize as _rule_recognize

# 物流轨迹语义信号词：命中说明用户想查「物流/到哪」，即使含状态约束也应保留
# logistics_inquiry（例：「已发货的到哪了」「未发货订单的物流进度」）。
_LOGISTICS_SIGNALS = (
    "到哪了", "到哪", "物流", "轨迹", "快递", "运单", "到货",
    "多久到", "什么时候到",
)

# 订单清单查询语义词：配合状态约束把「查询未发货订单」这类清单查询从
# logistics_inquiry 修正回 order_inquiry。
_ORDER_LIST_QUERY_TERMS = (
    "查询", "查一下", "查查", "列", "哪些", "所有", "全部", "有哪", "看看",
)


def recognize(user_input: str, messages: list[dict] | None = None) -> Intent:
    """识别意图：微调分类器优先，解析失败/不可用则回退规则。

    :param user_input: 用户当前消息（规则回退与最终兜底都依赖它）
    :param messages: 完整对话上下文（OpenAI 格式，不含 system），最后一条为
        当前 user 消息；缺省时退化为单轮（仅当前消息）。
    """
    effective_text = active_user_text(user_input)
    context = messages if messages is not None else [
        {"role": "user", "content": user_input},
    ]
    if effective_text != user_input and context:
        context = [*context]
        last_user = next((index for index in range(len(context) - 1, -1, -1)
                          if context[index].get("role") == "user"), None)
        if last_user is not None:
            context[last_user] = {**context[last_user], "content": effective_text}
    predicted = get_classifier().classify(context)
    intent = predicted if predicted is not None else _rule_recognize(effective_text)

    # 状态约束意图修正：分类器或规则把「查询未发货订单」这类含订单状态约束的
    # 清单查询误判为物流查询时，若消息无物流轨迹语义且是清单查询/提到订单，
    # 修正为订单查询。例：「查询未发货订单」→ order_inquiry；
    # 「已发货的到哪了」→ 保留 logistics；「退款」→ 不修正（非清单语义）。
    if intent == Intent.LOGISTICS_INQUIRY:
        status = extract_order_status(effective_text)
        is_list_query = any(w in effective_text for w in _ORDER_LIST_QUERY_TERMS)
        mentions_order = "订单" in effective_text
        if (
            status
            and not any(w in effective_text for w in _LOGISTICS_SIGNALS)
            and (is_list_query or mentions_order)
        ):
            return Intent.ORDER_INQUIRY
    return intent


__all__ = ["has_order_id", "recognize"]

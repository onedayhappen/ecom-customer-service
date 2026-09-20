"""长期记忆查询路由。

这层只做一个很窄的判断：当前用户 query 是否“明显需要”先检索长期记忆。

设计上分成两条路径：
- 自动注入路径：少量带有明确历史指代的 query，例如“昨天你不是说……”
  由编排器先 recall，再把相关记忆注入 prompt。
- 工具路径：大多数 query 不预注入记忆，Agent 在 ReAct 推理中按需调用
  ``recall_user_memory`` 工具。
- 跳过路径：明显的实时业务查询，例如“查询订单 DX123 的物流”，直接走业务工具。

这里故意保持保守：误注入长期记忆比漏注入更危险，因为旧画像或旧 case 线索可能
污染实时业务判断。漏掉的情况仍可由 Agent 工具路径补查。
"""

from __future__ import annotations

from dataclasses import dataclass
import re


# “昨天/上次/你说过”这类词通常意味着用户在引用历史承诺或历史上下文。
_MEMORY_CUES = (
    "记得", "还记得", "上次", "上回", "之前", "昨天", "刚才",
    "我说过", "你说过", "别忘了", "历史记录", "那次",
    "承诺", "说好的", "我是谁", "当时",
)

# 实时业务词不等于完全不需要记忆，但如果同时出现明确订单号，优先让业务工具回答。
_REALTIME_CUES = (
    "查询订单", "查订单", "看订单", "订单状态", "物流", "物流轨迹",
    "查询物流", "退款状态", "退款", "发货", "到货", "签收", "运单",
    "订单详情", "查询", "查看", "状态",
)

# 当前项目里常见订单号/工单号形态。命中后通常意味着 query 有明确业务实体。
_ORDER_ID_PATTERN = re.compile(r"\b(?:ORD|DX)[-]?[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryRouteDecision:
    """记忆路由结果。

    ``reason`` 用于测试和线上观测：以后如果发现误判，可以直接统计 reason 分布，
    而不是只能从最终回答里反推。
    """

    should_auto_inject: bool
    reason: str


class MemoryQueryRouter:
    """基于 query 的长期记忆路由器。

    当前实现是确定性规则，优点是可解释、低成本、不会引入额外 LLM 调用。
    如果后续 query 形态变复杂，可以把这里升级成小模型分类器，但输出契约保持不变。
    """

    def decide(self, query: str) -> MemoryRouteDecision:
        text = (query or "").strip()
        if not text:
            return MemoryRouteDecision(False, "empty_query")

        has_memory_cue = any(cue in text for cue in _MEMORY_CUES)
        has_realtime_cue = any(cue in text for cue in _REALTIME_CUES)
        has_order_id = bool(_ORDER_ID_PATTERN.search(text))

        # 历史指代优先级最高：即便提到“退款/物流”，用户问的也可能是历史承诺。
        if has_memory_cue:
            return MemoryRouteDecision(True, "memory_cue")

        # 明确实体 + 实时词，通常应该查订单/售后系统，而不是查长期记忆。
        if has_order_id and has_realtime_cue:
            return MemoryRouteDecision(False, "explicit_realtime_query")

        if has_order_id:
            return MemoryRouteDecision(False, "explicit_entity_query")

        if has_realtime_cue:
            return MemoryRouteDecision(False, "realtime_query")

        return MemoryRouteDecision(False, "no_clear_memory_need")

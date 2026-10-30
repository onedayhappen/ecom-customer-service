# 文件说明：根据查询类型和时间语义决定长期记忆的读取路径。
"""长期记忆查询路由。

这层只做一个很窄的判断：当前用户 query 是否“明显需要”先检索长期记忆。

设计上分成两条路径：
- 自动注入路径：明确用户偏好或历史画像的 query，例如“我以前喜欢什么颜色”。
- 事件路径：客服承诺等问题需要公开会话事件，不能使用用户画像代替。
- 工具路径：大多数 query 不预注入记忆，Agent 在 ReAct 推理中按需调用
  ``recall_user_memory`` 工具。
- 跳过路径：明显的实时业务查询，例如“查询订单 DX123 的物流”，直接走业务工具。

这里故意保持保守：误注入长期记忆比漏注入更危险，因为旧画像或历史事项线索可能
污染实时业务判断。漏掉的情况仍可由 Agent 工具路径补查。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
_ORDER_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:ORD|DX)[-]?[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryRouteDecision:
    """记忆路由结果。

    ``reason`` 用于测试和线上观测：以后如果发现误判，可以直接统计 reason 分布，
    而不是只能从最终回答里反推。
    """

    should_auto_inject: bool
    reason: str
    route: str = "facts"
    history: bool = False
    as_of: str | None = None


def temporal_query(query: str, now: datetime | None = None) -> tuple[bool, str | None]:
    """Resolve explicit dates; an unspecified past requests labeled history."""
    now = now or datetime.now(timezone.utc)
    from app.config.settings import settings
    try:
        zone = ZoneInfo(settings.memory_timezone)
    except ZoneInfoNotFoundError:
        if settings.memory_timezone != "Asia/Shanghai":
            raise ValueError("Unknown memory timezone") from None
        zone = timezone(timedelta(hours=8))
    now = now.astimezone(zone)
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?", query)
    if match:
        try:
            day = datetime(*map(int, match.groups()), tzinfo=zone)
        except ValueError:
            return True, None
        # A date without a clock time means the state at that date's end.
        return True, (day + timedelta(days=1, microseconds=-1)).isoformat()
    if "昨天" in query:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(microseconds=1)
        return True, day.isoformat()
    month = re.search(r"(20\d{2})[-/年](\d{1,2})(?:月)?", query)
    if month:
        year, number = map(int, month.groups())
        try:
            start = datetime(year, number, 1, tzinfo=zone)
        except ValueError:
            return True, None
        end = start.replace(year=year + 1, month=1) if number == 12 else start.replace(month=number + 1)
        return True, (end - timedelta(microseconds=1)).isoformat()
    if "上个月" in query:
        end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return True, (end - timedelta(microseconds=1)).isoformat()
    year = re.search(r"(20\d{2})年", query)
    if year or "去年" in query:
        number = int(year[1]) if year else now.year - 1
        return True, (datetime(number + 1, 1, 1, tzinfo=zone) - timedelta(microseconds=1)).isoformat()
    history = any(cue in query for cue in ("以前", "之前", "过去", "当时", "曾经", "去年", "上个月", "变过", "变化", "历史偏好"))
    return history, None


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

        # Explicit business entities always require the authorized business path.
        if has_order_id:
            return MemoryRouteDecision(False, "explicit_entity_query", route="realtime")

        # Historical commitments belong to public task events, not the user
        # profile. This decision prevents accidental preference substitution.
        if has_memory_cue and any(cue in text for cue in ("你说", "你不是说", "承诺", "答应", "说好的", "回电")):
            return MemoryRouteDecision(False, "historical_event", route="events", history=True)

        from app.memory.atomic import query_slots
        predicates, qualifiers = query_slots(text)
        history, as_of = temporal_query(text)
        explicit_preference = (bool(predicates or qualifiers)
                               and any(cue in text for cue in ("我", "偏好", "喜欢", "适合")))
        if has_realtime_cue and not explicit_preference:
            return MemoryRouteDecision(False, "realtime_query", route="realtime")

        if has_memory_cue:
            return MemoryRouteDecision(True, "memory_cue", history=history, as_of=as_of)

        if predicates or qualifiers or any(cue in text for cue in ("推荐", "适合我", "我的偏好", "我喜欢什么")) or (history and "偏好" in text):
            return MemoryRouteDecision(True, "personalization", history=history, as_of=as_of)

        return MemoryRouteDecision(False, "no_clear_memory_need")

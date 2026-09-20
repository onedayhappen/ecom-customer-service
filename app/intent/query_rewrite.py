"""实体抽取层（原「查询改写层」）：意图识别前的实体消歧、指代消解与通用实体抽取。

职责（架构文档 §1.1 定稿，概念上本层即「实体抽取层」）：在意图识别之前，把用户
消息中的「指代」和「实体线索」消解为明确的订单号/物流号/状态约束（或澄清），让
后续意图识别与执行层拿到带实体的消息，而不是模糊指代。

除消歧外，本层还统一抽取与意图无关的通用实体（订单号、物流号、商品词、
**订单状态约束 order_status**），结果放入 ``RewriteOutcome.entities``，经
``rewrite_query`` 返回后由调用方透传给执行层（``engine.execute(entities=...)`` →
``dispatch`` → ``run_workflow`` → 对应 handler），执行层直接复用，不再自行重复
解析原文。

本层处理三类输入，按确定性优先级依次判断：

1. **精确订单号**（``ORD-xxx``）→ 直接放行（订单号已在消息中，执行层可用）；
2. **跨会话时间指代**（"上次的退款到账了吗"）→ Case 记忆消歧；
3. **商品指代**（"我买的鞋到哪了"）→ 抽商品词 → 商品库反查 → 订单定位。

三级消歧策略（成本控制，不逐个指代都确认）：
- 唯一候选 → 改写消息注入订单号；
- 多个候选 → 澄清列候选让用户选（本层拦截，不做意图分类）；
- 无候选   → 放行（执行层 ``list_user_orders`` 列全部兜底，状态约束仍透传）。

边界约定：
- 「实体/指代不明」归本层；「意图不明」是分类器置信度兜底的职责，不混入。
- 「依赖意图的槽位」（新地址、发票抬头、退款原因）归执行层抽取，本层只抽
  与意图无关的通用实体（订单号、物流号、商品词、订单状态）。
- 会话内指代（紧接上一轮聊过的订单）由意图识别的多轮上下文（最近 5 轮）
  覆盖，本层不重复处理。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from app.data_plan.enums import Intent

if TYPE_CHECKING:  # pragma: no cover
    from app.memory.case_manager import CaseManager, CaseRecord

# 工具执行器：与 execution.ToolRunner.execute 同签名（name, arguments）→ 结构化 dict。
ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class DisambiguationHint:
    """指代消歧提示：消息指代的是「业务事项」还是「商品/订单」。

    - ``needs_case``：True 表示指代历史业务事项（退款/投诉/物流跟进）→ 查 Case 记忆。
    - ``product_keywords``：非空表示指代商品/订单 → 查订单库定位。
    """

    needs_case: bool = False
    product_keywords: tuple[str, ...] = ()


# 指代消歧器：消息 → 指代提示（LLM 或规则实现）。
Disambiguator = Callable[[str], DisambiguationHint]

# 订单号正则（与执行层 workflow.ORDER_ID_RE 保持同构）
ORDER_ID_RE = re.compile(r"\bORD-[A-Za-z0-9-]+\b", re.IGNORECASE)

# 物流单号正则：常见快递前缀 + 数字。保守起见只认已知前缀（顺丰/圆通/中通/
# 申通/京东/邮政 EMS/德邦/韵达/百世等），避免把订单号或普通数字误判为运单号。
# 注意：快递单号格式因公司而异且会变化，生产环境建议交由 LLM 抽取或接入
# 快递单号校验服务，这里的前缀表只是规则兜底。
TRACKING_NO_RE = re.compile(
    r"\b(?:SF|JD|YT|YTO|ZTO|STO|EMS|DB|YD|HTKY|SFK|YZPY)\d{8,}\b",
    re.IGNORECASE,
)

# 需要「订单实体消歧」的意图（定位具体订单）。其余意图（商品咨询、对话流、
# 兜底）不需要订单定位，查询改写直接放行，不触发 LLM/工具调用。
_ORDER_DISAMBIGUATION_INTENTS = frozenset({
    Intent.ORDER_INQUIRY, Intent.LOGISTICS_INQUIRY, Intent.REFUND_STATUS,
    Intent.ADDRESS_CHANGE, Intent.ORDER_CANCEL, Intent.EXPEDITE,
    Intent.INVOICE, Intent.AFTER_SALE_REQUEST, Intent.ISSUE_REPORT,
    Intent.COMPLAINT,
})

# 业务事项词：Case 记忆保存的是「业务事项」（退款/退货/换货/价保/投诉/售后/
# 物流跟进），命中且无商品词时，用户指代的是历史业务事项 → 查 Case 记忆。
_BUSINESS_MATTER_TERMS = (
    "退款", "退货", "换货", "价保", "退差价", "投诉", "售后",
    "物流", "催发货", "加急",
)

# 指代信号词（时间/指代词）：辅助判断「指代历史事项」还是「发起新动作」。
# 规则兜底时，业务词 + 指代信号 才触发 Case（避免把「我要退款」误判为查 Case）。
_REFERENCE_CUES = (
    "上次", "上回", "之前", "以前", "昨天", "前天", "前几天", "前几",
    "上个月", "那个", "那笔", "那次", "我的", "我这",
)

# 商品词表（规则兜底）。覆盖常见品类泛词；精确商品名由 LLM 抽取器补充。
_PRODUCT_TERMS = (
    "运动鞋", "跑鞋", "板鞋", "球鞋", "鞋",
    "耳机", "耳塞", "手机", "电脑", "平板", "阅读器",
    "牛仔裤", "防晒衣", "四件套", "吸尘器", "滤芯", "保护壳",
    "游戏机", "手表",
)

# 会话内指代词：命中且当前消息无订单号/业务事项/商品词时，
# 从短期记忆（会话上下文）回填最近出现过的订单号（"那它到哪了" → 补上上轮的订单号）。
_CONTEXT_REFERENCE_TERMS = (
    "它", "那个", "这单", "那单", "这票", "那票",
    "刚才", "刚说", "前面", "上一条", "上一句",
)

# 回填订单号时，最多回溯的最近消息条数（≈ 最近 2 轮对话）。
_CONTEXT_RECALL_WINDOW = 4

# 指代消歧 system prompt：明确「什么场景需要 Case 记忆，其他场景查订单库/放行」。
_DISAMBIGUATOR_SYSTEM = (
    "你是电商客服的实体消歧器。判断用户消息指代的是「业务事项」还是「商品/订单」，"
    "只输出 JSON：{\"needs_case\": bool, \"product_keywords\": [\"...\"]}。\n"
    "判定规则：\n"
    "1. needs_case=true：用户指代「正在处理的业务事项」（退款/退货/换货/价保/投诉/"
    "售后/物流跟进），而不是某个具体商品或订单。典型：「上次的退款到账了吗」"
    "「那个投诉处理了吗」「之前的物流到哪了」。\n"
    "2. product_keywords 非空：用户指代「买过的商品/订单」。典型：「我买的鞋」"
    "「昨天买的手机」「那个耳机」。抽取商品关键词（鞋、手机、耳机等）。\n"
    "3. 两者都不是（问候、闲聊、无指代、已给订单号、发起新动作或新查询如「我要退款」"
    "「查询未发货订单」「查一下已发货的到哪了」）："
    "{\"needs_case\": false, \"product_keywords\": []}。\n"
    "注意：时间词（昨天/上次/之前）只是修饰，不单独决定类型；关键是用户指代的对象"
    "是「业务事项」还是「商品/订单」。**用户正在发起一个带状态/订单条件的新查询"
    "（如「查询未发货订单」「查所有已签收的」）时不是指代历史事项，needs_case=false**。"
)


@dataclass(frozen=True)
class EntityExtraction:
    """从用户消息抽取的通用实体（与意图无关，任何意图前都可定位）。

    这些实体由「实体抽取层」（原查询改写层）统一抽一次，结果通过
    ``RewriteOutcome.entities`` 透传给执行层（Workflow/ReAct），执行层
    直接复用，不再自行重复解析原文。
    """

    order_id: str | None = None              # 精确订单号（ORD-xxx）
    tracking_number: str | None = None       # 物流单号/运单号（SF1234567890 等）
    product_keywords: tuple[str, ...] = ()   # 商品线索词
    order_status: str | None = None          # 订单状态约束（待发货/已发货/已签收/退款中）
    has_cross_session_ref: bool = False      # 是否跨会话时间指代
    has_product_ref: bool = False            # 是否商品指代
    has_tracking_ref: bool = False           # 是否物流单号指代


@dataclass(frozen=True)
class RewriteOutcome:
    """查询改写结果：要么改写后继续识别，要么返回澄清拦截。

    - ``message``：送入意图识别的消息（未改写时与原文一致）。
    - ``clarification``：非 None 时优先返回给用户，不再做意图识别。
    - ``case``：Case 消歧命中的唯一 case（改写注入订单号时携带）。
    - ``entities``：抽取到的通用实体（可观测，供执行层复用）。
    - ``order_candidates``：商品消歧的候选订单（多候选澄清时填充）。
    - ``needs_case``：是否触发了 Case 记忆检查（可观测）。
    - ``source``：改写来源（order_id / case / product / none），用于线上观测。
    """

    message: str
    clarification: str | None = None
    case: "CaseRecord | None" = None
    entities: EntityExtraction = field(default_factory=EntityExtraction)
    order_candidates: tuple[dict[str, Any], ...] = ()
    needs_case: bool = False
    source: str = "none"


# ---------------------------------------------------------------------------
# 实体抽取
# ---------------------------------------------------------------------------

def extract_order_id(text: str) -> str | None:
    """从消息中抽精确订单号（大写）；无则返回 None。"""
    match = ORDER_ID_RE.search(text or "")
    return match.group(0).upper() if match else None


def extract_tracking_number(text: str) -> str | None:
    """从消息中抽精确物流单号（大写）；无则返回 None。"""
    match = TRACKING_NO_RE.search(text or "")
    return match.group(0).upper() if match else None


def extract_product_keywords(text: str) -> tuple[str, ...]:
    """规则兜底：从消息抽商品线索词（词表子串命中）。"""
    return tuple(term for term in _PRODUCT_TERMS if term in (text or ""))


# 订单状态关键词 → 展示标签（与下游一致：待发货/已发货/已签收/退款中）。
# 由实体抽取层在此统一解析一次，结果通过 entities.order_status 透传给执行层。
_ORDER_STATUS_MAPPING: tuple[tuple[tuple[str, ...], str], ...] = (
    (("未发货", "待发货", "还没发", "还没发货", "还没有发货", "没发货", "尚未发货"), "待发货"),
    (("已发货", "发货了", "已寄出", "在途", "运输中", "物流中"), "已发货"),
    (("已签收", "已收到", "签收", "已送达"), "已签收"),
    (("退款中", "退款"), "退款中"),
)


def extract_order_status(text: str) -> str | None:
    """从用户消息抽取订单状态约束（待发货/已发货/已签收/退款中）；无则 None。

    如「查询未发货订单」「已发货的到哪了」「退款中的那笔」分别得到
    「待发货」「已发货」「退款中」。供执行层把状态约束下推到工具，
    使返回与证据包只含目标订单。
    """
    for keywords, label in _ORDER_STATUS_MAPPING:
        if any(k in (text or "") for k in keywords):
            return label
    return None


def build_entities(text: str, **overrides: Any) -> EntityExtraction:
    """统一构建 ``EntityExtraction``：始终附带 ``order_status``，再叠加调用方指定的字段。

    实体抽取层各 return 路径统一走这里，避免遗漏状态约束的透传。
    """
    fields: dict[str, Any] = {"order_status": extract_order_status(text)}
    fields.update(overrides)
    return EntityExtraction(**fields)


def rule_disambiguator(text: str) -> DisambiguationHint:
    """规则兜底的指代消歧器（无 LLM 时用）。

    判别优先级：商品词（查订单库）→ 业务词 + 指代信号（查 Case）→ 放行。
    时间词（昨天/上次）不单独决定类型，只作「指代历史」的辅助信号。
    """
    text = text or ""
    # 商品指代优先：有商品词 → 查订单库（"我昨天买的鞋"虽带"昨天"，仍走商品定位）
    keywords = extract_product_keywords(text)
    if keywords:
        return DisambiguationHint(product_keywords=keywords)
    # 业务事项指代：业务词 + 指代信号（"上次的退款"），且无商品词
    has_business = any(term in text for term in _BUSINESS_MATTER_TERMS)
    has_reference = any(term in text for term in _REFERENCE_CUES)
    if has_business and has_reference:
        return DisambiguationHint(needs_case=True)
    return DisambiguationHint()


def make_llm_disambiguator(client: Any, model: str) -> Disambiguator:
    """构造基于 LLM 的指代消歧器（结构化输出 + 容错解析）。

    输出 ``DisambiguationHint``：needs_case（查 Case 记忆）或
    product_keywords（查订单库）。LLM 失败/输出无法解析时返回空提示，
    由调用方按「放行」处理，绝不抛异常污染主链路。
    """
    def disambiguate(text: str) -> DisambiguationHint:
        if not client or not model:
            return DisambiguationHint()
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _DISAMBIGUATOR_SYSTEM},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
            )
            content = response.choices[0].message.content or ""
        except Exception:  # noqa: BLE001
            return DisambiguationHint()
        data = _parse_json(content) or {}
        needs_case = bool(data.get("needs_case"))
        keywords = data.get("product_keywords") or []
        return DisambiguationHint(
            needs_case=needs_case,
            product_keywords=tuple(
                dict.fromkeys(str(k).strip() for k in keywords if str(k).strip())
            ),
        )

    return disambiguate


def _parse_json(content: str) -> dict[str, Any] | None:
    """解析 LLM 输出 JSON；兼容被额外文本包裹的情况。"""
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 容错：提取首个 {...} 子串
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# 会话内指代（短期记忆回填订单号）
# ---------------------------------------------------------------------------

def has_context_reference(text: str) -> bool:
    """当前消息是否为会话内指代性话语（"那它到哪了"）。"""
    return any(term in (text or "") for term in _CONTEXT_REFERENCE_TERMS)


def recall_order_id_from_context(
    session_context: list[dict[str, Any]] | None,
    max_messages: int = _CONTEXT_RECALL_WINDOW,
) -> str | None:
    """从短期记忆（会话上下文）回填最近出现过的明确订单号。

    倒序遍历最近 ``max_messages`` 条消息，返回第一条含订单号的消息中的
    订单号；找不到返回 None。
    """
    if not session_context:
        return None
    recent = session_context[-max_messages:] if max_messages > 0 else []
    for message in reversed(recent):
        content = ""
        if isinstance(message, dict):
            content = message.get("content", "")
        elif isinstance(message, str):
            content = message
        order_id = extract_order_id(str(content))
        if order_id:
            return order_id
    return None


# ---------------------------------------------------------------------------
# 订单定位（商品线索 → 候选订单）
# ---------------------------------------------------------------------------

def resolve_orders_by_product(
    keywords: tuple[str, ...],
    execute: ToolExecutor | None = None,
) -> list[dict[str, Any]]:
    """把商品线索词定位为候选订单（唯一/多个/空）。

    链路：每个商品词 → ``query_product`` 反查匹配商品名 → ``list_user_orders``
    列用户订单 → 按商品名匹配 ``items_summary`` → 返回命中订单。

    :param keywords: 商品线索词。
    :param execute: 工具执行器（``query_product`` / ``list_user_orders``）。
        由调用方注入（本层不依赖 ``app.tools``，避免拉入向量库等重依赖）；
        None 时不做定位，返回空列表（放行，执行层列全部兜底）。
    """
    if not keywords or execute is None:
        return []

    matched_names: set[str] = set()
    for keyword in keywords:
        product_result = execute(
            "query_product", {"keyword": keyword, "top_k": 5},
        )
        if not isinstance(product_result, dict) or not product_result.get("success"):
            continue
        for product in product_result.get("matched_products") or []:
            name = product.get("name")
            if name:
                matched_names.add(name)
    if not matched_names:
        return []

    orders_result = execute("list_user_orders", {})
    if not isinstance(orders_result, dict) or not orders_result.get("success"):
        return []
    orders = orders_result.get("orders") or []

    candidates: list[dict[str, Any]] = []
    for order in orders:
        summary = str(order.get("items_summary") or "")
        # 匹配条件双保险：完整商品名命中（商品库反查一致时）或 原始商品关键词
        # 命中（商品库型号与订单型号不一致时，如库中「戴森 V15 Detect 吸尘器」
        # vs 订单「戴森 V12 无线吸尘器」——V15≠V12 但关键词「吸尘器」一致）。
        name_hit = any(name in summary for name in matched_names)
        kw_hit = any(kw in summary for kw in keywords)
        if name_hit or kw_hit:
            candidates.append(order)
    return candidates


# ---------------------------------------------------------------------------
# 澄清文案
# ---------------------------------------------------------------------------

def _clarification_for_case(resolution) -> str | None:
    """Case 消歧结果 → 澄清文本；无需澄清返回 None。"""
    if resolution.need_clarification and resolution.candidates:
        lines = ["您有多个相关的事项，请问指的是哪一笔？"]
        for case in resolution.candidates:
            order = " / ".join(case.order_ids) if case.order_ids else case.case_id
            state = str(case.business_state.get("value", "处理中"))
            lines.append(f"- {order}（{state}）")
        return "\n".join(lines)
    if resolution.reason in {"no_related_case", "entity_verification_failed"}:
        return "没有找到您之前的相关记录，请提供订单号或手机号，我帮您核实。"
    return None


def _clarification_for_product(candidates: list[dict[str, Any]]) -> str | None:
    """商品消歧多候选 → 澄清文本；唯一/空候选返回 None。"""
    if len(candidates) <= 1:
        return None
    lines = ["您有多个相关的订单，请问指的是哪一个？"]
    for order in candidates:
        lines.append(
            f"- {order.get('order_id')}：{order.get('items_summary', '')}"
            f"（{order.get('status', '')}）"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def needs_case_resolution(text: str) -> bool:
    """规则兜底：是否需查 Case 记忆（业务事项指代，无商品词/订单号）。"""
    if ORDER_ID_RE.search(text or ""):
        return False
    text = text or ""
    # 商品指代优先：有商品词 → 查订单库，不是 Case
    if any(term in text for term in _PRODUCT_TERMS):
        return False
    has_business = any(term in text for term in _BUSINESS_MATTER_TERMS)
    has_reference = any(term in text for term in _REFERENCE_CUES)
    return has_business and has_reference


def rewrite_query(
    text: str,
    *,
    intent: Intent | str | None = None,
    case_manager: "CaseManager",
    session_context: list[dict[str, Any]] | None = None,
    disambiguator: Disambiguator | None = None,
    execute: ToolExecutor | None = None,
) -> RewriteOutcome:
    """意图识别后的查询改写：按意图做实体消歧 → 改写 / 澄清 / 放行。

    提供 ``intent`` 时只在「需要订单定位」的意图下工作，其余直接放行；
    ``intent=None`` 走完整消歧（保守）。消歧判定优先级：

    精确订单号 → 指代消歧（Case 业务事项 / 订单库商品定位）→ 会话内指代
    （短期记忆回填）→ 放行。指代消歧用 LLM（``disambiguator``）判断
    「查 Case 还是查订单库」，无 LLM 时用规则兜底（``rule_disambiguator``）。

    :param text: 用户当前消息（已做意图识别，尚未执行）。
    :param intent: 意图识别结果（17 类 Intent）；None 表示未知（完整消歧）。
    :param case_manager: 当前用户的 CaseManager（resolve 只读路径，线程安全）。
    :param session_context: 会话历史 messages（短期记忆），用于会话内指代回填。
    :param disambiguator: 指代消歧器（LLM）；None 时用规则兜底。
    :param execute: 工具执行器（商品定位用 query_product / list_user_orders）；
        由调用方注入（含用户上下文），None 时商品定位不生效。
    """
    intent = Intent(intent) if isinstance(intent, str) else intent

    # 0) 提供意图且非订单消歧意图 → 直接放行（商品咨询/对话流/兜底不干预）
    if intent is not None and intent not in _ORDER_DISAMBIGUATION_INTENTS:
        return RewriteOutcome(message=text, source="none")

    # 1) 精确实体 → 放行（订单号 / 物流单号已在消息中，执行层可用）
    order_id = extract_order_id(text)
    if order_id:
        entities = build_entities(text, order_id=order_id)
        return RewriteOutcome(message=text, entities=entities, source="order_id")

    tracking_number = extract_tracking_number(text)
    if tracking_number:
        entities = build_entities(
            text, tracking_number=tracking_number, has_tracking_ref=True,
        )
        return RewriteOutcome(message=text, entities=entities, source="tracking")

    # 1.5) 明确的订单状态约束 + 无指代信号词 → 是「发起新查询」而非「指代历史事项」，
    # 直接放行（把 order_status 透传给执行层），不做 Case/商品消歧。
    # 例：「查询未发货订单」→ 放行；「上次的退款到账了吗」→ 含指代信号，仍走 Case 消歧。
    order_status = extract_order_status(text)
    if order_status and not any(cue in text for cue in _REFERENCE_CUES):
        return RewriteOutcome(
            message=text,
            entities=build_entities(text, order_status=order_status),
            source="none",
        )

    # 2) 指代消歧（LLM 优先，规则兜底）：业务事项 → Case；商品/订单 → 订单库
    hint = (disambiguator or rule_disambiguator)(text)

    if hint.needs_case:
        resolution = case_manager.resolve(text)
        entities = build_entities(text, has_cross_session_ref=True)
        if resolution.selected is not None:
            case_order_id = (
                resolution.selected.order_ids[0]
                if resolution.selected.order_ids else ""
            )
            rewritten = f"{text}（订单 {case_order_id}）" if case_order_id else text
            return RewriteOutcome(
                message=rewritten, case=resolution.selected,
                entities=entities, needs_case=True, source="case",
            )
        clarification = _clarification_for_case(resolution)
        if clarification:
            return RewriteOutcome(
                message=text, clarification=clarification,
                entities=entities, needs_case=True, source="case",
            )
        # Case 无候选且无需澄清 → 落到会话内指代判断

    if hint.product_keywords:
        candidates = resolve_orders_by_product(hint.product_keywords, execute)
        entities = build_entities(
            text, product_keywords=hint.product_keywords, has_product_ref=True,
        )
        if len(candidates) == 1:
            order_id = candidates[0].get("order_id", "")
            rewritten = f"{text}（订单 {order_id}）" if order_id else text
            return RewriteOutcome(
                message=rewritten, entities=entities, source="product",
            )
        clarification = _clarification_for_product(candidates)
        if clarification:
            return RewriteOutcome(
                message=text, clarification=clarification,
                entities=entities,
                order_candidates=tuple(candidates), source="product",
            )

    # 2.5) 商品词主动定位兜底：LLM/规则消歧漏识别 product_keywords 但消息含商品词时，
    # 主动调 query_product + list_user_orders 做商品→订单定位（不依赖消歧器）。
    # 例：「吸尘器什么时候发货」→ LLM 返回空 hint → 兜底命中 _PRODUCT_TERMS
    # 主动查 → 唯一候选 ORD-ADMIN-001 → 改写消息注入订单号，workflow 据此直接查。
    if not hint.product_keywords and not hint.needs_case and execute is not None:
        fallback_keywords = extract_product_keywords(text)
        if fallback_keywords:
            candidates = resolve_orders_by_product(fallback_keywords, execute)
            if candidates:
                entities = build_entities(
                    text, product_keywords=fallback_keywords, has_product_ref=True,
                )
                if len(candidates) == 1:
                    order_id = candidates[0].get("order_id", "")
                    rewritten = f"{text}（订单 {order_id}）" if order_id else text
                    return RewriteOutcome(
                        message=rewritten, entities=entities, source="product",
                    )
                clarification = _clarification_for_product(candidates)
                if clarification:
                    return RewriteOutcome(
                        message=text, clarification=clarification,
                        entities=entities,
                        order_candidates=tuple(candidates), source="product",
                    )

    # 3) 会话内指代 → 短期记忆回填最近订单号（"那它到哪了"）
    if has_context_reference(text):
        recalled = recall_order_id_from_context(session_context)
        if recalled:
            entities = build_entities(text, order_id=recalled)
            return RewriteOutcome(
                message=f"{text}（订单 {recalled}）",
                entities=entities, source="context",
            )

    # 4) 无实体指代 → 放行（执行层列全部订单兜底；仍透传 order_status 等约束）
    return RewriteOutcome(
        message=text, entities=build_entities(text), source="none",
    )


__all__ = [
    "DisambiguationHint",
    "Disambiguator",
    "EntityExtraction",
    "RewriteOutcome",
    "extract_order_id",
    "extract_tracking_number",
    "extract_product_keywords",
    "extract_order_status",
    "build_entities",
    "has_context_reference",
    "make_llm_disambiguator",
    "needs_case_resolution",
    "recall_order_id_from_context",
    "resolve_orders_by_product",
    "rewrite_query",
    "rule_disambiguator",
]

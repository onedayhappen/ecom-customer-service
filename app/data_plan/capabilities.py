"""工具能力全集目录（单一事实源）。

IntentSpec 引用的是**工具能力 ID**，而非直接绑定某个实现。每个能力标注
`implemented`（是否已在 `app/tools/registry.py` 落地）与 `side_effect`
（是否写操作，决定 permission 等级）。Data Plan 装配时只把 `implemented=True`
的能力真正放进 prompt，未实现的进入 `pending_tools`，供执行层显式降级
（澄清 / 转人工），避免模型调用一个不存在的工具。

当前已实现 8 个（含 meta 工具 `load_skill`），对应 `registry.py` 的
`TOOL_DEFINITIONS`。其余为架构定稿中规划、待 Phase 3 扩充的能力。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data_plan.enums import CapabilityCategory


@dataclass(frozen=True)
class ToolCapability:
    """一个工具能力的静态描述。"""

    name: str                     # 能力 ID，IntentSpec 引用它
    category: CapabilityCategory  # 所属类别
    side_effect: bool             # True = 写操作（执行前须确认）
    implemented: bool             # True = 已在 registry 实现
    description: str              # 语义说明


# ---------------------------------------------------------------------------
# 业务域（18）：订单/商品/物流/售后/执行
# ---------------------------------------------------------------------------
_BUSINESS = CapabilityCategory.BUSINESS
_MEMORY = CapabilityCategory.MEMORY
_ENGINE = CapabilityCategory.ENGINE
_AUGMENT = CapabilityCategory.AUGMENT

# 已实现（与 registry.py 逐一对齐）
_QUERY_ORDER = ToolCapability("query_order", _BUSINESS, False, True, "按订单号查订单详情（状态/商品/金额/物流单号）")
_LIST_USER_ORDERS = ToolCapability("list_user_orders", _BUSINESS, False, True, "列出当前用户全部订单概要")
_QUERY_LOGISTICS = ToolCapability("query_logistics", _BUSINESS, False, True, "按订单号查物流轨迹")
_QUERY_PRODUCT = ToolCapability("query_product", _BUSINESS, False, True, "按关键词/条件检索候选商品")
_APPLY_REFUND = ToolCapability("apply_refund", _BUSINESS, True, True, "为订单申请退款")
_SEARCH_KNOWLEDGE = ToolCapability("search_knowledge", _ENGINE, False, True, "检索政策/FAQ/退换货规则等知识库")
_RECALL_USER_MEMORY = ToolCapability("recall_user_memory", _MEMORY, False, True, "召回用户长期记忆与偏好画像")

# 待实现
_QUERY_REFUND = ToolCapability("query_refund", _BUSINESS, False, False, "按订单/退款单号查退款进度与到账状态")
_QUERY_INVENTORY = ToolCapability("query_inventory", _BUSINESS, False, False, "查商品库存与实时价格")
_CALC_PROMOTION = ToolCapability("calc_promotion", _BUSINESS, False, False, "计算优惠券/满减等优惠金额")
_CALC_PRICE_PROTECT = ToolCapability("calc_price_protect", _BUSINESS, False, False, "计算价保差额与可退金额")
_QUERY_MEMBERSHIP = ToolCapability("query_membership", _BUSINESS, False, False, "查会员等级与积分权益")
_INVOICE_SERVICE = ToolCapability("invoice_service", _BUSINESS, True, False, "开具/查询/重开发票")
_VALIDATE_ADDRESS = ToolCapability("validate_address", _BUSINESS, False, False, "校验收货地址有效性")
_UPDATE_ADDRESS = ToolCapability("update_address", _BUSINESS, True, False, "修改未发货订单的收货地址/收件信息")
_CANCEL_ORDER = ToolCapability("cancel_order", _BUSINESS, True, False, "取消未发货订单")
_EXPEDITE_LOGISTICS = ToolCapability("expedite_logistics", _BUSINESS, True, False, "催发货/加急物流")
_APPLY_EXCHANGE = ToolCapability("apply_exchange", _BUSINESS, True, False, "为订单申请换货")
_APPLY_PRICE_PROTECT = ToolCapability("apply_price_protect", _BUSINESS, True, False, "为订单申请价保退差价")
_ESCALATE_TO_HUMAN = ToolCapability("escalate_to_human", _BUSINESS, False, False, "转人工/创建升级工单")

# 记忆域（2）
_READ_CASE_MEMORY = ToolCapability("read_case_memory", _MEMORY, False, False, "读取当前/历史 Case 业务事实")

# 引擎域（2）
_RECOMMEND_PRODUCTS = ToolCapability("recommend_products", _ENGINE, False, False, "基于用户画像与商品库生成个性化推荐")

# 增强域（3）
_SEND_SATISFACTION = ToolCapability("send_satisfaction", _AUGMENT, False, False, "触发满意度评价触达")
_ANALYZE_IMAGE = ToolCapability("analyze_image", _AUGMENT, False, False, "理解用户上传的故障/货损图片")
_VERIFY_IDENTITY = ToolCapability("verify_identity", _AUGMENT, False, False, "核验用户身份（手机号/实名）")


# 25 个业务/记忆/引擎/增强能力（不含 meta 工具 load_skill）
CAPABILITIES: dict[str, ToolCapability] = {
    c.name: c for c in (
        # 业务域（18）
        _QUERY_ORDER, _LIST_USER_ORDERS, _QUERY_LOGISTICS, _QUERY_REFUND,
        _QUERY_PRODUCT, _QUERY_INVENTORY, _CALC_PROMOTION, _CALC_PRICE_PROTECT,
        _QUERY_MEMBERSHIP, _INVOICE_SERVICE, _VALIDATE_ADDRESS, _UPDATE_ADDRESS,
        _CANCEL_ORDER, _EXPEDITE_LOGISTICS, _APPLY_REFUND, _APPLY_EXCHANGE,
        _APPLY_PRICE_PROTECT, _ESCALATE_TO_HUMAN,
        # 记忆域（2）
        _RECALL_USER_MEMORY, _READ_CASE_MEMORY,
        # 引擎域（2）
        _SEARCH_KNOWLEDGE, _RECOMMEND_PRODUCTS,
        # 增强域（3）
        _SEND_SATISFACTION, _ANALYZE_IMAGE, _VERIFY_IDENTITY,
    )
}

# meta 工具：装配 Skill 剧本，不计入业务能力全集（IntentSpec.skills 引用 skill 名）。
LOAD_SKILL = "load_skill"

# 已实现能力（供 Data Plan 装配器与 registry 交叉校验）
IMPLEMENTED_CAPABILITIES: frozenset[str] = frozenset(
    name for name, c in CAPABILITIES.items() if c.implemented
)

# 写操作能力（执行前须用户确认）
MUTATING_CAPABILITIES: frozenset[str] = frozenset(
    name for name, c in CAPABILITIES.items() if c.side_effect
)


def is_implemented(name: str) -> bool:
    """能力是否已在 registry 落地。"""
    return CAPABILITIES[name].implemented


def get_capability(name: str) -> ToolCapability:
    """按能力 ID 取描述；未知能力抛 KeyError（保证 IntentSpec 引用合法）。"""
    return CAPABILITIES[name]

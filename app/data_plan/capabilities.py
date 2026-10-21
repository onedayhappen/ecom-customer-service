"""工具能力全集目录（单一事实源）。

IntentSpec 引用的是**工具能力 ID**，而非直接绑定某个实现。每个能力标注
`implemented`（是否已在 `app/tools/registry.py` 落地）与 `side_effect`
（是否写操作，决定 permission 等级）。Data Plan 装配时只把 `implemented=True`
的能力真正放进 prompt，未实现的进入 `pending_tools`，供执行层显式降级
（澄清 / 转人工），避免模型调用一个不存在的工具。

已实现集合由下方 implemented 标记派生，meta 工具 load_skill 单独装配。
新增业务工具已按 MySQL / 本地计算 / 会话状态实现；implemented 表示代码已注册，
不代表数据库已初始化或 Workflow 已接入。
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

# 分阶段接入（以每项 implemented 标记为准）
_QUERY_REFUND = ToolCapability("query_refund", _BUSINESS, False, False, "按订单/退款单号查退款进度与到账状态")
_QUERY_INVENTORY = ToolCapability("query_inventory", _BUSINESS, False, False, "查商品库存与实时价格")
_CALC_PROMOTION = ToolCapability("calc_promotion", _BUSINESS, False, True, "确定性计算固定方案的优惠金额（整数分）")
_CALC_PRICE_PROTECT = ToolCapability("calc_price_protect", _BUSINESS, False, False, "计算价保差额与可退金额")
_QUERY_MEMBERSHIP = ToolCapability("query_membership", _BUSINESS, False, False, "查会员等级与积分权益")
_INVOICE_SERVICE = ToolCapability("invoice_service", _BUSINESS, True, False, "开具/查询/重开发票")
_VALIDATE_ADDRESS = ToolCapability("validate_address", _BUSINESS, False, False, "校验收货地址有效性")
_UPDATE_ADDRESS = ToolCapability("update_address", _BUSINESS, True, False, "修改未发货订单的收货地址/收件信息")
_CANCEL_ORDER = ToolCapability("cancel_order", _BUSINESS, True, False, "取消未发货订单")
_EXPEDITE_LOGISTICS = ToolCapability("expedite_logistics", _BUSINESS, True, False, "催发货/加急物流")
_APPLY_EXCHANGE = ToolCapability("apply_exchange", _BUSINESS, True, False, "为订单申请换货")
_APPLY_PRICE_PROTECT = ToolCapability("apply_price_protect", _BUSINESS, True, False, "为订单申请价保退差价")
_ESCALATE_TO_HUMAN = ToolCapability("escalate_to_human", _BUSINESS, True, True, "转人工/创建升级工单")

# 记忆域（2）
_READ_TASK_CONTEXT = ToolCapability("read_task_context", _MEMORY, False, True, "读取当前事项的进度、原话和历史回执")
_SAVE_TASK_PROGRESS = ToolCapability("save_task_progress", _MEMORY, True, True, "按版本保存当前事项进度，不执行业务操作")

# 引擎域（2）
_RECOMMEND_PRODUCTS = ToolCapability("recommend_products", _ENGINE, False, False, "基于用户画像与商品库生成个性化推荐")

# 增强域（3）
_SEND_SATISFACTION = ToolCapability("send_satisfaction", _AUGMENT, False, False, "触发满意度评价触达")
_ANALYZE_IMAGE = ToolCapability("analyze_image", _AUGMENT, False, False, "理解用户上传的故障/货损图片")
_VERIFY_IDENTITY = ToolCapability("verify_identity", _AUGMENT, False, False, "核验用户身份（手机号/实名）")


# 业务/记忆/引擎/增强能力目录（不含 meta 工具 load_skill）
CAPABILITIES: dict[str, ToolCapability] = {
    c.name: c for c in (
        # 业务域（18）
        _QUERY_ORDER, _LIST_USER_ORDERS, _QUERY_LOGISTICS, _QUERY_REFUND,
        _QUERY_PRODUCT, _QUERY_INVENTORY, _CALC_PROMOTION, _CALC_PRICE_PROTECT,
        _QUERY_MEMBERSHIP, _INVOICE_SERVICE, _VALIDATE_ADDRESS, _UPDATE_ADDRESS,
        _CANCEL_ORDER, _EXPEDITE_LOGISTICS, _APPLY_REFUND, _APPLY_EXCHANGE,
        _APPLY_PRICE_PROTECT, _ESCALATE_TO_HUMAN,
        # 记忆域（2）
        _RECALL_USER_MEMORY, _READ_TASK_CONTEXT, _SAVE_TASK_PROGRESS,
        # 引擎域（2）
        _SEARCH_KNOWLEDGE, _RECOMMEND_PRODUCTS,
        # 增强域（3）
        _SEND_SATISFACTION, _ANALYZE_IMAGE, _VERIFY_IDENTITY,
    )
}

# v4 tools use local MySQL evidence, scoped support state and deterministic helpers.
for _name, _write, _description in (
    ("query_offers", False, "查询带来源、规格与有效期的购买报价"),
    ("query_promotions", False, "查询结构化活动和叠加规则"),
    ("query_user_coupons", False, "查询当前用户的券及生命周期"),
    ("query_delivery_quote", False, "查询配送可达、运费和预计时效"),
    ("query_price_history", False, "查询带口径和覆盖范围的历史价格"),
    ("query_after_sale", False, "查询退货退款换货价保单据与进度"),
    ("query_invoice", False, "查询发票、规则和办理预览"),
    ("issue_invoice", True, "申请开具发票"),
    ("reissue_invoice", True, "申请修改或重开发票"),
    ("evaluate_order_action", False, "核验改址、取消、催办的当前可执行条件"),
    ("query_handoff_status", False, "封装已有 SupportStore 的归属安全转接状态查询"),
    ("query_mutation_status", False, "封装幂等记录及下游写入回执核实"),
    ("query_payment_status", False, "查询支付交易和对账证据"),
):
    CAPABILITIES[_name] = ToolCapability(_name, _BUSINESS, _write, True, _description)
for _name, _description in (
    ("check_coupon_eligibility", "确定性核验优惠券使用条件与失败原因"),
    ("compare_offers", "核验报价可比性并比较完整费用"),
    ("summarize_price_history", "统计历史低价和样本覆盖"),
    ("validate_policy_evidence", "校验政策来源、版本、适用范围与冲突"),
):
    CAPABILITIES[_name] = ToolCapability(_name, _ENGINE, False, True, _description)

# meta 工具：装配 Skill 剧本，不计入业务能力全集（IntentSpec.skills 引用 skill 名）。
LOAD_SKILL = "load_skill"

# 已实现能力（供 Data Plan 装配器与 registry 交叉校验）
IMPLEMENTED_CAPABILITIES: frozenset[str] = frozenset(
    name for name, c in CAPABILITIES.items() if c.implemented
)

# 写操作能力（执行前须用户确认）
MUTATING_CAPABILITIES: frozenset[str] = frozenset(
    # Conversation-control writes have their own authorization/transaction path,
    # and do not require an order ID or a second refund-style confirmation.
    name for name, c in CAPABILITIES.items() if c.side_effect and name not in {"escalate_to_human", "save_task_progress"}
)


def is_implemented(name: str) -> bool:
    """能力是否已在 registry 落地。"""
    return CAPABILITIES[name].implemented


def get_capability(name: str) -> ToolCapability:
    """按能力 ID 取描述；未知能力抛 KeyError（保证 IntentSpec 引用合法）。"""
    return CAPABILITIES[name]

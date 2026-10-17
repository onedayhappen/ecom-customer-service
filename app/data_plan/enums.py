"""意图装配层的枚举定义。

本模块是 `app/data_plan/` 的类型基础。所有枚举的字符串值都是稳定的
契约：当前分类器只输出 taxonomy 中的 31 类标签；5 个旧值保留用于
读取持久化任务，不能作为新版分类器输出。所有值均可查询 IntentSpec。
"""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    """31 类分类标签和 5 个旧任务兼容值。

    有效分类输出以 taxonomy.CLASSIFICATION_INTENTS 为准。
    """

    # v4 分类标签：28 个业务目标 + greetings/closure/others。
    PRODUCT_COMPARISON = "product_comparison"
    INVENTORY_INQUIRY = "inventory_inquiry"
    DELIVERY_INQUIRY = "delivery_inquiry"
    SERVICE_POLICY_INQUIRY = "service_policy_inquiry"
    PROMOTION_INQUIRY = "promotion_inquiry"
    PROMOTION_CALCULATION = "promotion_calculation"
    COUPON_ISSUE = "coupon_issue"
    PRICE_COMPARISON = "price_comparison"
    CART_OPTIMIZATION = "cart_optimization"
    PRICE_HISTORY = "price_history"
    MEMBERSHIP_INQUIRY = "membership_inquiry"
    PAYMENT_ISSUE = "payment_issue"
    REFUND_SERVICE = "refund_service"
    EXCHANGE_SERVICE = "exchange_service"
    PRICE_PROTECTION_SERVICE = "price_protection_service"
    AFTER_SALE_STATUS = "after_sale_status"
    INVOICE_INQUIRY = "invoice_inquiry"
    INVOICE_REQUEST = "invoice_request"
    HUMAN_HANDOFF = "human_handoff"

    # 售前
    PRODUCT_INQUIRY = "product_inquiry"              # 商品咨询
    PRODUCT_RECOMMENDATION = "product_recommendation"  # 商品推荐

    # 订单
    ORDER_INQUIRY = "order_inquiry"                  # 订单查询
    LOGISTICS_INQUIRY = "logistics_inquiry"          # 物流查询
    ADDRESS_CHANGE = "address_change"                # 修改地址
    ORDER_CANCEL = "order_cancel"                    # 取消订单
    EXPEDITE = "expedite"                            # 催发货/加急

    # 售后
    AFTER_SALE_REQUEST = "after_sale_request"        # 售后申请（退/换/价保，槽位路由）
    REFUND_STATUS = "refund_status"                  # 退款进度
    ISSUE_REPORT = "issue_report"                    # 问题反馈
    INVOICE = "invoice"                              # 发票管理
    COMPLAINT = "complaint"                          # 投诉

    # 对话流（不发起业务任务，走决策规则填槽）
    CONFIRMATION = "confirmation"                    # 确认/认可
    ANSWER_QUESTION = "answer_question"              # 回答客服提问（填槽）
    CLOSURE = "closure"                              # 收尾（感谢 + 告别）
    GREETINGS = "greetings"                          # 问候

    # 兜底
    OTHERS = "others"                                # 其他/无法归类


class Domain(str, Enum):
    """业务域，用于子 Agent 路由与工具域裁剪。"""

    PRESALE = "presale"        # 售前
    ORDER = "order"            # 订单
    AFTER_SALE = "after_sale"  # 售后
    DIALOG = "dialog"          # 对话流
    FALLBACK = "fallback"      # 兜底


class Execution(str, Enum):
    """执行路径。

    - WORKFLOW：步骤与分支可枚举的确定性脚本，查表走固定流程。
    - REACT：下一步依赖理解与判断，交给 Agent 动态规划（ReAct 循环）。
    - RULE：对话流类，不发起任务，由决策规则（intent × pending_slots）推进。
    """

    WORKFLOW = "workflow"
    REACT = "react"
    RULE = "rule"


class Permission(str, Enum):
    """工具调用权限等级。

    - READ_ONLY：仅只读查询，无副作用，直接装配并调用。
    - CONFIRM_REQUIRED：含写操作（改址/取消/退款/换货/价保/加急/开票），执行前须用户确认。
    - ESCALATE：升级型意图（投诉），走工单/转人工流程，不由普通执行直接落库。
    """

    READ_ONLY = "read_only"
    CONFIRM_REQUIRED = "confirm_required"
    ESCALATE = "escalate"


class Fallback(str, Enum):
    """未满足执行前置条件时的降级策略。"""

    CLARIFY = "clarify"        # 澄清追问（缺信息/多候选/检索未命中）
    EXPLAIN = "explain"        # 说明无法满足（已发货不可改/超期不可退等硬约束）
    ESCALATE = "escalate"      # 转人工/工单（诊断无果、投诉升级）
    NORMAL_REPLY = "normal_reply"  # 无活跃流程时的普通对话回应（对话流类）
    NONE = "none"              # 无降级（closure / greetings）


class CapabilityCategory(str, Enum):
    """工具能力所属类别（区别于 Intent 的业务域）。"""

    BUSINESS = "business"  # 业务域（订单/商品/物流/售后/执行）
    MEMORY = "memory"      # 记忆域（用户长期记忆 / Case 记忆）
    ENGINE = "engine"      # 引擎域（检索 / 推荐）
    AUGMENT = "augment"    # 增强域（满意度触达 / 图像 / 身份核验）

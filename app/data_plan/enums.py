"""意图装配层的枚举定义。

本模块是 `app/data_plan/` 的类型基础。所有枚举的字符串值都是稳定的
契约：`Intent` 的取值必须与分类器约定的 17 类标签逐字符一致，
分类器输出值直接查表映射到 `IntentSpec`。
"""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    """17 类业务任务级意图标签（分类器输出值）。

    分类器只输出该值；退/换/价保等子类型由槽位区分，不在此拆标签。
    """

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

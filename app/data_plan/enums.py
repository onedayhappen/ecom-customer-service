"""Stable labels for execution results and stored records; labels grant no authority."""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    """Business labels, including values still present in historical records."""

    # 业务标签，仅用于描述与记录，不决定执行路径。
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

    # 对话标签（不代表业务授权）
    CONFIRMATION = "confirmation"                    # 确认/认可
    ANSWER_QUESTION = "answer_question"              # 回答客服提问（填槽）
    CLOSURE = "closure"                              # 收尾（感谢 + 告别）
    GREETINGS = "greetings"                          # 问候

    # 兜底
    OTHERS = "others"                                # 其他/无法归类


class Execution(str, Enum):
    """The single supported execution loop."""

    REACT = "react"


class CapabilityCategory(str, Enum):
    """工具能力所属类别（区别于 Intent 的业务域）。"""

    BUSINESS = "business"  # 业务域（订单/商品/物流/售后/执行）
    MEMORY = "memory"      # 记忆域（用户长期记忆 / 事项上下文）
    ENGINE = "engine"      # 引擎域（检索 / 推荐）
    AUGMENT = "augment"    # 增强域（满意度触达 / 图像 / 身份核验）

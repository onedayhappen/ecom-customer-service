"""Versioned classification contract, separate from legacy persisted task labels.

The five legacy enum members remain readable for existing cases and confirmation
records. They are never accepted as outputs of the v4 classifier.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.data_plan.enums import Execution, Intent

TAXONOMY_VERSION = "ecom-intent-v4-31"
MANIFEST_NAME = "intent_taxonomy.json"
LEGACY_INTENTS = frozenset({
    Intent.AFTER_SALE_REQUEST, Intent.REFUND_STATUS, Intent.INVOICE,
    Intent.CONFIRMATION, Intent.ANSWER_QUESTION,
})

# Canonical order is also used to render the training and inference prompts.
DEFINITIONS = {
    Intent.PRODUCT_INQUIRY: ("商品咨询", "单品参数、功能、兼容性、使用说明和当前标价，不含优惠、库存和服务政策"),
    Intent.PRODUCT_RECOMMENDATION: ("商品推荐", "按预算、用途和偏好发现或选择商品，包括在已有候选中选适合的一款"),
    Intent.PRODUCT_COMPARISON: ("商品对比", "比较不同型号或规格的属性、性能或价格差异，不代替用户按偏好选品"),
    Intent.INVENTORY_INQUIRY: ("库存查询", "查询 SKU 是否有货、可售数量和已公布补货计划"),
    Intent.DELIVERY_INQUIRY: ("配送咨询", "下单前的配送范围、运费和预计时效，以及一般配送规则"),
    Intent.SERVICE_POLICY_INQUIRY: ("服务政策咨询", "一般退换货、保修、价保和退货运费责任规则；具体订单资格归对应售后服务"),
    Intent.PROMOTION_INQUIRY: ("优惠咨询", "查询活动、领券条件、叠加规则、正常优惠资格和赠品资格"),
    Intent.PROMOTION_CALCULATION: ("到手价计算", "对确定的待购组合计算优惠后应付费用，包括固定方案的会员费测算；已支付订单的实付查询归订单查询"),
    Intent.COUPON_ISSUE: ("优惠券异常", "已发生领券、用券、优惠扣减或返券异常的诊断"),
    Intent.PRICE_COMPARISON: ("同款比价", "同规格商品跨渠道或明确购买时点的报价比较；缺报价不改变目标"),
    Intent.CART_OPTIMIZATION: ("凑单优化", "在预算、必买项等约束内选择加购、数量或拆合单方案"),
    Intent.PRICE_HISTORY: ("历史价格", "查询历史价格、走势和已观测低价，不预测未知未来价格"),
    Intent.MEMBERSHIP_INQUIRY: ("会员权益", "查询会员等级、积分余额、抵扣上限及权益资格，不计算整单应付"),
    Intent.ORDER_INQUIRY: ("订单查询", "查询订单状态、明细、实付、是否支付或发货"),
    Intent.LOGISTICS_INQUIRY: ("物流查询", "查询具体订单或包裹的承运商、轨迹、拆包和预计送达"),
    Intent.ADDRESS_CHANGE: ("修改收货信息", "修改已下订单的地址、收件人或电话；报价时补地区只是原任务信息"),
    Intent.ORDER_CANCEL: ("取消订单", "取消交易订单，不包括撤回未提交任务或撤销售后申请"),
    Intent.EXPEDITE: ("履约催办", "明确要求加快发货或配送，单纯询问为何未发货不属于催办"),
    Intent.PAYMENT_ISSUE: ("支付异常", "支付失败、重复扣款、扣款与订单不同步等支付问题"),
    Intent.REFUND_SERVICE: ("退款服务", "针对具体已购商品查询退款资格、可退金额，或申请退款、退货退款、退运费"),
    Intent.EXCHANGE_SERVICE: ("换货服务", "针对已购商品查询换货资格或办理换货，不包括售前更换推荐候选"),
    Intent.PRICE_PROTECTION_SERVICE: ("价保服务", "针对已购订单查询价保资格、补差金额或办理价保"),
    Intent.AFTER_SALE_STATUS: ("售后进度", "查询已申请退款、退货、换货和价保的进度、记录和结果"),
    Intent.ISSUE_REPORT: ("商品或收货问题", "排查故障、破损、错漏发、签收未收到等异常，尚无明确办理或投诉目标"),
    Intent.INVOICE_INQUIRY: ("发票查询", "查询发票规则、已有发票、开票状态或下载入口"),
    Intent.INVOICE_REQUEST: ("发票办理", "请求新开、修改或重开发票"),
    Intent.COMPLAINT: ("投诉", "明确要求投诉、举报或升级争议；愤怒语气本身不构成投诉"),
    Intent.HUMAN_HANDOFF: ("人工服务", "明确要求人工服务、回电或查询已有转接状态"),
    Intent.GREETINGS: ("问候", "纯问候或开场，没有业务诉求"),
    Intent.CLOSURE: ("收尾", "明确感谢、告别或结束咨询，没有新业务诉求"),
    Intent.OTHERS: ("其他", "结合上下文仍无法确定目标、未覆盖请求或其他非业务话语"),
}
CLASSIFICATION_INTENTS = frozenset(DEFINITIONS)
VALID_INTENTS = frozenset(intent.value for intent in DEFINITIONS)
REACT_INTENTS = frozenset({
    Intent.PRODUCT_INQUIRY, Intent.PRODUCT_RECOMMENDATION, Intent.PRODUCT_COMPARISON,
    Intent.PROMOTION_INQUIRY, Intent.CART_OPTIMIZATION, Intent.PAYMENT_ISSUE,
    Intent.REFUND_SERVICE, Intent.EXCHANGE_SERVICE, Intent.PRICE_PROTECTION_SERVICE,
    Intent.ISSUE_REPORT, Intent.COMPLAINT, Intent.OTHERS,
})
DEFAULT_EXECUTION = {
    intent: Execution.REACT if intent in REACT_INTENTS else Execution.WORKFLOW
    for intent in DEFINITIONS
}


def validate_manifest(directory: Path) -> None:
    """Reject old adapters rather than silently changing their trained label space."""
    path = directory / MANIFEST_NAME
    if not path.is_file():
        raise ValueError(f"意图模型缺少 {MANIFEST_NAME}；不能将旧 17 类 adapter 当作 31 类模型")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    labels = manifest.get("labels")
    if (manifest.get("taxonomy_version") != TAXONOMY_VERSION
            or not isinstance(labels, list) or len(labels) != len(VALID_INTENTS)
            or any(not isinstance(label, str) for label in labels)
            or set(labels) != VALID_INTENTS):
        raise ValueError("意图模型标签版本与当前 31 类契约不匹配")


def write_manifest(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST_NAME).write_text(json.dumps({
        "taxonomy_version": TAXONOMY_VERSION, "labels": sorted(VALID_INTENTS),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

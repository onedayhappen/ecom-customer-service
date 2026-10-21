"""Explicit status words for intent classification; never rewrites messages."""
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

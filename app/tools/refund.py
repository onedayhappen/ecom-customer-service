from app.data.order_service import request_refund


def apply_refund(order_id: str, reason: str) -> dict:
    """为当前用户的指定订单申请退款，需提供退款原因。"""
    return request_refund(order_id, reason, "pdd")

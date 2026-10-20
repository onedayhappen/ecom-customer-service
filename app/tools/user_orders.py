"""用户订单列表工具：展示层映射，数据访问下沉到 app/data。

从 order_service 取原始订单列表，映射为下游（ReAct / 查询改写订单定位）
需要的概要结构——状态中文标签、商品名摘要、金额、下单时间。

函数内延迟导入 app.data，避免与 tools 注册表构成循环依赖（见 order.py 注释）。
"""

STATUS_LABELS = {
    "pending": "待发货",
    "shipped": "已发货",
    "delivered": "已签收",
    "refund_processing": "退款中",
}


def list_user_orders(status: str | None = None, platform_code: str = "pdd") -> dict:
    """查询当前用户的所有订单概要列表；status 指定时仅返回该状态的订单。

    ``status`` 使用与下游一致的展示标签（待发货/已发货/已签收/退款中），
    由调用方（Workflow 状态筛选）从用户原句中抽取后传入，约束下推到此处，
    使返回结果天然只含目标订单，证据包与最终答复都自动收敛。
    """
    from app.data.order_service import list_user_orders as _list_user_orders

    data = _list_user_orders(platform_code=platform_code)
    if not data["success"]:
        return data
    orders = [
        {
            "order_id": order["order_id"],
            "platform_code": platform_code,
            "status": STATUS_LABELS.get(order.get("status", ""), order.get("status", "")),
            "items_summary": "、".join(item["name"] for item in order.get("items", [])),
            "total": order.get("total", 0),
            "created_at": order.get("created_at"),
        }
        for order in data["orders"]
    ]
    if status:
        # Workflow 的实体抽取使用内部状态值（pending/shipped/delivered），
        # 而展示层使用中文标签；两者都应支持，避免「待发货」查询被过滤为空。
        expected = STATUS_LABELS.get(status, status)
        orders = [o for o in orders if o["status"] == expected]
    return {"success": True, "count": len(orders), "orders": orders}

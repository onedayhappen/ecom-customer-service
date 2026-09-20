"""订单工具：编排入口，数据访问下沉到 app/data（SQL 唯一边界）。

安全变化（相对旧实现）：返回的订单 dict 不再包含 ``user_id``（归属信息
不暴露给模型/下游）；他人订单统一按「未找到」处理（evidence_unavailable），
不泄漏订单存在性。

注意：函数内延迟导入 app.data——data 层要读 tools.request_context 的身份
上下文，而 tools 包注册表又引用本模块，顶部互导会构成循环依赖。
"""


def query_order(order_id: str) -> dict:
    """根据订单号查询订单详情（状态、商品、金额、物流等）。"""
    from app.data.order_service import query_order_detail

    return query_order_detail(order_id, "pdd")

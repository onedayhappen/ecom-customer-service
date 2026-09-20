"""物流工具：编排入口，数据访问下沉到 app/data（SQL 唯一边界）。

安全变化（相对旧实现）：``tracking_number`` 直查路径现在也做订单归属
反查（JOIN orders），运单不存在与无权访问统一返回「暂无轨迹信息」，
修复旧 mock 实现「凭运单号即可查任意轨迹」的越权漏洞。

函数内延迟导入 app.data，避免与 tools 注册表构成循环依赖（见 order.py 注释）。
"""


def query_logistics(
    order_id: str | None = None,
    tracking_number: str | None = None,
) -> dict:
    """根据订单号或物流单号查询物流轨迹信息。

    二选一传入：
    - ``order_id``：按订单号查订单（含归属校验）→ 取运单号 → 查轨迹；
    - ``tracking_number``：按运单号直查（反查订单校验归属，防越权枚举）。
    """
    from app.data.logistics_service import query_logistics as _query_logistics

    return _query_logistics(order_id=order_id, tracking_number=tracking_number)

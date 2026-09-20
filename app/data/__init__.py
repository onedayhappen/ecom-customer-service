"""数据访问层：业务数据源（本地 MySQL / mock 回退）与 SQL 的唯一边界。

设计约定（与执行层/MCP 工具的边界）：
- **SQL 只允许出现在本包**：DAO 是唯一执行 SQL 的地方，MCP 工具与
  Workflow/ReAct 只调 ``order_service``，从代码结构上杜绝「工具层拼 SQL」。
- **user_id 永不来自工具参数**：由 ``order_service`` 从请求级 ContextVar
  （``app.tools.request_context``）读取，DAO 收到的 user_id 只可能是
  已鉴权身份；恶意/失真的模型参数无法覆盖归属校验。
- **fail-safe**：``ORDER_DB_DSN`` 未配置或 MySQL 不可用时回退内置 mock
  数据（与意图模型不可用回退规则识别同款降级哲学），本地离线/测试零依赖。
"""

from app.data.dto import OrderDTO
from app.data.order_dao import OrderDAO
from app.data.order_service import query_order_detail

__all__ = ["OrderDTO", "OrderDAO", "query_order_detail"]

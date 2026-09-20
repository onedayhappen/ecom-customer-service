"""Text2SQL 工具：把用户自然语言查询翻译为 SQL 并查询数据库。

动机
----
电商客服里大量问题本质是「结构化数据查询」（哪个订单、库存多少、某商品
销量/金额统计、谁下的单……）。与其为每种问法写一个专用工具，不如给模型
一张**数据库表头（schema）**，让模型自己生成只读 ``SELECT`` 语句去查库，
覆盖长尾问法。

本模块做了三件事：

1. **表头定义**：把用户/商品/订单三类实体规范化为关系表
   ``users`` / ``products`` / ``orders`` / ``order_items``，并提供
   一份人可读的表结构说明（:data:`SCHEMA_DOC`），这份表头会被直接塞进
   发送给模型的 prompt。
2. **数据落库**：当前 user/product/order 数据以 ``app.tools.mock_data``
   内存字典形式存在（并非真实 SQL 表）。本模块在首次使用时把它们物化到
   一个进程级单例的 SQLite 内存库，得到真正可 ``SELECT`` 的数据库。
   （SQLite 来自标准库，零新增依赖、离线可跑、进程内隔离。）
3. **生成与执行**：``query -> 组装 prompt -> LLM 生成 SQL -> 只读校验 ->
   执行 -> 返回行数据``。只读校验保证模型不能借工具改库。

若未来 user/product/order 迁入 PostgreSQL，只需替换 :func:`_get_engine` /
:func:`execute_sql` 的执行层为 ``app.db`` 连接池，prompt 表头与生成逻辑不变。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from typing import Any, Optional

from app.reliability.failure_policy import FailureCategory
from app.tools.mock_data import ORDERS, PRODUCTS

# ---------------------------------------------------------------------------
# 表头（DDL + 人可读 schema 文档）
# ---------------------------------------------------------------------------

#: 规范化关系表结构（SQLite 方言）。字段注释即「表头」语义。
SCHEMA_DDL = """
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,   -- 用户唯一ID
    name    TEXT NOT NULL       -- 用户昵称
);

CREATE TABLE products (
    product_id  TEXT PRIMARY KEY,  -- 商品ID / SKU
    name        TEXT NOT NULL,     -- 商品名称
    category    TEXT,              -- 品类（运动鞋/耳机/手机/家电/服饰/家居…）
    price       REAL,              -- 售价（元）
    stock       INTEGER,           -- 库存数量
    description TEXT,              -- 商品卖点描述
    specs       TEXT               -- 规格参数，JSON 键值对（颜色/尺码/品牌/续航等）
);

CREATE TABLE orders (
    order_id            TEXT PRIMARY KEY,  -- 订单号，如 ORD-20240115-001
    user_id             TEXT,              -- 下单用户（关联 users.user_id）
    status              TEXT,              -- 状态：pending待发货/shipped已发货/delivered已签收/refund_processing退款中
    total               REAL,              -- 订单总额（元）
    created_at          TEXT,              -- 下单时间 'YYYY-MM-DD HH:MM:SS'
    shipped_at          TEXT,              -- 发货时间（未发货为 NULL）
    delivered_at        TEXT,              -- 签收时间（未签收为 NULL）
    tracking_number     TEXT,              -- 物流单号
    carrier             TEXT,              -- 快递公司
    estimated_delivery  TEXT,              -- 预计送达日期 'YYYY-MM-DD'
    refund_reason       TEXT,              -- 退款原因（仅退款中订单）
    refund_status       TEXT,              -- 退款进度
    refund_requested_at TEXT               -- 退款申请时间
);

CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,  -- 自增主键
    order_id   TEXT,                 -- 所属订单（关联 orders.order_id）
    product_id TEXT,                 -- 商品ID/SKU（关联 products.product_id，可为空）
    name       TEXT,                 -- 商品名称快照
    quantity   INTEGER,              -- 购买数量
    price      REAL                  -- 单价（元）
);
""".strip()

#: 发送给模型的表头说明（含字段、类型、关系、枚举与示例）。这是 prompt 的核心。
SCHEMA_DOC = """【数据库表结构（表头）】

表 users（用户）
| 字段 | 类型 | 说明 |
|------|------|------|
| user_id | TEXT | 用户唯一ID（主键） |
| name | TEXT | 用户昵称 |

表 products（商品）
| 字段 | 类型 | 说明 |
|------|------|------|
| product_id | TEXT | 商品ID / SKU（主键） |
| name | TEXT | 商品名称 |
| category | TEXT | 品类，如 运动鞋/耳机/手机/家电/服饰/家居/游戏机/数码 |
| price | REAL | 售价（元） |
| stock | INTEGER | 库存数量 |
| description | TEXT | 商品卖点描述 |
| specs | TEXT | 规格参数 JSON 对象，如 {"颜色":"黑色","尺码":"42"} |

表 orders（订单）
| 字段 | 类型 | 说明 |
|------|------|------|
| order_id | TEXT | 订单号（主键），如 ORD-20240115-001 |
| user_id | TEXT | 下单用户，关联 users.user_id |
| status | TEXT | 状态：pending待发货 / shipped已发货 / delivered已签收 / refund_processing退款中 |
| total | REAL | 订单总额（元） |
| created_at | TEXT | 下单时间 'YYYY-MM-DD HH:MM:SS' |
| shipped_at | TEXT | 发货时间（未发货为 NULL） |
| delivered_at | TEXT | 签收时间（未签收为 NULL） |
| tracking_number | TEXT | 物流单号 |
| carrier | TEXT | 快递公司 |
| estimated_delivery | TEXT | 预计送达日期 'YYYY-MM-DD' |
| refund_reason | TEXT | 退款原因（仅退款中订单） |
| refund_status | TEXT | 退款进度 |
| refund_requested_at | TEXT | 退款申请时间 |

表 order_items（订单明细）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 自增主键 |
| order_id | TEXT | 所属订单，关联 orders.order_id |
| product_id | TEXT | 商品ID/SKU，关联 products.product_id（可能为空） |
| name | TEXT | 商品名称快照 |
| quantity | INTEGER | 购买数量 |
| price | REAL | 单价（元） |

表关系：
- orders.user_id → users.user_id（一个用户多笔订单）
- order_items.order_id → orders.order_id（一笔订单多个明细）
- order_items.product_id → products.product_id（明细对应商品，可能缺失）

查询技巧：
- 时间字段是 TEXT（'YYYY-MM-DD HH:MM:SS'），用字符串比较或 LIKE '2024-01%' 过滤月份；
- products.specs 是 JSON 字符串，可按 json_extract(specs, '$.颜色') 取指定规格，或用 LIKE '%黑色%' 模糊匹配；
- 统计类问题常用聚合 + GROUP BY + ORDER BY + LIMIT。"""

#: 少样本示例：帮助模型稳定输出单条 SELECT。
_FEWSHOT_EXAMPLES = """示例：
问：库存大于 50 的商品有哪些？
SQL：SELECT product_id, name, category, stock FROM products WHERE stock > 50 ORDER BY stock DESC;

问：订单总金额最高的前 3 笔订单？
SQL：SELECT order_id, user_id, total, status FROM orders ORDER BY total DESC LIMIT 3;

问：每个用户的订单数和消费总额？
SQL：SELECT u.user_id, u.name, COUNT(o.order_id) AS order_cnt, COALESCE(SUM(o.total), 0) AS total_amount FROM users u LEFT JOIN orders o ON o.user_id = u.user_id GROUP BY u.user_id, u.name;

问：退款中的订单有哪些？
SQL：SELECT order_id, user_id, total, refund_reason, refund_status FROM orders WHERE status = 'refund_processing';"""


def build_sql_prompt(query: str) -> list[dict]:
    """组装发送给模型的 messages：system 携带表头 + 规则 + 少样本，user 携带问题。

    表头（:data:`SCHEMA_DOC`）是 prompt 的核心，保证模型只基于已知字段
    生成 SQL，避免编造不存在的表/列。
    """
    system = (
        "你是数据库 SQL 专家。根据用户的自然语言问题，生成一条只读的 SQLite "
        "SELECT 查询语句。\n\n"
        f"{SCHEMA_DOC}\n\n"
        "规则：\n"
        "1. 只输出一条 SELECT 语句本身，不要任何解释、注释、前后缀或 Markdown 代码块标记；\n"
        "2. 只能查询，禁止 INSERT / UPDATE / DELETE / DROP / ALTER / CREATE 等写操作；\n"
        "3. 只能使用上面列出的表与字段，不要臆造不存在的列；\n"
        "4. 字符串字面量用单引号；字段/表名无需引号；\n"
        "5. 若问题无法映射到上述表，只输出：UNSUPPORTED\n\n"
        f"{_FEWSHOT_EXAMPLES}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": query},
    ]


# ---------------------------------------------------------------------------
# SQL 提取与只读校验
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "attach", "detach", "grant", "revoke", "vacuum", "pragma",
)

_UNSUPPORTED_MARKER = "UNSUPPORTED"


def extract_sql(raw: str) -> str:
    """从模型输出中稳健提取一条 SQL（去掉 Markdown 代码块、前后缀等）。"""
    text = (raw or "").strip()

    # 模型明确表示无法映射
    if _UNSUPPORTED_MARKER in text.upper() and not text.upper().strip().startswith(("SELECT", "WITH")):
        return _UNSUPPORTED_MARKER

    # 去掉 ```sql ... ``` 代码块
    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 去掉可能的 "SQL:" 前缀
    text = re.sub(r"^SQL[:：]\s*", "", text, flags=re.IGNORECASE).strip()

    # 截取第一条语句（SELECT / WITH 开头）
    match = re.search(r"\b(SELECT|WITH)\b.*", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return _UNSUPPORTED_MARKER
    sql = match.group(0).strip().rstrip(";").strip()
    return sql or _UNSUPPORTED_MARKER


def validate_sql(sql: str) -> tuple[bool, str]:
    """只读校验：仅允许单条 SELECT/WITH 查询，拒绝一切写操作与多语句。"""
    if not sql or sql == _UNSUPPORTED_MARKER:
        return False, "未生成可用的 SQL"
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return False, "SQL 为空"
    if ";" in stripped:
        return False, "一次只允许执行一条 SQL"
    upper = stripped.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, "只允许 SELECT 只读查询"
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"禁止使用 {kw.upper()}"
    return True, ""


# ---------------------------------------------------------------------------
# SQLite 数据落库（进程级单例，线程安全）
# ---------------------------------------------------------------------------

_engine: Optional[sqlite3.Connection] = None
_engine_lock = threading.Lock()
_exec_lock = threading.Lock()

#: 用户表播种：mock 数据中只出现这两个 user_id（other_user 为示例合集账号）。
_USERS = {"demo_user": "小明", "other_user": "其他用户"}


def _seed(conn: sqlite3.Connection) -> None:
    """把 mock_data 的用户/商品/订单物化进 SQLite。"""
    conn.executescript(SCHEMA_DDL)

    for user_id, name in _USERS.items():
        conn.execute("INSERT INTO users (user_id, name) VALUES (?, ?)", (user_id, name))

    for product in PRODUCTS.values():
        conn.execute(
            "INSERT INTO products (product_id, name, category, price, stock, description, specs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                product["product_id"],
                product["name"],
                product["category"],
                product["price"],
                product["stock"],
                product.get("description", ""),
                json.dumps(product.get("specs", {}), ensure_ascii=False),
            ),
        )

    for order in ORDERS.values():
        conn.execute(
            "INSERT INTO orders (order_id, user_id, status, total, created_at, shipped_at, "
            "delivered_at, tracking_number, carrier, estimated_delivery, refund_reason, "
            "refund_status, refund_requested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order["order_id"],
                order["user_id"],
                order["status"],
                order["total"],
                order["created_at"],
                order.get("shipped_at"),
                order.get("delivered_at"),
                order.get("tracking_number"),
                order.get("carrier"),
                order.get("estimated_delivery"),
                order.get("refund_reason"),
                order.get("refund_status"),
                order.get("refund_requested_at"),
            ),
        )
        for item in order.get("items", []):
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, name, quantity, price) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    order["order_id"],
                    item.get("sku"),
                    item["name"],
                    item.get("quantity", 1),
                    item.get("price", 0.0),
                ),
            )

    conn.commit()


def _get_engine() -> sqlite3.Connection:
    """进程级 SQLite 内存库单例（double-checked locking），首次使用时播种。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.row_factory = sqlite3.Row
                _seed(conn)
                _engine = conn
    return _engine


def reset_engine() -> None:
    """清空并重建 SQLite 单例（测试或重载 mock 数据时使用）。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.close()
            except Exception:
                pass
            _engine = None


# ---------------------------------------------------------------------------
# 生成与执行
# ---------------------------------------------------------------------------

def _default_llm() -> tuple[Any, str]:
    """懒加载共享 LLM 客户端（延迟 import，避免模块加载期拉入 FastAPI 重依赖）。"""
    from app.api.main import _get_openai_client
    from app.config.settings import settings

    return _get_openai_client(), settings.model_name


def generate_sql(query: str, *, client: Any = None, model: str | None = None) -> str:
    """调用模型把自然语言问题翻译为一条 SQL。

    ``client`` / ``model`` 可注入（测试 / 多模型）；缺省时懒加载共享客户端。
    """
    if client is None:
        client, resolved_model = _default_llm()
        model = model or resolved_model
    model = model or ""
    messages = build_sql_prompt(query)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
    )
    content = response.choices[0].message.content or ""
    return extract_sql(content)


def execute_sql(sql: str) -> dict:
    """只读执行一条 SQL，返回列名与行数据。"""
    ok, reason = validate_sql(sql)
    if not ok:
        return {
            "success": False,
            "error": reason,
            "error_code": FailureCategory.VALIDATION_ERROR.value,
            "sql": sql,
        }
    try:
        with _exec_lock:
            cursor = _get_engine().execute(sql)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description] if cursor.description else []
            data = [list(row) for row in rows]
    except Exception as exc:  # noqa: BLE001 —— 返回结构化错误，不向上抛
        return {
            "success": False,
            "error": f"SQL 执行失败: {exc}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
            "sql": sql,
        }
    return {
        "success": True,
        "sql": sql,
        "columns": columns,
        "rows": data,
        "row_count": len(data),
    }


def text_to_sql(query: str, *, client: Any = None, model: str | None = None) -> dict:
    """Text2SQL 工具入口：自然语言 → SQL → 查询结果。

    Returns:
        成功: ``{"success": true, "query", "sql", "columns", "rows", "row_count"}``
        失败: ``{"success": false, "error", "error_code", "query"}``
    """
    query = (query or "").strip()
    if not query:
        return {
            "success": False,
            "error": "query 不能为空",
            "error_code": FailureCategory.VALIDATION_ERROR.value,
            "query": query,
        }
    try:
        sql = generate_sql(query, client=client, model=model)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"SQL 生成失败: {exc}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
            "query": query,
        }
    if not sql or sql == _UNSUPPORTED_MARKER:
        return {
            "success": False,
            "error": "问题无法映射到可用数据表（用户/商品/订单/订单明细）",
            "error_code": FailureCategory.EVIDENCE_UNAVAILABLE.value,
            "query": query,
        }
    result = execute_sql(sql)
    result["query"] = query
    return result


# ---------------------------------------------------------------------------
# CLI 调试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--schema" in sys.argv:
        print(SCHEMA_DOC)
    elif len(sys.argv) > 1:
        print(json.dumps(text_to_sql(sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        print("用法：python -m app.tools.text2sql --schema | python -m app.tools.text2sql '<自然语言问题>'")

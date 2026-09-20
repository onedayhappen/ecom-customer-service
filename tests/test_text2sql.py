"""Text2SQL 工具测试：表头落库、SQL 提取、只读校验、执行与端到端生成。"""

from types import SimpleNamespace

import pytest

from app.tools import text2sql
from app.tools.mock_data import ORDERS, PRODUCTS


@pytest.fixture(autouse=True)
def _fresh_engine():
    """每个用例前重建 SQLite 单例，隔离任何潜在状态污染。"""
    text2sql.reset_engine()
    yield
    text2sql.reset_engine()


def make_client(sql: str):
    """构造一个返回固定 SQL 的 fake LLM 客户端。"""
    message = SimpleNamespace(content=sql)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))
    return SimpleNamespace(chat=chat)


# ---------------------------------------------------------------------------
# 表头落库
# ---------------------------------------------------------------------------

def test_schema_seed_populates_all_tables():
    conn = text2sql._get_engine()

    def count(table: str) -> int:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    assert count("users") == 2
    assert count("products") == len(PRODUCTS)
    assert count("orders") == len(ORDERS)
    expected_items = sum(len(o.get("items", [])) for o in ORDERS.values())
    assert count("order_items") == expected_items


def test_schema_doc_contains_table_headers():
    doc = text2sql.SCHEMA_DOC
    for table in ("users", "products", "orders", "order_items"):
        assert table in doc
    for column in ("order_id", "product_id", "user_id", "stock", "total", "status"):
        assert column in doc


def test_build_sql_prompt_embeds_schema_and_query():
    messages = text2sql.build_sql_prompt("库存大于50的商品")
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "库存大于50的商品"}
    system = messages[0]["content"]
    assert "users" in system and "orders" in system and "products" in system
    assert "stock" in system  # 表头字段进入 prompt


# ---------------------------------------------------------------------------
# SQL 提取
# ---------------------------------------------------------------------------

def test_extract_sql_strips_markdown_fence():
    assert text2sql.extract_sql("```sql\nSELECT * FROM orders;\n```") == "SELECT * FROM orders"


def test_extract_sql_handles_plain_and_prefix():
    assert text2sql.extract_sql("SELECT name FROM products") == "SELECT name FROM products"
    assert text2sql.extract_sql("SQL: SELECT 1") == "SELECT 1"


def test_extract_sql_returns_unsupported_when_no_select():
    assert text2sql.extract_sql("UNSUPPORTED") == "UNSUPPORTED"
    assert text2sql.extract_sql("抱歉，这个问题我无法回答") == "UNSUPPORTED"


# ---------------------------------------------------------------------------
# 只读校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM products",
        "UPDATE orders SET total = 0",
        "DROP TABLE products",
        "INSERT INTO users VALUES ('x', 'y')",
        "CREATE TABLE foo (id INT)",
    ],
)
def test_validate_sql_rejects_writes(sql):
    ok, reason = text2sql.validate_sql(sql)
    assert ok is False
    assert reason


def test_validate_sql_rejects_multiple_statements():
    ok, _ = text2sql.validate_sql("SELECT 1; DELETE FROM products")
    assert ok is False


def test_validate_sql_accepts_select_and_with():
    assert text2sql.validate_sql("SELECT * FROM orders")[0] is True
    assert text2sql.validate_sql("WITH t AS (SELECT 1) SELECT * FROM t")[0] is True


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def test_execute_sql_returns_rows():
    result = text2sql.execute_sql(
        "SELECT name, stock FROM products WHERE stock > 50 ORDER BY stock DESC"
    )
    assert result["success"] is True
    assert result["columns"] == ["name", "stock"]
    assert result["row_count"] == len(result["rows"]) > 0


def test_execute_sql_rejects_write():
    result = text2sql.execute_sql("DELETE FROM products")
    assert result["success"] is False
    assert result["error_code"] == "validation_error"


def test_execute_sql_join_across_tables():
    result = text2sql.execute_sql(
        "SELECT o.order_id, u.name, o.total FROM orders o "
        "JOIN users u ON u.user_id = o.user_id WHERE u.user_id = 'demo_user'"
    )
    assert result["success"] is True
    assert result["row_count"] > 0
    assert all(row[1] == "小明" for row in result["rows"])


# ---------------------------------------------------------------------------
# 端到端（fake LLM）
# ---------------------------------------------------------------------------

def test_text_to_sql_end_to_end():
    result = text2sql.text_to_sql(
        "库存大于50的商品",
        client=make_client("SELECT name, stock FROM products WHERE stock > 50"),
    )
    assert result["success"] is True
    assert result["sql"].startswith("SELECT")
    assert result["row_count"] > 0
    assert result["query"] == "库存大于50的商品"


def test_text_to_sql_unsupported_maps_to_evidence_unavailable():
    result = text2sql.text_to_sql("今天天气如何", client=make_client("UNSUPPORTED"))
    assert result["success"] is False
    assert result["error_code"] == "evidence_unavailable"


def test_text_to_sql_empty_query():
    result = text2sql.text_to_sql("   ")
    assert result["success"] is False
    assert result["error_code"] == "validation_error"

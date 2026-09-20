"""验证商品检索工具支持候选排序与过滤。"""

from app.tools.product import query_product


def test_query_product_returns_ranked_candidates():
    result = query_product("降噪耳机", top_k=3)

    assert result["success"] is True
    assert result["top_k"] == 3
    assert result["total_matches"] >= 3
    assert len(result["products"]) == 3
    assert all(product["category"] == "耳机" for product in result["products"])
    assert all("match_reason" in product for product in result["products"])


def test_query_product_honors_price_and_filters():
    result = query_product(
        "耳机",
        top_k=5,
        price_max=1800,
        filters={"color": "黑色", "in_stock": True},
    )

    assert result["success"] is True
    assert result["products"]
    assert all(product["price"] <= 1800 for product in result["products"])
    assert all(product["stock"] > 0 for product in result["products"])
    assert all("黑色" in str(product["specs"]) for product in result["products"])


def test_query_product_returns_empty_when_no_match():
    result = query_product("不存在的商品关键词", top_k=3)

    assert result["success"] is True
    assert result["products"] == []
    assert result["matched_products"] == []
    assert "message" in result

from __future__ import annotations

import json
from typing import Any

_MAX_TOP_K = 5
_DESCRIPTION_LIMIT = 72


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "是", "有"}:
            return True
        if lowered in {"false", "0", "no", "n", "否", "无"}:
            return False
    return bool(value)


def _truncate(text: Any, limit: int = _DESCRIPTION_LIMIT) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def _searchable_text(product: dict) -> str:
    specs = product.get("specs") or {}
    parts = [
        product.get("product_id", ""),
        product.get("name", ""),
        product.get("category", ""),
        product.get("description", ""),
        " ".join(str(v) for v in specs.values()),
    ]
    return " ".join(part for part in parts if part).lower()


def _normalize_filters(filters: Any) -> dict[str, Any]:
    if not filters:
        return {}
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except json.JSONDecodeError:
            return {}
    if not isinstance(filters, dict):
        return {}

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in filters.items():
        if raw_value in (None, "", [], {}):
            continue
        key = str(raw_key).strip().lower()
        if key in {"color", "colour", "颜色"}:
            normalized["color"] = str(raw_value).strip()
        elif key in {"size", "尺码"}:
            normalized["size"] = str(raw_value).strip()
        elif key in {"brand", "品牌"}:
            normalized["brand"] = str(raw_value).strip()
        elif key in {"category", "品类", "类型"}:
            normalized["category"] = str(raw_value).strip()
        elif key in {"in_stock", "stock_only", "only_in_stock"}:
            normalized["in_stock"] = _coerce_bool(raw_value)
        elif key in {"price_min", "min_price"}:
            normalized["price_min"] = _coerce_float(raw_value)
        elif key in {"price_max", "max_price"}:
            normalized["price_max"] = _coerce_float(raw_value)
        else:
            normalized[key] = raw_value
    return normalized


def _apply_filters(
    product: dict,
    *,
    price_min: float | None,
    price_max: float | None,
    filters: dict[str, Any],
) -> bool:
    price = product.get("price")
    stock = int(product.get("stock", 0) or 0)
    searchable = _searchable_text(product)
    specs = product.get("specs") or {}

    if price_min is not None and (price is None or float(price) < price_min):
        return False
    if price_max is not None and (price is None or float(price) > price_max):
        return False

    if filters.get("in_stock") is True and stock <= 0:
        return False
    if filters.get("in_stock") is False and stock > 0:
        return False

    category = filters.get("category")
    if category:
        category_norm = _normalize_text(category)
        if category_norm not in searchable:
            return False

    brand = filters.get("brand")
    if brand:
        brand_norm = _normalize_text(brand)
        if brand_norm not in searchable:
            return False

    color = filters.get("color")
    if color:
        color_norm = _normalize_text(color)
        if color_norm not in searchable and color_norm not in _normalize_text(specs.get("颜色")):
            return False

    size = filters.get("size")
    if size:
        size_norm = _normalize_text(size)
        if size_norm not in searchable and size_norm not in _normalize_text(specs.get("尺码")):
            return False

    return True


def _score_product(product: dict, query: str) -> tuple[int, list[str]]:
    searchable = _searchable_text(product)
    query_norm = _normalize_text(query)
    score = 0
    reasons: list[str] = []

    product_id = _normalize_text(product.get("product_id"))
    name = _normalize_text(product.get("name"))
    category = _normalize_text(product.get("category"))
    description = _normalize_text(product.get("description"))
    specs = product.get("specs") or {}

    if product_id and product_id == query_norm:
        return 200, [f"产品ID={product.get('product_id')}"]

    if product_id and product_id in query_norm:
        score += 120
        reasons.append(f"产品ID={product.get('product_id')}")

    if name and name in query_norm:
        score += 90
        reasons.append(f"名称={product.get('name')}")
    elif query_norm and query_norm in name:
        score += 40
        reasons.append(f"名称包含{product.get('name')}")

    if category and category in query_norm:
        score += 60
        reasons.append(f"品类={product.get('category')}")

    for label, value, weight in (
        ("卖点", description, 18),
        ("规格", " ".join(str(v) for v in specs.values()), 12),
    ):
        if not value:
            continue
        value_norm = _normalize_text(value)
        if value_norm and value_norm in query_norm:
            score += weight
            reasons.append(f"{label}命中")

    for spec_key, spec_value in specs.items():
        spec_norm = _normalize_text(spec_value)
        if spec_norm and spec_norm in query_norm:
            score += 10
            reasons.append(f"{spec_key}={spec_value}")

    if product.get("stock", 0):
        score += 4
    else:
        score -= 6

    if not reasons and query_norm and query_norm in searchable:
        score += 20
        reasons.append("整体文本命中")

    return score, reasons


def _project_product(product: dict, score: int, reasons: list[str]) -> dict:
    return {
        "product_id": product.get("product_id", ""),
        "merchant_id": product.get("merchant_id", ""),
        "name": product.get("name", ""),
        "category": product.get("category", ""),
        "price": product.get("price"),
        "stock": product.get("stock", 0),
        "description": _truncate(product.get("description", "")),
        "specs": product.get("specs", {}),
        "match_score": score,
        "match_reason": "；".join(reasons) if reasons else "基础匹配",
    }


def query_product(
    keyword: str,
    top_k: int = 3,
    price_min: float | None = None,
    price_max: float | None = None,
    filters: dict | None = None,
) -> dict:
    """根据商品关键词检索候选商品。

    支持一次返回多个候选，并允许用价格与基础属性过滤，
    让推荐场景尽量一次查全，而不是反复换词重搜。
    """
    top_k = max(1, min(int(top_k or 3), _MAX_TOP_K))
    filters = _normalize_filters(filters)
    price_min = _coerce_float(price_min if price_min is not None else filters.pop("price_min", None))
    price_max = _coerce_float(price_max if price_max is not None else filters.pop("price_max", None))

    query = str(keyword or "").strip()
    query_norm = _normalize_text(query)

    # 数据源：本地 MySQL（或 mock 回退），评分逻辑不随数据源降级。
    # 函数内延迟导入，避免与 tools 注册表构成循环依赖（见 order.py 注释）。
    from app.data.product_service import list_products

    catalog = list_products()
    if not catalog["success"]:
        # fail-closed：数据源不可用直接报瞬时故障，不让模型凭空回答。
        return catalog

    candidates: list[dict] = []
    for product in catalog["products"]:
        if not _apply_filters(
            product,
            price_min=price_min,
            price_max=price_max,
            filters=filters,
        ):
            continue

        score, reasons = _score_product(product, query_norm)
        if query_norm and not reasons:
            continue

        candidates.append(_project_product(product, score, reasons))

    candidates.sort(key=lambda item: (-item["match_score"], item["price"] or float("inf"), -int(item["stock"] or 0), item["name"]))
    results = candidates[:top_k]

    response = {
        "success": True,
        "query": query,
        "top_k": top_k,
        "price_min": price_min,
        "price_max": price_max,
        "filters": filters,
        "total_matches": len(candidates),
        "matched_products": results,
        "products": results,
    }

    if not results:
        response["message"] = "未找到匹配商品，请补充品牌、颜色、预算或品类。"

    return response

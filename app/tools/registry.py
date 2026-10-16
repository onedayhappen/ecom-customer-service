"""工具注册表：OpenAI function calling schema + 分发执行"""

import json
from typing import Callable

from app.tools.order import query_order
from app.tools.product import query_product
from app.tools.logistics import query_logistics
from app.tools.refund import apply_refund
from app.tools.knowledge import search_knowledge
from app.tools.user_orders import list_user_orders
from app.tools.memory_tool import recall_user_memory
from app.tools.skill_tool import load_skill
from app.tools.text2sql import text_to_sql
from app.reliability.failure_policy import FailureCategory
from app.support.runtime import call_tool as controlled_tool_call
from app.support.runtime import escalate_to_human
from app.tools.commerce import TOOL_MAP as COMMERCE_TOOLS, TOOL_DEFINITIONS as COMMERCE_DEFINITIONS

_TOOL_MAP: dict[str, Callable] = {
    **COMMERCE_TOOLS,
    "escalate_to_human": escalate_to_human,
    "query_order": query_order,
    "query_product": query_product,
    "query_logistics": query_logistics,
    "apply_refund": apply_refund,
    "search_knowledge": search_knowledge,
    "list_user_orders": list_user_orders,
    "recall_user_memory": recall_user_memory,
    "load_skill": load_skill,
    "text_to_sql": text_to_sql,
}

# 同一份 schema 同时作为模型可调用工具清单和本地分发白名单，避免两处配置漂移。

TOOL_DEFINITIONS: list[dict] = [
    {"type": "function", "function": {
        "name": "escalate_to_human", "description": "为当前会话创建人工工单并暂停机器人。适用于明确请求人工或需要升级处理。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "根据订单号查询订单详情，包括订单状态、商品信息、金额、物流单号等",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单号，例如 ORD-20240115-001",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_product",
            "description": (
                "根据商品关键词检索候选商品。"
                "支持一次返回多个候选，并可用 top_k、price_min、price_max 和 filters "
                "（如品牌、颜色、尺码、品类、是否有货）做筛选。"
                "推荐场景优先一次查全，不要反复换词重搜。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "商品名称关键词、商品ID或用户自然语言需求，例如「耳机」「运动鞋」「预算 2000 的降噪耳机」",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回候选数量，默认 3，最大 5",
                        "default": 3,
                    },
                    "price_min": {
                        "type": "number",
                        "description": "最低价格过滤",
                    },
                    "price_max": {
                        "type": "number",
                        "description": "最高价格过滤",
                    },
                    "filters": {
                        "type": "object",
                        "description": "附加筛选条件，如 brand、category、color、size、in_stock",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logistics",
            "description": "根据订单号或物流单号查询物流轨迹信息，包括快递公司、运单号、运输状态和轨迹事件",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单号，例如 ORD-20240115-001",
                    },
                    "tracking_number": {
                        "type": "string",
                        "description": "物流单号/运单号，例如 SF1234567890",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "检索并夕夕的政策与帮助文档（退换货政策、配送说明、会员权益、常见问题 FAQ）。"
                "当顾客询问规则、流程、时效、是否支持等政策类问题时使用，"
                "比如「能退货吗」「多久到账」「钻石会员有什么权益」「偏远地区包邮吗」。"
                "返回 Top-K 命中片段及来源文档，请基于检索结果回答，不要编造政策"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用顾客的原问题或一句简洁中文描述要查的政策点",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回片段数，默认 3，最大 5",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_orders",
            "description": (
                "查询当前用户的所有订单概要列表（订单号、状态、商品、金额、下单时间）。"
                "当用户想查订单但未提供订单号，或提供的订单号查不到时，"
                "调用此工具列出订单供用户确认"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_refund",
            "description": "为指定订单申请退款。注意：这是一个敏感操作，调用前应先与用户确认",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "要退款的订单号",
                    },
                    "reason": {
                        "type": "string",
                        "description": "退款原因，例如「尺码不合适」「质量问题」「不想要了」",
                    },
                },
                "required": ["order_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_user_memory",
            "description": (
                "查询当前用户有来源的长期偏好，支持过去日期的历史版本。"
                "适用于颜色、尺码、预算、沟通偏好等；客服承诺走公开历史回查，订单和会员状态走业务工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                      "query": {
                          "type": "string",
                          "description": "当前用户问题或需求，用于检索相关记忆",
                      }
                  },
                  "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载指定技能的完整操作指令。"
                "当用户问题匹配某个可用技能时，调用此工具获取该技能的详细处理流程，"
                "然后按流程指引使用已有工具完成用户请求。"
                "可用技能会在系统提示中列出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要加载的技能名称，如 process-return、track-order、product-recommend",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_to_sql",
            "description": (
                "根据自然语言问题生成并执行只读 SQL，查询数据库中的用户、商品、"
                "订单与订单明细表，返回查询结果。适合统计、筛选、聚合、排序等"
                "结构化数据查询，例如「库存大于50的商品」「订单总金额最高的前3笔」「"
                "退款中的订单」。工具内部已内置表结构，无需额外传参。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要查询的自然语言问题，例如「库存大于50的商品有哪些」",
                    }
                },
                "required": ["query"],
            },
        },
    },
]


TOOL_DEFINITIONS.extend(COMMERCE_DEFINITIONS)


def execute_tool(name: str, arguments: dict) -> str:
    """根据工具名称分发执行，返回 JSON 字符串结果。"""
    # 禁止模型通过任意函数名触达本地代码，仅允许显式注册的工具。
    func = _TOOL_MAP.get(name)
    if not func:
        return json.dumps({
            "success": False,
            "error": f"未知工具: {name}",
            "error_code": FailureCategory.VALIDATION_ERROR.value,
        }, ensure_ascii=False)
    try:
        result = controlled_tool_call(name, arguments, func)
    except Exception as e:
        result = {
            "success": False,
            "error": f"工具执行出错: {e}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
        }
    return json.dumps(result, ensure_ascii=False)

"""Evidence Pack 校验：最终回复中的关键实体必须有工具证据支撑。

防止"幻觉溢出"：模型最终回复里的订单号、金额、状态关键词必须能在
某个工具结果中找到；找不到的句子被替换为保守表述，而不是任由模型
基于常识补全。

本模块是纯确定性实现（正则提取 + 文本比对），可独立测试、可在
故障注入评测中直接复用。不依赖 LLM。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 高风险实体：订单号 / 金额 / 业务状态关键词
_ORDER_ID = re.compile(r"\bORD-[A-Za-z0-9-]+", re.IGNORECASE)
_MONEY = re.compile(r"[¥￥]\s*\d[\d,]*(?:\.\d+)?")
_STATUSES = (
    "待发货", "已发货", "已签收", "退款中", "已取消",
    "退款成功", "退货中", "退款申请已提交",
)

# 含未证实实体的句子被替换为该保守表述
UNVERIFIED_SENTENCE = "（该信息暂未能核实，请以订单详情页为准）"


@dataclass
class EvidenceReport:
    """一次校验的结果。"""

    supported: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def has_unsupported(self) -> bool:
        return bool(self.unsupported)


def extract_entities(text: str) -> dict[str, list[str]]:
    """从回复文本中提取三类高风险实体。"""
    order_ids = sorted({m.group(0).upper() for m in _ORDER_ID.finditer(text or "")})
    amounts = sorted({m.group(0) for m in _MONEY.finditer(text or "")})
    statuses = sorted({s for s in _STATUSES if s in (text or "")})
    return {"order_ids": order_ids, "amounts": amounts, "statuses": statuses}


def _normalize_amount(value: str) -> str:
    """¥129.00 / ￥1,299 → 提取纯数字串用于证据比对。"""
    digits = re.sub(r"[^\d.]", "", value)
    try:
        number = float(digits)
    except ValueError:
        return digits
    if number == int(number):
        return str(int(number))
    return str(number)


def verify_reply(reply: str, tool_results: list[dict[str, Any]]) -> EvidenceReport:
    """逐个检查回复中的实体是否有工具证据支撑。

    ``tool_results`` 是本轮流过的全部工具结果（只读证据；模型生成的
    arguments 不算证据）。
    """
    evidence_text = "\n".join(
        json.dumps(result, ensure_ascii=False)
        for result in tool_results
        if isinstance(result, dict)
    ).upper()

    report = EvidenceReport()
    entities = extract_entities(reply)

    for order_id in entities["order_ids"]:
        (report.supported if order_id.upper() in evidence_text else report.unsupported).append(order_id)

    for amount in entities["amounts"]:
        normalized = _normalize_amount(amount)
        (report.supported if normalized in evidence_text else report.unsupported).append(amount)

    for status in entities["statuses"]:
        (report.supported if status.upper() in evidence_text else report.unsupported).append(status)

    return report


def sanitize_reply(reply: str, report: EvidenceReport) -> str:
    """把包含未证实实体的句子替换为保守表述，其余句子保持不变。"""
    if not report.has_unsupported or not reply:
        return reply

    # 按句末标点切分并保留分隔符，逐句检查
    parts = re.split(r"([。！？!?\n])", reply)
    sentences: list[str] = []
    for i in range(0, len(parts), 2):
        sentence = parts[i]
        delimiter = parts[i + 1] if i + 1 < len(parts) else ""
        if not sentence:
            if delimiter:
                sentences.append(delimiter)
            continue
        found = extract_entities(sentence)
        has_bad = any(
            entity in report.unsupported
            for entity in [*found["order_ids"], *found["amounts"], *found["statuses"]]
        )
        sentences.append(UNVERIFIED_SENTENCE + delimiter if has_bad else sentence + delimiter)
    return "".join(sentences).strip()

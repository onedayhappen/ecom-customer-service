# 文件说明：合并结构化长期记忆，处理重复、替换和存储额度控制。
"""长期结构化记忆合并与防膨胀规则。

MemoryConsolidator 是“热记忆 active set”的管家。它解决两个核心问题：

1. 同槽位替换
   例如用户先说“喜欢白色耳机”，后来明确说“现在更喜欢黑色耳机”，这不是新增一条
   偏好，而是同一个 memory_key 下的新事实替换旧事实。

2. 容量控制
   长期运行时，facts 会天然增长。如果每条都永远 active，检索成本和 prompt 噪声都会
   增加。因此这里对 active facts 做总量和按 category 的上限裁剪。

注意：Consolidator 不判断候选记忆“应不应该写入”。可信度和来源准入由
MemoryValidator 负责；这里假设传入的 incoming facts 已经通过了准入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from app.memory.long_term import MemoryFact


_ORDER_ID = re.compile(r"\b(?:ORD|DX)[-]?[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryQuota:
    """结构化记忆热存储限额。

    max_active_facts 控制用户画像热层总体大小；
    max_active_per_category 防止某一类事实（例如 issue）挤占全部预算。
    """

    max_active_facts: int = 50
    max_active_per_category: int = 10


class MemoryConsolidator:
    """把候选结构化记忆合并进 active facts。

    这层不判断候选记忆是否可信；可信度由 MemoryValidator 负责。
    Consolidator 只处理去重、同槽位替换和容量裁剪。
    """

    def __init__(self, quota: MemoryQuota | None = None):
        self.quota = quota or MemoryQuota()

    def consolidate(
        self,
        existing: Iterable["MemoryFact"],
        incoming: Iterable["MemoryFact"],
        now: datetime | None = None,
    ) -> list["MemoryFact"]:
        now = now or datetime.now(timezone.utc)
        # 先确保现有 facts 都有 memory_key/memory_id；旧 snapshot 数据可能没有这些字段。
        active = [self._with_key(fact) for fact in existing]
        by_key = {fact.memory_key: fact for fact in active}

        for fact in incoming:
            candidate = self._with_key(fact)
            current = by_key.get(candidate.memory_key)
            if current is None:
                # 新槽位：直接进入 active set，后面统一做 quota 裁剪。
                active.append(candidate)
                by_key[candidate.memory_key] = candidate
                continue

            # Event time, rather than worker completion order, determines
            # whether a delayed batch can change an already newer slot.
            if candidate.source_event_id and current.source_event_id:
                if candidate.source_event_id == current.source_event_id:
                    continue
                old_time = _event_time(current)
                new_time = _event_time(candidate)
                if old_time and new_time and new_time < old_time:
                    continue

            if self._same_content(current, candidate):
                # 同槽位同内容：不是新增，只刷新时间/置信度/过期时间。
                self._refresh(current, candidate, now)
                continue

            if self._candidate_wins(current, candidate):
                # 同槽位不同内容：分数更高的新事实替换旧事实，同时保留 supersede 链路。
                current.status = "superseded"
                candidate.supersedes_memory_id = current.memory_id
                current.superseded_by_memory_id = candidate.memory_id
                active = [item for item in active if item is not current]
                active.append(candidate)
                by_key[candidate.memory_key] = candidate

        return self._apply_quota(active)

    def _with_key(self, fact: "MemoryFact") -> "MemoryFact":
        """补齐 MemoryFact 的稳定标识。

        memory_key 表示“语义槽位”，memory_id 表示“具体事实版本”。
        同一个 memory_key 可以随着时间出现多个版本，但 active 层只保留当前版本。
        """
        if not fact.memory_key:
            fact.memory_key = self.derive_memory_key(fact)
        if not fact.memory_id:
            fact.memory_id = self._stable_memory_id(fact)
        return fact

    @staticmethod
    def derive_memory_key(fact: "MemoryFact") -> str:
        """从事实内容中派生可替换的语义槽位。

        这里暂时使用规则而非 LLM：规则更稳定，也更容易测试。未来可以把同义表达归一化
        做得更强，但仍应保持“同一槽位只能有一个 active 版本”的原则。
        """
        content = fact.content
        category = fact.category or "other"
        order_match = _ORDER_ID.search(content)
        if category == "identity":
            if "会员" in content:
                return "identity:membership_level"
            if any(word in content for word in ("地址", "城市", "收货")):
                return "identity:shipping_location"
            return f"identity:{_fingerprint(content)}"
        if category == "preference":
            if any(word in content for word in ("电子", "手机", "耳机", "电脑")) and "颜色" in content:
                return "preference:electronics_color"
            if any(word in content for word in ("鞋", "运动鞋")) and "颜色" in content:
                return "preference:shoe_color"
            if "颜色" in content or any(color in content for color in ("黑色", "白色", "红色", "蓝色")):
                return "preference:color"
            if any(word in content for word in ("尺码", "码数", "鞋码")):
                return "preference:size"
            return f"preference:{_fingerprint(content)}"
        if category == "issue":
            if order_match:
                order_id = order_match.group(0).upper()
                if any(word in content for word in ("退款", "退货", "换货")):
                    return f"issue:refund:{order_id}"
                if any(word in content for word in ("物流", "快递", "发货", "签收")):
                    return f"issue:logistics:{order_id}"
                return f"issue:order:{order_id}"
            return f"issue:{_fingerprint(content)}"
        return f"{category}:{_fingerprint(content)}"

    @staticmethod
    def _same_content(left: "MemoryFact", right: "MemoryFact") -> bool:
        return left.content.strip().lower() == right.content.strip().lower()

    @staticmethod
    def _refresh(target: "MemoryFact", source: "MemoryFact", now: datetime) -> None:
        """刷新已存在事实的生命周期信息。

        对偏好类事实，如果没有显式 expires_at，则给一个默认 TTL；这可以让“可能变化的
        口味/偏好”自然退场，避免多年以前的偏好长期污染推荐。
        """
        legacy_now = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
        timestamp = (source.updated_at or source.created_at) if source.source_event_id else legacy_now.isoformat(timespec="seconds")
        target.updated_at = timestamp
        target.confidence = max(target.confidence, source.confidence)
        if source.expires_at:
            target.expires_at = source.expires_at
        elif target.category == "preference" and not target.expires_at:
            target.expires_at = (legacy_now + timedelta(days=90)).isoformat(timespec="seconds")
        if source.source_event_id:
            target.source_event_id = source.source_event_id
            target.source_session = source.source_session
            target.source_span = source.source_span

    @staticmethod
    def _candidate_wins(current: "MemoryFact", candidate: "MemoryFact") -> bool:
        """判断新事实是否可以替换旧事实。

        当前策略偏简单：来源权重 + 置信度。tool_verified > user_explicit > llm_inferred。
        """
        if current.source_type == candidate.source_type == 'user_explicit':
            old_time, new_time = _event_time(current), _event_time(candidate)
            if old_time and new_time and candidate.source_event_id and current.source_event_id:
                return new_time >= old_time
        return _memory_score(candidate) >= _memory_score(current)

    def _apply_quota(self, facts: list["MemoryFact"]) -> list["MemoryFact"]:
        """只保留高价值 active facts。

        superseded/expired/deleted 不再进入热层；它们可以由 Store 归档，用于审计或离线分析。
        """
        active = [fact for fact in facts if fact.status == "active"]
        active.sort(key=_sort_key, reverse=True)

        category_counts: dict[str, int] = {}
        kept: list["MemoryFact"] = []
        for fact in active:
            if len(kept) >= self.quota.max_active_facts:
                break
            count = category_counts.get(fact.category, 0)
            if count >= self.quota.max_active_per_category:
                continue
            kept.append(fact)
            category_counts[fact.category] = count + 1
        return kept

    @staticmethod
    def _stable_memory_id(fact: "MemoryFact") -> str:
        return f"mem-{_fingerprint(f'{fact.memory_key}|{fact.content}')}"


def _memory_score(fact: "MemoryFact") -> float:
    source_weight = {
        "tool_verified": 3.0,
        "user_explicit": 2.5,
        "llm_inferred": 1.0,
    }.get(fact.source_type, 0.0)
    return source_weight + fact.confidence


def _event_time(fact: "MemoryFact") -> datetime | None:
    try:
        value = datetime.fromisoformat(fact.updated_at or fact.created_at)
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _sort_key(fact: "MemoryFact") -> tuple[float, str]:
    return _memory_score(fact), fact.updated_at or fact.created_at


def _fingerprint(value: str) -> str:
    """生成短哈希，作为兜底 memory_key/memory_id 的稳定片段。"""
    import hashlib

    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]

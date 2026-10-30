# 文件说明：根据事实版本引用构建可重建的热点画像与主题摘要视图。
"""Rebuildable hot profiles and topic summaries backed by fact/version refs."""
from __future__ import annotations

from collections import defaultdict
import json

from app.memory.retrieval import content_hash, parse_time, time_eligible
from app.session.context_budget import estimate_text_tokens


def fact_reference(fact) -> dict:
    return {"memory_id": fact.memory_id, "version": getattr(fact, "version", 1), "content_hash": content_hash(fact)}


def resolve_view(view: dict, facts: list) -> list:
    """Reject the entire derived text when even one source version is stale."""
    refs = view.get("refs", [])
    current = {(fact.memory_id, getattr(fact, "version", 1)): fact for fact in facts if time_eligible(fact)}
    resolved = []
    for ref in refs:
        fact = current.get((ref.get("memory_id"), ref.get("version")))
        if fact is None or ref.get("content_hash") != content_hash(fact):
            return []
        resolved.append(fact)
    if "text" in view and view["text"] != "\n".join(fact.content for fact in resolved):
        return []
    return resolved


def build_views(facts: list, hot_limit: int = 20, summary_tokens: int = 300) -> dict:
    active = [fact for fact in facts if time_eligible(fact) and fact.memory_id]

    def priority(fact):
        try:
            recent = parse_time(fact.updated_at or fact.created_at)
        except (TypeError, ValueError):
            recent = None
        tier = getattr(fact, "metadata", {}).get("tier")
        return (0 if tier == "hot" else 2 if tier == "cold" else 1,
                -len(getattr(fact, "evidence", [])), -(recent.timestamp() if recent else 0), fact.memory_id)

    active.sort(key=priority)
    profile = {"refs": [fact_reference(fact) for fact in active[:max(0, hot_limit)]]}
    groups = defaultdict(list)
    for fact in active:
        topic = getattr(fact, "qualifiers", {}).get("product_category") or getattr(fact, "predicate", "") or fact.category
        groups[topic].append(fact)
    topics = {}
    for topic, candidates in groups.items():
        selected = []
        for fact in candidates:
            proposed = selected + [fact]
            view = {"refs": [fact_reference(item) for item in proposed], "text": "\n".join(item.content for item in proposed)}
            if estimate_text_tokens(json.dumps(view, ensure_ascii=False, separators=(",", ":"))) <= summary_tokens:
                selected = proposed
        if selected:
            topics[topic] = {"refs": [fact_reference(fact) for fact in selected], "text": "\n".join(fact.content for fact in selected)}
    return {"profile": profile, "topics": topics}

"""LLM 事实提取：从对话中抽取长期记忆候选。

这里的关键词是“候选”。LLM 负责从会话里提出可能值得长期保存的 facts，
但真正写入前还要经过 MemoryValidator 和 MemoryConsolidator：

格式化对话 → 调用 LLM → 解析 JSON → Validator 准入 → Consolidator 合并 → Store 持久化

这样做是为了防止 LLM 把临时状态、猜测、幻觉或缺少证据的内容直接污染长期记忆。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from openai import OpenAI

from app.memory.validator import MemoryValidator
from app.prompts.memory import LTM_EXTRACTION_PROMPT

if TYPE_CHECKING:
    from app.memory.long_term import MemoryFact


def _build_transcript(messages: list[dict]) -> str:
    """将消息列表格式化为可供 LLM 抽取的对话文本。

    工具调用只保留工具名和截断后的结果，避免把完整工具返回的大 JSON 原样塞给模型。
    证据校验也依赖这个 transcript：user_explicit 的 source_span 必须能在这里找到。
    """
    lines = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    lines.append(f"客服：[调用工具 {name}]")
            if content:
                lines.append(f"客服：{content}")
        elif role == "tool":
            display = content if len(content) <= 200 else content[:200] + "..."
            lines.append(f"[工具结果] {display}")

    return "\n".join(lines)


def extract_long_term_facts(
    client: OpenAI,
    model: str,
    messages: list[dict],
    summary: str | None,
    existing_facts: list,
) -> list[MemoryFact]:
    """从完整会话中提取稳定、可复用的长期事实。"""
    from app.memory.long_term import MemoryFact

    parts = []
    if summary:
        parts.append(f"【对话摘要】\n{summary}")

    transcript = _build_transcript(messages)
    if transcript:
        parts.append(f"【对话内容】\n{transcript}")

    if not parts:
        return []

    # 把已有 facts 提供给模型，减少重复抽取；最终去重仍由 Consolidator 保证。
    existing_text = (
        "\n".join(f"- [{f.category}] {f.content}" for f in existing_facts)
        if existing_facts
        else "（暂无）"
    )
    prompt = LTM_EXTRACTION_PROMPT.format(existing_ltm=existing_text)
    from app.config.settings import settings

    from app.observability.llm_client import LlmClient
    client = client if isinstance(client, LlmClient) else LlmClient(client, model, default_scene='memory')
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        **settings.chat_completion_options,
    )
    # SDK 在极端情况下可能返回空 content；空响应与无效 JSON 一样，安全跳过。
    raw = (response.choices[0].message.content or "").strip()
    validator = MemoryValidator()

    # 长期提取要求 JSON；解析失败时宁可跳过，不能把不可信文本写进用户记忆。
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    now = datetime.now().isoformat(timespec="seconds")
    new_facts = []
    fact_items = data.get("facts", [])
    # JSON 根对象正确不代表 facts 一定是数组；不能让模型的异常结构打断会话收尾。
    if not isinstance(fact_items, list):
        fact_items = []
    for item in fact_items:
        if not isinstance(item, dict):
            continue
        raw_content = item.get("content", "")
        content = raw_content.strip() if isinstance(raw_content, str) else ""
        raw_category = item.get("category", "other")
        category = raw_category.strip() if isinstance(raw_category, str) else "other"
        if content:
            raw_source_type = item.get("source_type", "llm_inferred")
            source_type = raw_source_type.strip() if isinstance(raw_source_type, str) else "llm_inferred"
            # 不可信枚举一律降级，避免模型伪造“用户明确陈述”。
            if source_type not in {"user_explicit", "tool_verified", "llm_inferred"}:
                source_type = "llm_inferred"
            raw_source_span = item.get("source_span", "")
            source_span = raw_source_span.strip() if isinstance(raw_source_span, str) else ""
            # 不能只相信模型声称“用户明确表达”：证据必须实际出现在会话中。
            if source_type == "user_explicit" and (not source_span or source_span not in transcript):
                source_type = "llm_inferred"
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            # MemoryFact 是内部统一结构；即便未来 prompt 字段变化，也尽量在这里做兼容映射。
            raw_memory_key = item.get("memory_key", "")
            memory_key = raw_memory_key.strip() if isinstance(raw_memory_key, str) else ""
            raw_scope = item.get("scope", "user_preference")
            scope = raw_scope.strip() if isinstance(raw_scope, str) else "user_preference"
            fact = MemoryFact(
                content=content,
                category=category,
                created_at=now,
                memory_key=memory_key,
                source_type=source_type,
                source_span=source_span,
                confidence=max(0.0, min(confidence, 1.0)),
                scope=scope,
            )
            # Validator 是最后的确定性闸门；LLM 低置信或无证据输出不会进入长期层。
            if validator.validate(fact, transcript).accepted:
                new_facts.append(fact)

    return new_facts

"""LLM 事实提取：从对话中抽取长期记忆候选。

这里的关键词是“候选”。LLM 负责从会话里提出可能值得长期保存的 facts，
生产 v2 先经过 atomic 的结构与证据校验，再由 runtime 绑定认证来源，
经 reconciliation 决定更新并持久化。v1 的 Validator/Consolidator 保留用于旧调用。

这样做是为了防止 LLM 把临时状态、猜测、幻觉或缺少证据的内容直接污染长期记忆。
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TYPE_CHECKING

from openai import OpenAI

from app.memory.validator import MemoryValidator
from app.prompts.memory import LTM_EXTRACTION_PROMPT, ATOMIC_EXTRACTION_PROMPT

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


class MemoryExtractionError(ValueError):
    """Model response could not be safely interpreted as an extraction batch."""


class ExtractionBatch(list):
    """Accepted facts plus candidate outcomes; compatible with legacy list callers."""

    def __init__(self, facts=(), issues=()):
        super().__init__(facts)
        self.issues = list(issues)


def candidate_group(item, messages):
    """A failed candidate can fence its own slot, not unrelated preferences."""
    from app.memory.atomic import CATEGORY_ALIASES, SIZE_SYSTEM_ALIASES, _canonical, _categories, _scope_quote
    if not isinstance(item, dict):
        return None, None
    predicate = item.get('predicate')
    if not isinstance(predicate, str):
        return None, None
    qualifiers = item.get('qualifiers')
    if not isinstance(qualifiers, dict):
        return predicate, None
    try:
        normalized = {}
        for name, aliases in (('product_category', CATEGORY_ALIASES), ('size_system', SIZE_SYSTEM_ALIASES)):
            if name in qualifiers:
                normalized[name] = _canonical(qualifiers[name], aliases)
        index, quote = item.get('source_index'), item.get('source_quote')
        if predicate == 'budget_range' and 'product_category' not in normalized:
            if type(index) is int and 0 <= index < len(messages) and isinstance(quote, str) and quote in messages[index]['content']:
                categories = _categories(_scope_quote(messages[index]['content'], quote))
                if len(categories) == 1:
                    normalized['product_category'] = categories[0]
        return predicate, tuple(sorted(normalized.items())) if normalized else None
    except ValueError:
        return predicate, None


def isolate_related_updates(batch):
    """A rejected replacement must not leave a matching revocation half applied."""
    from collections import defaultdict

    result = ExtractionBatch(issues=batch.issues)
    blocked, retractions = defaultdict(set), defaultdict(set)
    for issue in batch.issues:
        blocked[issue['predicate']].add(issue['group'])
        if issue.get('operation') == 'RETRACT':
            retractions[issue['predicate']].add(issue['group'])
    for fact in batch:
        if fact.operation == 'RETRACT':
            retractions[fact.predicate].add(tuple(sorted(fact.qualifiers.items())))
    for fact in batch:
        group = (fact.predicate, tuple(sorted(fact.qualifiers.items())))
        fences, changes = blocked.get(group[0], ()), retractions.get(group[0], ())
        if (None in changes or group[1] in changes) and (None in fences or group[1] in fences):
            result.issues.append(dict(source_index=fact.metadata['source_index'], predicate=fact.predicate,
                group=group[1], operation=fact.operation, reason='related_candidate_failed', status='deferred'))
        else:
            result.append(fact)
    return result


def build_extraction_messages(messages, summary, existing_facts, *, user_sources_only=False, schema_version=1):
    if schema_version == 2:
        sources = [{'role': m['role'], 'content': m['content']} for m in messages]
        existing = [dict(predicate=getattr(f, 'predicate', ''), value=getattr(f, 'value', None),
                         qualifiers=getattr(f, 'qualifiers', {}), content=f.content) for f in existing_facts]
        return [{'role': 'system', 'content': ATOMIC_EXTRACTION_PROMPT},
                {'role': 'user', 'content': json.dumps({'messages': sources, 'existing_facts': existing}, ensure_ascii=False)}]
    parts = []
    if summary and not user_sources_only:
        parts.append(f"【对话摘要】\n{summary}")
    transcript = _build_transcript(messages)
    if transcript:
        parts.append(f"【对话内容】\n{transcript}")
    existing_text = '\n'.join(f'- [{f.category}] {f.content}' for f in existing_facts) or '（暂无）'
    prompt = LTM_EXTRACTION_PROMPT.format(existing_ltm=existing_text)
    if user_sources_only:
        from app.prompts.memory import USER_SOURCE_EXTRACTION_NOTE
        prompt += '\n' + USER_SOURCE_EXTRACTION_NOTE
    return [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': '\n\n'.join(parts)}]


def extract_long_term_facts(
    client: OpenAI,
    model: str,
    messages: list[dict],
    summary: str | None,
    existing_facts: list,
    *,
    strict: bool = False,
    user_sources_only: bool = False,
    schema_version: int = 1,
) -> list[MemoryFact]:
    """从完整会话中提取稳定、可复用的长期事实。"""
    from app.memory.long_term import MemoryFact

    transcript = _build_transcript(messages)
    if not transcript and not summary:
        return []
    request_messages = build_extraction_messages(messages, summary, existing_facts,
                                                user_sources_only=user_sources_only, schema_version=schema_version)
    from app.config.settings import settings

    from app.observability.llm_client import LlmClient
    client = client if isinstance(client, LlmClient) else LlmClient(client, model, default_scene='memory')
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=request_messages,
        **settings.chat_completion_options,
    )
    # SDK 在极端情况下可能返回空 content；空响应与无效 JSON 一样，安全跳过。
    raw = (response.choices[0].message.content or "").strip()
    validator = MemoryValidator()

    # 长期提取要求 JSON；解析失败时宁可跳过，不能把不可信文本写进用户记忆。
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if strict:
            raise MemoryExtractionError('Invalid extraction JSON') from None
        return []
    if not isinstance(data, dict):
        if strict:
            raise MemoryExtractionError('Extraction root must be an object')
        return []

    if schema_version == 2:
        return _atomic_facts(data, messages)

    now = datetime.now().isoformat(timespec="seconds")
    new_facts = []
    fact_items = data.get("facts", [])
    if strict and ('facts' not in data or not isinstance(fact_items, list)):
        raise MemoryExtractionError('Extraction facts must be an explicit list')
    # JSON 根对象正确不代表 facts 一定是数组；不能让模型的异常结构打断会话收尾。
    if not isinstance(fact_items, list):
        fact_items = []
    for item in fact_items:
        if not isinstance(item, dict):
            if strict:
                raise MemoryExtractionError('Extraction fact must be an object')
            continue
        if strict and (not isinstance(item.get('content'), str) or not item['content'].strip()
                       or item.get('category') not in {'identity', 'preference', 'behavior', 'issue', 'other'}
                       or not isinstance(item.get('source_span'), str)
                       or 'confidence' not in item
                       or ('scope' in item and not isinstance(item['scope'], str))
                       or ('memory_key' in item and not isinstance(item['memory_key'], str))
                       or item.get('source_type') not in {'user_explicit', 'tool_verified', 'llm_inferred'}):
            raise MemoryExtractionError('Invalid extraction fact fields')
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
            user_quotes = [m.get('content', '') for m in messages if m.get('role') == 'user']
            if user_sources_only and source_type != 'user_explicit':
                continue
            if user_sources_only and (not source_span or not any(source_span in quote for quote in user_quotes)):
                raise MemoryExtractionError('Explicit fact lacks an exact user source')
            # 不能只相信模型声称“用户明确表达”：证据必须实际出现在会话中。
            if source_type == "user_explicit" and (not source_span or source_span not in transcript):
                source_type = "llm_inferred"
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                if strict:
                    raise MemoryExtractionError('Invalid confidence') from None
                confidence = 0.0
            if strict and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise MemoryExtractionError('Invalid confidence')
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


def _atomic_facts(data, messages):
    from app.memory.atomic import AtomicValidationError, normalize_candidate
    from app.memory.long_term import MemoryFact

    if set(data) != {'schema_version', 'facts'} or data['schema_version'] != 2 or not isinstance(data['facts'], list):
        raise MemoryExtractionError('Invalid atomic extraction root')
    if len(data['facts']) > 128:
        raise MemoryExtractionError('Too many atomic candidates')
    result = ExtractionBatch()
    for candidate_index, item in enumerate(data['facts']):
        try:
            normalized = normalize_candidate(item, messages, explain=True)
        except AtomicValidationError as error:
            predicate, group = candidate_group(item, messages)
            index = item.get('source_index') if isinstance(item, dict) else None
            from app.memory.atomic import PREDICATE_ALIASES
            result.issues.append(dict(candidate_index=candidate_index,
                source_index=index if type(index) is int and 0 <= index < len(messages) else None,
                operation=item.get('operation') if isinstance(item, dict) and item.get('operation') in ('ASSERT', 'RETRACT') else None,
                predicate=predicate if predicate in PREDICATE_ALIASES else None, group=group,
                reason=str(error), status='rejected' if str(error) in {
                    'non_stable_or_uncertain_source', 'undated_past_preference'} else 'deferred'))
            continue
        result.append(MemoryFact(content=normalized['content'], category='preference', created_at='',
            source_type='user_explicit', source_span=normalized['source_quote'], confidence=normalized['confidence'],
            scope='user_preference', memory_key=normalized['memory_key'], schema_version=2,
            predicate=normalized['predicate'], value=normalized['value'], qualifiers=normalized['qualifiers'],
            polarity=normalized['polarity'], operation=normalized['operation'],
            metadata={'source_index': normalized['source_index'], 'valid_time_quote': normalized['valid_time_quote']}))
    return isolate_related_updates(result)

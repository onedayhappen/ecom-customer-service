"""Validate exact, authenticated multi-message evidence without splicing quotes."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from app.memory import atomic as a
from app.memory.long_term import MemoryFact
from app.memory.v3.models import EvidenceRejected


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def family(predicate, qualifiers):
    # The unit describes the value, not a different user preference.
    return encoded([predicate, {k: v for k, v in qualifiers.items() if k != 'size_system'}])


def order(fact):
    sid, _, seq = fact.source_event_id.rpartition(':')
    return (fact.observed_at, sid, int(seq), fact.evidence[0].get('start_offset', 0) if fact.evidence else 0)


@dataclass
class Validated:
    change: object
    fact: MemoryFact
    family: str


def clause(text, quote):
    position = text.index(quote)
    left = max((text.rfind(c, 0, position) for c in '。！？!?；;\n，,'), default=-1) + 1
    end = position + len(quote)
    right = min((p for c in '。！？!?；;\n，,' if (p := text.find(c, end)) >= 0), default=len(text))
    return text[left:right]


def validate(change, messages):
    sources = {m['message_id']: m for m in messages}
    primary = sources.get(change.primary_message_id)
    if not primary or not primary['is_new'] or primary['role'] != 'user':
        raise EvidenceRejected('primary_source_must_be_new_user_message')
    bound = []
    for evidence in change.evidence:
        source = sources.get(evidence.message_id)
        if not source or source['role'] != 'user' or evidence.quote not in source['content']:
            raise EvidenceRejected('invalid_exact_user_evidence')
        # Later messages in this authenticated batch may clarify an earlier
        # value (e.g. its units). Never fetch unprocessed future events here.
        start = source['content'].find(evidence.quote)
        bound.append(dict(source_event_id=source['message_id'], source_session=source['session_id'],
            source_span=evidence.quote, observed_at=source['created_at'], start_offset=start,
            end_offset=start + len(evidence.quote), source_proof=source['proof']))
    primary_evidence = [e for e in bound if e['source_event_id'] == change.primary_message_id]
    if not primary_evidence:
        raise EvidenceRejected('missing_primary_quote')
    # Keep the primary first: it determines event ordering, never old context.
    bound = primary_evidence + [e for e in bound if e not in primary_evidence]
    quote = primary_evidence[0]['source_span']
    local = '\n'.join(clause(primary['content'], e['source_span']) for e in primary_evidence)
    context = '\n'.join(e['source_span'] for e in bound)
    primary_text = '\n'.join(e['source_span'] for e in primary_evidence)
    if any(marker in local for marker in (*a._TEMPORARY, *a._UNSAFE, '我爸', '我妈', '妹妹', '哥哥')):
        raise EvidenceRejected('temporary_uncertain_or_other_subject')
    predicate, scope = change.predicate, change.scope
    if predicate not in a.PREDICATE_ALIASES:
        raise EvidenceRejected('unsupported_predicate')
    qualifiers = {}
    if scope.product_category:
        if not a._categories(primary['content']):
            # Resolve a direct topic continuation, e.g. "衣服旧的喜好清一下"
            # followed by "旧的蓝色忘掉". Only the immediately preceding
            # user message in this session qualifies; never search a profile
            # for a convenient category or cross an ambiguous topic change.
            previous = [m for m in messages if m['session_id'] == primary['session_id']
                        and m['sequence'] < primary['sequence']]
            previous = max(previous, key=lambda m: m['sequence']) if previous else None
            if previous and a._categories(previous['content']) == [scope.product_category] and re.search(
                    r'喜好|偏好|尺码|预算|说下|还是说', previous['content']) and not any(
                    w in previous['content'] for w in (*a._TEMPORARY, *a._UNSAFE, *a._NEGATIVE, '不说', '不聊', '别说')):
                if not any(e['source_event_id'] == previous['message_id'] for e in bound):
                    bound.append(dict(source_event_id=previous['message_id'], source_session=previous['session_id'],
                        source_span=previous['content'], observed_at=previous['created_at'], start_offset=0,
                        end_offset=len(previous['content']), source_proof=previous['proof']))
                    context += '\n' + previous['content']
        # A model often quotes just "M码" from a message that explicitly says
        # "衣服...M码". Bind the unambiguous category word from that SAME
        # authenticated source, rather than guess from a prior profile.
        if scope.product_category in a.CATEGORY_ALIASES and not a._mentioned(scope.product_category, context, a.CATEGORY_ALIASES):
            for evidence in list(bound):
                source = sources[evidence['source_event_id']]
                if a._categories(source['content']) != [scope.product_category]:
                    continue
                anchor = next((word for word in a.CATEGORY_ALIASES[scope.product_category]
                               if word in source['content']), None)
                if anchor:
                    start = source['content'].index(anchor)
                    bound.append(dict(evidence, source_span=anchor, start_offset=start, end_offset=start+len(anchor)))
                    context += '\n' + anchor
                    break
        if scope.product_category not in a.CATEGORY_ALIASES or not a._mentioned(scope.product_category, context, a.CATEGORY_ALIASES):
            raise EvidenceRejected('category_needs_context_evidence')
        qualifiers['product_category'] = scope.product_category
    if predicate in {'prefers_color', 'wears_size', 'budget_range', 'prefers_fit', 'prefers_operating_system'} and not scope.product_category:
        raise EvidenceRejected('missing_category')
    if predicate in a.REQUIRED_CATEGORY and scope.product_category != a.REQUIRED_CATEGORY[predicate]:
        raise EvidenceRejected('wrong_category')
    if predicate in {'preferred_language', 'response_detail', 'contact_channel', 'prefers_category'} and scope.product_category:
        raise EvidenceRejected('unexpected_category')
    if scope.usage != 'general':
        words = {'daily': ('日常', '平时'), 'running': ('跑步', '跑鞋')}
        if scope.product_category != 'shoes' or not any(w in context for w in words[scope.usage]):
            raise EvidenceRejected('usage_needs_evidence')
        qualifiers['usage'] = scope.usage
    if predicate == 'wears_size':
        qualifiers['size_system'] = scope.size_system or 'unknown'
        if scope.size_system not in (None, 'unknown') and not a._mentioned(scope.size_system, context, a.SIZE_SYSTEM_ALIASES):
            raise EvidenceRejected('size_system_needs_evidence')
    elif scope.size_system:
        raise EvidenceRejected('unexpected_size_system')
    value = change.value
    if value is None and change.action not in {'FORGET', 'NEEDS_CLARIFICATION', 'NOOP'}:
        raise EvidenceRejected('missing_value')
    if value is not None:
        try:
            if predicate == 'budget_range':
                if not isinstance(value, dict) or set(value) != {'min', 'max', 'currency'}:
                    raise ValueError('invalid_budget')
                currency = value['currency']
                value = a._normal_value(predicate, dict(value, currency=currency or 'CNY'), qualifiers)
                value['currency'] = currency
                for amount in (value['min'], value['max']):
                    implicit_zero = amount == 0 and value['min'] == 0 and any(
                        w in primary_text for w in ('不超过', '以内', '以下', '上限'))
                    if amount is not None and not implicit_zero and amount not in a._amounts(primary_text):
                        raise ValueError('amount_needs_primary_evidence')
                if currency and not a._mentioned(currency, primary_text, a.CURRENCY_ALIASES):
                    raise ValueError('currency_needs_evidence')
                foreign = {unit for unit in ('USD', 'EUR', 'GBP') if a._mentioned(unit, primary_text, a.CURRENCY_ALIASES)}
                cny = bool(re.search(r'人民币|(?<![美欧港日澳加韩新])元|[￥¥]|\bcny\b|\brmb\b', primary_text, re.I))
                units = foreign | ({'CNY'} if cny else set())
                if currency and units != {currency}:
                    raise ValueError('ambiguous_currency')
                upper = any(w in primary_text for w in ('不超过', '以内', '以下', '上限'))
                lower = any(w in primary_text for w in ('不少于', '以上', '至少', '下限'))
                if upper and not lower and (value['max'] is None or value['min'] not in (None, 0)):
                    raise ValueError('budget_direction_mismatch')
                if lower and not upper and (value['min'] is None or value['max'] is not None):
                    raise ValueError('budget_direction_mismatch')
            else:
                value = a._normal_value(predicate, value, qualifiers)
        except (ValueError, TypeError, KeyError) as error:
            raise EvidenceRejected(str(error)) from None
        aliases = a.VALUE_ALIASES | {'prefers_brand': a.BRAND_ALIASES}
        if predicate == 'prefers_fit':
            aliases = dict(aliases, prefers_fit=dict(a.FIT_ALIASES, loose=(*a.FIT_ALIASES['loose'], '松一点')))
        if predicate in aliases and not a._mentioned(value, primary_text, aliases[predicate]):
            raise EvidenceRejected('value_needs_primary_evidence')
        if predicate == 'wears_size' and not a._contains(primary_text, value):
            raise EvidenceRejected('size_needs_primary_evidence')
    if change.action == 'FORGET':
        if not re.search(r'忘掉|忘了|忘记|删除|别再|不再|不要再|撤销|forget|remove', local, re.I):
            raise EvidenceRejected('forget_needs_explicit_request')
    elif change.action not in {'NOOP', 'NEEDS_CLARIFICATION'}:
        negative = any(w in local for w in a._NEGATIVE)
        if negative != (change.polarity == 'negative'):
            raise EvidenceRejected('polarity_mismatch')
        if re.search(r'忘掉|删除|撤销|forget', local, re.I):
            raise EvidenceRejected('forget_is_not_assertion')
        if re.search(r'以前|曾经|原来|过去', local) and not re.search(r'现在|以后|如今|今后', local) and not change.valid_time_quote:
            raise EvidenceRejected('past_is_not_current')
    explicit_restore = bool(re.search(r'重新记|再记|又喜欢|又改|恢复|remember again', local, re.I))
    if change.restore and not explicit_restore:
        raise EvidenceRejected('restore_needs_explicit_request')
    if change.valid_time_quote and change.valid_time_quote not in primary['content']:
        raise EvidenceRejected('invalid_time_evidence')
    temporal = a.ground_time(change.valid_time_quote, primary['created_at']) if change.valid_time_quote else {}
    if change.valid_time_quote and not temporal:
        raise EvidenceRejected('ambiguous_effective_time')
    operation = 'RETRACT' if change.action == 'FORGET' else 'ASSERT'
    content = a.render_content(predicate, value, qualifiers, change.polarity, operation)
    if predicate == 'budget_range' and value and value['currency'] is None:
        content = content.replace(' None', '（币种未确认）')
    if scope.usage != 'general':
        content += '（' + {'daily': '日常', 'running': '跑步'}[scope.usage] + '）'
    if predicate == 'wears_size' and qualifiers['size_system'] == 'unknown':
        content += '；尺码体系未确认，不能据此选定商品规格'
    fact = MemoryFact(content=content, category='preference', created_at=primary['created_at'],
        updated_at=primary['created_at'], observed_at=max(e['observed_at'] for e in bound), source_type='user_explicit',
        source_event_id=change.primary_message_id, source_session=primary['session_id'], source_span=quote,
        confidence=1, schema_version=3, predicate=predicate, value=value, qualifiers=qualifiers,
        polarity=change.polarity, operation=operation, evidence=bound,
        valid_from=temporal.get('valid_from', ''), valid_to=temporal.get('valid_to', ''),
        metadata=dict(source_proof=primary['proof'], restore=explicit_restore, action=change.action))
    fact.memory_key = a.canonical_memory_key(predicate, {k: v for k,v in qualifiers.items() if k != 'size_system'})
    return Validated(change, fact, family(predicate, qualifiers))


def sources_valid(policy, fact, tx):
    if not policy.allows(fact, tx=tx):
        return False
    for evidence in fact.evidence:
        sid, _, sequence = evidence.get('source_event_id', '').rpartition(':')
        if not sequence.isdecimal():
            return False
        source = policy.source(tx, sid, int(sequence))
        if not source or evidence['source_span'] not in source['content']:
            return False
        proof = evidence.get('source_proof')
        if proof and (proof['digest'] != source['proof']['digest'] or proof['user_id'] != policy.user):
            return False
    return True

"""Strict atomic user preferences, deterministic normalization and source checks."""
from __future__ import annotations

import calendar
from functools import lru_cache
from datetime import datetime, timedelta, timezone
import json
import math
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class AtomicValidationError(ValueError):
    pass


class AtomicEvidenceError(AtomicValidationError):
    """A well-shaped candidate needs evidence or represents a non-stable fact."""


PREDICATE_ALIASES = {
    'preferred_language': ('语言', '中文', '英文', '英语', '日语', 'language'),
    'response_detail': ('回复', '详细', '简洁', '简短', 'detail'),
    'contact_channel': ('联系', '短信', '电话', '邮件', 'contact'),
    'prefers_category': ('品类', '商品类型', 'category'),
    'prefers_brand': ('品牌', 'brand'),
    'prefers_color': ('颜色', '色系', 'color', 'colour'),
    'wears_size': ('尺码', '鞋码', '码数', 'size'),
    'budget_range': ('预算', '价格', '价位', 'budget'),
    'prefers_operating_system': ('系统', '安卓', 'ios', 'android', 'operating system'),
    'prefers_fit': ('版型', '宽松', '修身', '合身', 'fit'),
}
CATEGORY_ALIASES = {
    'headphones': ('耳机', '耳麦', 'headphone', 'earphone', 'headphones', 'earbuds'),
    'shoes': ('鞋', '鞋子', '运动鞋', '跑鞋', 'shoes', 'sneakers'),
    'clothing': ('衣服', '服装', '衬衫', '外套', '短袖', 'T恤', 't-shirt', 'clothing', 'apparel'),
    'phone': ('手机', 'phone', 'smartphone'),
    'computer': ('电脑', '笔记本', 'computer', 'laptop'),
    'cosmetics': ('美妆', '化妆品', 'cosmetics'),
    'food': ('食品', '零食', 'food'),
    'appliance': ('家电', '电器', 'appliance'),
}
COLOR_ALIASES = {
    'black': ('黑', '黑色', '纯黑', '漆黑', 'black'), 'white': ('白', '白色', '纯白', 'white'),
    'red': ('红', '红色', 'red'), 'blue': ('蓝', '蓝色', 'blue'),
    'green': ('绿', '绿色', 'green'), 'yellow': ('黄', '黄色', 'yellow'),
    'gray': ('灰', '灰色', 'gray', 'grey'), 'pink': ('粉', '粉色', 'pink'),
    'purple': ('紫', '紫色', 'purple'), 'brown': ('棕', '棕色', 'brown'),
    'orange': ('橙', '橙色', 'orange'),
}
LANGUAGE_ALIASES = {'zh-CN': ('中文', '简体中文', '普通话', 'zh-cn', 'chinese'),
                    'zh-TW': ('繁体中文', '繁體中文', 'zh-tw'),
                    'en': ('英语', '英文', 'en', 'english'), 'ja': ('日语', '日文', 'ja', 'japanese'),
                    'ko': ('韩语', '韩文', 'ko', 'korean')}
CHANNEL_ALIASES = {'sms': ('短信', 'sms'), 'email': ('邮件', '邮箱', '电子邮件', 'email', 'e-mail'),
                   'phone': ('电话', '致电', 'phone'), 'chat': ('在线聊天', '站内消息', '聊天', 'chat')}
SIZE_SYSTEM_ALIASES = {'EU': ('欧码', '欧标', 'eu', '欧洲码'), 'US': ('美码', '美标', 'us'),
                       'UK': ('英码', '英国码', 'uk'), 'CN': ('中国码', '国标', 'cn', '毫米', 'mm'),
                       'INT': ('国际码', 'int', 'xs', 'xxs', 's', 'm', 'l', 'xl', 'xxl', 'xxxl')}
DETAIL_ALIASES = {'concise': ('简洁', '简短', '精简', '言简意赅', 'concise', 'brief'),
                  'detailed': ('详细', '详尽', '具体解释', 'detailed')}
BRAND_ALIASES = {'nike': ('耐克', 'nike'), 'adidas': ('阿迪达斯', '阿迪', 'adidas'),
                 'apple': ('苹果', 'apple'), 'sony': ('索尼', 'sony'), 'huawei': ('华为', 'huawei'),
                 'xiaomi': ('小米', 'xiaomi'), 'samsung': ('三星', 'samsung')}
CURRENCY_ALIASES = {'CNY': ('cny', 'rmb', '人民币', '元', '￥'),
                    'USD': ('usd', '美元', '美金', '$'), 'EUR': ('eur', '欧元', '€'),
                    'GBP': ('gbp', '英镑', '£')}
OS_ALIASES = {'android': ('安卓', 'android'), 'ios': ('iOS', '苹果系统')}
FIT_ALIASES = {'loose': ('宽松', 'loose'), 'slim': ('修身', 'slim'), 'regular': ('合身', 'regular')}
VALUE_ALIASES = {
    'preferred_language': LANGUAGE_ALIASES, 'response_detail': DETAIL_ALIASES,
    'contact_channel': CHANNEL_ALIASES, 'prefers_category': CATEGORY_ALIASES,
    'prefers_color': COLOR_ALIASES, 'prefers_operating_system': OS_ALIASES,
    'prefers_fit': FIT_ALIASES,
}
REQUIRED_CATEGORY = {'prefers_operating_system': 'phone', 'prefers_fit': 'clothing'}

_TEMPORARY = ('这次', '这单', '本次', '这一次', '这回', '临时', '仅一次', '一次性', '仅限本单')
_UNSAFE = ('给朋友', '替朋友', '帮朋友', '送朋友', '送给', '代购', '朋友喜欢', '妈妈喜欢', '爸爸喜欢',
           '同事喜欢', '他说', '她说', '朋友说', '据说', '假如', '假设', '如果', '要是', '也许', '可能',
           '不一定', '不是我', '不代表我', '并非我的', '转述')
_RETRACT = ('不再按', '不要再按', '别再按', '撤销', '删除', '忘掉', '取消', '不再使用', '不再喜欢',
            '不喜欢了', '不再偏好', 'forget', 'retract')
_NEGATIVE = ('不喜欢', '不偏好', '不要', '不想要', '讨厌', '排除', '避免', '不穿', '不接受', '不爱')


def _fold(value):
    return unicodedata.normalize('NFKC', str(value)).strip().casefold()


def _contains(text, term):
    text, term = _fold(text), _fold(term)
    if re.fullmatch(r'[a-z0-9_-]+', term):
        return bool(_term_pattern(term).search(text))
    return term in text


def _canonical(value, aliases):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise AtomicValidationError('Invalid atomic scalar')
    result = _alias_index(tuple((key, tuple(forms)) for key, forms in aliases.items())).get(_fold(value))
    if result is None:
        raise AtomicValidationError('Unsupported normalized value')
    return result


@lru_cache(maxsize=32)
def _alias_index(entries):
    return {_fold(form): key for key, forms in entries for form in (key, *forms)}


@lru_cache(maxsize=256)
def _term_pattern(term):
    return re.compile(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])')


def _mentioned(canonical, text, aliases):
    return bool(_mention_pattern((canonical, *aliases.get(canonical, ())), aliases is COLOR_ALIASES).search(_fold(text)))


@lru_cache(maxsize=256)
def _mention_pattern(aliases, color):
    # Cache vocabulary only, never user text. Fold each source once per match.
    patterns = []
    for alias in aliases:
        alias = _fold(alias)
        escaped = re.escape(alias)
        if color and len(alias) == 1 and '\u3400' <= alias <= '\u9fff':
            escaped += r'(?=色|的|款|耳机|耳麦|鞋|衣|手机|电脑|[\s，。！？,!?、]|$)'
        elif re.fullmatch(r'[a-z0-9_-]+', alias):
            escaped = r'(?<![a-z0-9])' + escaped + r'(?![a-z0-9])'
        patterns.append(escaped)
    return re.compile('|'.join(patterns))


def canonical_memory_key(predicate, qualifiers):
    """Values/polarities are managed by the server's explicit slot semantics."""
    if predicate not in PREDICATE_ALIASES:
        raise AtomicValidationError('Unsupported predicate')
    suffix = ':'.join(f'{key}={qualifiers[key]}' for key in sorted(qualifiers))
    return f'preference:{predicate}' + (':' + suffix if suffix else '')


def query_slots(query):
    predicates = {key for key, words in PREDICATE_ALIASES.items() if any(_contains(query, word) for word in words)}
    if any(_mentioned(color, query, COLOR_ALIASES) for color in COLOR_ALIASES):
        predicates.add('prefers_color')
    if any(_mentioned(brand, query, BRAND_ALIASES) for brand in BRAND_ALIASES):
        predicates.add('prefers_brand')
    categories = [key for key in CATEGORY_ALIASES if _mentioned(key, query, CATEGORY_ALIASES)]
    return predicates, ({'product_category': categories[0]} if len(categories) == 1 else {})


def _normal_value(predicate, value, qualifiers):
    maps = VALUE_ALIASES
    if predicate in maps:
        return _canonical(value, maps[predicate])
    if predicate == 'prefers_brand':
        if not isinstance(value, str) or not value.strip() or len(value) > 60:
            raise AtomicValidationError('Invalid brand')
        try:
            return _canonical(value, BRAND_ALIASES)
        except AtomicValidationError:
            if not re.fullmatch(r'[\w .&+-]{1,60}', value, re.UNICODE):
                raise AtomicValidationError('Invalid brand') from None
            return _fold(value)
    if predicate == 'wears_size':
        if not isinstance(value, str) or len(value) > 12:
            raise AtomicValidationError('Size must be an explicit string')
        result = unicodedata.normalize('NFKC', value).strip().upper()
        if qualifiers.get('size_system') == 'INT':
            if result not in {'XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL', '2XL', '3XL', '4XL'}:
                raise AtomicValidationError('Invalid international size')
        elif not re.fullmatch(r'\d{1,3}(?:\.5)?', result):
            raise AtomicValidationError('Invalid numeric size')
        return result
    if predicate == 'budget_range':
        if not isinstance(value, dict) or set(value) != {'min', 'max', 'currency'}:
            raise AtomicValidationError('Budget must have min, max and currency')
        for key in ('min', 'max'):
            number = value[key]
            if number is not None and (type(number) not in {int, float} or not math.isfinite(number) or number < 0 or number > 100000000):
                raise AtomicValidationError('Invalid budget bound')
        if value['min'] is None and value['max'] is None or (
                value['min'] is not None and value['max'] is not None and value['min'] > value['max']):
            raise AtomicValidationError('Invalid budget interval')
        return dict(min=value['min'], max=value['max'], currency=_canonical(value['currency'], CURRENCY_ALIASES))
    raise AtomicValidationError('Unsupported predicate')


def render_content(predicate, value, qualifiers, polarity='positive', operation='ASSERT'):
    category = qualifiers.get('product_category', '')
    category_label = CATEGORY_ALIASES.get(category, (category,))[0] if category else ''
    label = {'preferred_language': '沟通语言', 'response_detail': '回复详细程度', 'contact_channel': '联系渠道',
             'prefers_category': '商品品类', 'prefers_brand': '品牌', 'prefers_color': '颜色',
             'wears_size': '尺码', 'budget_range': '预算',
             'prefers_operating_system': '操作系统', 'prefers_fit': '版型'}[predicate]
    maps = {'preferred_language': LANGUAGE_ALIASES, 'response_detail': DETAIL_ALIASES, 'contact_channel': CHANNEL_ALIASES,
            'prefers_category': CATEGORY_ALIASES, 'prefers_color': COLOR_ALIASES, 'prefers_brand': BRAND_ALIASES,
            'prefers_operating_system': OS_ALIASES, 'prefers_fit': FIT_ALIASES}
    display = maps.get(predicate, {}).get(value, (value,))[0] if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if predicate == 'budget_range' and value is not None:
        display = f"{value['min'] if value['min'] is not None else '不限下限'}–{value['max'] if value['max'] is not None else '不限上限'} {value['currency']}"
    if predicate == 'wears_size':
        display = f"{qualifiers['size_system']} {display}"
    prefix = '撤销' if operation == 'RETRACT' else ('排除' if polarity == 'negative' else '偏好')
    return f"用户{prefix}{category_label}{label}" + (f'：{display}' if value is not None else '（整个槽位）')


def _categories(text):
    return [category for category in CATEGORY_ALIASES if _mentioned(category, text, CATEGORY_ALIASES)]


def _scope_quote(text, quote):
    """Bind a short value to its nearest local category, never another message.

    A sentence/subject boundary stops inheritance. Multiple categories in the
    same clause remain ambiguous; an explicit category in the quote wins.
    The returned span is contiguous original text, so runtime can bind offsets.
    """
    if _categories(quote) or text.count(quote) != 1:
        return quote
    start = text.index(quote)
    end = start + len(quote)
    left = max((text.rfind(mark, 0, start) for mark in '。！？!?；;\n'), default=-1) + 1
    right = min((position for mark in '。！？!?；;\n' if (position := text.find(mark, end)) >= 0), default=len(text))
    # Include the clause containing the quote, then at most the immediately
    # preceding clauses of this sentence up to the nearest category.
    clause_left = max((text.rfind(mark, left, start) for mark in '，,'), default=-1) + 1
    clause_left = max(left, clause_left)
    clause_right = min((position for mark in '，,' if (position := text.find(mark, end, right)) >= 0), default=right)
    while clause_left >= left:
        span = text[clause_left:clause_right]
        if len(span) > 4000 or re.search(r'我爸|我妈|爸爸|妈妈|朋友|他|她|但|不过|而是|另外', span):
            return quote
        categories = _categories(span)
        if categories:
            return span if len(categories) == 1 else quote
        if clause_left == left:
            break
        clause_left = max(left, max((text.rfind(mark, left, clause_left - 1) for mark in '，,'), default=-1) + 1)
    return quote


def _amounts(quote):
    amounts = [float(m[1]) * {'': 1, '千': 1000, 'k': 1000, '万': 10000, 'w': 10000}[m[2].lower()]
               for m in re.finditer(r'(\d+(?:\.\d+)?)\s*([千kK万wW]?)', quote)]
    digits = dict(zip('零一二三四五六七八九', range(10))) | {'两': 2, '〇': 0}
    units = {'十': 10, '百': 100, '千': 1000, '万': 10000}
    # Do not read the suffix of "3千" again as the independent amount 1000.
    chinese = re.sub(r'\d+(?:\.\d+)?\s*[千kK万wW]?', ' ', quote)
    for match in re.finditer(r'[零〇一二两三四五六七八九十百千万]+', chinese):
        token = match[0]
        if len(token) > 16:
            continue
        # A numeral embedded in "一直"/"一般" is not monetary evidence.
        if not re.match(r'\s*(?:元|美元|美金|欧元|英镑|人民币|以内|以下|以上|到|至|[-~～，。,、]|$)',
                        chinese[match.end():]) and not re.search(
                            r'(?:人民币|美元|美金|欧元|英镑|[￥¥$€£])\s*$', chinese[:match.start()]):
            continue
        if not any(c in units for c in token):
            amounts.append(float(''.join(str(digits[c]) for c in token)))
            continue
        total = section = number = 0
        last_unit, valid = 100000, True
        for position, char in enumerate(token):
            if char in digits:
                if position and token[position-1] in digits and digits[token[position-1]] != 0:
                    valid = False
                    break
                number = digits[char]
            elif char == '万':
                if total or not (section + number):
                    valid = False
                    break
                total += (section + number or 1) * 10000
                section = number = 0
                last_unit = 10000
            else:
                if units[char] >= last_unit or not number and not (position == 0 and char == '十'):
                    valid = False
                    break
                section += (number or 1) * units[char]
                last_unit = units[char]
                number = 0
        # "一百五"/"三千五" is colloquial and ambiguous; do not guess 105/150.
        if number and last_unit > 10 and len(token) > 1 and token[-2] in units:
            valid = False
        if valid:
            amounts.append(total + section + number)
    return amounts


def normalize_candidate(item, source_messages, *, explain=False):
    def reject(reason):
        if explain:
            raise AtomicEvidenceError(reason)
        return None

    required = {'predicate', 'value', 'qualifiers', 'polarity', 'operation', 'source_index', 'source_quote', 'confidence'}
    if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {'valid_time_quote'}:
        raise AtomicValidationError('Atomic candidate has incorrect fields')
    predicate, polarity, operation = item['predicate'], item['polarity'], item['operation']
    if not isinstance(predicate, str) or predicate not in PREDICATE_ALIASES:
        raise AtomicValidationError('Unsupported predicate')
    if polarity not in ('positive', 'negative') or operation not in ('ASSERT', 'RETRACT'):
        raise AtomicValidationError('Invalid polarity or operation')
    index, quote, confidence = item['source_index'], item['source_quote'], item['confidence']
    if type(index) is not int or not 0 <= index < len(source_messages):
        raise AtomicValidationError('Invalid source index')
    source = source_messages[index]
    if source.get('role') != 'user' or not isinstance(quote, str) or not quote.strip() or len(quote) > 4000:
        raise AtomicValidationError('Invalid user quote')
    if quote not in source['content']:
        raise AtomicValidationError('Quote does not match the indexed source')
    if type(confidence) not in {float, int} or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise AtomicValidationError('Invalid confidence')
    full_text = source.get('_full_content', source['content'])
    if predicate in {'prefers_color', 'wears_size', 'budget_range', *REQUIRED_CATEGORY}:
        quote = _scope_quote(source['content'], quote)
    if any(marker in full_text for marker in (*_TEMPORARY, *_UNSAFE, '都喜欢', '都偏好', '都可以')) or confidence < .6:
        return reject('non_stable_or_uncertain_source')
    if re.search(r'\b(this time|for this order|for (?:my |a )?friend|hypothetically|if i|my (?:friend|wife|husband) (?:likes|prefers))\b', _fold(full_text)):
        return reject('non_stable_or_uncertain_source')
    qualifiers = item['qualifiers']
    if not isinstance(qualifiers, dict) or set(qualifiers) - {'product_category', 'size_system'}:
        raise AtomicValidationError('Invalid qualifiers')
    normalized = {}
    if predicate == 'budget_range':
        categories = _categories(quote)
        if len(categories) == 1 and 'product_category' not in qualifiers:
            qualifiers = dict(qualifiers, product_category=categories[0])
        elif not categories and 'product_category' not in qualifiers and not re.search(
                r'所有(?:商品|东西|品类)|不分品类|全局预算|overall budget|all products', quote, re.I):
            return reject('budget_scope_unknown')
        elif len(categories) > 1:
            return reject('ambiguous_product_category')
    if 'product_category' in qualifiers:
        normalized['product_category'] = _canonical(qualifiers['product_category'], CATEGORY_ALIASES)
        if not _mentioned(normalized['product_category'], quote, CATEGORY_ALIASES):
            return reject('missing_product_category')
        if sum(_mentioned(category, quote, CATEGORY_ALIASES) for category in CATEGORY_ALIASES) != 1:
            return reject('ambiguous_product_category')
    if 'size_system' in qualifiers:
        normalized['size_system'] = _canonical(qualifiers['size_system'], SIZE_SYSTEM_ALIASES)
        if not _mentioned(normalized['size_system'], quote, SIZE_SYSTEM_ALIASES):
            return reject('missing_size_system')
        if sum(_mentioned(system, quote, SIZE_SYSTEM_ALIASES) for system in SIZE_SYSTEM_ALIASES) != 1:
            return reject('ambiguous_size_system')
    if predicate in {'preferred_language', 'response_detail', 'contact_channel', 'prefers_category'} and normalized:
        raise AtomicValidationError('This predicate does not accept qualifiers')
    if predicate != 'wears_size' and 'size_system' in normalized:
        raise AtomicValidationError('Size system requires wears_size')
    if predicate in {'prefers_color', 'wears_size'} and 'product_category' not in normalized:
        return reject('missing_product_category')
    if predicate in REQUIRED_CATEGORY and normalized.get('product_category') != REQUIRED_CATEGORY[predicate]:
        return reject('missing_product_category')
    if predicate in REQUIRED_CATEGORY and re.search(r'我爸|我妈|父亲|母亲|爸爸|妈妈|朋友|同事|他|她', quote):
        return reject('non_stable_or_uncertain_source')
    if predicate == 'wears_size' and 'size_system' not in normalized:
        return reject('missing_size_system')
    if operation == 'RETRACT' and not any(_contains(full_text, word) for word in _RETRACT):
        return reject('missing_retraction_evidence')
    if item['value'] is None:
        if operation != 'RETRACT':
            raise AtomicValidationError('Only RETRACT can omit a value')
        value = None
    else:
        value = _normal_value(predicate, item['value'], normalized)
    value_maps = VALUE_ALIASES | {'prefers_brand': BRAND_ALIASES}
    if value is not None:
        if predicate in value_maps and not _mentioned(value, quote, value_maps[predicate]):
            return reject('missing_value_evidence')
        if predicate == 'wears_size' and not _contains(quote, value):
            return reject('missing_value_evidence')
        if predicate == 'budget_range':
            # '元' inside '美元/欧元' is not CNY evidence. Reject mixed or
            # unsupported currencies instead of silently changing the unit.
            foreign = {currency for currency in ('USD', 'EUR', 'GBP')
                       if _mentioned(currency, quote, CURRENCY_ALIASES)}
            cny = bool(re.search(r'人民币|(?<![美欧港日澳加韩新])元|[￥¥]|\bcny\b|\brmb\b', _fold(quote)))
            currencies = foreign | ({'CNY'} if cny else set())
            if currencies != {value['currency']} or re.search(r'港币|台币|日元|韩元|澳元|加元|新元', quote):
                return reject('missing_or_ambiguous_currency')
            upper = any(word in quote for word in ('不超过', '以内', '以下', '至多', '最高', '上限'))
            lower = any(word in quote for word in ('不少于', '至少', '以上', '最低', '下限', '起步'))
            if upper and not lower and (value['max'] is None or value['min'] not in (None, 0)):
                return reject('budget_direction_mismatch')
            if lower and not upper and (value['min'] is None or value['max'] is not None):
                return reject('budget_direction_mismatch')
            amounts = _amounts(quote)
            for amount in (value['min'], value['max']):
                if amount is not None and amount not in amounts:
                    return reject('missing_amount_evidence')
    if operation == 'ASSERT':
        if any(word in quote for word in ('都喜欢', '都偏好', '都可以', '或者', '均可', '皆可')):
            return reject('ambiguous_value')
        if predicate == 'prefers_color' and sum(_mentioned(color, quote, COLOR_ALIASES) for color in COLOR_ALIASES) > 1:
            return reject('ambiguous_value')
        if predicate == 'prefers_category' and sum(_mentioned(category, quote, CATEGORY_ALIASES) for category in CATEGORY_ALIASES) > 1:
            return reject('ambiguous_value')
        if predicate == 'prefers_brand' and sum(_mentioned(brand, quote, BRAND_ALIASES) for brand in BRAND_ALIASES) > 1:
            return reject('ambiguous_value')
        if predicate in REQUIRED_CATEGORY and sum(
                _mentioned(value, quote, VALUE_ALIASES[predicate]) for value in VALUE_ALIASES[predicate]) > 1:
            return reject('ambiguous_value')
        negative = any(word in quote for word in _NEGATIVE)
        if negative != (polarity == 'negative'):
            return reject('polarity_mismatch')
        if any(word in quote for word in _RETRACT):
            return reject('operation_mismatch')
        # Context needed to interpret 'black' alone must not disappear when the
        # model chooses a narrow quote from a negated original sentence.
        if len(quote) < 8 and any(word in full_text for word in _NEGATIVE):
            return reject('negated_source')
    time_quote = item.get('valid_time_quote', '')
    if not isinstance(time_quote, str) or len(time_quote) > 120 or time_quote and time_quote not in full_text:
        raise AtomicValidationError('Temporal quote has no exact source')
    if operation == 'ASSERT':
        # An undated past preference must not become a current preference just
        # because an old conversation is processed today. Include nearby source
        # context so a narrow model quote cannot erase '以前'.
        position = full_text.find(quote)
        context = full_text[max(0, position-32):position+len(quote)]
        past = max((context.rfind(word) for word in ('以前', '之前', '曾经', '过去', '原来')), default=-1)
        current = max((context.rfind(word) for word in ('现在', '如今', '以后', '今后')), default=-1)
        uncertain_past = any(word in time_quote for word in ('以前', '之前', '曾经', '过去', '原来')) or past > current
        explicit_date = re.search(r'20\d{2}|去年|昨天|上个月', quote + time_quote)
        if uncertain_past and not explicit_date:
            return reject('undated_past_preference')
    return dict(predicate=predicate, value=value, qualifiers=normalized, polarity=polarity, operation=operation,
                source_index=index, source_quote=quote, confidence=float(confidence), valid_time_quote=time_quote,
                memory_key=canonical_memory_key(predicate, normalized),
                content=render_content(predicate, value, normalized, polarity, operation))


def ground_time(quote, source_created_at):
    """Parse explicit calendar bounds relative to the original source event."""
    from app.config.settings import settings

    observed = datetime.fromisoformat(source_created_at)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    zone_name = getattr(settings, 'memory_timezone', 'Asia/Shanghai')
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        if zone_name != 'Asia/Shanghai':
            raise
        zone = timezone(timedelta(hours=8))
    local = observed.astimezone(zone)
    start = end = None
    precision = ''
    if '去年' in quote:
        start, end, precision = local.replace(year=local.year-1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0), local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), 'year'
    elif '昨天' in quote:
        end = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start, precision = end-timedelta(days=1), 'day'
    elif '上个月' in quote:
        end = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start, precision = (end-timedelta(days=1)).replace(day=1), 'month'
    else:
        match = re.search(r'(20\d{2})[年/-](\d{1,2})(?:[月/-](\d{1,2})日?)?', quote)
        year = re.search(r'(20\d{2})年', quote)
        try:
            if match:
                y, m, d = int(match[1]), int(match[2]), int(match[3]) if match[3] else 1
                start = datetime(y, m, d, tzinfo=zone)
                precision = 'day' if match[3] else 'month'
                end = start + timedelta(days=1 if match[3] else calendar.monthrange(y, m)[1])
            elif year:
                start, end, precision = datetime(int(year[1]), 1, 1, tzinfo=zone), datetime(int(year[1])+1, 1, 1, tzinfo=zone), 'year'
            elif any(word in quote for word in ('现在', '从现在', '以后', '今后')):
                start, precision = observed, 'instant'
        except ValueError:
            return {}
    if start is None:
        return {}
    if re.search(r'(从|自|自从).*(起|开始|以来)', quote):
        end = None
    return {'valid_from': start.astimezone(timezone.utc).isoformat(),
            'valid_to': end.astimezone(timezone.utc).isoformat() if end else '',
            'metadata': {'temporal_expression': quote, 'valid_time_precision': precision}}

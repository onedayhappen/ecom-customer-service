# 文件说明：核验商品查询结论与筛选范围，防止把空结果泛化为商品不存在。
"""Query-scoped product observations for the local final-reply guard.

A successful empty search proves only a lack of matches for that query. It
never authorizes a catalogue-wide absence or a product's sale status. Queries
are not product aliases: two searches in a task remain separate observations.
"""
import re

from app.tasks.reply_facts import label


ABSENCE = re.compile(
    r'不存在|(?:不|没|没有)(?:再)?(?:卖|售卖|销售|出售|售)|'
    r'(?:没有|没|无)(?:这[个款种类件口]|该|此)(?:商品|产品|型号|手机|锅)?|'
    r'(?:本店|店里|店内|商城|平台|我们这边|我们这里)(?:目前|现在|确实|根本)?没有|'
    r'(?:已经|已|确实|肯定)?(?:下架|停售)')
NO_MATCH = re.compile(r'(?:没有|没|未)(?:有)?(?:搜到|找到|查到|查询到|搜索到|检索到)|'
                      r'(?:没有|无|未有)(?:任何)?匹配(?:的)?(?:商品|产品|结果|记录)')
QUALIFIER = re.compile(r'无法(?:确认|确定|判断)|不能|不(?:代表|等于|意味着|一定)|未必|是否|会不会|有没有')
OTHER_SUBJECT = re.compile(r'订单|物流|包裹|退款|发票|积分|会员价|重量|材质')
PRODUCT_SUBJECT = re.compile(r'商品|产品|型号|这款|这种|这口|店里|本店')
ATTRIBUTE = re.compile(r'^(?:的)?(?:重量|材质|会员价|价格|尺寸|参数|规格|说明|资料|记录|信息|问题|优惠|折扣)')


def observation(receipt):
    """Retain an empty result without creating a fictitious product object."""
    if receipt.get('tool') != 'search_products':
        return None
    args, result = receipt.get('arguments') or {}, receipt.get('result') or {}
    query = args.get('query')
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        return None
    data = result.get('data')
    data = data if isinstance(data, dict) else {}
    rows = data.get('items')
    products = [dict(product_id=row['product_id'], name=row['name']) for row in rows[:50]
                if isinstance(row, dict) and isinstance(row.get('product_id'), str)
                and isinstance(row.get('name'), str) and row['name']] if isinstance(rows, list) else []
    total = data.get('total')
    total = total if type(total) is int and total >= 0 else None
    succeeded = result.get('success') is True
    outcome = ('matched' if succeeded and products else 'no_match'
               if succeeded and total == 0 and rows == [] and not data.get('has_more')
               and not result.get('next_cursor') and not args.get('cursor') else 'unknown')
    # Only retain typed fields needed by this guard, never arbitrary descriptions.
    unapplied = data.get('unapplied_conditions') or []
    if isinstance(rows, list):
        unapplied = unapplied or any(row.get('unapplied_conditions') for row in rows[:50] if isinstance(row, dict))
    return dict(kind='product_search', evidence_id=receipt.get('event_id'), query=query,
                max_price=args.get('max_price'), specs=args.get('specs') or [],
                cursor=bool(args.get('cursor')), outcome=outcome, total=total, products=products,
                completeness=result.get('completeness'), data_as_of=result.get('data_as_of'),
                observed_at=result.get('observed_at'), unapplied_conditions=bool(unapplied),
                has_more=bool(data.get('has_more') or result.get('next_cursor')))


def _qualified(clause, match):
    # A disclaimer before "but/therefore" cannot qualify the assertion after it.
    prefix = re.split(r'但|却|所以|因此|故而', clause[:match.start()])[-1]
    return bool(QUALIFIER.search(prefix) or re.match(r'(?:了)?(?:吗|么|与否|？|\?)', clause[match.end():]))


def _scope_text(item):
    text = '按关键词「' + label(item['query'], 200) + '」'
    cap = item['max_price']
    if isinstance(cap, dict):
        text += '、价格上限' + label(cap.get('amount'), 64) + ' ' + label(cap.get('currency'), 8)
    specs = item['specs']
    if isinstance(specs, list):
        values = [label(s.get('name'), 50) + '=' + label(s.get('value'), 50)
                  for s in specs[:5] if isinstance(s, dict)]
        if values:
            text += '、规格条件「' + '；'.join(values) + '」'
    return text + ('查询的这一页' if item['cursor'] else '查询')


class SearchGuard:
    def __init__(self, evidence):
        self.groups = [[item for item in group if item.get('kind') == 'product_search'] for group in evidence]
        self.groups = [group for group in self.groups if group]
        self.by_name = {}
        for index, group in enumerate(self.groups):
            for item in group:
                for name in [item['query'], *[p['name'] for p in item['products']]]:
                    self.by_name.setdefault(name.casefold(), set()).add(index)
        self.names = re.compile('|'.join(re.escape(k) for k in sorted(self.by_name, key=len, reverse=True)),
                                re.IGNORECASE) if self.by_name else None
        self.named_absence = re.compile(r'(?:没有|没|无)(?:' + self.names.pattern + r')', re.I) if self.names else None

    def check(self, clause):
        if not self.groups:
            return [], []
        named = {i for m in self.names.finditer(clause) for i in self.by_name[m[0].casefold()]}
        # Avoid rewriting an order lookup or a missing product attribute as a
        # product-catalogue lookup, even in a reply containing several matters.
        if OTHER_SUBJECT.search(clause) and not PRODUCT_SUBJECT.search(clause):
            return [], []
        selected = [self.groups[i] for i in sorted(named)] if named else self.groups if len(self.groups) == 1 else []
        searches = [item for group in selected or self.groups for item in group]
        issues = []
        if any(not _qualified(clause, m) and not ATTRIBUTE.match(clause[m.end():])
               for pattern in (ABSENCE, self.named_absence) for m in pattern.finditer(clause)):
            issues.append('product_absence')
        if any(not _qualified(clause, m) for m in NO_MATCH.finditer(clause)):
            # An explicitly quoted/query-marked keyword can refer to its own
            # earlier zero result even if another wording subsequently matched.
            explicit = [s for s in searches if re.search(
                r'(?:按(?:关键词)?|搜(?:索)?|查(?:询)?|关键词)[「“\"\s]*' + re.escape(s['query']), clause, re.I)]
            scoped = explicit or searches
            if not scoped or any(s['outcome'] != 'no_match' for s in scoped):
                issues.append('product_search_result')
        return issues, selected

    @staticmethod
    def correction(groups):
        searches = [item for group in groups for item in group]
        # Show the zero lookup and a successful lookup separately; never infer
        # an alias relation or borrow products from another task.
        empty = next((s for s in searches if s['outcome'] == 'no_match'), None)
        found = next((s for s in reversed(searches) if s['outcome'] == 'matched'), None)
        lines = []
        if empty:
            lines.append('本次' + _scope_text(empty) + '暂未找到匹配商品记录。')
        if found:
            names = list(dict.fromkeys(label(p['name'], 100) for p in found['products']))[:3]
            lines.append('本次' + _scope_text(found) + '返回了商品记录，包括「' + '」「'.join(names) + '」。')
            if found['unapplied_conditions']:
                lines.append('部分规格条件未能应用，不能认为这些商品已满足所有要求。')
        if not lines:
            lines.append('目前没有足够的搜索结果确认是否有匹配商品。')
        lines.append('搜索结果仅限对应关键词和筛选条件，不能据此判断商品不存在、下架或停售。')
        if searches and any(not s['data_as_of'] for s in searches):
            lines.append('数据同步时间未记录。')
        return ''.join(lines)

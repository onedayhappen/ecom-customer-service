"""Conservative, deterministic control parsing; ambiguous targets are asked."""
import re
from app.data_plan.taxonomy import DEFINITIONS, LEGACY_INTENTS
from app.intent.rules import recognize

LABELS = {
    'after_sale_request': '售后申请', 'refund_status': '退款进度',
    'logistics_inquiry': '物流查询', 'order_inquiry': '订单查询',
    'invoice': '发票申请', 'address_change': '修改地址', 'order_cancel': '取消订单',
    'expedite': '催发货', 'complaint': '投诉', 'product_inquiry': '商品咨询',
    'product_recommendation': '商品推荐', 'issue_report': '问题反馈',
    'others': '其他咨询',
}
LABELS.update({intent.value: title for intent, (title, _) in DEFINITIONS.items()})
ORDER = re.compile(r'\bORD-[A-Za-z0-9-]+\b', re.I)


def intent_of(text):
    intent = recognize(text).value
    return None if intent in {'greetings', 'closure', 'others'} else intent


def task_matches(task, intent):
    stored = task['intent']
    if stored == intent:
        return True
    if stored == 'refund_status' and intent == 'after_sale_status':
        return True
    if stored in {item.value for item in LEGACY_INTENTS}:
        return intent_of(task['request_text']) == intent
    return False


def is_new_request(text):
    return bool(re.search(r'我要|我想|帮我|请(?:帮|查|退|开)|先(?:查|退|开)|查(?:询|一下|物流|订单)|申请(?:退款|退货|换货)|开发票|开票|另外|顺便', text))


def parse(text):
    action, target, specs, replacement, keep = None, None, [], None, False
    clauses = re.split(r'[，,；;。\n]|(?:然后|另外|顺便|还要|还想)|再(?=查|退|开|帮)|(?:并且|并|以及|和)(?=(?:帮我)?(?:查|申请|退|开|改|取消|催|投诉|ORD-))', text.strip())
    for clause in filter(None, (c.strip() for c in clauses)):
        kind = intent_of(clause)
        if re.search(r'(?:不要|别|不)(?:取消|暂停|停止|撤销)', clause):
            keep = True
            continue
        control_question = bool(re.search(r'怎么|如何|能否|能不能|可以.*吗|是什么意思|是否', clause))
        if not control_question and (re.search(r'先放着|先放一放|稍后处理|暂停|先不(?:做|处理)|等会处理', clause) or clause in {'稍后', '等会'}):
            action, target = 'pause', kind
            continue
        if not control_question and (re.search(r'^(?:算了|不用了|取消(?:此|这个|刚才|之前|旧|当前)?(?:任务|申请|操作)?$)', clause)
                or re.search(r'不(?:要|用|想)?(?:退款|退货|退|换货|开票|查物流)了|取消.*(?:任务|申请)|撤销.*任务', clause)
                or (clause.startswith('取消') and kind and kind != 'order_cancel')):
            # This cancels a local pending task only; it never revokes a
            # submitted after-sale record. Classification still returns others
            # for an unsupported request to revoke an actual refund application.
            target_kind = kind or intent_of(re.sub(r'^(?:取消|撤销)', '', clause))
            action, target = 'cancel', target_kind or ('refund_service' if '不退了' in clause else None)
            continue
        if re.match(r'^(?:继续|恢复)', clause):
            action, target = 'resume', kind
            continue
        changed = re.search(r'(?:改成|改为|换成|更正为|改查)(.+)', clause)
        if changed:
            clause = changed.group(1)
            replacement = clause
            kind = intent_of(clause)
            action = action or 'amend'
        if kind:
            specs.append({'intent': kind, 'text': clause})
    return {'action': action, 'target': target, 'specs': specs, 'replacement': replacement, 'keep': keep}


def effective_text(task):
    text = task['request_text']
    slots = task['slots']
    if slots.get('order_id'):
        text = ORDER.sub('', text)
    labels = {'order_id': '订单号', 'reason': '原因', 'title': '抬头', 'new_address': '新地址', 'details': '补充信息'}
    return text + ''.join(f"\n{labels.get(k, k)}：{ORDER.sub('', str(v)) if slots.get('order_id') and k != 'order_id' else v}" for k, v in slots.items())


def select_option(options, text):
    value = text.strip()
    selected = next((o for o in options if value in {o['value'], o['label']}), None)
    if selected:
        return selected
    match = re.fullmatch(r'(?:第)?([1-5一二两三四五])(?:个|项|笔|单)?[。！!]?', value)
    if match:
        number = match.group(1)
        index = int(number) if number.isdigit() else {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5}[number]
        if index <= len(options):
            return options[index - 1]
    return None

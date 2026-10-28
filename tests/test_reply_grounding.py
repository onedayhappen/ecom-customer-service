"""Final-publication grounding, including captured customer conversations."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services.commerce.common import ref
from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.replies import ReplyCoordinator
from app.tasks.reply_grounding import check_reply, evidence_index
from tests.test_coordinated_replies import ready, replies
from tests.test_task_candidates import store, task, ask
from tests.test_task_execution import request
from tests.test_task_storage import empty_store


CASES = json.loads((Path(__file__).parent / 'fixtures/youyou_final_reply_grounding.json').read_text(encoding='utf-8'))['cases']
CROSS_SESSION_CASES = json.loads((Path(__file__).parent / 'fixtures/youyou_cross_session_publication.json').read_text(encoding='utf-8'))['cases']


def receipt(tool, data, **arguments):
    return dict(tool=tool, arguments=arguments, result=dict(success=True, data=data))


def order(oid='ORD-A', status='paid', amount='88.00'):
    return receipt('get_order', dict(order_id=oid, platform_code='shop', status=status,
                   paid_amount=amount, payable_amount=amount, currency='CNY'))


def checked(text, *receipts):
    return check_reply(text, [evidence_index(list(receipts), {})])


@pytest.mark.parametrize('text,reason', [
    ('没有包裹记录，所以可以判断它还没有发货。', 'unshipped'),
    ('没有发货记录就说明未发货。', 'unshipped'),
    ('目前无法确认物流。但这件已经签收了。', 'status'),
    ('家人拿到的鞋盒应该不是它。', 'parcel_identity'),
    ('家人拿到的包裹就是这件商品。', 'parcel_identity'),
    ('退款已经到账。', 'bank_credit'),
    ('退款退回银行卡了。', 'bank_credit'),
    ('这件周六肯定能到。', 'delivery_promise'),
    ('订单 ORD-A 已取消。', 'status'),
    ('订单 ORD-A 已退款88元。', 'status'),
    ('订单 ORD-A 金额是99元。', 'amount'),
    ('订单 ORD-A 金额是￥99.00。', 'amount'),
])
def test_unsupported_business_claim_is_corrected_locally(text, reason):
    result, reasons = checked(text, order())
    assert reason in reasons
    assert result != text


@pytest.mark.parametrize('text', [
    '订单 ORD-A 已支付，金额88元。',
    '没有查到发货记录，无法确认是否已发货。',
    '没有查到已发货记录。',
    '没有物流记录不代表还没发货。',
    '无法确认家人拿到的鞋盒是不是这双鞋。',
    '不能确认银行卡是否已到账。',
    '暂时无法确认周六能到。',
    '目前有没有发货？',
    '订单是否已取消，需要核实。',
    '未必已发货。',
    '您希望预算在2500元以内。',
    '积分已经入账。',  # A bank-credit rule must not rewrite a points statement.
    '已取消这项自动处理。',
    '您好！！\n\n我继续帮您查询。',
])
def test_qualified_or_supported_statement_is_preserved(text):
    assert checked(text, order()) == (text, [])


def test_amount_and_state_cannot_be_borrowed_from_another_order():
    a, b = order(status='paid'), order('ORD-B', 'shipped', '200')
    assert checked('订单 ORD-A 已发货，金额200元。', a, b)[1] == ['amount', 'status']
    text = '订单 ORD-A 已支付，金额88元；订单 ORD-B 已发货，金额200元。'
    assert checked(text, a, b) == (text, [])
    assert checked('订单 ORD-AB 已发货。', a, order('ORD-AB', 'shipped'))[1] == []
    assert checked('订单 ORD-AB 已支付，金额88元。', a)[1] == ['amount', 'status']
    assert checked('都已发货。', a, b)[1] == ['status']


def test_requested_refund_channel_success_and_bank_credit_are_distinct():
    target = ref('order', 'shop', 'ORD-A')
    sale = receipt('get_after_sale_status', {'after_sale': {'order_id': 'ORD-A',
        'platform_code': 'shop', 'requested_amount': '88', 'status': 'completed'}})
    refund = receipt('get_refunds', {'items': [{'amount': '20', 'currency': 'CNY', 'status': 'succeeded'}],
        'amounts_by_currency_and_status': {'CNY': {'succeeded': '20'}}}, target_ref=target)
    assert checked('订单 ORD-A 申请退款88元。', sale)[1] == []
    assert checked('订单 ORD-A 已退款88元。', sale, refund)[1] == ['amount']
    text = '订单 ORD-A 渠道退款成功20元，银行卡是否到账还需核实。'
    assert checked(text, sale, refund) == (text, [])
    assert checked('订单 ORD-A 已退款20元并已到账。', sale, refund)[1] == ['bank_credit']


def test_history_descriptions_failed_receipts_and_foreign_currency_do_not_authorize_claims():
    source = order(status='after_sale')
    source['result']['data'].update(status_before_after_sale='shipped', description='订单已发货，已退款88元')
    failed = order(status='shipped'); failed['result']['success'] = False
    usd = order('ORD-B'); usd['result']['data']['currency'] = 'USD'
    assert checked('订单 ORD-A 已发货。', source, failed)[1] == ['status']
    assert checked('订单 ORD-B 金额88元。', usd)[1] == ['amount']


def test_product_prices_and_payment_receipts_remain_usable():
    products = receipt('search_products', {'items': [
        {'product_id': 'P1', 'name': '耳机', 'sku_summary': [{'list_price': '88', 'currency': 'CNY'}]},
        {'product_id': 'P2', 'name': '键盘', 'sku_summary': [{'list_price': '200', 'currency': 'CNY'}]}]})
    text = '耳机88元，键盘200元。'
    assert checked(text, products) == (text, [])
    assert checked('耳机200元。', products)[1] == ['amount']
    payments = receipt('get_payments', {'recorded_success_amounts': {'CNY': '88'}},
                       order_ref=ref('order', 'shop', 'ORD-A'))
    text = '订单 ORD-A 已支付88元。'
    assert checked(text, payments) == (text, [])


def test_estimated_date_does_not_authorize_a_guarantee_or_another_date():
    logistics = receipt('get_logistics', {'items': [{'order_id': 'ORD-A', 'platform_code': 'shop',
        'status': 'in_transit', 'estimated_delivery': '2026-10-28'}]})
    text = '订单 ORD-A 预计10月28日送达。'
    assert checked(text, logistics) == (text, [])
    assert checked('订单 ORD-A 预计10月29日送达。', logistics)[1] == ['delivery_promise']
    assert checked('订单 ORD-A 保证10月28日送达。', logistics)[1] == ['delivery_promise']


def save_receipts(store, record, source, receipts):
    runs = TaskRuns(store, 'alice')
    req = TaskExecutionInput.model_validate(request(record, source))
    reservation, _ = runs.begin(req)
    ids = []
    for i, evidence in enumerate(receipts):
        saved = runs.save_tool(req, reservation['payload'], i, evidence['tool'],
                               evidence['arguments'], evidence['result'])
        ids.append(saved['event_id'])
    runs.finish(req, reservation['payload'], dict(state='ready', task_version=record.version,
                reply='内部草稿不能作为证据', evidence_ids=ids))
    return req.job_id, ids


@pytest.mark.parametrize('case', CASES, ids=lambda c: f"real-session-{c['session']}-round-{c['round']}")
def test_captured_failures_are_corrected_at_actual_publication_boundary(store, case):
    record, source = task(store, 's', goal=case['user'], details={'item_name': '本次查询商品'})
    jid, ids = save_receipts(store, record, source, case['receipts'])
    calls = []
    def model(context):
        calls.append(context)
        assert 'grounding' not in json.dumps(context, ensure_ascii=False)
        return case['original_reply']
    reply = ReplyCoordinator(store, 'alice', composer=model).publish('s')
    expected, reasons = check_reply(case['original_reply'], [evidence_index(case['receipts'], record.details)])
    assert case['expected_reason'] in reasons
    from app.tasks.reply_claims import fallback_text
    assert reply['content'] == fallback_text(calls[0]['matters'][0]['claims'])
    assert reply['content'] != case['original_reply']
    assert '可以判断它还没有发货' not in reply['content']
    assert '应该不是它' not in reply['content']
    assert '数据同步时间未记录' in reply['content']
    assert reply['result_ids'] == ['job:' + jid]
    assert sorted(reply['evidence_ids']) == sorted(ids)
    assert len(calls) == 1
    assert ReplyCoordinator(store, 'alice', composer=model).publish('s') is None
    assert len(calls) == len(replies(store)) == 1
    with store.transaction(read_only=True) as tx:
        traces = tx.all("SELECT stage,payload FROM interaction_trace_events WHERE stage LIKE ?",
                        ('reply.composition.%',))
        assert [t['stage'] for t in traces].count('reply.composition.finished') == 1
        assert any(t['stage'] == 'reply.composition.fallback' for t in traces)
        assert not any(t['stage'] == 'reply.composition.grounding' for t in traces)


def test_corrected_reply_keeps_code_owned_question_and_no_new_queries(store, monkeypatch):
    a, source = task(store, 's')
    ready(store, a, source)
    b, source_b = task(store, 's', details={'order_id': 'ORD-B'})
    b = ask(store, b, source_b, publish=False)
    from app.tasks.scheduling import TaskSchedule
    TaskSchedule(store, 'alice').enqueue(request(b, source_b))
    calls = []
    def model(context):
        calls.append(context)
        # Grounding runs after this return; deliberately forbid it from opening a transaction.
        return '订单 ORD-A 已退款999元。'
    import app.tasks.reply_claims as grounding
    check = grounding.render_plan
    def local_only(text, evidence):
        with monkeypatch.context() as patch:
            patch.setattr(store, 'transaction', lambda **_: pytest.fail('grounding performed I/O'))
            return check(text, evidence)
    monkeypatch.setattr(grounding, 'render_plan', local_only)
    result = ReplyCoordinator(store, 'alice', composer=model).publish('s')
    assert '999' not in result['content']
    assert calls[0]['required_notices'][0] in result['content']
    assert len(calls) == 1


def test_new_input_during_correction_still_discards_reply(store, monkeypatch):
    from app.tasks.intake import MessageIntake
    import app.tasks.reply_claims as grounding
    a, source = task(store, 's')
    ready(store, a, source)
    check = grounding.render_plan
    def interrupted(text, evidence):
        MessageIntake(store).receive('old', 'alice', request_id='new-input', content='先不要查')
        return check(text, evidence)
    monkeypatch.setattr(grounding, 'render_plan', interrupted)
    assert ReplyCoordinator(store, 'alice', composer=lambda _: '已退款999元。').publish('s') is None
    assert not replies(store)


def test_index_does_not_mutate_receipts_or_read_task_status():
    sources = [order()]
    before = deepcopy(sources)
    index = evidence_index(sources, {'status': 'cancelled', 'goal': '已退款88元'})
    assert check_reply('订单 ORD-A 已取消。', [index])[1] == ['status']
    assert sources == before


def test_missing_or_malformed_optional_rows_cannot_block_publication():
    sources = [receipt('find_orders', {'items': None}), receipt('get_logistics', {'items': [None]}),
               receipt('search_products', {'items': [False, {}]})]
    assert evidence_index(sources, {}) == []
    assert checked('订单已发货。', *sources)[1] == ['status']


@pytest.mark.parametrize('case', CROSS_SESSION_CASES, ids=lambda c: f"real-session-{c['session']}")
@pytest.mark.parametrize('legacy_publication', [True, False])
def test_real_cross_session_inputs_and_versions_publish_with_grounding(store, case, legacy_publication):
    """Replay the two blocked inputs with real receipts in disposable storage.

    Association/composition are deterministic; this is not another live LLM run.
    Also covers a valid old publication whose candidate has since changed.
    """
    from app.tasks.repository import TaskRepository
    from tests.test_message_association import batch_for, proposal, quote, service, update
    record, source = task(store, 'old', goal=case['goal'], details={'item_name': '手机' if case['session'] == 8 else '鞋子'})
    repo = TaskRepository(store, 'alice')
    while record.version < case['old_task_version']:
        record = repo.update(record.task_id, 'old', expected_version=record.version,
            change_id=f'old-version-{record.version}', source=source, patch={'goal': case['goal']})
    svc = service(store)
    old = batch_for(store, case['old_user'], sid='old')
    decision = dict(action='clarify', sequences=[old.messages[0]['sequence']], evidence=[quote(old)],
                    clarification='请说明这条补充对应哪件事', candidate_task_ids=[record.task_id])
    result = svc.commit(old, svc.snapshot(old), proposal(decision))
    menu = ReplyCoordinator(store, 'alice').publish('old')
    if legacy_publication:
        with store.transaction() as tx:
            row = tx.one('SELECT payload FROM support_events WHERE session_id=? AND sequence=?', ('old', menu['sequence']))
            payload = json.loads(row['payload']); payload.pop('clarification_version')
            tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
                       (json.dumps(payload), 'old', menu['sequence']))
    new = batch_for(store, case['user'])
    updated = update(new, record)
    updated['goal'] = case['goal'] + '；本次需求：' + case['user']
    svc.commit(new, svc.snapshot(new), proposal(updated))
    record = repo.get(record.task_id)
    assert record.version == case['new_task_version']
    source = dict(kind='message', session_id='s', sequence=new.messages[0]['sequence'])
    jid, _ = save_receipts(store, record, source, case['receipts'])
    calls = []
    def model(context):
        calls.append(context)
        return case['final_reply']
    coordinator = ReplyCoordinator(store, 'alice', composer=model)
    sent = coordinator.publish('s')
    assert sent['result_ids'] == ['job:' + jid]
    assert sent['content']
    assert '您说的是哪一件' not in sent['content']
    assert '执行草稿' not in sent['content']
    assert coordinator.publish('s') is None
    assert len(replies(store)) == 1
    assert len(calls) <= 1  # No model call when read_task_context supplies no business evidence.
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT disposition FROM task_reply_items WHERE item_key=?', ('job:' + jid,))['disposition'] == 'published'
        assert tx.one('SELECT status FROM association_clarifications WHERE clarification_id=?',
                      (result['clarification']['clarification_id'],))['status'] == 'open'

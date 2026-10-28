"""Archived query receipts plus deliberately bad final-model outputs.

The two original conversations expose query spelling mismatches; they did not
contain the injected absence claims. We exercise the actual publication gate,
not a new live model run or a replacement search implementation.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.tasks.replies import ReplyCoordinator
from app.tasks.reply_grounding import check_reply, evidence_index
from tests.test_reply_grounding import receipt, checked, save_receipts
from tests.test_coordinated_replies import replies
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store


CASES = json.loads((Path(__file__).parent / 'fixtures/youyou_product_search_grounding.json').read_text(encoding='utf-8'))['cases']


def search(query='红米K50', total=0, items=None, **arguments):
    return receipt('search_products', dict(total=total, items=items or [], has_more=False), query=query, **arguments)


@pytest.mark.parametrize('text', [
    '没找到红米K50，所以这个商品不存在。',
    '红米K50不存在。',
    '我们这里没有红米K50。',
    '本店目前没有红米K50。',
    '红米K50已经下架。',
    '没查到，应该已经停售了。',
    '这个商品可能不存在。',
    '店里不卖这种锅。',
    '没有这款商品。',
    '没有红米K50这款手机。',
    '目前无法确认价格，但红米K50不存在。',
])
def test_zero_matches_never_prove_product_absence(text):
    output, reasons = checked(text, search())
    assert reasons == ['product_absence']
    assert output != text
    assert '按关键词「红米K50」查询暂未找到匹配商品记录' in output
    assert '不能据此判断商品不存在' in output


@pytest.mark.parametrize('text', [
    '按「红米K50」查询暂时没找到匹配商品记录。',
    '本次没有找到匹配商品。',
    '查询未命中，不代表商品不存在。',
    '无法确认红米K50是否已经下架。',
    '红米K50停售了吗？',
    '不能因为没找到就认定商品不存在。',
    '还没找到退款记录。',
    '现有资料没有这口锅的重量。',
    '没有红米K50的重量信息。',
    '没有这个问题。',
])
def test_scoped_qualified_or_unrelated_statements_are_preserved(text):
    assert checked(text, search()) == (text, [])


@pytest.mark.parametrize('case', CASES, ids=lambda c: f"session-{c['session']}-round-{c['round']}")
def test_real_query_results_keep_separate_scopes_and_successful_matches(case):
    sources = case['receipts']
    before = deepcopy(sources)
    index = evidence_index(sources, {'item_name': case['subject']})
    observations = [item for item in index if item.get('kind') == 'product_search']
    assert [item['outcome'] for item in observations] == ['no_match'] * (len(sources) - 1) + ['matched']
    assert [item['evidence_id'] for item in observations] == [r['event_id'] for r in sources]
    assert all(item['completeness'] == 'partial' and item['data_as_of'] is None for item in observations)
    raw, found = sources[0]['arguments']['query'], sources[-1]['arguments']['query']
    name = sources[-1]['result']['data']['items'][0]['name']
    text = f'按「{raw}」查询没找到匹配商品记录；按「{found}」查询返回了「{name}」。'
    assert check_reply(text, [index]) == (text, [])
    wrong = '本次没有找到任何匹配商品。'
    output, reasons = check_reply(wrong, [index])
    assert reasons == ['product_search_result']
    assert name in output and raw in output and found in output
    assert sources == before


@pytest.mark.parametrize('case', CASES, ids=lambda c: f"session-{c['session']}-round-{c['round']}")
@pytest.mark.parametrize('only_zero', [True, False], ids=['initial-miss', 'after-rewording'])
def test_actual_publication_corrects_injected_absence_once(store, case, only_zero, monkeypatch):
    sources = case['receipts'][:1] if only_zero else case['receipts']
    record, source = task(store, 's', goal=case['user'], details={'item_name': case['subject']})
    jid, ids = save_receipts(store, record, source, sources)
    calls = []
    def model(context):
        calls.append(context)
        return case['injected_reply']
    # Verify the guard uses the draft's already-loaded receipts, with no DB access.
    import app.tasks.reply_grounding as grounding
    original_check = grounding.check_reply
    def local_check(text, evidence):
        with monkeypatch.context() as patch:
            patch.setattr(store, 'transaction', lambda **_: pytest.fail('grounding performed I/O'))
            return original_check(text, evidence)
    monkeypatch.setattr(grounding, 'check_reply', local_check)
    coordinator = ReplyCoordinator(store, 'alice', composer=model)
    result = coordinator.publish('s')
    assert result['content'] != case['injected_reply']
    assert '不能据此判断商品不存在' in result['content']
    assert sources[0]['arguments']['query'] in result['content']
    if not only_zero:
        assert sources[-1]['result']['data']['items'][0]['name'] in result['content']
    assert result['result_ids'] == ['job:' + jid]
    assert sorted(result['evidence_ids']) == sorted(ids)
    assert coordinator.publish('s') is None
    assert len(calls) == len(replies(store)) == 1
    with store.transaction(read_only=True) as tx:
        finished = tx.all("SELECT payload FROM interaction_trace_events WHERE stage='reply.composition.finished'")
        assert len(finished) == 1
        assert 'product_absence' in json.loads(finished[0]['payload'])['grounding']['reasons']


def test_price_and_spec_filters_cannot_be_generalized_to_catalogue_absence():
    source = search(max_price={'amount': '100', 'currency': 'CNY'}, specs=[{'name': '内存', 'value': '8GB'}])
    text, _ = checked('没有这款商品。', source)
    assert '价格上限100 CNY' in text and '内存=8GB' in text
    assert '不能据此判断商品不存在' in text


@pytest.mark.parametrize('change', ['failed', 'missing_total', 'malformed_items', 'next_page', 'empty_page'])
def test_incomplete_or_failed_lookup_is_not_a_zero_match(change):
    source = search()
    if change == 'failed': source['result']['success'] = False
    if change == 'missing_total': source['result']['data'].pop('total')
    if change == 'malformed_items': source['result']['data']['items'] = None
    if change == 'next_page': source['result']['next_cursor'] = 'page2'
    if change == 'empty_page': source['arguments']['cursor'] = 'page2'
    text, reasons = checked('本次没找到匹配商品。', source)
    assert reasons == ['product_search_result']
    assert '暂未找到匹配商品记录' not in text


def test_search_matches_cannot_be_borrowed_from_another_task():
    phone = evidence_index([search()], {})
    pan = evidence_index([search('锅 28cm', 1, [{'product_id': 'P2', 'name': 'Carat钻石煎锅28cm'}])], {})
    text, reasons = check_reply('红米K50不存在。', [phone, pan])
    assert reasons == ['product_absence']
    assert 'Carat' not in text
    assert check_reply('按「红米K50」没找到商品。', [phone, pan])[1] == []


def test_repeated_unsupported_claims_do_not_repeat_the_correction():
    text, reasons = checked('红米K50不存在。我们这里没有红米K50。', search())
    assert reasons == ['product_absence']
    assert text.count('不能据此判断商品不存在') == 1


def test_unapplied_spec_conditions_are_not_reported_as_satisfied():
    source = search('锅', 1, [{'product_id': 'P1', 'name': '锅', 'unapplied_conditions': [{'name': '重量', 'value': '500g'}]}],
                    specs=[{'name': '重量', 'value': '500g'}])
    text, reasons = checked('没有找到商品。', source)
    assert reasons == ['product_search_result']
    assert '部分规格条件未能应用' in text


def test_query_scope_survives_fact_projection_truncation():
    from app.tasks.tool_facts import render
    result = deepcopy(CASES[0]['receipts'][-1]['result'])
    conditions = result['data'].pop('applied_conditions')
    result['data']['items'] *= 10
    result['data']['applied_conditions'] = conditions  # Last in insertion order.
    facts = render('search_products', result)
    assert facts[0] == '按「Redmi K50」查询'
    assert '以上为部分结果' in facts[-1]


@pytest.mark.parametrize('change', ['query', 'expiry', 'new_input'])
def test_search_correction_retains_publication_fence(store, change):
    from app.tasks.intake import MessageIntake
    case = CASES[0]
    record, source = task(store, 's', goal=case['user'], details={'item_name': case['subject']})
    save_receipts(store, record, source, case['receipts'])
    def model(_):
        if change == 'new_input':
            MessageIntake(store).receive('old', 'alice', request_id='changed-search', content='说错了，是另一部手机')
        else:
            with store.transaction() as tx:
                row = tx.one("SELECT sequence,payload FROM support_events WHERE session_id='s' AND kind='task_tool_result'")
                value = json.loads(row['payload'])
                if change == 'query': value['arguments']['query'] = '另一部手机'
                else: value['observed_epoch'] = 0
                tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
                           (json.dumps(value), 's', row['sequence']))
        return case['injected_reply']
    assert ReplyCoordinator(store, 'alice', composer=model).publish('s') is None
    assert not replies(store)

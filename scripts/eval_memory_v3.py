# 文件说明：在隔离会话和存储中调用真实模型，评测 v3 记忆归档与提取。
"""Isolated live-model memory evaluation: 10 archives, 10 user turns per archive.

This exercises extraction and durable memory, not commerce/task-agent replies.
No business database is used; all evidence is synthetic and stored in a fresh DB.
"""
import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from time import perf_counter, sleep, time
from uuid import uuid4

from app.config.settings import settings
from app.memory.runtime import build_memory_manager, process_memory_jobs
from app.memory.v3.jobs import archive, archive_status
from app.support.store import SupportStore


SCENARIOS = [
    ['先记下我的衣服喜好哈', '衣服一直喜欢蓝色，平时穿M码', '衣服预算一百元以内，版型要宽松点'],
    ['说下我的鞋子偏好', '日常鞋我穿38，跑步鞋39，白色优先', '尺码体系我还真不知道，先记我说的数字就好'],
    ['手机也记一下', '以后手机预算三千元以内，喜欢黑色安卓手机', '手机预算跟衣服的分开记哈'],
    ['这次给妹妹买粉色小号衣服', '那是她要的，不是我改喜好了', '我自己的喜好保持之前的'],
    ['最近胖了点，改下衣服喜好', '以后衣服按L码看吧，现在更喜欢黑的，蓝色M那套老喜好改一下', '这说的是长期偏好，不是让你改订单'],
    ['还是说我的衣服', '第一个才是现在的，黑色L', '黑色衣服、L码就对了，以后按这个'],
    ['衣服旧的喜好清一下', '旧的蓝色偏好忘掉吧，M码也不再拿来推荐了', '现在的黑色L衣服偏好保留'],
    ['衣服的预算也调一下', '以后衣服预算改成一百五十元以内', '手机那边先不动'],
    ['手机预算我暂时没想好', '忘掉我的手机预算，整个预算偏好都不要保留', '手机颜色和系统不用变'],
    ['重新记一下手机预算', '现在手机预算重新记为两千五百元以内', '衣服还是黑色L，鞋日常38跑步39'],
]
FILLER = ['嗯嗯', '好，那就这样', '只是聊聊，不下单', '谢谢优优', '我没有别的要改的', '这会儿不查订单', '下次再聊哈']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use the configured model (10 paid API calls).')
    args = parser.parse_args()
    if not args.live:
        parser.error('Specify --live; offline regressions are in tests/test_memory_v3.py')
    if not settings.openai_api_key:
        parser.error('Configured model credentials are missing')
    destination = Path('data/evaluation') / ('memory_v3_' + datetime.now().strftime('%Y%m%d') + '_' + uuid4().hex[:6])
    destination.mkdir(parents=True)
    settings.memory_engine, settings.memory_enabled = 'v3', True
    settings.memory_storage_backend = 'json'
    settings.memory_dir = str(destination / 'empty_legacy')
    settings.memory_redis_url = ''
    settings.memory_retrieval_enabled = False
    settings.short_term_memory_enabled = False
    settings.memory_extraction_debounce_seconds = 0
    settings.memory_extraction_input_tokens = 12000
    store = SupportStore('sqlite:///' + (destination / 'support.db').resolve().as_posix())
    results = []
    from app.memory.v3.extractor import extract
    def recorded_extract(messages, existing):
        plan = extract(messages, existing)
        (destination / f'plan-{index+1}.json').write_text(plan.model_dump_json(indent=2), encoding='utf8')
        return plan
    for index, opening in enumerate(SCENARIOS):
        sid = f'eval-{index+1}'
        store.ensure(sid, 'memory-eval')
        texts = opening + FILLER
        for text in texts:
            with store.transaction() as tx:
                c = store._control(tx, sid, 'memory-eval')
                store._event(tx, c, 'message', 'memory-eval', dict(role='user', content=text))
        checkpoint = archive(store, 'memory-eval', sid)
        start = perf_counter()
        counts = process_memory_jobs(store, extract_fn=recorded_extract)
        state = archive_status(store, 'memory-eval', checkpoint['archive_id'])
        while state['status'] == 'retrying':
            with store.transaction(read_only=True) as tx:
                due = tx.one('SELECT available_at FROM memory_v3_jobs WHERE session_id=?', (sid,))['available_at']
            sleep(max(0, due-time()) + .01)
            counts = process_memory_jobs(store, extract_fn=recorded_extract)
            state = archive_status(store, 'memory-eval', checkpoint['archive_id'])
        facts = build_memory_manager('memory-eval', store=store).ltm.facts
        result = dict(session=index+1, messages=texts, seconds=round(perf_counter()-start, 3),
                      counts=counts, archive=state, facts=[asdict(f) for f in facts])
        results.append(result)
        (destination / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(dict(session=index+1, status=state['status'], applied=counts['facts'],
                              issues=[o for o in state['outcomes'] if o['status'] not in {'applied', 'noop'}]), ensure_ascii=False), flush=True)
        if counts['failed']:
            raise SystemExit('Memory extraction failed; checkpoint retained in ' + str(destination))
    values = {(f.qualifiers.get('product_category'), f.predicate, f.qualifiers.get('usage', 'general')): f.value for f in facts}
    assert values[('clothing', 'prefers_color', 'general')] == 'black'
    assert values[('clothing', 'wears_size', 'general')] == 'L'
    assert values[('shoes', 'wears_size', 'daily')] == '38'
    assert values[('shoes', 'wears_size', 'running')] == '39'
    assert values[('phone', 'budget_range', 'general')]['max'] == 2500
    assert values[('clothing', 'budget_range', 'general')]['max'] == 150
    history = build_memory_manager('memory-eval', store=store).ltm.history_facts()
    assert not any(f.qualifiers.get('product_category') == 'clothing' and f.value in ('blue', 'M') for f in history)
    print('PASS: 10 archives / 100 user messages; report=' + str(destination / 'results.json'), flush=True)


if __name__ == '__main__':
    main()

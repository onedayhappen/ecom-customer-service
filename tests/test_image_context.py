import io
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from app.agents.association import AssociationProposal
from app.config.settings import settings
from app.media.context import message_images, task_images
from app.media.jobs import ImageWorker
from app.media.service import MediaService
from app.media.vision import VisionClient
from app.support.store import SupportError, SupportStore
from app.tasks.association import AssociationRejected, AssociationService
from app.tasks.intake import MessageIntake
from app.tasks.provenance import ProvenanceGuard


def png(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (64, 32), color).save(out, 'PNG')
    return out.getvalue()


class Vision:
    def __init__(self):
        self.calls = 0
        self.revision = VisionClient().revision

    def observe(self, data, question, detail, *, timeout):
        assert data[:2] == b'\xff\xd8' and timeout > 0
        self.calls += 1
        return dict(image_type='商品照片', summary='杯子边缘有裂纹', visible_text='ORD-123',
                    candidate_entities=['ORD-123'], uncertainties=['无法确定破损原因'])


@pytest.fixture
def images(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'media_storage_dir', str(tmp_path / 'media'))
    monkeypatch.setattr(settings, 'short_term_memory_enabled', False)
    monkeypatch.setattr(settings, 'memory_enabled', False)
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')
    store = SupportStore(f'sqlite:///{tmp_path / "images.db"}')
    for sid, user in [('s', 'alice'), ('next', 'alice'), ('other', 'bob')]:
        store.ensure(sid, user)
    vision = Vision()
    media = MediaService(store, vision=vision)
    worker = ImageWorker(store, media=media)
    yield store, media, worker, vision
    worker.close()


def upload(media, sid='s'):
    return media.upload('alice', sid, str(uuid4()), png())['attachment_id']


def send(store, aid, text='', sid='s', request_id=None):
    return MessageIntake(store).receive(sid, 'alice', request_id=request_id or str(uuid4()),
                                       content=text, attachment_ids=[aid])


def analyzed(images, text=''):
    store, media, worker, _ = images
    aid = upload(media)
    receipt = send(store, aid, text)
    worker.run(worker.claim())
    batch = MessageIntake(store).claim('s', 'alice')
    service = AssociationService(store, 'alice')
    snapshot = service.snapshot(batch)
    image = snapshot.context.current_messages[0]['visual_context'][0]
    evidence = dict(kind='image_observation', session_id='s', sequence=receipt['sequence'],
                    attachment_id=aid, observation_id=image['observations'][0]['observation_id'], field_path='summary')
    plan = AssociationProposal.model_validate(dict(decisions=[dict(action='create', effect='new',
        goal='了解杯子破损情况', sequences=[receipt['sequence']], evidence=[evidence])]))
    return aid, receipt, batch, service, snapshot, plan


def test_upload_validates_bytes_and_is_idempotent(images):
    store, media, _, _ = images
    request_id = str(uuid4())
    one = media.upload('alice', 's', request_id, png())
    assert media.upload('alice', 's', request_id, png()) == one
    assert one['mime_type'] == 'image/png' and one['width'] == 64
    assert 'storage_key' not in one
    assert media.content('alice', one['attachment_id'], 'original')[0] == png()
    assert media.content('alice', one['attachment_id'])[1] == 'image/jpeg'
    for sid, data in [('next', png()), ('s', png('blue'))]:
        with pytest.raises(SupportError):
            media.upload('alice', sid, request_id, data)
    with pytest.raises(SupportError):
        media.upload('alice', 's', str(uuid4()), b'<svg>fake image</svg>')
    with pytest.raises(SupportError):
        media.content('bob', one['attachment_id'])


def test_limits_animation_and_atomic_attachment_validation(images, monkeypatch):
    store, media, _, _ = images
    aid = upload(media)
    with pytest.raises(SupportError):
        send(store, aid, sid='next')
    with pytest.raises(SupportError):
        MessageIntake(store).receive('s', 'alice', request_id='duplicates', content='', attachment_ids=[aid, aid])
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM message_intake_items')['n'] == 0
        assert tx.one('SELECT COUNT(*) AS n FROM image_jobs')['n'] == 0
    monkeypatch.setattr(settings, 'media_max_pixels', 5)
    with pytest.raises(SupportError):
        upload(media)


def test_image_queue_blocks_contiguous_prefix_but_not_controls(images):
    store, media, worker, _ = images
    intake = MessageIntake(store)
    intake.receive('s', 'alice', request_id='text', content='你好')
    aid = upload(media)
    send(store, aid)
    intake.receive('s', 'alice', request_id='later', content='请看一下')
    batch = intake.claim('s', 'alice')
    assert [m['content'] for m in batch.messages] == ['你好']
    intake.complete(batch, [dict(sequence=batch.messages[0]['sequence'], status='associated', decision_id='d')])
    assert intake.claim('s', 'alice') is None
    worker.run(worker.claim())
    assert len(intake.claim('s', 'alice').messages) == 2
    intake.receive('s', 'alice', request_id='human', content='转人工')
    assert store.state('s', 'alice')['mode'] == 'handoff_pending'


def test_pure_image_associates_and_survives_raw_history_window(images):
    store, media, _, _ = images
    aid, receipt, batch, service, snapshot, plan = analyzed(images)
    result = service.commit(batch, snapshot, plan)
    tid = result['decisions'][0]['task_id']
    with store.transaction(read_only=True) as tx:
        visual = task_images(tx, 'alice', tid)
        assert visual[0]['attachment_id'] == aid
        assert visual[0]['observations'][0]['result']['summary'] == '杯子边缘有裂纹'
        assert 'visible_text' not in visual[0]['observations'][0]['result']
    from app.memory.task_context import task_history
    record = service.repo.get(tid)
    runs = SimpleNamespace(store=store, user='alice', _scope=lambda tx, task: None)
    for n in range(10):
        new = MessageIntake(store).receive('next', 'alice', request_id=f'follow-{n}', content='补充文字')
        with store.transaction() as tx:
            tx.execute('INSERT INTO support_task_messages VALUES (?,?,?)', (tid, 'next', new['sequence']))
    history = task_history(runs, record)
    assert len(history['sources']) == 8
    assert history['visual_context'][0]['attachment_id'] == aid


def test_images_cannot_be_user_quotes_or_canonical_fields(images):
    _, _, _, service, snapshot, plan = analyzed(images)
    batch = MessageIntake(service.store).claim('s', 'alice')
    # Existing batch has a live lease; use its original reservation from helper.
    assert batch is None
    ref = plan.decisions[0].evidence[0]
    with pytest.raises(ValueError):
        type(ref).model_validate({**ref.model_dump(), 'quote': '申请退款'})
    from app.agents.association import Detail
    plan.decisions[0].details = [Detail(field='order_id', value='ORD-123', evidence=ref)]
    with service.store.transaction() as tx:
        with pytest.raises(AssociationRejected):
            service._quote(tx, ref, {('s', ref.sequence): ''}, user_only=True)


def test_failed_analysis_falls_back_without_inventing_text(images):
    store, media, worker, vision = images
    aid = upload(media)
    send(store, aid)
    vision.observe = lambda *args, **kw: (_ for _ in ()).throw(SupportError('unconfigured', 503))
    worker.run(worker.claim())
    batch = MessageIntake(store).claim('s', 'alice')
    service = AssociationService(store, 'alice')
    snapshot = service.snapshot(batch)
    assert snapshot.context.current_messages[0]['content'] == ''
    assert snapshot.context.current_messages[0]['visual_context'][0]['status'] == 'failed'
    result = service.commit(batch, snapshot, service._fallback(batch, snapshot))
    assert result['clarification']


def test_request_hash_includes_ordered_attachments(images):
    store, media, _, _ = images
    first, second = upload(media), upload(media)
    args = dict(request_id='same', content='查看', attachment_ids=[first, second])
    intake = MessageIntake(store)
    result = intake.receive('s', 'alice', **args)
    assert intake.receive('s', 'alice', **args) == result
    with pytest.raises(SupportError):
        intake.receive('s', 'alice', **dict(args, attachment_ids=[second, first]))


def test_stale_lease_cannot_commit_and_expired_queue_is_terminal(images):
    store, media, worker, _ = images
    aid = upload(media)
    send(store, aid)
    old = worker.claim()
    with store.transaction() as tx:
        tx.execute('UPDATE image_jobs SET lease_until=0')
    new = worker.claim()
    worker.run(old)
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM image_observations')['n'] == 0
    worker.run(new)
    send(store, aid)
    with store.transaction() as tx:
        tx.execute("UPDATE image_jobs SET deadline_at=0 WHERE status='pending'")
    assert worker.claim() is None
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT error_code FROM image_jobs WHERE status='failed'")['error_code'] == 'deadline_exceeded'


def test_inspection_cache_is_question_detail_model_and_task_scoped(images):
    store, media, _, vision = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    tid = service.commit(batch, snapshot, plan)['decisions'][0]['task_id']
    calls = []
    def inspect(question='裂纹在哪里', detail='full'):
        return media.inspect('alice', tid, aid, question, detail, before_call=lambda: calls.append(1),
                             check=lambda: None, timeout=5)
    first = inspect()
    assert inspect()['observation_id'] == first['observation_id']
    assert len(calls) == 1 and vision.calls == 2
    assert inspect('文字写了什么')['observation_id'] != first['observation_id']
    assert inspect(detail='brief')['observation_id'] != first['observation_id']
    vision.revision = 'different-model'
    assert inspect()['observation_id'] != first['observation_id']
    with pytest.raises(SupportError):
        media.inspect('alice', 'unrelated-task', aid, '问题', 'brief', before_call=lambda: pytest.fail('called model'),
                      check=lambda: None, timeout=5)


def test_revoke_invalidates_observations_reply_stamp_and_late_work(images):
    store, media, worker, _ = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    tid = service.commit(batch, snapshot, plan)['decisions'][0]['task_id']
    guard = ProvenanceGuard(store, 'alice')
    stamp = guard.capture([tid])
    send(store, aid)
    late = worker.claim()
    media.revoke('alice', aid)
    assert not guard.valid(stamp)
    worker.run(late)
    with store.transaction(read_only=True) as tx:
        assert task_images(tx, 'alice', tid) == []
        assert tx.one("SELECT COUNT(*) AS n FROM image_observations WHERE status='valid'")['n'] == 0
    with pytest.raises(SupportError):
        media.content('alice', aid)
    media.cleanup()
    assert not media.path(aid).exists()


def test_retry_is_supplemental_and_preserves_original_snapshot(images):
    store, _, _, _ = images
    aid, receipt, batch, service, snapshot, plan = analyzed(images)
    service.commit(batch, snapshot, plan)
    with store.transaction(read_only=True) as tx:
        before = message_images(tx, 'alice', 's', receipt['sequence'])
    retry_args = dict(request_id='retry', content='', attachment_ids=[aid], retry_source_sequence=receipt['sequence'])
    retry = MessageIntake(store).receive('s', 'alice', **retry_args)
    assert retry['sequence'] > receipt['sequence']
    assert MessageIntake(store).receive('s', 'alice', **retry_args) == retry
    with store.transaction(read_only=True) as tx:
        assert message_images(tx, 'alice', 's', receipt['sequence']) == before
        assert tx.one("SELECT COUNT(*) AS n FROM image_jobs WHERE status='pending'")['n'] == 1
        pending = json.loads(tx.one("SELECT input_snapshot FROM image_jobs WHERE status='pending'")['input_snapshot'])
        assert pending['context_sources'][0] == dict(session_id='s', sequence=retry['sequence'], text='')
        assert pending['context_sources'][1]['sequence'] == receipt['sequence']


def test_revoke_during_association_rejects_stale_observation(images):
    _, media, _, _ = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    media.revoke('alice', aid)
    with pytest.raises(AssociationRejected, match='image_context_changed'):
        service.commit(batch, snapshot, plan)
    fresh = service.snapshot(batch)
    assert service.commit(batch, fresh, service._fallback(batch, fresh))['clarification']


def test_real_tool_dispatch_counts_vision_and_publishes_attributed_reply(images, monkeypatch):
    from tests.test_task_execution import Client, engine, request, response
    from app.tasks.replies import ReplyCoordinator
    store, media, _, vision = images
    aid, receipt, batch, service, snapshot, plan = analyzed(images)
    tid = service.commit(batch, snapshot, plan)['decisions'][0]['task_id']
    record = service.repo.get(tid)
    monkeypatch.setattr('app.media.service.MediaService', lambda store: media)
    client = Client(response(calls=[('inspect_image', {'attachment_id': aid, 'question': '裂纹在哪里'})]), response('图片有裂纹'))
    result = engine(client).execute_task(store, 'alice', request(record, dict(kind='message', session_id='s', sequence=receipt['sequence'])))
    assert result['state'] == 'ready'
    assert result['model_calls'] == 3 and result['tool_calls'] == 1 and vision.calls == 2
    assert 'base64' not in json.dumps(client.requests)
    reply = ReplyCoordinator(store, 'alice').publish('s')
    assert '图片观察：' in reply['content'] and '仍需核实' in reply['content']
    assert any(eid.startswith('image:') for eid in reply['evidence_ids'])


def test_revoke_after_execution_blocks_publication_and_history(images, monkeypatch):
    from tests.test_task_execution import Client, engine, request, response
    from app.tasks.execution_store import TaskRuns
    from app.tasks.replies import ReplyCoordinator
    store, media, _, _ = images
    aid, receipt, batch, service, snapshot, plan = analyzed(images)
    tid = service.commit(batch, snapshot, plan)['decisions'][0]['task_id']
    record = service.repo.get(tid)
    monkeypatch.setattr('app.media.service.MediaService', lambda store: media)
    client = Client(response(calls=[('inspect_image', {'attachment_id': aid, 'question': '裂纹在哪里'})]), response('图片有裂纹'))
    engine(client).execute_task(store, 'alice', request(record, dict(kind='message', session_id='s', sequence=receipt['sequence'])))
    assert TaskRuns(store, 'alice').evidence(record)
    media.revoke('alice', aid)
    assert TaskRuns(store, 'alice').evidence(service.repo.get(tid)) == []
    assert ReplyCoordinator(store, 'alice').publish('s') is None


def test_expiration_fences_without_waiting_for_cleanup(images):
    store, _, _, _ = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    tid = service.commit(batch, snapshot, plan)['decisions'][0]['task_id']
    guard = ProvenanceGuard(store, 'alice')
    stamp = guard.capture([tid])
    with store.transaction() as tx:
        tx.execute('UPDATE media_assets SET expires_at=0 WHERE attachment_id=?', (aid,))
    assert not guard.valid(stamp)


def test_changed_task_context_does_not_reuse_old_inspection(images):
    store, media, _, vision = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    tid = service.commit(batch, snapshot, plan)['decisions'][0]['task_id']
    def inspect():
        return media.inspect('alice', tid, aid, '请解释这里', 'brief', before_call=lambda: None, check=lambda: None, timeout=5)
    first = inspect()
    with store.transaction() as tx:
        tx.execute('UPDATE conversation_tasks SET version=version+1 WHERE task_id=?', (tid,))
    assert inspect()['observation_id'] != first['observation_id']


def test_image_clarification_can_be_answered_with_text(images):
    from app.agents.association import complete_quoted_clarification_sources
    from app.tasks.replies import ReplyCoordinator
    store, media, worker, vision = images
    aid = upload(media)
    first = send(store, aid)
    vision.observe = lambda *args, **kw: (_ for _ in ()).throw(SupportError('unavailable'))
    worker.run(worker.claim())
    intake, service = MessageIntake(store), AssociationService(store, 'alice')
    batch = intake.claim('s', 'alice')
    snapshot = service.snapshot(batch)
    result = service.commit(batch, snapshot, service._fallback(batch, snapshot))
    ReplyCoordinator(store, 'alice').publish('s')
    second = intake.receive('s', 'alice', request_id='answer', content='杯子破了，想咨询能否换货')
    batch = intake.claim('s', 'alice')
    snapshot = service.snapshot(batch)
    assert snapshot.context.clarifications[0]['sources'][0]['visual_context'][0]['attachment_id'] == aid
    proposal = AssociationProposal.model_validate({'decisions': [dict(action='create', effect='new', goal='咨询杯子换货',
        sequences=[second['sequence']], clarification_id=result['clarification']['clarification_id'], evidence=[
            dict(session_id='s', sequence=first['sequence'], kind='attachment_ref', attachment_id=aid),
            dict(session_id='s', sequence=second['sequence'], quote='杯子破了，想咨询能否换货')])]})
    proposal = complete_quoted_clarification_sources(proposal, snapshot.context)
    assert first['sequence'] in proposal.decisions[0].sequences
    assert service.commit(batch, snapshot, proposal)['decisions'][0]['action'] == 'create'


def test_visual_content_is_not_exported_to_diagnostic_logs():
    from app.observability.interactions import sanitize
    from app.observability.audit import mask_sensitive
    observation = dict(source_type='image_observation', attachment_id='a', observation_id='o',
                       result={'visible_text': '某小区详细门牌', 'summary': '某小区详细门牌'})
    for redact in (sanitize, mask_sensitive):
        assert '某小区' not in json.dumps(redact(observation), ensure_ascii=False)
        assert '某小区' not in redact(json.dumps(observation, ensure_ascii=False))
        assert redact(observation)['observation_id'] == 'o'


def test_correction_moves_only_the_image_task_relation(images):
    from app.tasks.correction import CorrectionService
    store, media, _, _ = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    decision = service.commit(batch, snapshot, plan)['decisions'][0]
    intake = MessageIntake(store)
    target_source = intake.receive('next', 'alice', request_id='target', content='另一只杯子')
    target = service.repo.create('next', change_id='target', goal='另一只杯子', details={},
        source=dict(kind='message', session_id='next', sequence=target_source['sequence']))
    correction_source = intake.receive('s', 'alice', request_id='correction', content='刚才图片属于另一只杯子')
    CorrectionService(store, 'alice').correct('s', dict(correction_id='move-image', decision_id=decision['decision_id'],
        source=dict(kind='message', session_id='s', sequence=correction_source['sequence']), reason='user_correction',
        target_task_id=target.task_id, expected_versions={decision['task_id']: 1, target.task_id: target.version}))
    with store.transaction(read_only=True) as tx:
        assert task_images(tx, 'alice', decision['task_id']) == []
        assert task_images(tx, 'alice', target.task_id)[0]['attachment_id'] == aid
    assert media.content('alice', aid)[0]


def test_working_context_budget_keeps_image_reference(images, monkeypatch):
    store, _, _, _ = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    tid = service.commit(batch, snapshot, plan)['decisions'][0]['task_id']
    monkeypatch.setattr(settings, 'media_context_tokens', 500)
    with store.transaction() as tx:
        row = tx.one('SELECT observation_id,result FROM image_observations')
        large = dict(json.loads(row['result']), summary='描述' * 1000)
        tx.execute('UPDATE image_observations SET result=? WHERE observation_id=?', (json.dumps(large), row['observation_id']))
        view = task_images(tx, 'alice', tid)
        assert view[0]['attachment_id'] == aid
        assert view[0]['observations'] == [] and view[0]['observation_refs'] == [row['observation_id']]


def test_deadlines_advance_when_all_image_threads_are_busy(images):
    from concurrent.futures import Future
    store, media, worker, _ = images
    aid = upload(media)
    send(store, aid)
    with store.transaction() as tx:
        tx.execute('UPDATE image_jobs SET deadline_at=0')
    worker.futures = {Future() for _ in range(settings.media_concurrency)}
    worker.tick()
    assert MessageIntake(store).claim('s', 'alice') is not None
    worker.futures.clear()


def test_legacy_text_request_hash_remains_retryable(images):
    from app.tasks.repository import TaskRepository
    store, *_ = images
    receipt = MessageIntake(store).receive('s', 'alice', request_id='legacy', content='你好')
    previous_hash = TaskRepository._fingerprint(dict(session_id='s', request_id='legacy', content='你好',
                                                     action=None, task_id=None, task_version=None))
    with store.transaction(read_only=True) as tx:
        row = tx.one("SELECT request_hash FROM message_intake_items WHERE request_id='legacy'")
        assert row['request_hash'] == previous_hash
    assert MessageIntake(store).receive('s', 'alice', request_id='legacy', content='你好', attachment_ids=[]) == receipt

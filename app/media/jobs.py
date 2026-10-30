# 文件说明：管理持久图片识别队列、租约领取和事务外识别后的受控提交。
"""Separate durable image queue: calls outside transactions, leased fenced commits."""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.config.settings import settings
from app.media.service import MediaService, asset
from app.media.vision import PROMPT_VERSION, digest
from app.runtime.outbox import clock
from app.support.store import SupportError
from app.tasks.provenance import lock_input_user

log = logging.getLogger(__name__)


def bind_message(tx, user, sid, sequence, ids, text, *, retry_source=None):
    for ordinal, aid in enumerate(ids):
        row = asset(tx, user, aid)
        if row['origin_session_id'] != sid:
            raise SupportError('图片不属于当前会话', 422)
        tx.execute('INSERT INTO message_attachments(user_id,session_id,sequence,attachment_id,ordinal) VALUES (?,?,?,?,?)',
                   (user, sid, sequence, aid, ordinal))
        variant = json.loads(row['variants'])['model']
        from app.media.vision import VisionClient
        sources = [dict(session_id=sid, sequence=sequence, text=text)]
        question = text
        if retry_source:
            sources.append(retry_source)
            question = '重新观察图片；原消息问题（不代表新的授权）：' + retry_source['text']
        snapshot = dict(attachment_id=aid, generation=row['generation'], user_id=user, task_id=None,
                        input_hash=variant['sha256'], purpose='retry' if retry_source else 'initial', detail='brief',
                        question='描述图片类型、可见文字及与本条用户问题相关的线索。用户问题：' + question,
                        context_sources=sources,
                        model_revision=VisionClient().revision, prompt_version=PROMPT_VERSION)
        snapshot['context_digest'] = digest(snapshot['context_sources'])
        snapshot['reuse_key'] = digest(snapshot)
        tx.execute('''INSERT INTO image_jobs
            (job_id,user_id,session_id,sequence,attachment_id,generation,request_key,input_snapshot,deadline_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (str(uuid4()), user, sid, sequence, aid, row['generation'], digest([sid, sequence, aid]),
             json.dumps(snapshot, ensure_ascii=False), clock(tx) + settings.media_job_deadline_seconds))


class ImageWorker:
    def __init__(self, store, *, media=None):
        self.store = store
        self.media = media or MediaService(store)
        self.pool = ThreadPoolExecutor(settings.media_concurrency, thread_name_prefix='image-analysis')
        self.futures = set()
        self.cleanup_at = 0

    def claim(self):
        with self.store.transaction() as tx:
            now = clock(tx)
            tx.execute("""UPDATE image_jobs SET status='failed',error_code='deadline_exceeded'
                WHERE status IN ('pending','running','retrying') AND deadline_at<=?""", (now,))
            row = tx.one("""SELECT * FROM image_jobs WHERE deadline_at>? AND next_attempt_at<=?
                AND (status IN ('pending','retrying') OR (status='running' AND lease_until<=?))
                ORDER BY deadline_at LIMIT 1""" + (' FOR UPDATE SKIP LOCKED' if tx.postgres else ''), (now, now, now))
            if not row:
                return None
            if row['attempts'] >= 3:
                tx.execute("UPDATE image_jobs SET status='failed',error_code='attempts_exhausted' WHERE job_id=?", (row['job_id'],))
                return None
            owner = str(uuid4())
            lease = min(row['deadline_at'], now + settings.vision_timeout_seconds + 15)
            tx.execute("""UPDATE image_jobs SET status='running',lease_owner=?,lease_until=?,attempts=attempts+1
                WHERE job_id=?""", (owner, lease, row['job_id']))
            return dict(row, lease_owner=owner, lease_until=lease, attempts=row['attempts'] + 1)

    def run(self, job):
        snapshot = json.loads(job['input_snapshot'])
        result, error = None, None
        try:
            # A configuration/model revision change must not mislabel results.
            if (snapshot['model_revision'] != self.media.vision.revision
                    or snapshot['prompt_version'] != PROMPT_VERSION):
                raise SupportError('视觉模型配置已变化', 409)
            with self.store.transaction(read_only=True) as tx:
                remaining = job['deadline_at'] - clock(tx)
            if remaining <= 0:
                raise TimeoutError()
            result = self.media.recognize(snapshot, timeout=min(settings.vision_timeout_seconds, remaining))
        except Exception as exc:
            # Never store provider response bodies, URLs, credentials or base64.
            error = 'unavailable' if isinstance(exc, SupportError) else 'recognition_failed'
        with self.store.transaction() as tx:
            lock_input_user(tx, job['user_id'])
            current = tx.one('SELECT * FROM image_jobs WHERE job_id=?' + (' FOR UPDATE' if tx.postgres else ''),
                             (job['job_id'],))
            now = clock(tx)
            if (current['status'] != 'running' or current['lease_owner'] != job['lease_owner']
                    or current['lease_until'] <= now or current['deadline_at'] <= now):
                return
            try:
                row = asset(tx, job['user_id'], job['attachment_id'])
                if row['generation'] != job['generation']:
                    raise SupportError('图片版本已变化')
            except SupportError:
                tx.execute("UPDATE image_jobs SET status='cancelled',error_code='source_unavailable' WHERE job_id=?", (job['job_id'],))
                return
            if error:
                status = 'failed' if current['attempts'] >= 3 or error == 'unavailable' else 'retrying'
                tx.execute('UPDATE image_jobs SET status=?,error_code=?,next_attempt_at=? WHERE job_id=?',
                           (status, error, now + 2 ** current['attempts'], job['job_id']))
            else:
                observation = self.media.persist(tx, snapshot, result)
                tx.execute("UPDATE image_jobs SET status='succeeded',observation_id=?,error_code=NULL WHERE job_id=?",
                           (observation['observation_id'], job['job_id']))

    def tick(self):
        # A provider can outlive its socket timeout. Deadline advancement must
        # continue even while every image thread is occupied.
        with self.store.transaction() as tx:
            tx.execute("""UPDATE image_jobs SET status='failed',error_code='deadline_exceeded'
                WHERE status IN ('pending','running','retrying') AND deadline_at<=?""", (clock(tx),))
        for future in list(self.futures):
            if future.done():
                self.futures.remove(future)
                try:
                    future.result()
                except Exception:
                    log.exception('Image job transaction failed')
        if time.monotonic() >= self.cleanup_at:
            self.cleanup_at = time.monotonic() + 60
            try:
                self.media.cleanup()
            except OSError:
                log.warning('Image file cleanup deferred')
        while len(self.futures) < settings.media_concurrency:
            job = self.claim()
            if not job:
                break
            self.futures.add(self.pool.submit(self.run, job))

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)

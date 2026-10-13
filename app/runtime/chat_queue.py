"""Bounded Redis queue with atomic admission, claim and completion.

One admitted run per session (409 on overlap); request IDs deduplicate retries.
Expired queued jobs are safe to discard. Interrupted running jobs are NOT retried:
a tool may already have changed an order. Their session reservation stays locked
until the original worker completes or an operator verifies the business result.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from hashlib import sha256
from uuid import uuid4

from .concurrency import CoordinationUnavailable, redis_client

logger = logging.getLogger(__name__)


class SessionBusy(RuntimeError):
    pass


class RequestConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatJob:
    job_id: str
    user_id: str
    session_id: str
    message: str
    confirmation_token: str | None = None
    control_version: int | None = None

    def to_dict(self):
        return asdict(self)


class ChatQueue:
    _SUBMIT = """
    if redis.call('EXISTS', KEYS[2]) == 1 then
      if redis.call('HGET', KEYS[2], 'fingerprint') ~= ARGV[2] then return -3 end
      return 0
    end
    if redis.call('EXISTS', KEYS[3]) == 1 then return -2 end
    if redis.call('LLEN', KEYS[1]) >= tonumber(ARGV[3]) then return -1 end
    local t = redis.call('TIME')
    local now = tonumber(t[1])
    redis.call('HSET', KEYS[2], 'payload', ARGV[1], 'fingerprint', ARGV[2],
      'job_id', ARGV[4], 'user_id', ARGV[5], 'session_id', ARGV[6],
      'session_key', KEYS[3], 'status', 'queued')
    redis.call('SET', KEYS[3], ARGV[4])
    redis.call('LPUSH', KEYS[1], ARGV[4])
    redis.call('ZADD', KEYS[4], now + ARGV[7], ARGV[4])
    return 1
    """
    _CLAIM = """
    local id = redis.call('RPOP', KEYS[1])
    if not id then return nil end
    local key = ARGV[1] .. ':job:' .. id
    if redis.call('HGET', key, 'status') ~= 'queued' then return nil end
    local t = redis.call('TIME')
    redis.call('HSET', key, 'status', 'running', 'owner', ARGV[2])
    redis.call('ZADD', KEYS[2], tonumber(t[1]) + ARGV[3], id)
    return redis.call('HGET', key, 'payload')
    """
    _FINISH = """
    local state = redis.call('HGET', KEYS[1], 'status')
    if state ~= 'running' or
       redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] then return 0 end
    redis.call('HSET', KEYS[1], 'status', ARGV[2], 'result', ARGV[3], 'error', ARGV[4])
    redis.call('HDEL', KEYS[1], 'payload')
    redis.call('EXPIRE', KEYS[1], ARGV[5])
    redis.call('ZREM', KEYS[2], ARGV[6])
    local skey = redis.call('HGET', KEYS[1], 'session_key')
    if ARGV[2] ~= 'interrupted' and redis.call('GET', skey) == ARGV[6] then
      redis.call('DEL', skey)
    end
    return 1
    """
    _REAP = """
    local t = redis.call('TIME')
    local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', t[1], 'LIMIT', 0, 100)
    for _, id in ipairs(ids) do
      local key = ARGV[1] .. ':job:' .. id
      local state = redis.call('HGET', key, 'status')
      if state == 'queued' then
        redis.call('HSET', key, 'status', 'expired', 'error', '排队超时，任务未执行')
        redis.call('LREM', KEYS[2], 1, id)
        local skey = redis.call('HGET', key, 'session_key')
        if redis.call('GET', skey) == id then redis.call('DEL', skey) end
        redis.call('HDEL', key, 'payload')
        redis.call('EXPIRE', key, ARGV[2])
      elseif state == 'running' then
        redis.call('HSET', key, 'status', 'interrupted',
          'error', '任务执行超时或 Worker 中断，结果未知，请联系人工核实，勿重复提交')
      end
      redis.call('ZREM', KEYS[1], id)
    end
    return #ids
    """
    _UNLOCK = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
    return 0
    """
    _RETIRE = """
    local skey = redis.call('HGET', KEYS[1], 'session_key')
    if skey and redis.call('GET', skey) == ARGV[1] then redis.call('DEL', skey) end
    redis.call('LREM', KEYS[2], 1, ARGV[1])
    redis.call('ZREM', KEYS[3], ARGV[1])
    if redis.call('EXISTS', KEYS[1]) == 1 then
      redis.call('HSET', KEYS[1], 'status', 'retired')
      redis.call('HDEL', KEYS[1], 'payload')
      redis.call('EXPIRE', KEYS[1], ARGV[2])
    end
    return 1
    """
    _RESERVE = """
    local current = redis.call('GET', KEYS[1])
    if current then
      if string.sub(current, 1, 8) == 'control:' then return 0 end
      local state = redis.call('HGET', ARGV[1] .. ':job:' .. current, 'status')
      if state ~= 'interrupted' and state ~= 'expired' and state ~= 'failed' and state ~= 'completed' then
        return 0
      end
      redis.call('DEL', KEYS[1])
    end
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
    """

    def __init__(self, redis_url, *, prefix='ecom:chat', ttl=3600, max_depth=1000,
                 queue_timeout=120, run_timeout=600):
        if not redis_url:
            raise ValueError('异步队列必须配置 API_CONCURRENCY_REDIS_URL')
        self.prefix, self.ttl, self.max_depth = prefix, ttl, max_depth
        self.queue_timeout, self.run_timeout = queue_timeout, run_timeout
        self._redis = redis_client(redis_url)

    @property
    def queue_key(self):
        return f'{self.prefix}:queue'

    @property
    def deadlines_key(self):
        return f'{self.prefix}:deadlines'

    def status_key(self, job_id):
        return f'{self.prefix}:job:{job_id}'

    def session_key(self, session_id):
        return f'{self.prefix}:active:{session_id}'

    def _call(self, method, *args, **kwargs):
        try:
            return getattr(self._redis, method)(*args, **kwargs)
        except Exception as exc:
            raise CoordinationUnavailable('任务协调服务暂时不可用') from exc

    def ping(self):
        return self._call('ping')

    def reap(self):
        return self._call('eval', self._REAP, 2, self.deadlines_key, self.queue_key,
                          self.prefix, self.ttl)

    def submit(self, job):
        payload = json.dumps(job.to_dict(), ensure_ascii=False, sort_keys=True)
        result = self._call('eval', self._SUBMIT, 4, self.queue_key,
                            self.status_key(job.job_id), self.session_key(
                                job.session_id if job.control_version is None else f'{job.session_id}:v{job.control_version}'),
                            self.deadlines_key, payload, sha256(payload.encode()).hexdigest(),
                            self.max_depth, job.job_id, job.user_id, job.session_id,
                            self.queue_timeout)
        if result == -1:
            raise OverflowError('客服任务队列已满，请稍后重试')
        if result == -2:
            raise SessionBusy('该会话已有任务，请等待完成后继续')
        if result == -3:
            raise RequestConflict('request_id 已被用于另一条消息')
        return result == 1

    def claim(self, owner):
        payload = self._call('eval', self._CLAIM, 2, self.queue_key, self.deadlines_key,
                             self.prefix, owner, self.run_timeout)
        return ChatJob(**json.loads(payload)) if payload else None

    def finish(self, job, owner, *, result=None, error=None):
        final_status = "interrupted" if error and "结果未知" in error else ("failed" if error else "completed")
        return bool(self._call('eval', self._FINISH, 2, self.status_key(job.job_id),
                               self.deadlines_key, owner,
                               final_status,
                               json.dumps(result, ensure_ascii=False) if result else '',
                               error or '', self.ttl, job.job_id))

    def get(self, job_id):
        data = self._call('hgetall', self.status_key(job_id))
        if not data:
            return None
        data.pop('payload', None)
        data.pop('fingerprint', None)
        data['result'] = json.loads(data['result']) if data.get('result') else None
        data['error'] = data.get('error') or None
        return data

    def retire(self, job_id):
        """Caller must first verify the durable job is terminal."""
        return self._call('eval', self._RETIRE, 3, self.status_key(job_id), self.queue_key,
                          self.deadlines_key, job_id, self.ttl)

    def reserve_control(self, session_id):
        token = 'control:' + uuid4().hex
        # A bounded expiry prevents an API crash from leaving the session locked forever.
        if not self._call('eval', self._RESERVE, 1, self.session_key(session_id),
                          self.prefix, token, self.run_timeout):
            raise SessionBusy('该会话正在处理中，暂时不能重置或关闭')
        return token

    def release_control(self, session_id, token):
        self._call('eval', self._UNLOCK, 1, self.session_key(session_id), token)


class ChatWorker:
    def __init__(self, queue_backend, handler, *, concurrency=1):
        self.queue, self.handler = queue_backend, handler
        self.concurrency = max(1, concurrency)
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._threads:
                return
            self.queue.ping()  # Fail startup rather than silently create local tasks.
            for index in range(self.concurrency):
                thread = threading.Thread(target=self._run, name=f'chat-worker-{index}')
                thread.start()
                self._threads.append(thread)

    def stop(self):
        self._stop.set()
        # Drain running jobs before DB/client shutdown; never abandon daemon threads.
        for thread in self._threads:
            thread.join()
        self._threads.clear()

    def _run(self):
        while not self._stop.is_set():
            owner = uuid4().hex
            try:
                job = self.queue.claim(owner)
                from app.runtime.metrics import RUNTIME_HEARTBEAT
                RUNTIME_HEARTBEAT.labels(role='worker').set_to_current_time()
                if job is None:
                    self._stop.wait(.1)
                    continue
                try:
                    result = self.handler(job)
                    error = None
                except CoordinationUnavailable:
                    logger.exception('Agent job %s lost coordination lease', job.job_id)
                    result, error = None, 'Agent 租约丢失，业务结果未知，请联系人工核实，勿重复提交'
                except Exception:
                    logger.exception('Agent job %s failed', job.job_id)
                    result, error = None, '客服任务执行失败，请查询会话或联系人工核实'
                # A failed result write must never cause a repeated business execution.
                while True:
                    try:
                        finished = self.queue.finish(job, owner, result=result, error=error)
                        if not finished:
                            logger.warning('Agent job %s lost ownership before completion', job.job_id)
                        break
                    except CoordinationUnavailable:
                        if self._stop.wait(1):
                            return  # Reaper records interrupted; never replay the job.
            except CoordinationUnavailable:
                logger.warning('Worker coordination unavailable; retrying')
                self._stop.wait(1)

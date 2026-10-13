"""Atomic hierarchical quotas and separate resource leases.

Tenant membership is server configuration, never a body/header selected by the
caller. Empty Redis URL is explicit local development mode with local buckets.
"""
import json
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from hashlib import sha256
from weakref import WeakValueDictionary

from app.runtime.concurrency import DistributedLease, CoordinationUnavailable, redis_client


class RateExceeded(RuntimeError):
    def __init__(self, retry_after=1):
        self.retry_after = max(1, int(retry_after))
        super().__init__('请求额度已用完，请稍后重试')


class Buckets:
    LUA = """
      local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
      local values={}; local wait=0
      for i,key in ipairs(KEYS) do
        local cap=tonumber(ARGV[(i-1)*3+1]); local rate=tonumber(ARGV[(i-1)*3+2]); local cost=tonumber(ARGV[(i-1)*3+3])
        local prev=redis.call('HMGET',key,'tokens','at')
        local tokens=math.min(cap,tonumber(prev[1] or cap)+math.max(0,now-tonumber(prev[2] or now))*rate)
        values[i]=tokens-cost
        if tokens<cost then wait=math.max(wait,(cost-tokens)/rate) end
      end
      if wait>0 then return math.ceil(wait) end
      for i,key in ipairs(KEYS) do
        redis.call('HSET',key,'tokens',values[i],'at',now)
        redis.call('EXPIRE',key,math.ceil(tonumber(ARGV[(i-1)*3+1])/tonumber(ARGV[(i-1)*3+2]))+60)
      end
      return 0
    """

    def __init__(self, url, prefix):
        self.redis, self.prefix = redis_client(url), prefix
        self.local, self.lock = {}, threading.Lock()

    def consume(self, specs):
        # spec: (dimension, capacity per minute, reserved units)
        if any(cost > cap for _, cap, cost in specs):
            raise RateExceeded(60)
        keys = [self.prefix + ':budget:' + sha256(name.encode()).hexdigest() for name, _, _ in specs]
        args = [value for _, cap, cost in specs for value in (cap, cap / 60, cost)]
        if self.redis:
            try:
                wait = self.redis.eval(self.LUA, len(keys), *keys, *args)
            except Exception as exc:
                raise CoordinationUnavailable('额度服务暂时不可用') from exc
        else:
            with self.lock:
                now = time.monotonic()
                values, wait = {}, 0
                # Local fallback is bounded by expiry, not a permanent identity map.
                self.local = {k: v for k, v in self.local.items() if now - v[1] < 120}
                for key, (_, cap, cost) in zip(keys, specs):
                    tokens, previous = self.local.get(key, (cap, now))
                    available = min(cap, tokens + (now - previous) * cap / 60)
                    wait = max(wait, (cost - available) * 60 / cap)
                    values[key] = (available - cost, now)
                if wait <= 0:
                    self.local.update(values)
        if wait > 0:
            from app.runtime.metrics import RATE_REJECTIONS
            RATE_REJECTIONS.inc()
            raise RateExceeded(wait + 1)


@lru_cache(maxsize=16)
def buckets(url, prefix):
    return Buckets(url, prefix)


def tenant_for(user):
    from app.config.settings import settings
    return settings.api_user_tenants.get(user, 'default')


def admit_request(user, channel='chat'):
    from app.config.settings import settings
    multiplier = 1 if channel == 'chat' else 5
    specs = [
        (f'{channel}:global', settings.api_global_rpm * multiplier, 1),
        (f'{channel}:tenant:{tenant_for(user)}', settings.api_tenant_rpm * multiplier, 1),
        (f'{channel}:user:{user}', settings.api_user_rpm * multiplier, 1),
    ]
    try:
        buckets(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix).consume(specs)
    except CoordinationUnavailable:
        # Keep durable admission and human controls available during broker
        # failure. The database still enforces the global outstanding-job cap.
        # Local emergency rates are deliberately lower; distributed execution
        # leases remain fail-closed and are never downgraded.
        emergency = 10 if channel == 'chat' else 30
        buckets('', settings.api_chat_queue_prefix + ':emergency').consume([
            (name, min(cap, emergency * (6 if i == 0 else 3 if i == 1 else 1)), cost)
            for i, (name, cap, cost) in enumerate(specs)
        ])


@lru_cache(maxsize=512)
def _redis_lease(url, key, limit, seconds):
    return DistributedLease(url, key, limit, lease_seconds=seconds)


_local_leases = WeakValueDictionary()
_local_lease_lock = threading.Lock()


def lease(url, key, limit, seconds):
    if url:
        return _redis_lease(url, key, limit, seconds)
    # Active contexts hold a strong reference. Never evict a live local
    # semaphore, which would silently reset its quota under identity churn.
    identity = (key, limit, seconds)
    with _local_lease_lock:
        resource = _local_leases.get(identity)
        if resource is None:
            resource = DistributedLease('', key, limit, lease_seconds=seconds)
            _local_leases[identity] = resource
        return resource


@contextmanager
def agent_partition(user, lost_event=None):
    from app.config.settings import settings
    tenant = sha256(tenant_for(user).encode()).hexdigest()
    resource = lease(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix + ':agent-tenant:' + tenant,
                     settings.api_tenant_agent_concurrency, settings.api_distributed_lease_seconds)
    with resource.slot(settings.api_queue_wait_seconds, lost_event=lost_event) as acquired:
        if not acquired:
            raise RateExceeded()
        yield


@contextmanager
def model_budget(model, params):
    from app.config.settings import settings
    # Conservative input reservation (UTF-8 bytes + protocol overhead). Set a
    # bounded output before calling the provider; don't refund unknown outcomes.
    input_units = len(json.dumps(params, ensure_ascii=False).encode('utf-8')) + 512
    output_units = int(params.get('max_completion_tokens') or params.get('max_tokens') or settings.model_max_output_tokens)
    key = sha256(model.encode()).hexdigest()
    resource = lease(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix + ':model:' + key,
                     settings.model_max_concurrency, settings.api_distributed_lease_seconds)
    with resource.slot(settings.model_queue_wait_seconds) as acquired:
        if not acquired:
            raise RateExceeded()
        buckets(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix).consume([
            ('model-rpm:' + key, settings.model_rpm, 1),
            ('model-token-budget:' + key, settings.model_token_budget_per_minute, input_units + output_units),
        ])
        yield


@contextmanager
def embedding_budget(model, texts, remote=True):
    from app.config.settings import settings
    key = sha256(model.encode()).hexdigest()
    resource = lease(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix + ':embedding:' + key,
                     settings.embedding_max_concurrency, settings.api_distributed_lease_seconds)
    with resource.slot(settings.model_queue_wait_seconds) as acquired:
        if not acquired:
            raise RateExceeded()
        if remote:
            buckets(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix).consume([
                ('embedding-rpm:' + key, settings.embedding_rpm, 1),
                ('embedding-token-budget:' + key, settings.embedding_token_budget_per_minute,
                 sum(len(text.encode('utf-8')) for text in texts) + 512),
            ])
        yield

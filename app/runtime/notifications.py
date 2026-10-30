# 文件说明：持久登记并发布会话通知，在 API 进程中合并订阅唤醒。
"""Coalesced durable notifications; one Redis subscription per API process.

Notifications carry no message content. They are hints: consumers always read
authorized durable events by cursor and periodically catch up after lost hints.
"""
import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import uuid4

from app.runtime.outbox import clock
from app.runtime.concurrency import redis_client

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runtime_notifications (
        session_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
        owner TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0)""",
)
logger = logging.getLogger(__name__)


def enqueue(tx, session_id, sequence):
    tx.execute('''INSERT INTO runtime_notifications(session_id,sequence) VALUES (?,?)
        ON CONFLICT(session_id) DO UPDATE SET sequence=excluded.sequence''', (session_id, sequence))


class NotificationPublisher:
    def __init__(self, store, redis_url, prefix):
        self.store, self.client, self.channel = store, redis_client(redis_url), prefix + ':events'

    def publish_one(self):
        with self.store.transaction() as tx:
            timestamp = clock(tx)
            row = tx.one('SELECT * FROM runtime_notifications WHERE lease_until<=? LIMIT 1' +
                         (' FOR UPDATE SKIP LOCKED' if tx.postgres else ''), (timestamp,))
            if not row:
                return False
            owner = str(uuid4())
            tx.execute('UPDATE runtime_notifications SET owner=?,lease_until=? WHERE session_id=?',
                       (owner, timestamp + 10, row['session_id']))
        self.client.publish(self.channel, json.dumps({'session_id': row['session_id'], 'sequence': row['sequence']}))
        with self.store.transaction() as tx:
            tx.execute('DELETE FROM runtime_notifications WHERE session_id=? AND sequence=? AND owner=?',
                       (row['session_id'], row['sequence'], owner))
            # More events arrived during publish: leave them immediately eligible.
            tx.execute('UPDATE runtime_notifications SET lease_until=0 WHERE session_id=? AND owner=?', (row['session_id'], owner))
        return True


class NotificationHub:
    def __init__(self, redis_url, prefix, max_connections=2000):
        self.url, self.channel, self.max_connections = redis_url, prefix + ':events', max_connections
        self._listeners = {}
        self._scheduled = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._count = 0

    def wake(self, sid):
        with self._lock:
            listeners = tuple(entry for entry in self._listeners.get(sid, ()) if entry not in self._scheduled)
            self._scheduled.update(listeners)
        for loop, signal in listeners:
            def deliver(entry=(loop, signal)):
                with self._lock:
                    self._scheduled.discard(entry)
                entry[1].set()
            try:
                loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                with self._lock:
                    self._scheduled.discard((loop, signal))

    @asynccontextmanager
    async def subscribe(self, sid):
        from app.support.store import SupportError
        event = asyncio.Event()
        entry = (asyncio.get_running_loop(), event)
        with self._lock:
            if self._stop.is_set():
                if self._thread and self._thread.is_alive():
                    raise SupportError('实时通知正在重启，请稍后重连', 503)
                self._stop = threading.Event()
                self._thread = None
            if self._count >= self.max_connections:
                raise SupportError('实时连接容量已满，请稍后重试', 429)
            self._listeners.setdefault(sid, set()).add(entry)
            self._count += 1
            if self.url and self._thread is None:
                self._thread = threading.Thread(target=self._listen, daemon=True, name='event-notifications')
                self._thread.start()
        try:
            yield event
        finally:
            with self._lock:
                self._listeners[sid].discard(entry)
                self._scheduled.discard(entry)
                if not self._listeners[sid]:
                    del self._listeners[sid]
                self._count -= 1

    def _listen(self):
        client = redis_client(self.url)
        while not self._stop.is_set():
            try:
                with client.pubsub(ignore_subscribe_messages=True) as pubsub:
                    pubsub.subscribe(self.channel)
                    while not self._stop.is_set():
                        item = pubsub.get_message(timeout=1)
                        if item:
                            try:
                                self.wake(json.loads(item['data'])['session_id'])
                            except (ValueError, KeyError, TypeError):
                                logger.warning('Ignoring malformed notification')
            except Exception:
                logger.warning('Notification connection unavailable; cursor catch-up remains enabled')
                self._stop.wait(1)
        client.close()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def connections(self):
        with self._lock:
            return self._count


@lru_cache(maxsize=8)
def _hub(url, prefix, capacity):
    return NotificationHub(url, prefix, capacity)


def get_hub():
    from app.config.settings import settings
    return _hub(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix, settings.api_sse_max_connections)

"""Run durable delivery and recovery independently of API traffic."""
import logging
import signal
import threading

from app.runtime.outbox import OutboxDispatcher
from app.support.runtime import get_store
from app.runtime.chat_queue import ChatQueue
from app.config.settings import settings
from app.runtime.notifications import NotificationPublisher

logger = logging.getLogger(__name__)


def main():
    from app.runtime.metrics import install
    install(settings.runtime_metrics_port, settings.runtime_metrics_host)
    store = get_store()
    store.initialize()
    queue = ChatQueue(settings.api_concurrency_redis_url, prefix=settings.api_chat_queue_prefix,
                      ttl=settings.api_chat_job_ttl_seconds, max_depth=settings.api_chat_queue_max_depth,
                      queue_timeout=settings.api_chat_queue_timeout_seconds,
                      run_timeout=settings.api_chat_run_timeout_seconds)
    dispatcher = OutboxDispatcher(store, queue)
    notifications = NotificationPublisher(store, settings.api_concurrency_redis_url, settings.api_chat_queue_prefix)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            store.jobs.recover()
            queue.reap()
            store.jobs.retire_deliveries(queue)
            for _ in range(100):
                if stop.is_set() or not dispatcher.deliver_one():
                    break
            for _ in range(100):
                if stop.is_set() or not notifications.publish_one():
                    break
            from app.runtime.metrics import RUNTIME_HEARTBEAT
            RUNTIME_HEARTBEAT.labels(role='dispatcher').set_to_current_time()
        except Exception:
            logger.exception('Delivery/recovery iteration failed; durable records retained')
        stop.wait(.5)


if __name__ == '__main__':
    main()

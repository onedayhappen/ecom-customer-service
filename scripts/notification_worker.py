"""Publish durable conversation notifications; never deliver old chat jobs."""
import logging
import signal
import threading

from app.support.runtime import get_store
from app.config.settings import settings
from app.runtime.notifications import NotificationPublisher

logger = logging.getLogger(__name__)


def main():
    from app.runtime.metrics import install
    install(settings.runtime_metrics_port, settings.runtime_metrics_host)
    store = get_store()
    store.initialize()
    notifications = NotificationPublisher(store, settings.api_concurrency_redis_url, settings.api_chat_queue_prefix)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            for _ in range(100):
                if stop.is_set() or not notifications.publish_one():
                    break
            from app.runtime.metrics import RUNTIME_HEARTBEAT
            RUNTIME_HEARTBEAT.labels(role='notifications').set_to_current_time()
        except Exception:
            logger.exception('Notification publication failed; durable records retained')
        stop.wait(.5)


if __name__ == '__main__':
    main()

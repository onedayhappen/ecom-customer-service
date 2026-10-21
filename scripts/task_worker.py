"""Single Agent worker: associate input, advance matters and coordinate replies."""
import logging
import signal
import threading

from app.config.settings import settings
from app.runtime.task_queue import TaskQueue
from app.support.runtime import get_store
from app.tasks.conversation_worker import ConversationWorker


def main():
    if not settings.task_worker_enabled or not settings.task_intake_enabled:
        raise SystemExit("事项 Worker 或消息入口已在配置中关闭")
    from app.runtime.metrics import install, RUNTIME_HEARTBEAT
    install(settings.runtime_metrics_port, settings.runtime_metrics_host)
    from openai import OpenAI
    from app.agents.engine import AgentEngine
    from app.observability.llm_client import LlmClient
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=30, max_retries=0) as client:
        engine = AgentEngine("others", client=LlmClient(raw_client=client, model=settings.deepseek_model_name),
                             model=settings.deepseek_model_name)
        worker = ConversationWorker(get_store(), engine,
            concurrency=settings.api_chat_worker_concurrency,
            queue=TaskQueue(settings.api_concurrency_redis_url, prefix=settings.api_chat_queue_prefix + ":tasks"))
        try:
            while not stop.is_set():
                try:
                    worker.tick()
                    RUNTIME_HEARTBEAT.labels(role='worker').set_to_current_time()
                except Exception:
                    logging.exception('Task iteration failed; durable input and receipts retained')
                stop.wait(.2)
        finally:
            worker.close()


if __name__ == "__main__":
    main()

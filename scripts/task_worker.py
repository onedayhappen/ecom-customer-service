"""Controlled task worker; disabled until explicitly enabled for isolated trials."""
import signal
import threading

from app.config.settings import settings
from app.runtime.task_queue import TaskQueue
from app.support.store import SupportStore
from app.tasks.scheduling import TaskWorker


def main():
    if not settings.task_worker_enabled:
        raise SystemExit("事项 Worker 默认关闭；受控测试需配置 TASK_WORKER_ENABLED=true")
    from openai import OpenAI
    from app.agents.engine import AgentEngine
    from app.observability.llm_client import LlmClient
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=30, max_retries=0) as client:
        engine = AgentEngine("others", client=LlmClient(raw_client=client, model=settings.deepseek_model_name),
                             model=settings.deepseek_model_name)
        worker = TaskWorker(SupportStore(settings.api_session_postgres_dsn), engine,
            concurrency=settings.api_chat_worker_concurrency,
            queue=TaskQueue(settings.api_concurrency_redis_url, prefix=settings.api_chat_queue_prefix + ":tasks"))
        try:
            while not stop.is_set():
                worker.tick()
                stop.wait(.2)
        finally:
            worker.close()


if __name__ == "__main__":
    main()

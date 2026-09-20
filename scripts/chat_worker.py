"""独立 Agent Worker 进程入口。

生产环境可以将 API 容器与 Worker 容器分别部署：

    python -m scripts.chat_worker

Worker 与 API 通过 Redis 队列共享任务和结果；API 只接收请求，不执行慢的
LLM/工具链。没有 Redis 时脚本会启动但不会消费跨实例任务，应视为配置错误。
"""

from __future__ import annotations

import signal
import time

from app.api.main import _chat_queue, _run_chat_job
from app.config.settings import settings
from app.runtime.chat_queue import ChatWorker


def main() -> None:
    redis_url = settings.api_concurrency_redis_url or settings.memory_redis_url
    if not redis_url:
        raise RuntimeError("独立 Worker 必须配置 API_CONCURRENCY_REDIS_URL")
    worker = ChatWorker(
        _chat_queue,
        _run_chat_job,
        concurrency=settings.api_chat_worker_concurrency,
    )
    worker.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_args: worker.stop())
    try:
        while worker._threads:
            time.sleep(1)
    finally:
        worker.stop()


if __name__ == "__main__":
    main()

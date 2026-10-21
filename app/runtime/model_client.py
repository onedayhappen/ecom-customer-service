"""Lazy process-local model client shared by worker and maintenance code."""
import threading
from typing import Any
from app.config.settings import settings

_openai_client: Any | None = None
_openai_client_lock = threading.Lock()

def get_openai_client() -> Any:
    """懒加载进程级 OpenAI 客户端单例（线程安全）。"""
    global _openai_client
    if _openai_client is None:
        with _openai_client_lock:
            if _openai_client is None:
                from openai import OpenAI

                _openai_client = OpenAI(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                    timeout=settings.llm_timeout_seconds,
                    max_retries=0,
                )
                # 业务链日志监控：单例创建时对 completions.create 打一次幂等补丁，
                # 后续所有请求零 patch 开销，仅当线程有活跃 tracer 时采集 LLM 调用。
                if settings.observability_enabled:
                    from app.observability.tracer import patch_client

                    patch_client(_openai_client)
    return _openai_client

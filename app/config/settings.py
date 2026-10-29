from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


_PLACEHOLDER_MARKERS = (
    "your-",
    "your_",
    "replace-with",
    "replace_with",
    "changeme",
    "change-me",
    "placeholder",
    "secret-here",
    "example",
    "<",
    ">",
)


class Settings(BaseSettings):
    """项目配置，从 .env 文件读取"""

    # 兼容旧代码中的 openai_* 字段，但优先读取项目实际使用的
    # DEEPSEEK_* 配置。仅靠 default_factory + os.getenv 无法读取
    # pydantic-settings 加载的 .env 文件，必须显式声明环境变量别名。
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "OPENAI_BASE_URL"),
    )
    model_name: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices("DEEPSEEK_MODEL_NAME", "MODEL_NAME"),
    )
    # 业务 Agent、会话摘要、长期记忆与离线评估共用 DeepSeek 主模型。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_name: str = "deepseek-v4-flash"
    temperature: float = 0.7

    # Dedicated vision endpoint; text-only providers must not receive images.
    vision_api_key: str = ''
    vision_base_url: str = ''
    vision_model: str = ''
    vision_model_revision: str = '1'
    vision_timeout_seconds: float = Field(default=25, ge=1, le=60)
    media_storage_dir: str = 'data/media'
    media_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    media_max_pixels: int = Field(default=20_000_000, ge=1, le=40_000_000)
    media_max_assets_per_user: int = Field(default=500, ge=1)
    media_retention_days: int = Field(default=90, ge=1)
    media_concurrency: int = Field(default=2, ge=1, le=8)
    media_context_tokens: int = Field(default=3000, ge=500, le=12000)
    media_job_deadline_seconds: int = Field(default=120, ge=30, le=600)

    # One task pipeline. Database rollout controls govern users across instances.
    task_intake_enabled: bool = True
    task_worker_enabled: bool = True
    # Optional bounded look-ahead; association remains ordered per session.
    query_rewrite_prefetch: bool = True
    reply_composition_enabled: bool = True
    reply_composition_timeout_seconds: float = Field(default=15, ge=0.05, le=30)
    task_operations_enabled: bool = True
    task_external_progress_enabled: bool = True
    task_external_interval_seconds: float = Field(default=300, ge=1, le=86400)
    task_external_max_attempts: int = Field(default=12, ge=1, le=100)
    task_external_timeout_seconds: float = Field(default=10, ge=0.05, le=30)
    task_global_concurrency: int = Field(default=8, ge=1, le=64)
    task_user_concurrency: int = Field(default=2, ge=1, le=16)

    # ReAct 循环
    max_react_steps: int = 5
    compact_react_enabled: bool = True

    # '*' = all registered names, CSV = explicit allowlist, empty = no tools.
    # Task profiles further narrow this global ceiling.
    tool_allowlist: str = '*'

    @field_validator('tool_allowlist')
    @classmethod
    def validate_tool_allowlist(cls, value):
        from app.tools.selection import parse_allowlist
        parse_allowlist(value)
        return value

    # MCP 配置
    mcp_enabled: bool = False
    mcp_server_url: str = "http://127.0.0.1:9123/mcp"
    # MCP 服务间认证 + 短时用户上下文；两者均不可由模型或前端伪造。
    mcp_service_token: str = ""
    mcp_context_jwt_secret: str = ""
    mcp_context_jwt_algorithm: str = "HS256"
    mcp_context_token_ttl_seconds: int = 300

    # RAG 配置（第5期）
    embedding_provider: str = "local"  # local / openai
    embedding_model: str = "text-embedding-3-small"  # openai provider 时使用
    local_embedding_model_path: str = "model/bge-small-zh-v1.5"
    kb_dir: str = "app/rag/knowledge"
    # Chroma 是唯一的知识库向量存储后端。
    rag_backend: str = "chroma"
    # 内容哈希清单与 Chroma 持久化目录分离，便于增量同步。
    kb_manifest_path: str = "data/rag/kb_manifest.json"
    # ChromaBackend 的持久化目录与 collection 名
    chroma_persist_dir: str = "data/rag/chroma"
    chroma_collection: str = "ecom_kb"
    retrieval_service_url: str = ''
    retrieval_service_token: str = ''
    retrieval_timeout_seconds: float = Field(default=15, gt=0)
    retrieval_global_concurrency: int = Field(default=8, ge=1)
    retrieval_local_wait_seconds: float = Field(default=.1, ge=0)
    embedding_base_url: str = ''
    embedding_api_key: str = ''
    embedding_max_concurrency: int = Field(default=4, ge=1)
    embedding_rpm: int = Field(default=120, ge=1)
    embedding_token_budget_per_minute: int = Field(default=1000000, ge=1)

    # Memory 配置（第7期）
    memory_enabled: bool = True
    memory_engine: str = Field(default='v3', pattern='^(v3|legacy)$')
    memory_dir: str = "data/sessions/memory"
    memory_user_id: str = "default"
    max_ltm_facts: int = 50
    preference_memory_ttl_days: int = 90
    memory_retrieval_enabled: bool = True
    memory_retrieval_top_k: int = 4  # 兼容旧配置，作为语义记忆默认预算。
    semantic_memory_retrieval_top_k: int = 2
    memory_retrieval_min_score: float = 0.35
    # PostgreSQL 是长期记忆的权威主存储；json 仅用于本地离线测试。
    memory_storage_backend: str = "json"
    memory_postgres_dsn: str = "postgresql://postgres:postgres@127.0.0.1:5432/ecom_agent"
    # Redis 作为 cache-aside 高速访问层；留空则只使用主存储。
    memory_redis_url: str = "redis://127.0.0.1:6379/0"
    memory_cache_ttl_seconds: int = 1800
    memory_cache_key_prefix: str = "ecom:ltm"
    memory_context_tokens: int = Field(default=1200, ge=128)
    memory_hot_facts: int = Field(default=20, ge=1, le=200)
    memory_topic_tokens: int = Field(default=300, ge=64)
    memory_hybrid_candidates: int = Field(default=10, ge=1, le=100)
    memory_rrf_k: int = Field(default=60, ge=1)
    memory_timezone: str = "Asia/Shanghai"
    memory_extraction_batch_messages: int = Field(default=32, ge=1, le=128)
    memory_extraction_input_tokens: int = Field(default=7000, ge=512)
    memory_extraction_debounce_seconds: int = Field(default=30, ge=0, le=3600)

    # FastAPI 服务与认证配置
    api_jwt_secret: str = ""
    api_jwt_algorithm: str = "HS256"
    # MySQL 模式下 API_AUTH_USERS 仅用于旧账号一次性导入；无 DSN 时为本地兼容模式。
    api_auth_users: str = ""
    api_auth_backend: str = "auto"
    api_registration_enabled: bool = True
    api_allow_legacy_jwt: bool = False
    # Empty reuses the shared PostgreSQL session DB. SQLite is explicit local/test mode.
    support_database_url: str = ""
    # 登录签发的 JWT 有效期（秒）。
    api_auth_token_ttl_seconds: int = 86400
    # 浏览器登录态使用 HttpOnly Cookie；CSRF Cookie 只用于生成请求头令牌。
    auth_cookie_name: str = "ecom_agent_session"
    csrf_cookie_name: str = "ecom_agent_csrf"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"
    # 订单业务数据源（第15期）：mysql://user:pass@host:port/db。
    # 留空 = 显式本地模式，订单查询走内置 mock 数据（fail-safe，标注 source=mock）；
    # 配置后 MySQL 为权威数据源，查询异常 fail-closed（TRANSIENT_FAILURE），
    # 绝不静默回退 mock，防止演示数据冒充真实订单。
    order_db_dsn: str = ""
    business_data_keys: str = "{}"  # JSON: key version -> base64url AES-256 key
    business_data_key_version: int = 1
    order_db_pool_size: int = 10
    order_db_pool_timeout: float = 5.0
    # 订单查询缓存：留空跳过缓存（Redis 不可用自动降级为 no-op，不阻塞读）。
    order_cache_redis_url: str = "redis://127.0.0.1:6379/0"
    order_cache_ttl_seconds: int = 300
    # 平台白名单（逗号分隔）：platform_code 只允许在列出的值内，防任意字符串路由。
    order_platform_codes: str = "pdd"
    # 共享支持数据库的默认 PostgreSQL 地址；SUPPORT_DATABASE_URL 可覆盖。
    api_session_postgres_dsn: str = "postgresql://postgres:postgres@127.0.0.1:5432/ecom_agent"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 并发与连接池（工程优化）
    # LLM/API 调用并发上限：用信号量限制同时进行的慢调用，避免打爆上游并
    # 防止 FastAPI 线程池被长时间占满导致健康检查等轻请求排队。
    api_max_concurrent_llm: int = 8
    # OpenAI 兼容客户端的单次请求硬超时，避免上游慢请求长期占用并发槽位。
    llm_timeout_seconds: float = 60.0
    # PostgreSQL 连接池：min 预热、max 上限、取连接与建连超时（秒）。
    api_pg_pool_min_size: int = 1
    api_pg_pool_max_size: int = 10
    api_pg_pool_timeout: float = 5.0
    api_pg_connect_timeout: float = 5.0
    # RAG 向量检索并发上限：SentenceTransformer 编码为 CPU 密集操作，
    # 且 Chroma 内部有锁，过高的并发反而互相争抢；2 足够覆盖客服场景。
    api_rag_search_concurrency: int = 2
    # API/Worker 之间共享的 Redis 并发基础设施。生产环境必须配置为高可用 Redis。
    api_concurrency_redis_url: str = ""
    api_distributed_lease_seconds: float = Field(default=90.0, ge=6)
    api_queue_wait_seconds: float = Field(default=30.0, ge=0)
    # 持续消息由事项 Worker 执行，共享容量与通知命名空间。
    api_chat_worker_concurrency: int = Field(default=2, ge=1)
    api_chat_queue_prefix: str = "ecom:chat"
    api_chat_queue_max_depth: int = Field(default=1000, ge=1)
    api_sse_max_connections: int = Field(default=2000, ge=1)
    api_sse_per_user: int = Field(default=5, ge=1)
    api_sse_send_timeout_seconds: float = Field(default=10, gt=0)
    api_sse_catchup_seconds: float = Field(default=15, ge=1, le=60)
    runtime_metrics_port: int = Field(default=0, ge=0, le=65535)
    runtime_metrics_host: str = '127.0.0.1'
    api_user_tenants: dict[str, str] = Field(default_factory=dict)
    api_global_rpm: int = Field(default=1200, ge=1)
    api_tenant_rpm: int = Field(default=600, ge=1)
    api_user_rpm: int = Field(default=60, ge=1)
    api_tenant_agent_concurrency: int = Field(default=4, ge=1)
    model_max_concurrency: int = Field(default=8, ge=1)
    model_rpm: int = Field(default=120, ge=1)
    model_token_budget_per_minute: int = Field(default=1000000, ge=1)
    model_max_output_tokens: int = Field(default=2048, ge=1)
    model_queue_wait_seconds: float = Field(default=3, ge=0)

    # 业务观测：旧按日 trace 保持可读；当前链路写 conversations/ 会话日志。
    observability_enabled: bool = True
    observability_log_dir: str = "logs/traces"
    # One session file, one JSONL row per user-message group / formal reply.
    interaction_trace_enabled: bool = True
    interaction_trace_stale_seconds: int = Field(default=300, ge=30)

    # 可观测性埋点（第12期）：Trace/Span + Prometheus 指标 + 审计日志。
    # 审计：异常 100% 写 agent_audit_log（PostgreSQL），正常流量按抽样率；
    # 完整报文 JSONL 落 audit_log_dir（Loki/ELK 的本地等价物，可换 HTTP sink）。
    observability_trace_enabled: bool = True
    audit_enabled: bool = True
    audit_sample_rate: float = 0.1
    # 空串表示复用 api_session_postgres_dsn（与业务库同库，免额外配置）
    audit_postgres_dsn: str = ""
    audit_log_dir: str = "logs/audit"
    # 统一工具客户端：瞬时失败自动重试次数（默认 0，ReAct 层负责重试策略）
    tool_retries: int = 0


    # 从当前客服事件构造工作记忆与摘要。
    short_term_memory_enabled: bool = True
    short_term_recent_tokens: int = Field(default=4000, ge=256)
    short_term_summary_tokens: int = Field(default=1200, ge=128)
    short_term_recall_tokens: int = Field(default=1200, ge=128)
    short_term_recent_turns: int = Field(default=4, ge=1, le=20)
    short_term_compaction_batch_messages: int = Field(default=64, ge=8, le=256)
    # 最终提供商请求预算包含 system、工具定义、调用参数和结果。
    context_request_budget_enabled: bool = True
    context_request_input_tokens: int = Field(default=24000, ge=512)
    context_model_window_tokens: int = Field(default=0, ge=0)
    context_window_margin_tokens: int = Field(default=1024, ge=0)

    commerce_provider_config: str = "{}"
    commerce_invoice_root: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    def validate_runtime_configuration(self) -> None:
        """拒绝把示例占位符带入运行环境。

        只读和人工服务可以不配置模型 API key，但认证密钥必须存在；启用 MCP
        时则要求服务间认证和上下文签名密钥同时配置。把校验放在 lifespan
        启动阶段，避免服务启动后才在第一次请求时暴露配置错误。
        """
        errors: list[str] = []
        if self.api_max_concurrent_llm < 1:
            errors.append("API_MAX_CONCURRENT_LLM 必须大于 0")

        def check(name: str, label: str, *, required: bool = False) -> None:
            value = str(getattr(self, name, "") or "").strip()
            lowered = value.lower()
            if required and not value:
                errors.append(f"{label} 未配置")
            elif value and any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
                errors.append(f"{label} 仍是示例占位符")

        check("api_jwt_secret", "API_JWT_SECRET", required=True)
        check("openai_api_key", "DEEPSEEK_API_KEY")
        if self.mcp_enabled:
            check("mcp_service_token", "MCP_SERVICE_TOKEN", required=True)
            check("mcp_context_jwt_secret", "MCP_CONTEXT_JWT_SECRET", required=True)
        if errors:
            joined = "；".join(errors)
            raise RuntimeError(f"启动配置校验失败：{joined}。请配置真实密钥")

    @property
    def is_deepseek(self) -> bool:
        return "api.deepseek.com" in self.openai_base_url

    @property
    def chat_completion_options(self) -> dict:
        """提供商特定的 Chat Completions 参数。"""
        if self.is_deepseek:
            # ReAct 当前不持久化 reasoning_content，关闭思考模式以保证工具循环兼容。
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {}


settings = Settings()

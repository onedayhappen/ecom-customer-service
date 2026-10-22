"""审计日志（第12期）：异常 100% 落库，正常流量抽样；摘要进数据库，报文进日志。

职责分工（与 LLM/工具客户端解耦，客户端只负责「上报审计事件」）：

- :class:`AuditEvent`：一条审计记录的统一结构（摘要 + 完整报文两个视角）。
- :class:`PostgresAuditSink`：写 ``agent_audit_log`` 表（PostgreSQL，复用
  ``app.db`` 连接池）。**只存摘要**（脱敏后的入参/出参、耗时、状态、错误），
  避免大报文撑爆数据库。
- :class:`JsonlAuditSink`：完整原始报文（prompt/response、入参/出参）按天追加
  写 JSONL——Loki/ELK 的本地等价物，接口抽象，未来把该 sink 换成
  HTTP 推送即可无缝接 Loki/ELK。
- :class:`AuditLogger`：组合两个 sink + 抽样器。异常事件（status != ok）
  **100% 写入**，正常事件按 ``sample_rate`` 抽样。

可靠性约定：审计是 best-effort 观测，任何写入失败（DB 抖动、磁盘满）
都被吞掉并回源，绝不阻塞或污染客服主链路。
"""

from __future__ import annotations

import json
import os
import random
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional, Protocol

from app.db import ensure_schema_once, get_connection_pool

# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

#: 命中这些 key 的字段值整体打码（保留前 1 后 1 字符）
_SENSITIVE_KEYS = frozenset({
    "tax_number", "title", "sealed", "business_data_keys",
    "phone", "mobile", "tel", "telephone", "name", "address",
    "id_card", "idcard", "identity", "token", "password", "secret",
    "手机号", "电话", "姓名", "地址", "身份证",
})
#: 长文本中直接打码这些敏感值（手机号 / 身份证）
_PHONE_RE = None
_IDCARD_RE = None


def _lazy_regex():
    """延迟编译正则（模块级编译在导入期略重，这里只在首次脱敏时触发）。"""
    import re
    global _PHONE_RE, _IDCARD_RE
    if _PHONE_RE is None:
        _PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
        _IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def mask_value(value: Any, *, whole: bool = False) -> Any:
    """对单个值脱敏。

    ``whole=False``：只打码文本内嵌的手机号/身份证（普通内容保留）。
    ``whole=True``：整体打码——整串是号码则保留前后几位，否则保留首尾
    各 1 字符（用于命中敏感 key 的值，如姓名/地址）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        _lazy_regex()
        # 上游组件可能已经按字段完成一次脱敏；再次按 whole=True 处理会把
        # `138****00` 过度缩成 `1****0`，这里保持已有掩码不变。
        if "*" in value:
            return value
        if whole:
            if _PHONE_RE.fullmatch(value):
                return value[:3] + "****" + value[-2:]
            if _IDCARD_RE.fullmatch(value):
                return value[:6] + "********" + value[-2:]
            return value[:1] + "****" + value[-1:] if len(value) > 2 else "****"
        text = _PHONE_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-2:], value)
        text = _IDCARD_RE.sub(lambda m: m.group(0)[:6] + "********" + m.group(0)[-2:], text)
        return text
    return value


def mask_sensitive(data: Any, *, _key: str = "") -> Any:
    """递归脱敏：key 命中敏感词则整体打码，否则递归结构；文本内嵌号码也打码。

    例::

        mask_sensitive({"order_id": "ORD-1", "phone": "13800138000", "note": "手机13800138000"})
        # → {"order_id": "ORD-1", "phone": "138****00", "note": "手机138****00"}
    """
    if isinstance(data, dict):
        return {
            k: (mask_value(v, whole=True) if k.lower() in _SENSITIVE_KEYS else mask_sensitive(v, _key=k))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [mask_sensitive(item, _key=_key) for item in data]
    return mask_value(data)


# ---------------------------------------------------------------------------
# 事件模型
# ---------------------------------------------------------------------------


@dataclass
class AuditEvent:
    """一条审计事件。summary 入库；raw 是完整报文（仅落 JSONL/日志）。"""

    kind: str                       # llm / tool / fsm / rt
    name: str                       # 场景名或工具名或状态名
    trace_id: str = ""
    session_id: str = ""
    user_id: str = ""
    status: str = "ok"              # ok / error / timeout / blocked
    cost_ms: float = 0.0
    error: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)   # 脱敏摘要（入库）
    raw: dict[str, Any] = field(default_factory=dict)       # 完整报文（落日志）

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "status": self.status,
            "cost_ms": round(self.cost_ms, 1),
            "error": self.error,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Sink 抽象与实现
# ---------------------------------------------------------------------------


class AuditSink(Protocol):
    def write(self, event: AuditEvent) -> None: ...


class PostgresAuditSink:
    """PostgreSQL 审计表：只存脱敏摘要，避免大报文撑爆数据库。

    ``agent_audit_log`` 建表 DDL 由进程级守卫执行一次；连接复用共享连接池。
    未配置 DSN / psycopg 不可用时构造返回 None（由工厂函数处理）。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS agent_audit_log (
        id BIGSERIAL PRIMARY KEY,
        trace_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        event_kind TEXT NOT NULL,
        event_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok',
        cost_ms INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        summary JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        ensure_schema_once(dsn, "agent_audit_log", self._ddl)

    @classmethod
    def _ddl(cls, cur) -> None:
        cur.execute(cls.SCHEMA)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS agent_audit_log_created_idx "
            "ON agent_audit_log (created_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS agent_audit_log_trace_idx "
            "ON agent_audit_log (trace_id)"
        )

    def _connect(self):
        pool = get_connection_pool(self.dsn)
        if pool is not None:
            return pool.connection()
        import psycopg

        return psycopg.connect(self.dsn)

    def write(self, event: AuditEvent) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_audit_log (
                        trace_id, session_id, user_id, event_kind, event_name,
                        status, cost_ms, error, summary
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        event.trace_id, event.session_id, event.user_id,
                        event.kind, event.name, event.status,
                        int(event.cost_ms), event.error,
                        json.dumps(event.summary, ensure_ascii=False),
                    ),
                )
        except Exception:  # noqa: BLE001 —— 审计失败静默，不影响业务
            pass


class JsonlAuditSink:
    """完整原始报文 → 按天 JSONL（Loki/ELK 的本地等价物）。

    写入完整 ``event.raw``（prompt/response、入参/出参），一行一个事件；
    与 ``logs/traces/business-trace-*.jsonl`` 同一套追加写模式，可离线分析。
    """

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self._lock = threading.Lock()

    def _path(self) -> Path:
        day = date.today().isoformat()
        return self.log_dir / f"audit-{day}.jsonl"

    def write(self, event: AuditEvent) -> None:
        try:
            payload = {**event.to_dict(), "raw": event.raw}
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self._path(), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:  # noqa: BLE001 —— 日志盘故障不影响业务
            pass


# ---------------------------------------------------------------------------
# 门面：抽样 + 双 sink
# ---------------------------------------------------------------------------


class AuditLogger:
    """审计门面：异常 100% 写库，正常流量抽样；摘要入库，报文落日志。

    :param sample_rate: 正常事件抽样率（0~1），默认 0.1。
    :param postgres_dsn: PostgreSQL DSN；为空则跳过 DB sink（仅 JSONL）。
    :param log_dir: 完整报文 JSONL 目录；为空则跳过日志 sink。
    """

    def __init__(
        self,
        *,
        sample_rate: float = 0.1,
        postgres_dsn: str = "",
        log_dir: str = "",
    ):
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self._sinks: list[AuditSink] = []
        if postgres_dsn:
            try:
                self._sinks.append(PostgresAuditSink(postgres_dsn))
            except Exception:  # noqa: BLE001 —— 审计不可用不影响主链路
                pass
        if log_dir:
            self._sinks.append(JsonlAuditSink(log_dir))
        self._rand = random.Random()

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    def log(self, event: AuditEvent, *, force: bool = False) -> None:
        """写一条审计事件。

        ``force=True``（异常路径）100% 写入；否则按抽样率决定是否写库——
        注意：**抽样只作用于 DB 摘要**，完整报文 JSONL 始终追加（日志成本低）。
        """
        if not self._sinks:
            return
        write_db = force or self._rand.random() < self.sample_rate
        for sink in self._sinks:
            if isinstance(sink, PostgresAuditSink) and not write_db:
                continue
            try:
                sink.write(event)
            except Exception:  # noqa: BLE001
                continue


#: 进程级审计器单例（API 装配时用 build_audit_logger 覆盖；测试可注入）
_audit_logger: Optional[AuditLogger] = None


def build_audit_logger(
    *,
    sample_rate: float = 0.1,
    postgres_dsn: str = "",
    log_dir: str = "",
) -> AuditLogger:
    """按配置构建审计器。DSN 为空时回退为仅 JSONL，保证永远可用。"""
    return AuditLogger(
        sample_rate=sample_rate,
        postgres_dsn=postgres_dsn,
        log_dir=log_dir,
    )


def set_audit_logger(logger: Optional[AuditLogger]) -> None:
    """装配全局审计器（API lifespan 中调用；None 表示禁用审计）。"""
    global _audit_logger
    _audit_logger = logger


def get_audit_logger() -> Optional[AuditLogger]:
    return _audit_logger


def audit_event(
    kind: str,
    name: str,
    *,
    trace_id: str = "",
    session_id: str = "",
    user_id: str = "",
    status: str = "ok",
    cost_ms: float = 0.0,
    error: str | None = None,
    summary: dict | None = None,
    raw: dict | None = None,
    force: bool = False,
) -> None:
    """便捷函数：组装事件并交给全局审计器（未装配时静默跳过）。"""
    logger = _audit_logger
    if logger is None:
        return
    logger.log(
        AuditEvent(
            kind=kind,
            name=name,
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            status=status,
            cost_ms=cost_ms,
            error=mask_value(error),
            summary=mask_sensitive(summary or {}),
            raw=mask_sensitive(raw or {}),
        ),
        force=force,
    )

"""Case 绑定的确认令牌、幂等执行与审计。

此模块故意不信任客户端传来的 ``confirmed`` 布尔值：生产调用必须提供
由 :meth:`request_confirmation` 签发的短期令牌。令牌只在服务端保存 SHA-256
摘要，并且同时绑定用户、Case、工具名及规范化参数。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from app.memory.case_manager import CaseManager
from app.support.runtime import control_binding, valid_binding

_TTL = timedelta(minutes=10)
#: in_progress 记录超过该时长仍未收敛，视为进程崩溃遗留的"状态未知"，
#: 后续请求不再返回"正在处理中"，而是要求人工核实。
_IN_PROGRESS_STALE_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class MutationOutcome:
    result: dict[str, Any]
    replayed: bool = False


class MutationGuard:
    """在工具实际调用之前执行确认、幂等和审计检查。"""

    def __init__(self, case_manager: CaseManager, session_id: str,
                 in_progress_stale_after: timedelta = _IN_PROGRESS_STALE_AFTER):
        self.case_manager = case_manager
        self.session_id = session_id
        self.in_progress_stale_after = in_progress_stale_after

    @staticmethod
    def _canonical(arguments: dict[str, Any]) -> str:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _key(cls, case_id: str, action: str, arguments: dict[str, Any]) -> str:
        value = f"{case_id}:{action}:{cls._canonical(arguments)}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def request_confirmation(self, intent: str, action: str, arguments: dict[str, Any]) -> str:
        order_id = str(arguments.get("order_id") or "")
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = datetime.now()
        with self.case_manager.mutation_lock:
            with self.case_manager.transaction():
                case = self.case_manager.open_case_for_mutation(intent, order_id)
                pending = {
                    **control_binding(),
                    "intent": intent,
                    "token_hash": token_hash,
                    "action": action,
                    "arguments": self._canonical(arguments),
                    "idempotency_key": self._key(case.case_id, action, arguments),
                    "expires_at": (now + _TTL).isoformat(timespec="seconds"),
                    "used": False,
                }
                case.conversation_state["pending_mutation"] = pending
                self.case_manager.record_audit("confirmation_requested", self.session_id, case.case_id, {
                    "action": action, "arguments": arguments, "expires_at": pending["expires_at"],
                })
        return raw_token

    def peek_confirmation(self, confirmation_token: str) -> dict[str, Any] | None:
        """查找尚未消费的令牌，用于把“确认”轮路由回原确定性动作。"""
        token_hash = hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest()
        with self.case_manager.mutation_lock:
            with self.case_manager.transaction():
                for case in self.case_manager.cases.values():
                    pending = case.conversation_state.get("pending_mutation") or {}
                    try:
                        valid_time = datetime.fromisoformat(str(pending.get("expires_at"))) >= datetime.now()
                    except (TypeError, ValueError):
                        valid_time = False
                    if (
                        secrets.compare_digest(str(pending.get("token_hash", "")), token_hash)
                        and not pending.get("used")
                        and valid_time
                        and valid_binding(pending)
                    ):
                        return {"case_id": case.case_id, **pending, "arguments_dict": json.loads(pending["arguments"])}
        return None

    def execute(
        self,
        intent: str,
        action: str,
        arguments: dict[str, Any],
        confirmation_token: str | None,
        execute: Callable[[str, dict[str, Any]], str | dict],
    ) -> MutationOutcome:
        """验证令牌后至多执行一次；成功重放保存的权威工具结果。"""
        order_id = str(arguments.get("order_id") or "")
        with self.case_manager.mutation_lock:
            with self.case_manager.transaction():
                case = self.case_manager.open_case_for_mutation(intent, order_id)
                case_id = case.case_id
                # 幂等检查先于令牌校验：重复请求必须拿回首次权威结果，
                # 而不是在令牌已被消费后收到"令牌无效"。
                key = self._key(case.case_id, action, arguments)
                records = case.conversation_state.setdefault("idempotency", {})
                record = records.get(key)
                if record and record.get("status") == "succeeded":
                    self.case_manager.record_audit("mutation_replayed", self.session_id, case.case_id, {
                        "action": action, "idempotency_key": key,
                    })
                    return MutationOutcome(dict(record["result"]), replayed=True)
                if record and record.get("status") == "in_progress":
                    if self._is_stale(record):
                        # 进程在工具执行阶段崩溃的遗留记录：状态未知，不允许自动重试
                        self.case_manager.record_audit("mutation_rejected", self.session_id, case.case_id, {
                            "action": action, "reason": "in_progress_stale_unknown_state",
                        })
                        return MutationOutcome({"success": False, "error": "该操作此前执行状态未知，为避免重复执行已锁定，请联系人工客服核实"})
                    return MutationOutcome({"success": False, "error": "该操作正在处理中，请勿重复提交"})
                if record and record.get("status") == "unknown":
                    # 下游超时等状态不明场景：绝不自动重试写操作
                    return MutationOutcome({"success": False, "error": "该操作执行状态未知，为避免重复执行已锁定，请联系人工客服核实"})

                pending = case.conversation_state.get("pending_mutation") or {}
                expected = self._canonical(arguments)
                try:
                    not_expired = datetime.fromisoformat(str(pending.get("expires_at"))) >= datetime.now()
                except (TypeError, ValueError):
                    not_expired = False
                valid = (
                    valid_binding(pending)
                    and
                    confirmation_token
                    and secrets.compare_digest(
                        str(pending.get("token_hash", "")),
                        hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest(),
                    )
                    and pending.get("action") == action
                    and pending.get("arguments") == expected
                    and not pending.get("used")
                    and not_expired
                )
                if not valid:
                    self.case_manager.record_audit("mutation_rejected", self.session_id, case.case_id, {
                        "action": action, "reason": "invalid_or_expired_confirmation",
                    })
                    return MutationOutcome({"success": False, "error": "确认令牌无效、已过期或与当前操作不匹配"})

                pending["used"] = True
                records[key] = {
                    "status": "in_progress", "action": action, "arguments": arguments,
                    "started_at": datetime.now().isoformat(timespec="microseconds"),
                }
                self.case_manager.record_audit("mutation_started", self.session_id, case.case_id, {
                    "action": action, "idempotency_key": key,
                })

        try:
            raw = execute(action, arguments)
            result = json.loads(raw) if isinstance(raw, str) else raw
            result = result if isinstance(result, dict) else {"success": True, "value": result}
        except Exception as exc:  # noqa: BLE001
            # A lost response is not proof the business side effect failed.
            result = {"success": False, "error": f"工具执行出错: {exc}", "error_code": "transient_failure"}

        with self.case_manager.mutation_lock:
            with self.case_manager.transaction():
                case = self.case_manager.cases.get(case_id)
                if case is None:
                    return MutationOutcome({"success": False, "error": "操作状态已丢失，请联系人工客服核实"})
                records = case.conversation_state.setdefault("idempotency", {})
            # 下游瞬时失败（超时/网络）：结果未知而非确定失败——记录为 unknown，
            # 阻止自动重试，由幂等状态查询与人工流程接管。
                if result.get("success") is not True and result.get("error_code") == "transient_failure":
                    final_status = "unknown"
                else:
                    final_status = "succeeded" if result.get("success") else "failed"
                records[key] = {"status": final_status, "result": result}
                self.case_manager.record_audit(
                    {
                        "succeeded": "mutation_succeeded",
                        "failed": "mutation_failed",
                        "unknown": "mutation_unknown",
                    }[final_status],
                    self.session_id, case.case_id,
                    {"action": action, "idempotency_key": key, "success": bool(result.get("success"))},
                )
        return MutationOutcome(result)

    def query_idempotency_status(
        self, intent: str, action: str, arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """查询幂等记录状态（写操作超时后的第一步永远是查状态，而非重试）。

        返回 ``status`` 取值：

        - ``not_found``：从未执行过，重试是安全的；
        - ``in_progress``：正在执行；``stale=True`` 表示疑似进程崩溃遗留；
        - ``succeeded`` / ``failed`` / ``unknown``：终态或未知态，附 ``result``。
        """
        order_id = str(arguments.get("order_id") or "")
        with self.case_manager.mutation_lock:
            with self.case_manager.transaction():
                case = self.case_manager.find_case_for_mutation(intent, order_id)
                if case is None:
                    return {"status": "not_found"}
                key = self._key(case.case_id, action, arguments)
                record = case.conversation_state.get("idempotency", {}).get(key)
                if not record:
                    return {"status": "not_found"}
                status = str(record.get("status", ""))
                if status == "in_progress":
                    return {"status": "in_progress", "stale": self._is_stale(record)}
                return {"status": status, "result": record.get("result")}

    def _is_stale(self, record: dict[str, Any]) -> bool:
        started_at = record.get("started_at")
        if not started_at:
            # 旧版本记录无时间戳：保守视为过期，要求人工核实
            return True
        try:
            started = datetime.fromisoformat(str(started_at))
        except (TypeError, ValueError):
            return True
        return (datetime.now() - started) > self.in_progress_stale_after

"""确定性优先的业务 Case 管理。

Case 保存订单、物流、退款和投诉等跨会话业务事项；它与用户画像事实
同属持久化记忆层，但业务状态只能由权威工具更新。
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from hashlib import sha256
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

from app.memory.file_lock import ProcessFileLock


_ORDER_ID = re.compile(r"\bORD-[A-Za-z0-9-]+\b", re.IGNORECASE)
_INTENT_WORDS = {
    "refund": ("退款", "退货", "换货"),
    "logistics": ("物流", "快递", "发货", "签收", "配送"),
    "complaint": ("投诉", "赔偿", "不满", "举报"),
}


@dataclass
class CaseRecord:
    """一个可跨 session 继续处理的业务事项。"""

    case_id: str
    user_id: str
    intent: str
    order_ids: list[str] = field(default_factory=list)
    entity_verification: dict = field(default_factory=lambda: {"status": "unverified"})
    business_state: dict = field(default_factory=lambda: {"source": "unknown", "value": "unknown"})
    conversation_state: dict = field(default_factory=dict)
    status: str = "open"  # open / resolved / human_owned / closed / archived
    created_at: str = ""
    updated_at: str = ""
    last_active_at: str = ""
    expires_at: str = ""
    archived_at: str = ""


@dataclass
class CaseResolution:
    """Case Resolver 的确定性结果；模型只能据此回答或澄清。"""

    selected: CaseRecord | None = None
    candidates: list[CaseRecord] = field(default_factory=list)
    need_clarification: bool = False
    reason: str = "no_related_case"
    verification_errors: list[str] = field(default_factory=list)


class CaseManager:
    """为单个用户维护 case 索引和不可变事件日志。

    本实现使用 JSON 作为本地存储，并在快照文件旁使用跨进程排他锁、原子替换
    和事件/Case 合并，保证多 worker 不会并发覆盖确认令牌与幂等记录。生产环境
    可将同一结构迁移到 PostgreSQL；cases 保存当前快照，events 保存状态变化。
    """

    def __init__(self, user_id: str, storage_dir: str = "data/sessions/cases"):
        self.user_id = user_id
        # user_id 来自认证主体，仍不将其直接拼进路径，避免意外路径穿越。
        safe_id = sha256(user_id.encode("utf-8")).hexdigest()
        self.path = Path(storage_dir) / f"{safe_id}.json"
        self.lock_path = Path(storage_dir) / f"{safe_id}.lock"
        self.archive_path = Path(storage_dir) / "archive" / f"{safe_id}.jsonl"
        self.cases: dict[str, CaseRecord] = {}
        self.events: list[dict] = []
        self.pending_order_selections: dict[str, dict] = {}
        self._deleted_pending_order_selections: set[str] = set()
        self._lock_depth = 0
        self._file_lock: ProcessFileLock | None = None
        self._baseline_cases: dict[str, dict] = {}
        self._baseline_event_ids: set[str] = set()
        self._baseline_pending: dict[str, dict] = {}
        self.load()
        if self.prune_cases():
            self.save()

    @staticmethod
    def _now() -> str:
        # 微秒时间戳用于跨进程合并时稳定判断“哪个快照更新”。
        return datetime.now().isoformat(timespec="microseconds")

    def load(self) -> None:
        with ProcessFileLock(self.lock_path):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.cases = {
            item["case_id"]: CaseRecord(**item)
            for item in data.get("cases", [])
            if item.get("case_id") and item.get("user_id") == self.user_id
        }
        self.events = [event for event in data.get("events", []) if event.get("user_id") == self.user_id]
        self.pending_order_selections = {
            session_id: value for session_id, value in data.get("pending_order_selections", {}).items()
            if isinstance(value, dict)
        }
        self._capture_baseline()

    def _capture_baseline(self) -> None:
        self._baseline_cases = {
            case_id: asdict(case) for case_id, case in self.cases.items()
        }
        self._baseline_event_ids = {
            str(event.get("event_id")) for event in self.events if event.get("event_id")
        }
        self._baseline_pending = dict(self.pending_order_selections)

    @contextmanager
    def transaction(self):
        """跨进程串行化一次状态读改写；嵌套调用复用同一把锁。"""
        if self._lock_depth:
            yield
            return
        lock = ProcessFileLock(self.lock_path)
        lock.acquire()
        self._file_lock = lock
        self._lock_depth = 1
        try:
            self._refresh_unlocked()
            yield
        finally:
            self._lock_depth = 0
            self._file_lock = None
            lock.release()

    def _refresh_unlocked(self) -> None:
        """读取磁盘最新快照，同时保留当前对象尚未保存的本地修改。"""
        local_cases = {case_id: asdict(case) for case_id, case in self.cases.items()}
        local_events = list(self.events)
        local_pending = dict(self.pending_order_selections)
        baseline_cases = dict(self._baseline_cases)
        baseline_event_ids = set(self._baseline_event_ids)
        baseline_pending = dict(self._baseline_pending)
        deleted_pending = set(self._deleted_pending_order_selections)

        self._load_unlocked()
        disk_cases = {case_id: asdict(case) for case_id, case in self.cases.items()}
        for case_id, local in local_cases.items():
            if baseline_cases.get(case_id) == local:
                continue
            disk = disk_cases.get(case_id, {})
            merged = dict(disk)
            for field_name, value in local.items():
                if value != baseline_cases.get(case_id, {}).get(field_name):
                    if field_name == "conversation_state" and isinstance(value, dict):
                        nested = dict(disk.get(field_name) or {})
                        baseline_nested = baseline_cases.get(case_id, {}).get(field_name) or {}
                        for key, nested_value in value.items():
                            if nested_value != baseline_nested.get(key):
                                if key == "idempotency" and isinstance(nested_value, dict):
                                    records = dict(nested.get(key) or {})
                                    records.update(nested_value)
                                    nested[key] = records
                                else:
                                    nested[key] = nested_value
                        merged[field_name] = nested
                    else:
                        merged[field_name] = value
            self.cases[case_id] = CaseRecord(**merged)

        disk_event_ids = {str(event.get("event_id")) for event in self.events if event.get("event_id")}
        for event in local_events:
            event_id = str(event.get("event_id"))
            if event_id and event_id not in baseline_event_ids and event_id not in disk_event_ids:
                self.events.append(event)

        for session_id, value in local_pending.items():
            if value != baseline_pending.get(session_id):
                self.pending_order_selections[session_id] = value
        for session_id in deleted_pending:
            self.pending_order_selections.pop(session_id, None)

    def save(self) -> None:
        """原子写入，并合并其他 worker 在本次读取后的新增事件/Case。"""
        if self._lock_depth:
            self._save_unlocked()
            return
        with ProcessFileLock(self.lock_path):
            # 普通业务方法可能在旧快照上修改；保存时合并磁盘快照，避免覆盖
            # 另一个 worker 刚刚写入的 Case、事件或确认状态。
            self._save_unlocked(merge_disk=True)

    @staticmethod
    def _time_key(value: str) -> str:
        return str(value or "")

    def _save_unlocked(self, merge_disk: bool = False) -> None:
        disk = None
        if merge_disk and self.path.exists():
            try:
                disk = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                disk = None

        cases = {case.case_id: asdict(case) for case in self.cases.values()}
        events = list(self.events)
        pending = dict(self.pending_order_selections)
        if isinstance(disk, dict) and disk.get("user_id") == self.user_id:
            for item in disk.get("cases", []):
                case_id = item.get("case_id")
                if not case_id:
                    continue
                local = cases.get(case_id)
                if local is None:
                    self.cases[case_id] = CaseRecord(**item)
                    cases[case_id] = item
                elif self._time_key(item.get("updated_at")) > self._time_key(local.get("updated_at")):
                    self.cases[case_id] = CaseRecord(**item)
                    cases[case_id] = item
            by_event = {event.get("event_id"): event for event in disk.get("events", []) if event.get("event_id")}
            by_event.update({event.get("event_id"): event for event in events if event.get("event_id")})
            events = list(by_event.values())
            disk_pending = disk.get("pending_order_selections") or {}
            for key, value in disk_pending.items():
                pending.setdefault(key, value)
        for session_id in self._deleted_pending_order_selections:
            pending.pop(session_id, None)

        self.events = events
        self.pending_order_selections = pending
        payload = {
            "user_id": self.user_id, "cases": [asdict(case) for case in self.cases.values()],
            "events": self.events, "pending_order_selections": self.pending_order_selections,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._deleted_pending_order_selections.clear()
        self._capture_baseline()

    def resolve(
        self, text: str, verify_order: Callable[[str], dict] | None = None,
    ) -> CaseResolution:
        """精确实体优先的 Case Resolver；无法确定时明确要求澄清。"""
        order_ids = sorted({value.upper() for value in _ORDER_ID.findall(text)})
        if order_ids:
            cases: list[CaseRecord] = []
            errors: list[str] = []
            for order_id in order_ids:
                verified = verify_order(order_id) if verify_order else {"success": False}
                if not verified.get("success"):
                    errors.append(verified.get("error", f"订单 {order_id} 未验证"))
                    continue
                intent = self._intent_for_order(text, order_id)
                case = self._find_open_case(order_id, intent) or self._create_case(intent, order_id)
                case.entity_verification = {"status": "verified", "source": "order_service", "verified_at": self._now()}
                self._touch(case)
                cases.append(case)
            self.save()
            return CaseResolution(
                selected=cases[0] if len(cases) == 1 else None,
                candidates=cases,
                need_clarification=not cases or len(cases) > 1,
                reason="exact_order_id" if cases else "entity_verification_failed",
                verification_errors=errors,
            )

        intent = self._intent_of(text)
        candidates = [case for case in self.cases.values() if case.intent == intent and case.status == "open"]
        if len(candidates) == 1:
            return CaseResolution(selected=candidates[0], candidates=candidates, reason="single_active_case")
        if len(candidates) > 1:
            return CaseResolution(candidates=candidates[:3], need_clarification=True, reason="ambiguous_active_cases")
        return CaseResolution()

    def begin_turn(self, session_id: str, text: str, resolution: CaseResolution) -> str:
        """记录 turn 与 Resolver 结果；case 创建只能发生在实体验证之后。"""
        turn_id = f"turn-{uuid4()}"
        for case in resolution.candidates:
            self._touch(case)
            self._event("case_reference", session_id, turn_id, case.case_id, {
                "match": resolution.reason, "order_ids": case.order_ids, "intent": case.intent,
            })
        self._event("user_message", session_id, turn_id, None, {"content": text})
        self.save()
        return turn_id

    def record_tool_result(self, session_id: str, turn_id: str, tool_name: str, raw_result: str) -> None:
        """只接受工具结果更新 business_state；LLM 文本不会改变业务状态。"""
        try:
            result = json.loads(raw_result)
        except (TypeError, json.JSONDecodeError):
            return
        order = result.get("order") or {}
        # 列表结果可能包含多个订单号；不能把第一个订单误绑定成当前 Case。
        # 只有非列表的单实体结果才允许从原文兜底抽取订单号。
        order_id = order.get("order_id") or (
            None if "orders" in result else self._order_id_from_text(raw_result)
        )
        if not order_id:
            # 列表、知识库等工具结果没有单个订单号，但仍是当前用户会话的
            # 权威证据，不能因为无法更新某个 Case 就丢弃；后续总结/追问需要
            # 能从同一会话读取它们。失败结果也保留，便于解释能力不可用。
            self._event("tool_result", session_id, turn_id, None, {
                "tool": tool_name, "order_id": None,
                "success": bool(result.get("success")), "evidence": result,
            })
            self.save()
            return
        if not result.get("success"):
            return
        intent = "logistics" if tool_name == "query_logistics" else "refund" if tool_name == "apply_refund" else "order"
        case = self._find_open_case(order_id, intent) or self._create_case(intent, order_id)
        if tool_name == "query_order" and order.get("status"):
            self._set_business_state(case, "order_service", order["status"])
        elif tool_name == "query_logistics":
            status = (result.get("logistics") or {}).get("status", "available")
            self._set_business_state(case, "logistics_service", status)
        elif tool_name == "apply_refund":
            self._set_business_state(case, "refund_service", "submitted")
        self._event("tool_result", session_id, turn_id, case.case_id, {
            "tool": tool_name, "order_id": order_id, "success": True,
            # 保留用户本人会话的权威业务证据，供后续总结/追问复用；不把
            # assistant 文本当成事实，也不依赖当前轮次是否再次调用工具。
            "evidence": result,
        })
        self.save()

    def recent_tool_evidence(self, session_id: str, limit: int = 20) -> list[dict]:
        """返回当前会话最近的权威工具结果，供证据校验跨轮复用。"""
        evidence: list[dict] = []
        for event in reversed(self.events):
            if event.get("event_type") != "tool_result" or event.get("session_id") != session_id:
                continue
            value = (event.get("payload") or {}).get("evidence")
            if isinstance(value, dict):
                evidence.append(value)
            if len(evidence) >= limit:
                break
        evidence.reverse()
        return evidence

    def record_response(self, session_id: str, turn_id: str, content: str) -> None:
        self._event("assistant_response", session_id, turn_id, None, {"content": content})
        self._trim_events()
        self.save()

    def open_case_for_mutation(self, intent: str, order_id: str) -> CaseRecord:
        """为写操作获取一个稳定 Case；缺订单号不得进入可靠执行链。"""
        if not order_id:
            raise ValueError("写操作必须绑定 order_id")
        case = self._find_open_case(order_id, intent) or self._create_case(intent, order_id)
        self._touch(case)
        return case

    def find_case_for_mutation(self, intent: str, order_id: str) -> CaseRecord | None:
        """只读查找写操作绑定的 Case；不创建、不产生副作用。

        供幂等状态查询使用——查询不应改变业务状态。
        """
        if not order_id:
            return None
        return self._find_open_case(order_id, intent)

    def record_audit(self, event_type: str, session_id: str, case_id: str, payload: dict) -> None:
        """记录可回放的高风险动作审计事件并立即持久化。"""
        self._event(event_type, session_id, "", case_id, payload)
        self.save()

    def request_order_selection(
        self, session_id: str, message: str, intent: str, candidates: list[dict],
    ) -> None:
        """保存多订单澄清上下文；下一轮只能从此候选集明确选择。"""
        normalized = [
            {"order_id": str(item["order_id"]).upper(), "items_summary": item.get("items_summary", ""),
             "status": item.get("status", "")}
            for item in candidates if item.get("order_id")
        ]
        if len(normalized) < 2:
            return
        self.pending_order_selections[session_id] = {
            "message": message, "intent": intent, "candidates": normalized,
            "expires_at": (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds"),
        }
        self._event("order_selection_requested", session_id, "", None, {
            "intent": intent, "candidate_order_ids": [item["order_id"] for item in normalized],
        })
        self.save()

    def consume_order_selection(self, session_id: str, text: str) -> dict | None:
        """消费用户的明确选择，返回原始请求及选定订单；不猜测模糊回答。"""
        pending = self.pending_order_selections.get(session_id)
        if not pending:
            return None
        try:
            expired = datetime.fromisoformat(pending["expires_at"]) < datetime.now()
        except (KeyError, ValueError, TypeError):
            expired = True
        if expired:
            self.pending_order_selections.pop(session_id, None)
            self._deleted_pending_order_selections.add(session_id)
            self._event("order_selection_expired", session_id, "", None, {})
            self.save()
            return None
        candidates = pending.get("candidates") or []
        answer = (text or "").strip()
        selected = next((item for item in candidates if item["order_id"] == answer.upper()), None)
        if selected is None:
            # 用户常把序号和理由一起说（“第二个，就是那个耳机”），不能要求
            # 整句只包含数字；先解析阿拉伯/中文序号，再用候选商品词做校验。
            chinese_numbers = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
            match = re.search(r"(?:第\s*)?([1-9]\d*|[一二两三四五])\s*(?:个|单|笔|件)?", answer)
            if match:
                raw_index = match.group(1)
                index = (int(raw_index) if raw_index.isdigit() else chinese_numbers[raw_index]) - 1
                if 0 <= index < len(candidates):
                    selected = candidates[index]
        if selected is None:
            # 没有序号时，只有一个候选的商品概要与用户话语有明确词项重合才
            # 自动选择；多候选仍返回澄清，避免“那个”触发猜测。
            matches = []
            for item in candidates:
                summary = str(item.get("items_summary") or "")
                terms = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9-]{2,}", summary)
                if any(term in answer for term in terms):
                    matches.append(item)
            if len(matches) == 1:
                selected = matches[0]
        if selected is None:
            return {"invalid": True, "candidates": candidates}
        self.pending_order_selections.pop(session_id, None)
        self._deleted_pending_order_selections.add(session_id)
        self._event("order_selected", session_id, "", None, {
            "order_id": selected["order_id"], "intent": pending["intent"],
        })
        self.save()
        return {"order_id": selected["order_id"], "message": pending["message"], "intent": pending["intent"]}

    def relevant_context(self, query: str, limit: int = 3) -> list[CaseRecord]:
        """当前用户范围内的只读历史线索；精确订单号优先。"""
        order_ids = {value.upper() for value in _ORDER_ID.findall(query)}
        if order_ids:
            return [case for case in self.cases.values() if order_ids.intersection(case.order_ids)][:limit]
        intent = self._intent_of(query)
        return [case for case in self.cases.values() if case.intent == intent and case.status != "closed"][:limit]

    def _find_open_case(self, order_id: str, intent: str) -> CaseRecord | None:
        for case in list(self.cases.values()):
            if order_id in case.order_ids and case.intent == intent and case.status in {"open", "human_owned"}:
                return case
        return None

    def _create_case(self, intent: str, order_id: str) -> CaseRecord:
        now = self._now()
        case = CaseRecord(
            case_id=f"case-{uuid4()}", user_id=self.user_id, intent=intent,
            order_ids=[order_id], created_at=now, updated_at=now, last_active_at=now,
        )
        self.cases[case.case_id] = case
        self._event("case_created", "", "", case.case_id, {"intent": intent, "order_id": order_id})
        return case

    def _set_business_state(self, case: CaseRecord, source: str, value: str) -> None:
        case.business_state = {"source": source, "value": value, "updated_at": self._now()}
        self._touch(case)
        if self._is_terminal_business_state(case):
            case.status = "resolved"
            case.expires_at = (datetime.now() + self._closed_after(case.intent)).isoformat(timespec="seconds")
        self._event("business_state_changed", "", "", case.case_id, dict(case.business_state))

    def prune_cases(self, now: datetime | None = None) -> int:
        """关闭或归档长期不活跃 case，并裁剪热事件日志。"""
        now = now or datetime.now()
        changed = 0
        for case in list(self.cases.values()):
            last_active = self._parse_time(case.last_active_at or case.updated_at or case.created_at) or now
            inactive_for = now - last_active
            if case.status == "open" and inactive_for >= self._inactive_ttl(case.intent):
                case.status = "closed"
                case.expires_at = now.isoformat(timespec="seconds")
                case.updated_at = now.isoformat(timespec="seconds")
                self._event("case_auto_closed", "", "", case.case_id, {"reason": "inactive_ttl"})
                changed += 1
            elif case.status == "resolved" and inactive_for >= self._closed_after(case.intent):
                case.status = "closed"
                case.expires_at = now.isoformat(timespec="seconds")
                case.updated_at = now.isoformat(timespec="seconds")
                self._event("case_auto_closed", "", "", case.case_id, {"reason": "resolved_grace_period"})
                changed += 1
            elif case.status == "closed" and inactive_for >= timedelta(days=90):
                case.status = "archived"
                case.archived_at = now.isoformat(timespec="seconds")
                case.updated_at = case.archived_at
                self._event("case_archived", "", "", case.case_id, {"reason": "closed_retention"})
                self._archive_case(case)
                self._remove_case_from_hot_store(case.case_id)
                changed += 1
        changed += self._trim_events()
        return changed

    def _touch(self, case: CaseRecord) -> None:
        now = self._now()
        case.updated_at = now
        case.last_active_at = now

    def _event(self, event_type: str, session_id: str, turn_id: str, case_id: str | None, payload: dict) -> None:
        self.events.append({
            "event_id": f"event-{uuid4()}", "event_type": event_type, "user_id": self.user_id,
            "session_id": session_id, "turn_id": turn_id, "case_id": case_id,
            "payload": payload, "created_at": self._now(),
        })

    def _trim_events(self, max_hot_events: int = 500) -> int:
        if len(self.events) <= max_hot_events:
            return 0
        key_types = {
            "case_created", "case_reference", "tool_result", "business_state_changed",
            "case_auto_closed", "case_archived",
        }
        key_events = [event for event in self.events if event.get("event_type") in key_types]
        tail = self.events[-max_hot_events:]
        merged = []
        seen = set()
        for event in [*key_events, *tail]:
            event_id = event.get("event_id")
            if event_id in seen:
                continue
            merged.append(event)
            seen.add(event_id)
        removed = len(self.events) - len(merged)
        self.events = merged
        return removed

    def _archive_case(self, case: CaseRecord) -> None:
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "archived_at": self._now(),
            "case": asdict(case),
            "events": [event for event in self.events if event.get("case_id") == case.case_id],
        }
        with self.archive_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _remove_case_from_hot_store(self, case_id: str) -> None:
        self.cases.pop(case_id, None)
        self.events = [
            event for event in self.events
            if event.get("case_id") != case_id
        ]

    @staticmethod
    def _is_terminal_business_state(case: CaseRecord) -> bool:
        value = str(case.business_state.get("value", "")).lower()
        if case.intent == "logistics":
            return value in {"delivered", "signed", "签收", "已签收"}
        if case.intent == "refund":
            return value in {"refund_completed", "completed", "success", "退款成功"}
        return False

    @staticmethod
    def _inactive_ttl(intent: str) -> timedelta:
        days = {
            "logistics": 30,
            "refund": 45,
            "complaint": 60,
        }.get(intent, 30)
        return timedelta(days=days)

    @staticmethod
    def _closed_after(intent: str) -> timedelta:
        days = {
            "logistics": 7,
            "refund": 14,
            "complaint": 30,
        }.get(intent, 14)
        return timedelta(days=days)

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _order_id_from_text(text: str) -> str | None:
        match = _ORDER_ID.search(text)
        return match.group(0).upper() if match else None

    @staticmethod
    def _intent_of(text: str) -> str:
        for intent, words in _INTENT_WORDS.items():
            if any(word in text for word in words):
                return intent
        return "general"

    @classmethod
    def _intent_for_order(cls, text: str, order_id: str) -> str:
        """同一 turn 涉及多个订单时，按订单附近的子句识别意图。"""
        match = re.search(re.escape(order_id), text, flags=re.IGNORECASE)
        if not match:
            return cls._intent_of(text)
        # 标点和连接词是轻量级子句边界，避免把另一订单的退款意图套到物流订单。
        left = max(text.rfind(mark, 0, match.start()) for mark in "，。；、") + 1
        right_positions = [pos for mark in "，。；、" if (pos := text.find(mark, match.end())) != -1]
        right = min(right_positions) if right_positions else len(text)
        return cls._intent_of(text[left:right])

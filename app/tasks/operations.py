"""Durable, task-bound confirmations; no model ever invokes a write directly."""
import json
from types import SimpleNamespace

from app.config.settings import settings
from app.execution.business_operations import BusinessOperations
from app.services.commerce.common import BusinessError
from app.services.commerce.writes import PREPARES
from app.harness.bounded_call import bounded_calls
from app.tasks import confirmation_tokens
from app.runtime.outbox import clock
from app.support.store import SupportError, now
from app.tasks.association import _id
from app.tasks.execution_store import TaskRuns
from app.tasks.provenance import lock_input_user, revision
from app.tools.request_context import set_current_user_id, reset_current_user_id


def backend():
    return BusinessOperations()


def handoff_started_tasks(tx, store, user, sid, task_ids):
    """A changed request cannot undo an already reserved business operation."""
    ids = []
    for tid in sorted(set(task_ids)):
        ids.extend(r["operation_id"] for r in tx.all("""SELECT p.operation_id FROM task_operation_proposals p
            JOIN support_operations o ON o.operation_id=p.operation_id
            WHERE p.user_id=? AND p.task_id=? AND o.status IN ('running','unknown','succeeded')""", (user, tid)))
    if ids:
        control = store._control(tx, sid, user)
        store._handoff(tx, control, "business_change_after_start", "system")
    return bool(ids)


class TaskOperations:
    def __init__(self, store, user, *, scope=None, business=None):
        self.store, self.user = store, user
        self.runs = TaskRuns(store, user, scope=scope)
        self.business = business or backend()
        if isinstance(self.business, BusinessOperations):
            self.business.scope = scope

    def _enabled(self):
        if not settings.task_operations_enabled:
            raise SupportError("事项业务提交入口未启用", 404)

    def _require_business(self):
        if not getattr(self.business, "available", True):
            raise SupportError("业务工具已移除，暂时无法提交操作", 503)

    def _row(self, tx, oid, *, lock=False):
        row = tx.one("""SELECT p.*,o.session_id,o.control_version,o.tool,o.status,o.result
            FROM task_operation_proposals p JOIN support_operations o ON o.operation_id=p.operation_id
            WHERE p.operation_id=? AND p.user_id=?""" + (" FOR UPDATE" if lock and tx.postgres else ""),
            (oid, self.user))
        if not row:
            raise SupportError("操作不存在", 404)
        return row

    def _input_ready(self, tx):
        if tx.one("""SELECT i.request_id FROM message_intake_items i JOIN support_conversations c
            ON c.session_id=i.session_id WHERE i.user_id=? AND i.association_status='pending'
            AND c.mode='bot' LIMIT 1""", (self.user,)):
            raise SupportError("正在判断刚收到的补充信息，请稍后再确认")

    def _session(self, tx, row, sid):
        self.store._control(tx, sid, self.user)
        self.runs._scope(tx, row["task_id"])
        if not tx.one("SELECT task_id FROM task_sessions WHERE task_id=? AND session_id=?", (row["task_id"], sid)):
            raise SupportError("该事项不属于当前会话", 404)

    def _resource_busy(self, tx, row, payload):
        if any(tx.one("SELECT operation_id FROM task_operation_resources WHERE resource_key=? AND operation_id<>?",
                      (resource, row["operation_id"])) for resource in payload["resources"]):
            return True
        # A late receipt may reopen a manually resolved operation while another
        # operation owns the resource. The operation ledger is authoritative
        # even after that newer holder releases its single resource row.
        order = "p.payload::jsonb->'prepared'->'arguments'->>'order_id'" if tx.postgres else "json_extract(p.payload,'$.prepared.arguments.order_id')"
        platform = "p.payload::jsonb->'prepared'->'arguments'->>'platform_code'" if tx.postgres else "json_extract(p.payload,'$.prepared.arguments.platform_code')"
        return bool(tx.one(f"""SELECT p.operation_id FROM task_operation_proposals p
            JOIN support_operations o ON o.operation_id=p.operation_id
            WHERE p.operation_id<>? AND o.status IN ('running','unknown') AND {order}=?
            AND COALESCE({platform},'pdd')=? LIMIT 1""",
            (row["operation_id"], payload["prepared"]["arguments"]["order_id"],payload["prepared"]["arguments"].get('platform_code','pdd'))))

    def _check(self, tx, sid, record, payload=None):
        self.store._control(tx, sid, self.user)
        self.runs._scope(tx, record.task_id)
        self._input_ready(tx)
        if not tx.one("SELECT task_id FROM task_sessions WHERE task_id=? AND session_id=?", (record.task_id, sid)):
            raise SupportError("该事项不属于当前会话", 404)
        if record.status != "in_progress":
            raise SupportError("事项已暂停、取消或正在等待补充，不能提交")
        self.runs._control(tx, SimpleNamespace(session_id=sid, task_id=record.task_id))
        if tx.one("""SELECT clarification_id FROM association_clarifications WHERE user_id=? AND status='open'
            AND (candidates='[]' OR candidates LIKE ?) LIMIT 1""", (self.user, '%' + record.task_id + '%')):
            raise SupportError("事项归属尚需澄清，不能提交")
        if payload:
            self.runs.guard.require_current(payload["provenance"], tx=tx, lock=not tx.read_only)
            if not self.runs.control_versions_valid(tx, payload["controls"]):
                raise SupportError("人工接管或会话控制已变化")
            if payload.get("schedule"):
                from app.tasks.scheduling import dependencies_ready
                dependencies_ready(tx, self.runs, payload["schedule"])

    def _binding(self, row):
        return dict(operation_id=row["operation_id"], user_id=self.user, task_id=row["task_id"],
                    task_version=row["task_version"], payload=row["payload"], expires_at=row["expires_at"])

    def _token_valid(self, row, token):
        try:
            current = confirmation_tokens.bound_token(self._binding(row), settings.api_jwt_secret)
        except ValueError:
            return False
        return confirmation_tokens.token_matches(token, row["token_hash"]) and confirmation_tokens.token_matches(current, row["token_hash"])

    def _view(self, tx, row, sid, *, private=True):
        p = json.loads(row["payload"])
        status, error, token = row["status"], None, None
        review=p['prepared']['review']
        result = json.loads(row["result"]) if row["result"] else None
        if isinstance(result, dict) and "manual_evidence" in result:
            # Administrative notes remain private; a customer sees the outcome.
            result = dict(status=status, verification_source="human")
        if status == "prepared":
            try:
                self._require_business()
                if isinstance(self.business, BusinessOperations) and row["tool"] not in __import__('app.services.commerce.writes', fromlist=['PREPARES']).PREPARES:
                    raise SupportError("此操作契约已移除，请重新准备", 409)
                self.runs._scope(tx, row["task_id"])
                record = self.runs.repo._record(self.runs.repo._row(tx, row["task_id"]))
                self._check(tx, sid, record, p)
                if self._resource_busy(tx, row, p):
                    raise SupportError("同一订单已有执行中或待核实的操作，请先核实原结果")
                if clock(tx) >= row["expires_at"]:
                    raise SupportError("本次确认已过期，请重新核实操作内容")
                token = confirmation_tokens.bound_token(self._binding(row), settings.api_jwt_secret)
                if not confirmation_tokens.token_matches(token, row["token_hash"]):
                    raise SupportError("确认凭证已失效，请重新准备操作")
            except (SupportError, ValueError) as exc:
                token, error = None, str(exc)
        if private and isinstance(self.business,BusinessOperations) and row['tool'] in PREPARES:
            try: review=self.business.review(self.user,p['prepared'])
            except BusinessError as exc: token,error=None,exc.message
        return dict(operation_id=row["operation_id"], task_id=row["task_id"], task_version=row["task_version"],
                    status=status, review=review, can_confirm=bool(token), confirmation_token=token,
                    expires_at=row["expires_at"], error=error, result=result)

    def list(self, sid):
        self._enabled()
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, self.user)
            rows = tx.all("""SELECT p.operation_id FROM task_operation_proposals p
                WHERE p.user_id=? AND EXISTS(SELECT 1 FROM task_sessions s WHERE s.task_id=p.task_id AND s.session_id=?)
                ORDER BY p.created_at DESC LIMIT 20""", (self.user, sid))
            return [self._view(tx, self._row(tx, r["operation_id"]), sid) for r in rows]

    def prepare(self, sid, task_id, expected_version, action, *, arguments=None, fence=None, timeout=15):
        self._enabled()
        self._require_business()
        if isinstance(self.business, BusinessOperations) and action not in __import__('app.services.commerce.writes', fromlist=['PREPARES']).PREPARES:
            raise SupportError("此操作契约已移除，请重新准备", 409)
        from app.services.commerce.common import digest
        suffix = (":" + digest(arguments)) if arguments is not None else ""
        oid = _id(task_id, "operation:" + str(expected_version) + ":" + action + suffix)
        with self.store.transaction(read_only=True) as tx:
            record = self.runs.repo._record(self.runs.repo._row(tx, task_id))
            self._check(tx, sid, record)
            if record.version != expected_version:
                raise SupportError("事项已变化，请重新准备操作")
            old = tx.one("SELECT operation_id FROM task_operation_proposals WHERE operation_id=?", (oid,))
            if old:
                view = self._view(tx, self._row(tx, oid), sid, private=False)
                return {k: v for k, v in view.items() if k != "confirmation_token"}
        identity = set_current_user_id(self.user)
        try:
            prepared = bounded_calls.run(lambda: self.business.preview(record, action, arguments=arguments) if arguments is not None else self.business.preview(record, action), timeout)
        finally:
            reset_current_user_id(identity)
        with self.store.transaction() as tx:
            lock_input_user(tx, self.user)
            control = self.store._control(tx, sid, self.user)
            revision(tx, self.user, lock=True)
            current = self.runs.repo._record(self.runs.repo._row(tx, task_id, lock=True))
            self._check(tx, sid, current)
            if current.version != expected_version:
                raise SupportError("预检期间事项已变化，请重新准备操作")
            if fence:
                self.runs.check(tx, fence[0], fence[1], lock=True)
            old = tx.one("SELECT operation_id FROM task_operation_proposals WHERE operation_id=?", (oid,))
            if old:
                return dict(operation_id=oid, status=self._row(tx, oid)["status"])
            decisions = [r["decision_id"] for r in tx.all("""SELECT d.decision_id FROM association_decisions d
                WHERE d.user_id=? AND d.task_id=? AND NOT EXISTS(SELECT 1 FROM task_invalid_sources i
                    WHERE i.user_id=d.user_id AND i.kind='decision' AND i.source_id=d.decision_id)""", (self.user, task_id))]
            payload = dict(prepared=prepared, provenance=self.runs.guard.capture([task_id], decisions, tx=tx,
                               memory=fence[1]['provenance'].get('memory') if fence else None),
                           controls=self.runs.controls(tx, SimpleNamespace(session_id=sid, task_id=task_id)),
                           resources=[json.dumps(["order", prepared["arguments"].get("platform_code", "pdd"), prepared["arguments"]["order_id"]])])
            if fence and fence[1].get("schedule"):
                payload["schedule"] = fence[1]["schedule"]
            encoded, expiry = json.dumps(payload, ensure_ascii=False), clock(tx) + 600
            row = dict(operation_id=oid, task_id=task_id, task_version=expected_version, payload=encoded, expires_at=expiry)
            try:
                token_hash = confirmation_tokens.token_hash(confirmation_tokens.bound_token(self._binding(row), settings.api_jwt_secret))
            except ValueError:
                raise SupportError("操作确认签名配置不可用", 503) from None
            tx.execute("INSERT INTO support_operations VALUES (?,?,?,?,?,?,?)",
                       (oid, sid, control["control_version"], action, "prepared", now(), None))
            tx.execute("INSERT INTO support_operation_tasks VALUES (?,?)", (oid, task_id))
            tx.execute("INSERT INTO task_operation_proposals VALUES (?,?,?,?,?,?,?,?,?)",
                       (oid, self.user, task_id, expected_version, encoded, token_hash, expiry, None, now()))
            self.store._event(tx, control, "operation_prepared", "system", dict(
                operation_id=oid, task_id=task_id, task_version=expected_version), private=True,
                key="operation-prepared:" + oid)
            return dict(operation_id=oid, task_id=task_id, status="prepared", review=prepared["review"])

    def confirm(self, sid, oid, token):
        self._enabled()
        with self.store.transaction() as tx:
            lock_input_user(tx, self.user)
            control = self.store._control(tx, sid, self.user)
            revision(tx, self.user, lock=True)
            row = self._row(tx, oid, lock=True)
            self._session(tx, row, sid)
            if row["status"] != "prepared":
                return self._view(tx, row, sid)  # Read-only replay, including unknown.
            self._require_business()
            if isinstance(self.business, BusinessOperations) and row["tool"] not in __import__('app.services.commerce.writes', fromlist=['PREPARES']).PREPARES:
                raise SupportError("此操作契约已移除，请重新准备", 409)
            if not self._token_valid(row, token) or clock(tx) >= row["expires_at"]:
                raise SupportError("确认凭证无效或已过期")
            payload = json.loads(row["payload"])
            record = self.runs.repo._record(self.runs.repo._row(tx, row["task_id"], lock=True))
            self._check(tx, sid, record, payload)
            if self._resource_busy(tx, row, payload):
                raise SupportError("同一业务对象已有执行中或待核实的操作")
            for resource in sorted(payload["resources"]):
                if not tx.execute("INSERT INTO task_operation_resources VALUES (?,?) ON CONFLICT DO NOTHING",
                                  (resource, oid)).rowcount:
                    raise SupportError("同一业务对象已有执行中或待核实的操作")
            tx.execute("UPDATE support_operations SET status='running' WHERE operation_id=?", (oid,))
            tx.execute("UPDATE task_operation_proposals SET run_deadline=? WHERE operation_id=?", (clock(tx) + 60, oid))
            positions = tx.all("""SELECT s.session_id,s.received_sequence,s.associated_sequence FROM message_intake_sessions s
                JOIN support_conversations c ON c.session_id=s.session_id WHERE c.user_id=?""", (self.user,))
            self.store._event(tx, control, "operation_started", "system",
                dict(operation_id=oid, task_id=record.task_id, task_version=record.version, message_positions=positions),
                private=True, key="operation-started:" + oid)
        # Do not detach or automatically retry writes. Adapter IO has finite
        # transport timeouts; a crash after reservation is an unknown outcome.
        identity = set_current_user_id(self.user)
        try:
            try:
                p = payload["prepared"]
                result = self.business.write(self.user, row["tool"], p["arguments"], oid, p["business_version"])
            except Exception:
                result = dict(success=False, error_code="transient_failure", error="业务操作结果待核实")
            if not isinstance(result, dict) or (result.get("success") is not True and result.get("error_code") not in {
                    "business_rejection", "validation_error", "confirmation_invalid", "evidence_unavailable"}):
                try:
                    found = bounded_calls.run(lambda: self.business.query(self.user, oid), 10)
                    if isinstance(found, dict) and found.get("status") == "succeeded" and found.get("action") == row["tool"]:
                        result = found.get("result")
                except Exception:
                    pass
        finally:
            reset_current_user_id(identity)
        self.finish(oid, result)
        with self.store.transaction(read_only=True) as tx:
            return self._view(tx, self._row(tx, oid), sid)

    def finish(self, oid, result):
        with self.store.transaction() as tx:
            row = self._row(tx, oid)
            control = self.store._control(tx, row["session_id"], self.user)
            revision(tx, self.user, lock=True)
            row = self._row(tx, oid, lock=True)
            if row["status"] == "unknown" and not isinstance(result, dict):
                return
            if row["status"] not in {"running", "unknown"}:
                previous = json.loads(row["result"] or "{}")
                if "manual_evidence" in previous:
                    key = "late-receipt:" + oid + ":" + self.runs.repo._fingerprint(result)
                    if tx.one("SELECT sequence FROM support_events WHERE session_id=? AND event_key=?", (row["session_id"], key)):
                        return
                    self.store._event(tx, control, "operation_late_receipt", "system",
                        dict(operation_id=oid, task_id=row["task_id"], result=result), private=True,
                        key=key)
                    tx.execute("UPDATE support_operations SET status='unknown',result=? WHERE operation_id=?",
                        (json.dumps(dict(status="unknown", operation_id=oid, error="人工结论与迟到回执需重新核实")), oid))
                    for resource in json.loads(row["payload"])["resources"]:
                        tx.execute("INSERT INTO task_operation_resources VALUES (?,?) ON CONFLICT DO NOTHING", (resource, oid))
                    self.store._handoff(tx, control, "business_receipt_after_manual_review", "system")
                return
            result = result if isinstance(result, dict) else {}
            p = json.loads(row["payload"])["prepared"]
            entity = result.get("invoice") or result
            verified = (result.get("success") is True and result.get("request_id") == oid
                        and isinstance(entity, dict) and entity.get("order_id") == p["arguments"]["order_id"]
                        and entity.get("status") == ({"prepare_after_sale": "submitted", "prepare_invoice": "submitted",
                            "prepare_order_address_change": "updated", "prepare_order_update": "updated",
                            "prepare_order_cancel": "submitted" if p['review'].get('cancellation_mode') else "cancelled",
                            "prepare_after_sale_cancel": "cancelled", "apply_refund": "submitted",
                            "prepare_purchase":"pending_payment", "prepare_return_shipment":"registered",
                            "prepare_price_protection":"submitted", "prepare_exchange":"submitted",
                            "prepare_repair":"submitted", "prepare_invoice_reissue":"submitted",
                            "claim_coupon":"available", "request_dispatch":"queued"}.get(row["tool"], "issued_local"))
                        and (row['tool'] not in PREPARES or entity.get("platform_code") == p["arguments"]["platform_code"]))
            status = ("succeeded" if verified else "failed" if result.get("success") is False and
                      result.get("error_code") in {"business_rejection", "validation_error", "confirmation_invalid", "evidence_unavailable"}
                      else "unknown")
            if status == "unknown":
                result = dict(success=False, status="unknown", operation_id=oid, error="业务结果待核实，不能重复提交")
            if row["status"] == status:
                return
            tx.execute("UPDATE support_operations SET status=?,result=? WHERE operation_id=?",
                       (status, json.dumps(result, ensure_ascii=False), oid))
            tx.execute("UPDATE task_operation_proposals SET run_deadline=NULL WHERE operation_id=?", (oid,))
            if status != "unknown":
                tx.execute("DELETE FROM task_operation_resources WHERE operation_id=?", (oid,))
            self.store._event(tx, control, "operation_receipt", "system",
                dict(operation_id=oid, task_id=row["task_id"], status=status, result=result),
                private=True, key="operation-receipt:" + oid + ":" + status)
            self._receipt(tx, control, row, result, status)
            control = self.store._control(tx, row["session_id"], self.user)
            if status == "unknown" and control["mode"] == "bot":
                self.store._handoff(tx, control, "business_result_unknown", "system")

    def _receipt(self, tx, control, row, result, status):
        """Keep actual side effects even if a later message changed the task."""
        oid, tid = row["operation_id"], row["task_id"]
        eid = _id(oid, "receipt:" + status)
        self.store._event(tx, control, "task_operation_result", "system", dict(
            event_id=eid, tool=row["tool"], result=result, task_id=tid,
            task_version=row["task_version"], operation_id=oid, status=status, observed_at=now()),
            private=True, key="task-tool:" + eid)
        record = self.runs.repo._record(self.runs.repo._row(tx, tid, lock=True))
        if (status == "succeeded" and record.version == row["task_version"] and record.status == "in_progress"
                and control["mode"] == "bot" and not tx.one("""SELECT c.session_id FROM task_sessions s
                    JOIN support_conversations c ON c.session_id=s.session_id
                    WHERE s.task_id=? AND c.mode<>'bot' LIMIT 1""", (tid,))):
            from app.tasks.external_progress import only_submission_requested
            submission_complete = (row["tool"] in {"prepare_order_update", "prepare_order_address_change", "prepare_after_sale_cancel", "prepare_purchase", "prepare_return_shipment", "claim_coupon", "request_dispatch"}
                or row['tool']=='prepare_order_cancel' and result.get('status')=='cancelled'
                or settings.task_external_progress_enabled and only_submission_requested(tx, record))
            after = self.runs.repo.update(tid, row["session_id"], expected_version=record.version, change_id="receipt:" + oid,
                source=dict(kind="tool_result", session_id=row["session_id"], event_id=eid),
                patch=dict(status="completed" if submission_complete else "waiting_external", waiting_for=None if submission_complete else dict(kind="external", operation_id=oid,
                    evidence_id=eid, reason="业务申请已登记，仍需核实最终处理结果")), tx=tx)
            if settings.task_external_progress_enabled:
                tx.execute('UPDATE task_execution_wakes SET version=?,job_id=NULL,retry_at=0 WHERE task_id=?',
                           (after.version, tid))
        control = self.store._control(tx, row["session_id"], self.user)
        prepared = json.loads(row["payload"])["prepared"]
        subject = "订单 " + str(prepared["arguments"]["order_id"])
        if row['tool']=='claim_coupon': subject='优惠券 '+str(prepared['review']['name'])
        if status == "succeeded":
            messages = {"prepare_after_sale": "：售后申请已登记，审核和退款结果仍待核实。",
                        "claim_coupon":"：已领取，可在本人优惠券中查看。",
                        "request_dispatch":"：催单已登记，尚待接收方受理，不代表已经发货。",
                        "prepare_invoice": "：开票申请已登记，尚未开具正式发票。",
                        "prepare_order_address_change": "：收货地址已更新。",
                        "prepare_order_update": "：订单信息已更新。",
                        "prepare_order_cancel": "：取消申请已登记，审核、退回验货（如已发货）及退款结果仍待核实。" if result.get('status')=='submitted' else "：订单已取消。",
                        "prepare_after_sale_cancel": "：售后申请已撤回。",
                        "prepare_purchase":"：订单已创建，等待支付；库存和所选优惠券已占用。",
                        "prepare_return_shipment":"：退货运单已登记，尚不代表承运商已收件。",
                        "prepare_price_protection":"：价保申请已登记，审核及退款处理仍待核实。",
                        "prepare_exchange":"：换货申请已登记，指定规格已预留，后续处理仍待核实。",
                        "prepare_repair":"：维修申请已登记，检测及维修结果仍待核实。",
                        "prepare_invoice_reissue":"：发票更正申请已登记，原票作废及新票开具仍待核实。",
                        "apply_refund": "：退款申请已登记，审核结果和实际到账仍待确认。"}
            content = subject + messages.get(row["tool"], "：业务操作已处理，请查看回执。")
            if prepared["review"].get("is_demo") and row["tool"] == "apply_refund":
                content += "本次使用演示业务数据。"
        elif status == "failed":
            content = subject + "：业务系统拒绝了这次操作，请重新核实办理条件。"
        else:
            content = subject + "：操作结果尚未核实，已停止重复提交，将由人工协助核实。"
        self.store._event(tx, control, "message", "bot", dict(role="assistant", content=content,
            event_id=eid, operation_id=oid, task_id=tid, operation_status=status), key="operation-public:" + eid)

    def cancel(self, sid, oid):
        self._enabled()
        started = False
        with self.store.transaction() as tx:
            lock_input_user(tx, self.user)
            control = self.store._control(tx, sid, self.user)
            revision(tx, self.user, lock=True)
            row = self._row(tx, oid, lock=True)
            self._session(tx, row, sid)
            if row["status"] == "prepared":
                tx.execute("UPDATE support_operations SET status='cancelled' WHERE operation_id=?", (oid,))
                self.store._event(tx, control, "operation_cancelled", self.user,
                    dict(operation_id=oid, task_id=row["task_id"]), private=True, key="operation-cancelled:" + oid)
            elif row["status"] in {"running", "unknown", "succeeded"}:
                self.store._event(tx, control, "operation_cancellation_requested", self.user,
                    dict(operation_id=oid, task_id=row["task_id"]), private=True, key="operation-cancel-request:" + oid)
                handoff_started_tasks(tx, self.store, self.user, sid, [row["task_id"]])
                started = True
            if not started:
                return self._view(tx, self._row(tx, oid), sid)
        return self.reconcile(oid, sid=sid, query_running=True)

    def reconcile(self, oid, *, sid=None, query_running=False):
        """Query the original business request. Absence is never permission to retry."""
        self._enabled()
        with self.store.transaction(read_only=True) as tx:
            row = self._row(tx, oid)
            if sid:
                self._session(tx, row, sid)
            if row["status"] not in {"running", "unknown"}:
                return self._view(tx, row, row["session_id"])
            live = row["status"] == "running" and row["run_deadline"] is not None and row["run_deadline"] > clock(tx)
            if live and not query_running:
                return self._view(tx, row, row["session_id"])
        identity = set_current_user_id(self.user)
        try:
            found = bounded_calls.run(lambda: self.business.query(self.user, oid), 10)
        except Exception:
            found = {}
        finally:
            reset_current_user_id(identity)
        if isinstance(found, dict) and found.get("status") == "succeeded" and found.get("action") == row["tool"]:
            self.finish(oid, found.get("result"))
        elif row["status"] == "running" and not live:
            self.finish(oid, {})
        with self.store.transaction(read_only=True) as tx:
            return self._view(tx, self._row(tx, oid), row["session_id"])

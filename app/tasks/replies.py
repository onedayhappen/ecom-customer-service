"""Publish ready pieces once, in the transaction that checks their freshness.

Policy checks run before the transaction; no model or network call holds its
locks. Composition uses a frozen snapshot; publication rechecks its dependencies.
"""
import json
import time
from copy import deepcopy
from types import SimpleNamespace

from app.runtime.outbox import clock
from app.support.store import SupportError, now
from app.tasks.association import _id
from app.tasks.clarifications import PUBLICATION_FIELDS, assess, publication, published_event, refresh_needed, replace_invalid
from app.tasks.execution_store import TaskRuns, object_key
from app.tasks.provenance import invalid, lock_input_user, revision
from app.tasks.reply_facts import facts, label, question_text


class ReplyCoordinator:
    def __init__(self, store, user, *, scope=None, composer=None):
        self.store, self.user = store, user
        self.runs = TaskRuns(store, user, scope=scope)
        self.composer = composer

    def _covered(self, tx, key):
        return tx.one("""SELECT item_key FROM task_reply_items
            WHERE user_id=? AND item_key=? AND disposition<>'deferred'""", (self.user, key))

    def _mark(self, tx, sid, key, sequence=None, disposition="published"):
        previous = tx.one("SELECT disposition FROM task_reply_items WHERE user_id=? AND item_key=?", (self.user, key))
        if disposition != "published" and (not previous or previous["disposition"] != disposition):
            from app.observability.interactions import record
            record("reply.disposition", status=disposition, tx=tx, session_id=sid,
                   job_id=key[4:] if key.startswith("job:") else None,
                   output={"item_key": key, "disposition": disposition})
        tx.execute("""INSERT INTO task_reply_items VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id,item_key) DO UPDATE SET sequence=excluded.sequence,
                disposition=excluded.disposition,checked_at=excluded.checked_at
            WHERE task_reply_items.disposition='deferred'""",
            (self.user, key, sid, sequence, disposition, now(), clock(tx)))

    def _receipts(self, tx, record, result, policy_checks, source):
        receipts = []
        for eid in result.get("evidence_ids", [])[:30]:
            row = tx.one("""SELECT e.payload FROM support_events e JOIN support_conversations c
                ON c.session_id=e.session_id WHERE c.user_id=? AND e.event_key=?
                AND e.kind='task_tool_result'""", (self.user, "task-tool:" + eid))
            evidence = json.loads(row["payload"]) if row else {}
            if not self._valid_receipt(tx, record, evidence):
                continue
            if evidence.get('tool') in {'search_knowledge','search_policies'}:
                if eid not in policy_checks:
                    return None
                if not policy_checks[eid]:
                    continue
                if evidence['tool']=='search_knowledge' and not self._policy_context_current(tx, record, evidence['result'], source):
                    continue
            receipts.append(evidence)
        return receipts

    def _valid_receipt(self, tx, record, evidence):
        from app.media.context import dependency_stamp
        return (evidence.get('task_id') == record.task_id and evidence.get('object') == object_key(record)
            and evidence.get('provenance', {}).get('media', []) == dependency_stamp(tx, self.user, [record.task_id])
            and not evidence.get('late')
            and isinstance(evidence.get('result'), dict) and evidence['result'].get('success') is True
            and evidence.get('observed_epoch', 0) >= clock(tx) - 60
            and self.runs.control_versions_valid(tx, evidence.get('controls'))
            and not any(invalid(tx, self.user, 'decision', did)
                for did in evidence.get('provenance', {}).get('decisions', [])))

    def _policy_context_current(self, tx, record, result, source):
        from app.rag.task_policy import context_current, source_request_time
        from app.rag.policy import PolicyError
        try:
            request_time = source_request_time(self.runs, source, tx=tx)
        except (PolicyError, KeyError, TypeError):
            return False
        proof = []
        eid = result.get('context', {}).get('order_evidence_id')
        if eid:
            row = tx.one("""SELECT e.payload FROM support_events e JOIN support_conversations c
                ON c.session_id=e.session_id WHERE c.user_id=? AND e.event_key=?
                AND e.kind='task_tool_result'""", (self.user, 'task-tool:' + eid))
            evidence = json.loads(row['payload']) if row else {}
            if not self._valid_receipt(tx, record, evidence):
                return False
            proof.append(evidence)
        return context_current(SimpleNamespace(record=record, runs=self.runs, historical=proof,
            receipts=[], evidence_time=clock(tx), request_time=request_time), result)

    def _check_policy_results(self, sid):
        """Validate knowledge outside the publication transaction/locks.

        Clauses are cited as versioned retrieval evidence. A change after this
        check belongs to the next retrieval; business execution still requires
        independent preflight and confirmation against its actual backend.
        """
        from app.harness.bounded_call import bounded_calls, CallCapacityExceeded
        from app.rag.service import policy_result_current
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, self.user)
            rows = tx.all("""SELECT j.response FROM runtime_jobs j LEFT JOIN task_reply_items r
                ON r.user_id=j.user_id AND r.item_key='job:' || j.job_id
                WHERE j.user_id=? AND j.session_id=? AND j.kind='task_execution'
                AND j.status IN ('completed','interrupted','expired')
                AND (r.disposition IS NULL OR r.disposition='deferred')
                ORDER BY COALESCE(r.checked_at,0),j.created_at,j.job_id LIMIT 50""", (self.user, sid))
            ids = list(dict.fromkeys(eid for r in rows for eid in json.loads(r['response'] or '{}').get('evidence_ids', [])[:30]))
            evidence = []
            for eid in ids:
                row = tx.one("""SELECT e.payload FROM support_events e JOIN support_conversations c
                    ON c.session_id=e.session_id WHERE c.user_id=? AND e.event_key=? AND e.kind='task_tool_result'""",
                    (self.user, 'task-tool:' + eid))
                value = json.loads(row['payload']) if row else {}
                if value.get('tool') == 'search_knowledge' and value.get('result', {}).get('success'):
                    evidence.append((eid, value['result']))
                elif value.get('tool')=='search_policies' and value.get('result',{}).get('success'):
                    evidence.append((eid, value['result']))
        checked, deadline = {}, time.monotonic() + 3
        for eid, result in evidence:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                checked[eid] = bounded_calls.run(lambda r=result: self._new_policy_current(r) if 'data' in r else policy_result_current(r), remaining)
            except (TimeoutError, CallCapacityExceeded):
                checked[eid] = False
        return checked

    def _new_policy_current(self,result):
        from app.rag.service import policy_result_current
        from app.services.commerce import service
        from app.services.commerce.common import ref
        try:
            data=result['data']; policy=data['policy_evidence']; context=policy['context']
            if any(not c['text_version_matches'] for c in data.get('rule_checks',[])): return False
            if not policy_result_current(policy): return False
            if context.get('order_id'):
                svc=service(self.user,self.runs.candidates.scope)
                with svc.store.transaction() as tx:
                    order=svc.owned_order(tx,ref('order',context['platform_code'],context['order_id']))
                    if context.get('order_evidence_id')!='mysql:'+svc.snapshot(tx,order): return False
            return True
        except Exception: return False

    def _open_question(self, tx, sid, active_clarifications):
        # Only questions actually sent and still at their original version count.
        if active_clarifications:
            return "clarification"
        q = "payload::jsonb->>'question_id'" if tx.postgres else "json_extract(payload,'$.question_id')"
        rows = tx.all(f"""SELECT payload FROM support_events WHERE session_id=? AND kind='message'
            AND visibility='public' AND {q} IS NOT NULL
            ORDER BY sequence DESC LIMIT 100""", (sid,))
        has_task_question = False
        for row in rows:
            p = json.loads(row["payload"])
            if p.get("question_id") and p.get("task_id"):
                record = self.runs.repo._record(self.runs.repo._row(tx, p["task_id"]))
                if (record.version == p.get("task_version") and record.status == "waiting_user"
                        and isinstance(record.waiting_for, dict)
                        and record.waiting_for.get("question_id") == p["question_id"]):
                    has_task_question = True
        return "task" if has_task_question else None

    def publish(self, sid):
        from app.tasks.reply_claims import render_plan
        from app.config.settings import settings
        from app.harness.bounded_call import bounded_calls
        from app.observability.interactions import bind, record, span
        from app.tasks.reply_composition import ReplyDraft
        with bind(self.store, sid):
            draft = self._prepare(sid, self._check_policy_results(sid))
            if not isinstance(draft, ReplyDraft):
                return draft
            # Fixed questions/options and confirmation notices remain code-owned.
            if not any(m.get("claims") for m in draft.context["matters"]):
                text = draft.fallback
            else:
                try:
                    with span("reply.composition", context=draft.context) as observation:
                        plan = bounded_calls.run(
                            lambda: self.composer(deepcopy(draft.context)), settings.reply_composition_timeout_seconds)
                        text = render_plan(plan, draft.context)
                        text = "\n\n".join([text, *draft.context["required_notices"]])
                        observation["output"] = text
                except Exception as exc:
                    record("reply.composition.fallback", status="fallback",
                           reason="ungrounded_composed_reply" if isinstance(exc, ValueError) else type(exc).__name__)
                    text = draft.fallback
            return self._prepare(sid, self._check_policy_results(sid), draft=draft, composed=text)

    def _prepare(self, sid, policy_checks, *, draft=None, composed=None):
        from app.tasks.reply_composition import snapshot
        from app.observability.interactions import record as observe
        with self.store.transaction() as tx:
            from app.tasks.rollout import running
            if not running(tx, self.user):
                if draft is not None:
                    observe("reply.composition.discarded", status="stale", tx=tx,
                            reason="pipeline_stopped", session_id=sid)
                return None
            # Also fences an unassociated correction arriving in another session.
            lock_input_user(tx, self.user)
            control = self.store._control(tx, sid, self.user)
            revision(tx, self.user, lock=True)
            tx.execute("INSERT INTO task_reply_sessions(session_id) VALUES (?) ON CONFLICT DO NOTHING", (sid,))
            tx.execute("UPDATE task_reply_sessions SET checked_at=? WHERE session_id=?", (clock(tx), sid))
            if control["mode"] != "bot":
                if draft is not None:
                    observe("reply.composition.discarded", status="stale", tx=tx,
                            reason="control_changed", session_id=sid)
                return None
            position = tx.one("SELECT received_sequence FROM message_intake_sessions WHERE session_id=?", (sid,))
            received = position["received_sequence"] if position else 0
            pending = tx.one("""SELECT i.session_id FROM message_intake_items i
                JOIN support_conversations c ON c.session_id=i.session_id
                WHERE i.user_id=? AND i.association_status='pending' AND c.mode='bot' LIMIT 1""", (self.user,))
            if pending:
                if draft is not None:
                    observe("reply.composition.discarded", status="stale", tx=tx,
                            reason="new_input", session_id=sid)
                    return None
                # One receipt per uninterrupted pending episode, never one per message.
                noticed = tx.one("SELECT pending_notice FROM task_reply_sessions WHERE session_id=?", (sid,))
                if noticed["pending_notice"] or not received:
                    return None
                tx.execute("UPDATE task_reply_sessions SET pending_notice=1 WHERE session_id=?", (sid,))
                event_id = _id(sid, "receiving:" + str(received))
                return self._publish(tx, control, [], "消息已收到，正在核对补充内容。", received, event_id)
            tx.execute("UPDATE task_reply_sessions SET pending_notice=0 WHERE session_id=?", (sid,))
            # Association consumes questions within a conversation. A menu in
            # another chat cannot be answered here and must not veto this reply.
            # The global pending-input/provenance fences above remain in force.
            clarifications = tx.all("""SELECT * FROM association_clarifications
                WHERE user_id=? AND session_id=? AND status='open'
                ORDER BY created_at LIMIT 100""", (self.user, sid))
            # Read distinct candidates in batches and lock them through publication.
            records = {}
            task_ids = sorted({o["task_id"] for row in clarifications for o in json.loads(row["candidates"])})
            for start in range(0, len(task_ids), 200):
                ids = task_ids[start:start + 200]
                placeholders = ",".join("?" for _ in ids)
                rows = self.runs.candidates._rows(tx, [f"t.task_id IN ({placeholders})"], ids, limit=len(ids))
                records.update((r["task_id"], self.runs.repo._record(r)) for r in rows)
            for tid in sorted(records):
                record = self.runs.repo._record(self.runs.repo._row(tx, tid, lock=True))
                if all(v is None or record.details.get(k) == v for k, v in vars(self.runs.candidates.scope).items()):
                    records[tid] = record
                else:
                    records.pop(tid)
            versions = {tid: r.version for tid, r in records.items()}
            pending, active, blocked = [], set(), set()
            block_all = False
            for row in clarifications:
                event = published_event(tx, row)
                state = assess(row, versions, event)
                if state.status == "invalid":
                    if not refresh_needed(tx, row, records, event, received):
                        continue
                    row = replace_invalid(tx, row, records)
                    if row is None:
                        continue
                    state = assess(row, versions, None)
                if not state.valid and state.status != "pending":
                    continue
                options = json.loads(row["candidates"])
                blocked.update(o["task_id"] for o in options)
                block_all |= not options
                if state.valid:
                    active.add(row["clarification_id"])
                else:
                    pending.append(row)
            pieces, question = [], self._open_question(tx, sid, active)
            for row in pending:
                key = "clarification:" + row["clarification_id"]
                if question == "clarification" or self._covered(tx, key):
                    continue
                options = json.loads(row["candidates"])
                labels, versions = [], {}
                for option in options:
                    record = records[option["task_id"]]
                    versions[record.task_id] = record.version
                    labels.append(label(record.details.get("order_id") or record.goal))
                text = "请说明这次要处理的订单号，以及希望如何处理。"
                # Keep option numbering identical to the validated association contract.
                if labels and len(labels) == len(options):
                    text = "您说的是哪一件：" + "；".join(f"{i + 1}. {v}" for i, v in enumerate(labels)) + "？"
                pieces.append(dict(key=key, content=text, task_versions=versions, **publication(row)))
                question = "clarification"
            jobs = tx.all("""SELECT j.* FROM runtime_jobs j LEFT JOIN task_reply_items r
                ON r.user_id=j.user_id AND r.item_key='job:' || j.job_id
                WHERE j.user_id=? AND j.session_id=? AND j.kind='task_execution'
                AND j.status IN ('completed','interrupted','expired') AND j.payload<>'{}'
                AND (r.disposition IS NULL OR r.disposition='deferred')
                ORDER BY COALESCE(r.checked_at,0),j.created_at,j.job_id LIMIT 50""", (self.user, sid))
            for job in jobs:
                if len(pieces) >= 8:
                    break
                key = "job:" + job["job_id"]
                payload, result = json.loads(job["payload"]), json.loads(job["response"] or "{}")
                tid = payload["task_id"]
                self.runs._scope(tx, tid)
                record = self.runs.repo._record(self.runs.repo._row(tx, tid, lock=True))
                if not result.get("provenance") and job["status"] in {"expired", "interrupted"}:
                    # An expired queued job never acquired an execution stamp.
                    # Report only a failure, and only if no later retry supersedes it.
                    task_field = "payload::jsonb->>'task_id'" if tx.postgres else "json_extract(payload,'$.task_id')"
                    later = tx.one(f"""SELECT job_id FROM runtime_jobs WHERE user_id=? AND kind='task_execution'
                        AND {task_field}=? AND created_at>? LIMIT 1""", (self.user, tid, job["created_at"]))
                    if not later and record.status == "in_progress" and payload["request"]["expected_version"] == record.version:
                        result = dict(state="deferred", provenance=self.runs.guard.capture([tid], tx=tx))
                stamp = result.get("provenance")
                if (not self.runs.guard.valid(stamp, tx=tx)
                        or tx.one('SELECT ticket_id FROM task_handoffs WHERE user_id=? AND task_id=? AND released_at IS NULL LIMIT 1',
                                  (self.user, tid))
                        or not self.runs.control_versions_valid(tx, payload.get("controls"))
                        or tx.one("""SELECT c.session_id FROM task_sessions t JOIN support_conversations c
                            ON t.session_id=c.session_id WHERE t.task_id=? AND c.mode<>'bot' LIMIT 1""", (tid,))):
                    self._mark(tx, sid, key, disposition="stale")
                    continue
                if block_all or tid in blocked:
                    self._mark(tx, sid, key, disposition="deferred")
                    continue
                if result.get("state") == "interrupted":
                    self._mark(tx, sid, key, disposition="interrupted")
                    continue
                if record.status == 'completed':
                    self._mark(tx, sid, key, disposition='covered')
                    continue
                metadata = dict(key=key, task_versions={tid: record.version},
                                decision_ids=stamp["decisions"], evidence_ids=[],
                                execution_state=result.get("state"))
                if record.status == "waiting_user":
                    if question:
                        self._mark(tx, sid, key, disposition="deferred")
                        continue
                    text = question_text(record)
                    if text:
                        metadata.update(task_id=tid, task_version=record.version,
                                        question_id=record.waiting_for["question_id"])
                        question = "task"
                    else:
                        text = "补充信息已记录；目前还无法继续核实，可转人工客服协助。"
                elif record.status in {"paused", "cancelled"}:
                    text = f"订单 {label(record.details.get('order_id'))}的当前事项已" + (
                        "暂停。" if record.status == "paused" else "停止自动处理。")
                elif result.get("state") == "needs_confirmation":
                    from app.tasks.operations import TaskOperations
                    operations = TaskOperations(self.store, self.user, scope=self.runs.candidates.scope)
                    try:
                        row = operations._row(tx, result.get("operation_id"))
                        view = operations._view(tx, row, sid)
                        if row["task_id"] != tid or not view["can_confirm"]:
                            raise SupportError("操作已变化")
                    except SupportError:
                        self._mark(tx, sid, key, disposition="stale")
                        continue
                    text = "订单 " + label(view["review"]["order_id"]) + "：预检已完成，请核对确认卡片后决定是否提交。"
                    metadata["operation_id"] = row["operation_id"]
                else:
                    receipts = self._receipts(tx, record, result, policy_checks,
                                              payload.get('execution_source', payload['request']['source']))
                    if receipts is None:
                        self._mark(tx, sid, key, disposition="deferred")
                        continue
                    from app.tasks.reply_claims import build_claims, fallback_text
                    metadata["claims"] = build_claims(record, receipts)
                    from app.media.context import image_claims
                    visual_claims = image_claims(tx, self.user, record.task_id)
                    metadata['claims'] += visual_claims
                    text = fallback_text(metadata["claims"])
                    if any(r['tool'] in {'search_knowledge','search_policies'} for r in receipts):
                        from app.memory.source_policy import POLICY_REFRESH
                        metadata['context_content'] = '\n'.join(filter(None, (
                            facts(record, [r for r in receipts if r['tool'] not in {'search_knowledge','search_policies'}]), POLICY_REFRESH)))
                    metadata["evidence_ids"] = [r["event_id"] for r in receipts]
                    metadata['evidence_ids'] += [c['evidence_id'] for c in visual_claims]
                    if not text:
                        subject = "订单 " + label(record.details["order_id"]) if record.details.get("order_id") else label(record.goal)
                        text = subject + "：暂时没有可核实的查询结果，可补充信息后重试，或转人工客服核实。"
                    metadata["fallback"] = text
                metadata["content"] = text
                pieces.append(metadata)
            if not pieces:
                if draft is not None:
                    observe("reply.composition.discarded", status="stale", tx=tx,
                            reason="results_unavailable_or_already_published", session_id=sid)
                return None
            event_id = _id(sid, json.dumps(sorted(p["key"] for p in pieces)))
            content = "\n\n".join(p["content"] for p in pieces)
            if self.composer is not None:
                current = snapshot(self, tx, control, pieces, received)
                if draft is None:
                    return current
                if current.fingerprint != draft.fingerprint:
                    observe("reply.composition.discarded", status="stale", tx=tx,
                            reason="snapshot_changed", session_id=sid)
                    return None
                content = composed
            return self._publish(tx, control, pieces, content, received, event_id)

    def _publish(self, tx, control, pieces, content, received, event_id):
        # Locks remain held through the event, coverage ledger and notification.
        payload = dict(role="assistant", content=content, event_id=event_id,
                       received_sequence=received, result_ids=[p["key"] for p in pieces],
                       task_versions={k: v for p in pieces for k, v in p.get("task_versions", {}).items()},
                       decision_ids=sorted({d for p in pieces for d in p.get("decision_ids", [])}),
                       evidence_ids=sorted({e for p in pieces for e in p.get("evidence_ids", [])}))
        operations = [p["operation_id"] for p in pieces if p.get("operation_id")]
        if operations:
            payload["operation_ids"] = operations
        for p in pieces:
            for key in ("question_id", "task_id", "task_version", *PUBLICATION_FIELDS):
                if key in p:
                    payload[key] = p[key]
        if any('context_content' in p for p in pieces):
            payload['context_content'] = '\n\n'.join(p.get('context_content', p['content']) for p in pieces)
        seq = self.store._event(tx, control, "message" if pieces else "task_processing", "bot",
                               payload, key="coordinated-reply:" + event_id)
        for p in pieces:
            self._mark(tx, control["session_id"], p["key"], seq)
        for tid in payload["task_versions"]:
            tx.execute("INSERT INTO support_task_messages VALUES (?,?,?) ON CONFLICT DO NOTHING",
                       (tid, control["session_id"], seq))
        return dict(sequence=seq, **payload)

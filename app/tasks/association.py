"""Validate and atomically apply the Agent's message-to-task proposals."""
from dataclasses import dataclass
import json
import re
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from app.agents.association import AssociationProposal
from app.session.request_budget import ContextBudgetExceeded
from app.support.store import SupportError, now
from app.tasks.candidates import CandidateService
from app.tasks.clarifications import assess, published_event
from app.tasks.intake import MessageIntake
from app.tasks.repository import TaskRepository, VersionConflict
from app.observability.interactions import observed_association, record


class AssociationRejected(SupportError):
    def __init__(self, code):
        self.code = code
        super().__init__("归属建议未通过校验：" + code, 422)


@dataclass
class AssociationSnapshot:
    context: object
    control_sequence: int


def _id(job_id, suffix):
    return str(uuid5(NAMESPACE_URL, json.dumps([job_id, suffix], ensure_ascii=False)))


def _short_answer(text):
    return bool(re.fullmatch(r"\s*(?:是的?|好的?|好|嗯+|对|同意|确认|不是|不对|否|第?[一二两三四五六七八九十\d]+(?:个|笔|单)?)\s*[。！!]*\s*", text))


def _option(text, options):
    stripped = text.strip().rstrip("。！!")
    numbers = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
    match = re.fullmatch(r"第?([一二两三四五\d]+)(?:个|笔|单)?", stripped)
    if match:
        value = match[1]
        index = int(value) if value.isdigit() else numbers.get(value, 0)
        if 1 <= index <= len(options):
            return options[index - 1].get("value")
    exact = [o.get("value") for o in options if str(o.get("value", "")) == stripped]
    return exact[0] if len(exact) == 1 else None


class AssociationService:
    def __init__(self, store, user, *, scope=None, max_attempts=2):
        if not 1 <= max_attempts <= 3:
            raise ValueError("Association attempts must be 1..3")
        self.store, self.user = store, user
        self.repo = TaskRepository(store, user)
        self.candidates = CandidateService(store, user, scope=scope)
        self.intake = MessageIntake(store)
        self.max_attempts = max_attempts

    def result(self, job_id):
        with self.store.transaction(read_only=True) as tx:
            row = tx.one("SELECT result FROM association_runs WHERE job_id=? AND user_id=?", (job_id, self.user))
            return json.loads(row["result"]) if row else None

    def _controls(self, tx, batch):
        rows = tx.all("""SELECT i.sequence,e.payload FROM message_intake_items i JOIN support_events e
            ON e.session_id=i.session_id AND e.sequence=i.sequence
            WHERE i.session_id=? AND i.association_status='control' AND i.sequence>?
            ORDER BY i.sequence""", (batch.session_id, min(m["sequence"] for m in batch.messages)))
        return [dict(sequence=r["sequence"], **{k: p.get(k) for k in ("content", "intake_action", "control_task_id")})
                for r in rows if (p := json.loads(r["payload"]))]

    def _clarifications(self, tx, sid, context):
        rows = tx.all("""SELECT * FROM association_clarifications
            WHERE user_id=? AND session_id=? AND status='open' ORDER BY created_at LIMIT 10""", (self.user, sid))
        available = {c["task_id"]: c for c in context.candidates}
        for row in rows:
            options = json.loads(row["candidates"])
            # Pending choices can reference older tasks outside normal recall.
            for option in options:
                if option["task_id"] not in available:
                    found = self.candidates._rows(tx, ["t.task_id=?"], [option["task_id"]])
                    if found:
                        task = self.repo._record(found[0])
                        candidate = {**task.model_dump(), "question": self.candidates._question(tx, task),
                                     "required": True, "reasons": ["pending_clarification"],
                                     "sources": self.candidates._sources(tx, task.task_id)}
                        context.candidates.append(candidate)
                        available[task.task_id] = candidate
            visible = [o for o in options if o["task_id"] in available]
            event = published_event(tx, row, before_sequence=min(m["sequence"] for m in context.current_messages))
            state = assess(row, {tid: c["version"] for tid, c in available.items()}, event)
            sources = []
            for sequence in json.loads(row["sources"]):
                message = tx.one("""SELECT payload FROM support_events WHERE session_id=? AND sequence=?
                    AND actor=? AND kind='message' AND visibility='public'""", (sid, sequence, self.user))
                if message:
                    sources.append(dict(session_id=sid, sequence=sequence, content=json.loads(message["payload"])["content"]))
                    from app.media.context import message_images
                    visual = message_images(tx, self.user, sid, sequence)
                    if visual:
                        sources[-1]['visual_context'] = visual
            context.clarifications.append({
                "clarification_id": row["clarification_id"], "version": row["version"], "question": row["question"],
                "options": visible, "sources": sources, "published": state.published,
                "published_at": event["created_at"] if event else None,
                "valid": state.valid,
            })

    def snapshot(self, batch, *, check_orders=False, dialogue=None, rewritten_query=""):
        if batch.user_id != self.user:
            raise SupportError("归属判断任务不存在", 404)
        context = self.candidates.build(batch.session_id, [m["sequence"] for m in batch.messages],
                                        check_orders=check_orders, dialogue=dialogue, rewritten_query=rewritten_query)
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, batch.session_id, self.user)
            context.control_notices = self._controls(tx, batch)
            self._clarifications(tx, batch.session_id, context)
        return AssociationSnapshot(context, max((n["sequence"] for n in context.control_notices), default=0))

    def _quote(self, tx, quote, allowed, *, user_only=True):
        from app.tasks.provenance import context_event_valid
        key = (quote.session_id, quote.sequence)
        if quote.kind != 'text_quote':
            if user_only or key not in allowed or not context_event_valid(tx, *key):
                raise AssociationRejected('image_is_not_user_statement')
            from app.media.context import validate_evidence
            try:
                validate_evidence(tx, self.user, quote)
            except SupportError:
                raise AssociationRejected('invalid_image_evidence') from None
            return
        if key not in allowed or quote.quote not in allowed[key] or not context_event_valid(tx, *key):
            raise AssociationRejected("ungrounded_quote")
        row = tx.one("""SELECT e.payload,e.actor FROM support_events e JOIN support_conversations c ON c.session_id=e.session_id
            WHERE e.session_id=? AND e.sequence=? AND c.user_id=?
            AND e.kind='message' AND e.visibility='public'""",
            (quote.session_id, quote.sequence, self.user))
        payload = json.loads(row["payload"]) if row else {}
        if (not row or payload.get("role") not in {"user", "assistant", "agent"}
                or (user_only and (row["actor"] != self.user or payload["role"] != "user"))
                or quote.quote not in payload.get("content", "")):
            raise AssociationRejected("source_not_user_statement")

    def _validate(self, tx, batch, snapshot, proposal):
        context = snapshot.context
        if context.session_id != batch.session_id:
            raise AssociationRejected("context_session")
        current = {m["sequence"]: m["content"] for m in context.current_messages}
        from app.media.context import message_images
        for message in context.current_messages:
            if message.get('visual_context', []) != message_images(tx, self.user, batch.session_id, message['sequence']):
                raise AssociationRejected('image_context_changed')
        if set(current) != {m["sequence"] for m in batch.messages}:
            raise AssociationRejected("message_range")
        available = {c["task_id"]: c for c in context.candidates}
        clarifications = {c["clarification_id"]: c for c in context.clarifications}
        allowed = {(m["session_id"], m["sequence"]): m["content"] for m in context.current_messages + context.recent_messages}
        for candidate in available.values():
            for source in candidate["sources"]:
                if source.get("kind") == "message":
                    allowed[(source["session_id"], source["sequence"])] = source["content"]
        for clarification in clarifications.values():
            allowed.update({(s["session_id"], s["sequence"]): s["content"] for s in clarification["sources"]})
        # Lock existing tasks in a deterministic order. Different sessions can
        # modify shared matters without acquiring each other's conversation locks.
        locked = {}
        task_ids = {d.task_id for d in proposal.decisions if d.task_id}
        for decision in proposal.decisions:
            # The whole published option list is part of an answer's contract,
            # including candidates the user did not select.
            if decision.clarification_id in clarifications:
                task_ids.update(o["task_id"] for o in clarifications[decision.clarification_id]["options"])
            if decision.action == "clarify":
                task_ids.update(decision.candidate_task_ids)
        for task_id in sorted(task_ids):
            if task_id not in available:
                raise AssociationRejected("unknown_or_out_of_scope_task")
            if not self.candidates._rows(tx, ["t.task_id=?"], [task_id]):
                raise AssociationRejected("unknown_or_out_of_scope_task")
            row = self.repo._row(tx, task_id, lock=True)
            locked[task_id] = self.repo._record(row)
            if locked[task_id].version != available[task_id]["version"]:
                raise VersionConflict()
        valid_questions = [c["question"] for c in available.values() if c.get("question") and c["question"]["valid"]]
        covered, signatures, answered = set(), set(), set()
        for decision in proposal.decisions:
            signature = self.repo._fingerprint(decision.model_dump())
            if signature in signatures:
                raise AssociationRejected("duplicate_decision")
            signatures.add(signature)
            sources = set(decision.sequences)
            if not sources & current.keys():
                raise AssociationRejected("missing_current_message")
            clarification = clarifications.get(decision.clarification_id) if decision.clarification_id else None
            extra = {s["sequence"] for s in clarification["sources"]} if clarification else set()
            if decision.action == "clarify":
                extra |= {s["sequence"] for c in clarifications.values() for s in c["sources"]}
            if sources - (current.keys() | extra):
                raise AssociationRejected("source_outside_range")
            if clarification:
                row = tx.one("SELECT * FROM association_clarifications WHERE clarification_id=? AND user_id=?",
                             (decision.clarification_id, self.user))
                if (not row or row["version"] != clarification["version"] or not clarification["valid"]
                        or not assess(row, {tid: r.version for tid, r in locked.items()},
                                      published_event(tx, row, before_sequence=min(current))).valid):
                    raise AssociationRejected("expired_clarification")
                if not extra <= sources or (decision.action == "update" and clarification["options"]
                        and decision.task_id not in {o["task_id"] for o in clarification["options"]}):
                    raise AssociationRejected("clarification_target")
                if decision.clarification_id in answered:
                    raise AssociationRejected("duplicate_answer")
                answered.add(decision.clarification_id)
            elif decision.clarification_id:
                raise AssociationRejected("unknown_clarification")
            for quote in decision.evidence:
                self._quote(tx, quote, allowed, user_only=False)
            quoted = {q.sequence for q in decision.evidence if q.session_id == batch.session_id}
            if not sources <= quoted:
                raise AssociationRejected("missing_source_quote")
            covered.update(sources & current.keys())
            text = "\n".join(current[n] for n in sorted(sources & current.keys()))
            if decision.action == "clarify":
                if set(decision.candidate_task_ids) - available.keys():
                    raise AssociationRejected("unknown_clarification_target")
                continue
            if decision.action == "ignore":
                continue
            if context.gaps:
                raise AssociationRejected("incomplete_candidate_context")
            if _short_answer(text) and decision.action == "create":
                raise AssociationRejected("short_answer_cannot_create")
            responding_to_choice = False
            if clarification:
                role_field = "payload::jsonb->>'role'" if tx.postgres else "json_extract(payload,'$.role')"
                latest = tx.one(f"""SELECT payload FROM support_events WHERE session_id=? AND sequence<?
                    AND kind='message' AND visibility='public' AND {role_field} IN ('assistant','agent')
                    ORDER BY sequence DESC LIMIT 1""", (batch.session_id, min(sources & current.keys())))
                responding_to_choice = bool(latest and json.loads(latest["payload"]).get("clarification_id") == decision.clarification_id)
            if _short_answer(text) and not responding_to_choice and len(valid_questions) + sum(c["valid"] for c in clarifications.values()) != 1:
                raise AssociationRejected("ambiguous_short_answer")
            if _short_answer(text) and (decision.effect != "answer" or not (decision.question_id or clarification)):
                raise AssociationRejected("short_answer_requires_question")
            if clarification and decision.action == "create" and clarification["options"] and not re.search(r"新|另外|另一", text):
                raise AssociationRejected("clarification_does_not_create_duplicate")
            target = locked.get(decision.task_id)
            question = available[decision.task_id].get("question") if target else None
            if target:
                if target.storage_mode != "durable":
                    raise AssociationRejected("legacy_task_requires_migration")
                if decision.task_version != target.version:
                    raise VersionConflict()
                if tx.one("""SELECT s.task_id FROM task_sessions s JOIN support_conversations c ON c.session_id=s.session_id
                    WHERE s.task_id=? AND c.mode<>'bot' LIMIT 1""", (target.task_id,)):
                    raise AssociationRejected("human_owned_task")
                inflight = tx.one("""SELECT o.operation_id FROM support_operations o JOIN task_sessions s ON s.session_id=o.session_id
                    WHERE s.task_id=? AND o.status IN ('running','unknown')
                    AND (EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id AND m.task_id=?)
                        OR NOT EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id)) LIMIT 1""",
                    (target.task_id, target.task_id))
                if inflight:
                    # A validated amendment of a new operation is saved for
                    # human intervention after intake commits; it never undoes IO.
                    known = tx.one("SELECT operation_id FROM task_operation_proposals WHERE operation_id=? AND user_id=?",
                                   (inflight["operation_id"], self.user))
                    if not known or decision.effect not in {"cancel", "pause", "amend"}:
                        raise AssociationRejected("operation_in_progress_or_unknown")
                if target.status in {"completed", "cancelled", "paused"} and decision.effect != "resume":
                    raise AssociationRejected("explicit_resume_required")
                if decision.effect == "resume" and not re.search(r"继续|恢复|重开|接着|重新", text):
                    raise AssociationRejected("resume_not_explicit")
                if any(n["control_task_id"] == target.task_id and n["sequence"] > max(sources) for n in context.control_notices):
                    raise AssociationRejected("superseded_by_control")
                if decision.effect in {"cancel", "pause"} and re.search(r"(?:不|别)[^，。；;]{0,6}(?:取消|撤销|暂停|停止)", text):
                    raise AssociationRejected("negated_or_ambiguous_cancel")
                mentions = self.candidates._order_ids("\n".join(q.quote for q in decision.evidence if q.sequence in sources))
                order_id = str(target.details.get("order_id", "")).upper()
                if order_id and mentions and order_id not in mentions:
                    raise AssociationRejected("order_mismatch")
                if order_id and re.search(r"(?:不是|并非)\s*" + re.escape(order_id), text, re.I):
                    raise AssociationRejected("negated_target")
            selection = None
            if decision.question_id:
                if (not question or not question["valid"] or question["question_id"] != decision.question_id
                        or decision.question_id in answered):
                    raise AssociationRejected("expired_question")
                answered.add(decision.question_id)
                first_source = tx.one("SELECT created_at FROM support_events WHERE session_id=? AND sequence=?",
                                      (batch.session_id, min(sources & current.keys())))
                if not question.get("published_at") or first_source["created_at"] <= question["published_at"]:
                    raise AssociationRejected("answer_precedes_question")
                selection = _option(text, question["options"])
                if question["options"] and selection is None and _short_answer(text):
                    raise AssociationRejected("unresolved_option")
            if clarification and _short_answer(text):
                selected = _option(text, [dict(value=o["task_id"]) for o in clarification["options"]])
                if selected is None and len(clarification["options"]) == 1 and re.fullmatch(r"\s*(?:是的?|对|好的?)\s*[。！!]*", text):
                    selected = clarification["options"][0]["task_id"]
                if selected != decision.task_id:
                    raise AssociationRejected("ambiguous_clarification_answer")
            if decision.effect == "answer" and not (decision.question_id or clarification):
                raise AssociationRejected("missing_question")
            for detail in decision.details:
                self._quote(tx, detail.evidence, allowed)
                if detail.evidence.session_id != batch.session_id or detail.evidence.sequence not in sources:
                    raise AssociationRejected("field_source_outside_decision")
                if detail.field == "quantity":
                    numeral = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
                    value = detail.value
                    units = r"\s*(?:件|个|只|双|台|套|本|口)"
                    mentioned = re.search(r"(?<![A-Za-z\d-])" + re.escape(str(value)) + units, detail.evidence.quote)
                    named = any(re.search(n + units, detail.evidence.quote) and v == value for n, v in numeral.items())
                    answer = question and question["field"] == "quantity" and (
                        detail.evidence.quote.strip() == str(value) or selection == value)
                    if type(value) is not int or value < 1 or not (mentioned or named or answer):
                        raise AssociationRejected("ungrounded_quantity")
                elif not isinstance(detail.value, str) or not detail.value.strip() or (
                    detail.value.casefold() not in detail.evidence.quote.casefold()
                    and not (decision.effect == "answer" and question and detail.field == question["field"] and detail.value == selection)
                ):
                    raise AssociationRejected("ungrounded_field")
                if target and detail.field in {"order_id", "item_id", "sku_id", "product_id"}:
                    previous = target.details.get(detail.field)
                    if previous is not None and previous != detail.value:
                        raise AssociationRejected("entity_switch_requires_correction")
                scope_value = getattr(self.candidates.scope, detail.field, None)
                if scope_value is not None and detail.value != scope_value:
                    raise AssociationRejected("scope_mismatch")
        if covered != current.keys():
            raise AssociationRejected("uncovered_message")
        if any(d.action in {'create', 'update'} for d in proposal.decisions):
            images = {(m['sequence'], i['attachment_id']) for m in context.current_messages
                      for i in m.get('visual_context', []) if i['status'] != 'unavailable'}
            cited = {(e.sequence, e.attachment_id) for d in proposal.decisions for e in d.evidence
                     if e.kind != 'text_quote' and e.session_id == batch.session_id}
            if images - cited:
                raise AssociationRejected('missing_image_evidence')
        return locked

    def commit(self, batch, snapshot, proposal, *, rejections=()):
        if batch.user_id != self.user:
            raise SupportError("归属判断任务不存在", 404)
        proposal = AssociationProposal.model_validate(proposal)
        fingerprint = self.repo._fingerprint(proposal.model_dump())
        with self.store.transaction() as tx:
            from app.tasks.provenance import lock_input_user
            lock_input_user(tx, self.user)
            control = self.store._control(tx, batch.session_id, self.user)
            previous = tx.one("SELECT * FROM association_runs WHERE job_id=? AND user_id=?", (batch.job_id, self.user))
            if previous:
                if previous["proposal_hash"] != fingerprint:
                    raise AssociationRejected("committed_proposal_changed")
                return json.loads(previous["result"])
            if batch.user_id != self.user or control["mode"] != "bot":
                raise SupportError("归属判断已失效")
            latest_controls = max((c["sequence"] for c in self._controls(tx, batch)), default=0)
            if latest_controls != snapshot.control_sequence:
                raise VersionConflict()
            current_records = self._validate(tx, batch, snapshot, proposal)
            result = {"job_id": batch.job_id, "decisions": [], "clarification": None, "rejections": list(rejections)}
            per_message = {m["sequence"]: [] for m in batch.messages}
            ordered = sorted(enumerate(proposal.decisions), key=lambda pair: (min(pair[1].sequences), pair[0]))
            for index, decision in ordered:
                did, change_id = _id(batch.job_id, index), None
                tid, version = decision.task_id, None
                primary = min(decision.sequences)
                source = dict(kind="message", session_id=batch.session_id, sequence=primary)
                if decision.action in {"create", "update"}:
                    change_id = did
                    details = {d.field: d.value for d in decision.details}
                    if decision.action == "create":
                        details.update({k: v for k, v in vars(self.candidates.scope).items() if v is not None})
                        record = self.repo.create(batch.session_id, change_id=did, source=source,
                                                  goal=decision.goal, details=details, tx=tx)
                    else:
                        current = current_records[tid]
                        patch = {"details": {**current.details, **details}}
                        if decision.goal:
                            patch["goal"] = decision.goal
                        if decision.effect in {"pause", "cancel", "resume", "answer"}:
                            patch["status"] = {"pause": "paused", "cancel": "cancelled"}.get(decision.effect, "in_progress")
                        if decision.effect in {"cancel", "answer"}:
                            patch["waiting_for"] = None
                        if (decision.effect in {"supplement", "amend"}
                                and current.status in {"waiting_user", "waiting_external"}
                                and (decision.goal or decision.details)):
                            # A new request invalidates the old version-bound
                            # question/wait. Re-evaluate instead of leaving an
                            # unanswerable question attached to the new version.
                            patch.update(status="in_progress", waiting_for=None)
                        # Explicit field reaffirmations still need provenance:
                        # a later correction must not erase the user's restatement.
                        if not decision.details and decision.goal is None and all(getattr(current, k) == v for k, v in patch.items()):
                            record, change_id = current, None
                        else:
                            record = self.repo.update(tid, batch.session_id, expected_version=current.version,
                                                      change_id=did, source=source, patch=patch, tx=tx,
                                                      written_fields=["details." + d.field for d in decision.details])
                    tid, version = record.task_id, record.version
                    current_records[tid] = record
                    # The repository allocates provenance events in this same
                    # transaction. Reload the shared sequence before our event.
                    control = self.store._control(tx, batch.session_id, self.user)
                    for seq in decision.sequences:
                        tx.execute("INSERT INTO support_task_messages VALUES (?,?,?) ON CONFLICT DO NOTHING", (tid, batch.session_id, seq))
                elif decision.action == "clarify":
                    options = [dict(task_id=t, version=current_records[t].version, label=current_records[t].goal)
                               for t in dict.fromkeys(decision.candidate_task_ids)]
                    clarification_id = _id(did, "clarification")
                    tx.execute("""INSERT INTO association_clarifications
                        (clarification_id,user_id,session_id,question,candidates,sources,created_at) VALUES (?,?,?,?,?,?,?)""",
                        (clarification_id, self.user, batch.session_id, decision.clarification,
                         json.dumps(options, ensure_ascii=False), json.dumps(decision.sequences), now()))
                    result["clarification"] = dict(clarification_id=clarification_id, version=1,
                                                   prompt=decision.clarification, options=options)
                    for old in snapshot.context.clarifications:
                        if {s["sequence"] for s in old["sources"]} & set(decision.sequences):
                            tx.execute("""UPDATE association_clarifications SET status='superseded',answer_decision_id=?
                                WHERE clarification_id=? AND status='open'""", (did, old["clarification_id"]))
                event_sequence = self.store._event(tx, control, "association_decision", "system", {
                    "decision_id": did, "decision": decision.model_dump(), "task_id": tid,
                    "task_version": version, "change_id": change_id,
                }, private=True, key="association:" + did)
                tx.execute("INSERT INTO association_decisions VALUES (?,?,?,?,?,?,?,?)",
                           (did, batch.job_id, self.user, batch.session_id, tid, event_sequence, change_id, decision.action))
                if tid:
                    from app.media.context import bind_task_images
                    bind_task_images(tx, self.user, tid, did, decision.evidence)
                for seq in decision.sequences:
                    tx.execute("INSERT INTO association_sources VALUES (?,?,?)", (did, batch.session_id, seq))
                    if seq in per_message:
                        per_message[seq].append((did, decision.action))
                if decision.clarification_id:
                    tx.execute("""UPDATE association_clarifications SET status='answered',answer_decision_id=?
                        WHERE clarification_id=?""", (did, decision.clarification_id))
                    for seq in decision.sequences:
                        if seq not in per_message:
                            tx.execute("""UPDATE message_intake_items SET association_status='associated',decision_id=?
                                WHERE session_id=? AND sequence=? AND association_status='clarification'""", (did, batch.session_id, seq))
                elif decision.action == "clarify":
                    for seq in decision.sequences:
                        if seq not in per_message:
                            tx.execute("""UPDATE message_intake_items SET decision_id=?
                                WHERE session_id=? AND sequence=? AND association_status='clarification'""", (did, batch.session_id, seq))
                result["decisions"].append(dict(decision_id=did, action=decision.action, task_id=tid, task_version=version))
            outcomes = [dict(sequence=seq, decision_id=ids[0][0],
                             status="clarification" if any(action == "clarify" for _, action in ids) else "associated")
                        for seq, ids in per_message.items()]
            self.intake.complete(batch, outcomes, tx=tx)
            from app.tasks.operations import handoff_started_tasks
            affected = [d.task_id for d in proposal.decisions if d.action == "update" and d.effect in {"cancel", "pause", "amend"}]
            if handoff_started_tasks(tx, self.store, self.user, batch.session_id, affected):
                result["needs_human"] = True
            tx.execute("INSERT INTO association_runs VALUES (?,?,?,?,?,?)",
                       (batch.job_id, self.user, batch.session_id, fingerprint, json.dumps(result, ensure_ascii=False), now()))
            return result

    def _fallback(self, batch, snapshot):
        sources = {m["sequence"]: m["content"] for m in batch.messages}
        sources.update({s["sequence"]: s["content"] for c in snapshot.context.clarifications for s in c["sources"]})
        if len(sources) > 100:
            # Preserve old questions as pending; do not acknowledge their source
            # ranges when they exceed this batch's bounded clarification contract.
            sources = {m["sequence"]: m["content"] for m in batch.messages}
        return AssociationProposal.model_validate({"decisions": [{
            "action": "clarify", "sequences": sorted(sources),
            "evidence": [self._fallback_evidence(batch, n, text) for n, text in sources.items()],
            # Infrastructure/validation failure is not evidence that every
            # recalled task is a plausible choice. Keep the unresolved sources
            # but do not manufacture a historical task menu from retrieval.
            "clarification": "刚才没能准确接上您的意思。请直接说一下商品和现在想办的事，不用提供订单号。",
            "candidate_task_ids": [],
        }]})

    def _fallback_evidence(self, batch, sequence, text):
        if text:
            return dict(session_id=batch.session_id, sequence=sequence, quote=text[:1])
        with self.store.transaction(read_only=True) as tx:
            from app.media.context import message_images
            images = message_images(tx, self.user, batch.session_id, sequence)
        return dict(session_id=batch.session_id, sequence=sequence, kind='attachment_ref',
                    attachment_id=images[0]['attachment_id'])

    @observed_association
    def process(self, batch, engine):
        if batch.user_id != self.user:
            raise SupportError("归属判断任务不存在", 404)
        replay = self.result(batch.job_id)
        if replay:
            return replay
        self.intake.renew(batch, 60)
        dialogue = self.candidates.dialogue(batch.session_id, [m["sequence"] for m in batch.messages])
        rewritten = self._rewrite(batch, engine, dialogue)
        rejections = []
        for attempt in range(self.max_attempts):
            self.intake.renew(batch, 60)
            # A rejected proposal or provider timeout does not require order
            # retrieval. Refresh task versions/control without adding IO.
            snapshot = self.snapshot(batch, dialogue=dialogue, rewritten_query=rewritten)
            snapshot.context.retry_feedback = rejections[-2:]
            record("association.candidates", attempt=attempt + 1, output=snapshot.context.data())
            try:
                proposal = AssociationProposal.model_validate(engine.associate(snapshot.context))
                record("association.proposal", attempt=attempt + 1, output=proposal)
            except ContextBudgetExceeded:
                record("association.validation", status="error", attempt=attempt + 1, error="context_budget")
                rejections.append("context_budget")
                break
            except (ValueError, ValidationError) as exc:
                record("association.validation", status="error", attempt=attempt + 1,
                       error={"code": "invalid_model_output", "type": type(exc).__name__, "message": str(exc)})
                rejections.append("invalid_model_output")
                continue
            except Exception as exc:
                record("association.validation", status="error", attempt=attempt + 1,
                       error={"code": "model_unavailable", "type": type(exc).__name__, "message": str(exc)})
                rejections.append("model_unavailable")
                continue
            try:
                result = self.commit(batch, snapshot, proposal, rejections=rejections)
                record("association.validation", attempt=attempt + 1, output=result)
                return result
            except AssociationRejected as exc:
                record("association.validation", status="error", attempt=attempt + 1, error=exc.code)
                rejections.append(exc.code)
                if exc.code == 'image_context_changed':
                    dialogue = self.candidates.dialogue(batch.session_id, [m['sequence'] for m in batch.messages])
                    rewritten = ''
            except VersionConflict:
                record("association.validation", status="error", attempt=attempt + 1, error="version_or_control_changed")
                rejections.append("version_or_control_changed")
        # No guessed business fields survive an invalid/ambiguous proposal.
        # Snapshot again so the fallback itself does not reuse stale control.
        self.intake.renew(batch, 60)
        snapshot = self.snapshot(batch, rewritten_query=rewritten)
        record("association.fallback", status="fallback", rejections=rejections)
        return self.commit(batch, snapshot, self._fallback(batch, snapshot), rejections=rejections)

    def _rewrite(self, batch, engine, dialogue):
        """Persist one bounded rewrite per claimed range, including fallback."""
        from app.agents.query_rewrite import validate_rewrite
        from app.harness.bounded_call import bounded_calls
        with self.store.transaction(read_only=True) as tx:
            row = tx.one("SELECT payload FROM runtime_jobs WHERE job_id=?", (batch.job_id,))
            saved = json.loads(row["payload"]).get("query_rewrite") if row else None
        fingerprint = self.repo._fingerprint(dialogue.current_messages)
        if saved and saved["fingerprint"] == fingerprint:
            # Reuse the same original history after a lease recovery.
            dialogue.recent_messages = saved["recent_messages"]
            return saved["text"]
        text = "\n".join(m["content"] for m in dialogue.current_messages)
        status, error = "skipped", None
        if callable(getattr(engine, "rewrite", None)):
            try:
                text = validate_rewrite(bounded_calls.run(lambda: engine.rewrite(dialogue), 15))
                status = "ok"
            except Exception as exc:
                status, error = "fallback", type(exc).__name__
        self.intake.renew(batch, 60)
        with self.store.transaction() as tx:
            self.store._control(tx, batch.session_id, self.user)
            state = self.intake._state(tx, batch.session_id)
            if state["active_job_id"] != batch.job_id or state["lease_owner"] != batch.owner:
                raise SupportError("改写批次租约已失效")
            row = tx.one("SELECT payload FROM runtime_jobs WHERE job_id=?", (batch.job_id,))
            payload = json.loads(row["payload"])
            payload["query_rewrite"] = dict(fingerprint=fingerprint, text=text, status=status,
                                             recent_messages=dialogue.recent_messages)
            tx.execute("UPDATE runtime_jobs SET payload=? WHERE job_id=?", (json.dumps(payload, ensure_ascii=False), batch.job_id))
            record("query_rewrite", tx=tx, status=status, input=dialogue.data(), output=text, error=error)
        return text

# 文件说明：通过真实消息和确认入口驱动评测，采集事项、模型、工具及公开回复。
"""Service-entry driver. Only run in a disposable process (tests may supply a fake SDK)."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import patch
import json
import time

from .business import Business
from .contracts import COLLECTIONS, Capture, Inputs, digest
from .grading import selected
from .latency import ResponseTimer


class BudgetExceeded(RuntimeError):
    pass


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "data"):
        return value.data()
    if isinstance(value, SimpleNamespace):
        return {k: plain(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


class Journal:
    def __init__(self, capture, inputs):
        self.capture, self.inputs = capture, inputs
        self.lock, self.gates = RLock(), []
        self.ordinal, self.seen, self.sent, self.versions = 0, set(), {}, {}
        self.durable_generation, self.jobs = 0, {}
        self.exhausted, self.collection_error = False, False
        self.deadline = time.monotonic() + inputs.budget.seconds
        self.counts = dict(models=0, tool_calls=0)
        self.store = None
        self.aliases = {}
        self.timing = ResponseTimer()
        capture.observations = {s: [] for s in COLLECTIONS}
        capture.complete = {s: False for s in COLLECTIONS}

    def add(self, source, value):
        with self.lock:
            self.ordinal += 1
            row = dict(deepcopy(plain(value)), _seq=self.ordinal)
            self.capture.observations[source].append(row)
            return row

    def checkpoint(self, source, value):
        self.refresh()
        with self.lock:
            self.counts[source] += 1
            limit = self.inputs.budget.model_calls if source == "models" else self.inputs.budget.tool_calls
            if self.counts[source] > limit or time.monotonic() >= self.deadline:
                self.exhausted = True
                raise BudgetExceeded("Scenario budget exhausted")
            row = self.add(source, dict(value, finished=False))
            gate = Event()
            self.gates.append(gate)
        # Gives the user driver a deterministic intervention point before this call proceeds.
        if not gate.wait(max(0, self.deadline-time.monotonic())):
            self.exhausted = True
            raise BudgetExceeded("Scenario deadline")
        return row

    def release(self):
        with self.lock:
            gates, self.gates = self.gates, []
            for gate in gates:
                gate.set()

    def refresh(self):
        if self.store is None:
            return
        from app.tasks.repository import TaskRepository
        try:
            with self.lock, self.store.transaction(read_only=True) as tx:
                events = tx.all("SELECT * FROM support_events ORDER BY created_at,session_id,sequence")
                for event in events:
                    key = (event["session_id"], event["sequence"])
                    if key in self.seen:
                        continue
                    self.seen.add(key)
                    self.durable_generation += 1
                    event = dict(event, payload=json.loads(event["payload"]))
                    recorded = self.add("events", event)
                    if self.capture.started and event["visibility"] == "public" and event["kind"] == "message" and event["payload"].get("role") == "assistant":
                        # Same ordinal as its authoritative committed event.
                        self.capture.observations["replies"].append(dict(recorded,
                            content=event["payload"].get("content", "")))
                        self.timing.reply(event["session_id"], event["sequence"])
                tasks = []
                for row in tx.all("SELECT * FROM conversation_tasks ORDER BY task_id"):
                    task = TaskRepository(self.store, row["user_id"])._record(row).model_dump(mode="json")
                    task["alias"] = self.aliases.get(task["task_id"])
                    tasks.append(task)
                    if self.versions.get(task["task_id"]) != digest(task):
                        self.add("events", dict(kind="task_snapshot", task=task))
                        self.versions[task["task_id"]] = digest(task)
                        self.durable_generation += 1
                self.capture.observations["tasks"] = tasks
                for job in tx.all("SELECT job_id,session_id,user_id,kind,status,response,error FROM runtime_jobs ORDER BY job_id"):
                    signature = digest(job)
                    if self.jobs.get(job["job_id"]) != signature:
                        self.jobs[job["job_id"]] = signature
                        self.durable_generation += 1
                        self.add("events", dict(kind="execution_snapshot", job=dict(job,
                            response=json.loads(job["response"]) if job["response"] else None)))
                operations = tx.all("SELECT p.operation_id,p.task_id,p.task_version,p.user_id,p.payload,o.status,o.tool,o.result "
                                    "FROM task_operation_proposals p JOIN support_operations o ON o.operation_id=p.operation_id")
                self.capture.observations["operations"] = [dict(r, payload=json.loads(r["payload"]),
                    result=json.loads(r["result"]) if r["result"] else None) for r in operations]
                for step_id, sent in self.sent.items():
                    if step_id in self.capture.boundaries:
                        continue
                    item = tx.one("SELECT association_status FROM message_intake_items WHERE user_id=? AND request_id=?",
                                  (sent["user"], sent["request_id"]))
                    if item and item["association_status"] != "pending":
                        event = self.add("events", dict(kind="committed", step_id=step_id,
                            session_id=sent["session_id"], association_status=item["association_status"]))
                        self.capture.boundaries[step_id] = event["_seq"]
        except Exception:
            self.collection_error = True
            raise


class MeteredClient:
    def __init__(self, client, journal, scene="execution"):
        self.client, self.journal, self.scene = client, journal, scene
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def bind_scene(self, scene):
        client = self.client.bind_scene(scene) if hasattr(self.client, "bind_scene") else self.client
        return MeteredClient(client, self.journal, scene)

    def create(self, **kwargs):
        row = self.journal.checkpoint("models", dict(scene=self.scene, model=kwargs.get("model"),
            request={k: plain(v) for k, v in kwargs.items() if k not in {"api_key", "extra_headers"}}))
        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(**kwargs)
            with self.journal.lock:
                row.update(response=plain(response), success=True)
            return response
        except Exception as exc:
            with self.journal.lock:
                row.update(success=False, error=type(exc).__name__)
            raise
        finally:
            with self.journal.lock:
                row.update(finished=True, seconds=time.monotonic()-started)


def run_inputs(inputs: Inputs, *, client, model, run_id, scenario_id, repetition=1,
               source="scripted_regression", persist=None, store=None, driver=None,
               business_state=None, memory_observer=None):
    """No expected labels are accepted by this interface."""
    from app.agents.engine import AgentEngine
    from app.config.settings import settings
    from .sandbox import isolated_store, message
    from app.tasks.conversation_worker import ConversationWorker
    from app.tasks.operations import TaskOperations
    from app.tasks.repository import TaskRepository
    from app.tasks.rollout import Rollout
    from app.tasks.runtime import current_task
    from app.tools.dispatch import invoke
    from app.tools.request_context import get_current_user_id

    capture = Capture(run_id=run_id, scenario_id=scenario_id, input_digest=digest(inputs.model_dump()),
                      repetition=repetition, source=source, metadata=dict(model=model))
    journal, worker = Journal(capture, inputs), None
    business = Business(inputs, journal)
    if business_state is not None:
        business.objects = deepcopy(business_state["objects"])
        business.receipts = deepcopy(business_state["receipts"])

    def tools(name, arguments):
        ctx = current_task()
        row = journal.checkpoint("tool_calls", dict(tool=name, arguments=deepcopy(arguments),
            user_id=get_current_user_id(), task_id=ctx.record.task_id if ctx else None,
            task_version=ctx.record.version if ctx else None))
        try:
            result = invoke(name, arguments)
            with journal.lock:
                row.update(result=plain(result), finished=True)
            return result
        except Exception as exc:
            with journal.lock:
                row.update(error=type(exc).__name__, finished=True)
            raise

    class ObservedEngine(AgentEngine):
        def _observe(self, method, context):
            row = journal.add("events", dict(kind=method+"_context", context=plain(context)))
            try:
                # Association currently has no bind_scene in its production adapter.
                if method == "associate":
                    from app.agents.association import propose
                    result = propose(self.client.bind_scene("association"), self.model, context)
                else:
                    result = getattr(super(), method)(context)
                with journal.lock:
                    row["output"] = plain(result)
                return result
            except Exception as exc:
                with journal.lock:
                    row["error"] = type(exc).__name__
                raise

        def associate(self, context):
            return self._observe("associate", context)

        def rewrite(self, context):
            return self._observe("rewrite", context)

        def compose_reply(self, context):
            return self._observe("compose_reply", context)

    def save():
        with journal.lock:
            capture.metadata["latency"] = journal.timing.snapshot()
            if driver:
                capture.metadata["simulation"] = driver.summary()
            capture.metadata["calls"] = dict(journal.counts)
            capture.metadata["elapsed_seconds"] = time.monotonic() - (journal.deadline-inputs.budget.seconds)
            if persist:
                persist(capture.model_copy(deep=True))

    def send(step):
        user = inputs.sessions[step.session]
        journal.refresh()  # Attribute already-published events before starting the next request.
        journal.timing.begin(step)
        if step.action == "message":
            request = run_id + ":" + step.id
            worker.intake.receive(step.session, user, request_id=request, content=step.text)
            journal.sent[step.id] = dict(user=user, request_id=request, session_id=step.session)
            journal.add("events", dict(kind="user_step", step_id=step.id, session_id=step.session, text=step.text))
        else:
            ops = TaskOperations(journal.store, user)
            cards = [c for c in ops.list(step.session) if selected(c, step.card)]
            if len(cards) != 1 or (step.action == "confirm" and not cards[0]["can_confirm"]):
                raise ValueError("Confirmation selector is ambiguous or unavailable")
            card = cards[0]
            journal.add("events", dict(kind="user_confirmation", step_id=step.id,
                action=step.action, card={k: v for k, v in card.items() if k != "confirmation_token"}))
            if step.action == "confirm":
                ops.confirm(step.session, card["operation_id"], card["confirmation_token"])
            else:
                ops.cancel(step.session, card["operation_id"])
            boundary = journal.add("events", dict(kind="committed", step_id=step.id, session_id=step.session))
            capture.boundaries[step.id] = boundary["_seq"]

    def triggered(step):
        kind = step.trigger.kind
        if kind == "confirmation":
            # Never hold the journal lock across the business adapter's lock.
            rows = TaskOperations(journal.store, inputs.sessions[step.session]).list(step.session)
            rows = [{k: v for k, v in row.items() if k != "confirmation_token"} for row in rows]
            return sum(selected(row, step.trigger.where) for row in rows) >= step.trigger.occurrence
        with journal.lock:
            if kind == "initial":
                return True
            if kind in {"reply", "model_call", "tool_start"}:
                rows = capture.observations[{"reply": "replies", "model_call": "models", "tool_start": "tool_calls"}[kind]]
            else:
                rows = [r for r in capture.observations["events"] if r["kind"] == "committed"]
            return sum(selected(row, step.trigger.where) for row in rows) >= step.trigger.occurrence

    try:
        with (nullcontext(store) if store is not None else isolated_store(sessions=inputs.sessions.items())) as store, ExitStack() as stack:
            stack.enter_context(business.installed())
            journal.store = store
            if memory_observer is not None:
                memory_observer.journal = journal
            # Change only deployment dependencies; keep actual production decision logic.
            flow_tools = {"read_task_context", "ask_user", "handoff_to_human"}
            if memory_observer is not None:
                # Both paired arms expose the same tool. The observer gates
                # actual reads, rather than making one arm's schema different.
                flow_tools.add('recall_user_memory')
            overrides = dict(api_jwt_secret="evaluation-only-signing-key-"+run_id,
                task_operations_enabled=True, reply_composition_enabled=True,
                tool_allowlist=",".join(sorted({r.tool for r in [*inputs.reads, *inputs.writes]} |
                    flow_tools)))
            for key, value in overrides.items():
                stack.enter_context(patch.object(settings, key, value))
            capture.metadata["configuration"] = dict(overrides, api_jwt_secret="[ephemeral]",
                query_rewrite_prefetch=settings.query_rewrite_prefetch,
                chat_completion_options_digest=digest(settings.chat_completion_options))
            for sid, user in inputs.sessions.items():
                store.ensure(sid, user)
            Rollout(store).set_mode("all", reason="isolated evaluation")
            aliases = {}
            for seed in inputs.seeds:
                user = inputs.sessions[seed.session]
                origin = message(store, seed.goal, seed.session, user)
                repo = TaskRepository(store, user)
                record = repo.create(seed.session, change_id=run_id+seed.alias, source=origin,
                                     goal=seed.goal, details=seed.details)
                if seed.question:
                    repo.ask(record.task_id, seed.session, expected_version=record.version,
                        change_id=run_id+seed.alias+":ask", source=origin,
                        question=dict(field="details", prompt=seed.question, options=[]))
                from app.tasks.replies import ReplyCoordinator
                # A prior conversation must be registered for publication just as real intake
                # registers it. A waiting question is also actually published here.
                ReplyCoordinator(store, user).publish(seed.session)
                aliases[seed.alias] = record.task_id
            capture.metadata["seed_aliases"] = aliases
            journal.aliases = {v: k for k, v in aliases.items()}
            engine = ObservedEngine("others", client=MeteredClient(client, journal), model=model)
            worker = ConversationWorker(store, engine, tools=tools, concurrency=2)
            def shutdown():
                # Close while fake services/settings and the temp database are still installed.
                journal.release()
                if worker is not None:
                    worker.close()
            stack.callback(shutdown)
            journal.refresh()  # Initial history is evidence, not a newly generated reply.
            initial = driver.next_step(journal, store) if driver else inputs.steps[0]
            if initial is None:
                raise ValueError("User simulation ended before sending a message")
            send(initial)
            capture.started = True
            index = 1
            draining = False
            scanned_events = -1
            save()
            while time.monotonic() < journal.deadline:
                # Once durable work is idle, drain the last publication future instead of
                # continuously creating new empty publication futures and racing their completion.
                if not draining:
                    journal.refresh()
                    scanned_events = journal.durable_generation
                    worker.tick()
                journal.refresh()
                if not driver and index < len(inputs.steps) and triggered(inputs.steps[index]):
                    send(inputs.steps[index])
                    index += 1
                    draining = False
                journal.release()
                save()
                if journal.exhausted:
                    capture.termination = "budget"
                    break
                with store.transaction(read_only=True) as tx:
                    active = tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status IN ('pending','dispatched','running')")["n"]
                    pending = tx.one("SELECT COUNT(*) AS n FROM message_intake_items WHERE association_status='pending'")["n"]
                    wakes = tx.one("SELECT COUNT(*) AS n FROM conversation_tasks t JOIN task_execution_wakes w "
                                   "ON t.task_id=w.task_id WHERE t.version<>w.version")["n"]
                futures = [*worker.associations.values(), *worker.executions.futures.values(), *worker.reply_futures.values()]
                if worker.receipt_future:
                    futures.append(worker.receipt_future)
                if worker.rewrite_ahead:
                    futures.extend(p.future for p in worker.rewrite_ahead.pending.values())
                draining = not (active or pending or wakes)
                if not (active or pending or wakes) and all(f.done() for f in futures):
                    # A reply may have committed between refresh() and done(). Observe it before
                    # deciding whether the next user step can fire.
                    journal.refresh()
                    if journal.durable_generation != scanned_events:
                        # A result may commit after this tick's empty publication check.
                        # Scan once more against the latest durable generation before stopping.
                        draining = False
                        continue
                    if driver:
                        journal.timing.settle()  # Exclude the following simulator/reviewer call.
                        following = driver.next_step(journal, store)
                        if following is not None:
                            send(following)
                            draining = False
                            continue
                    elif index < len(inputs.steps) and triggered(inputs.steps[index]):
                        journal.timing.settle()
                        send(inputs.steps[index])
                        index += 1
                        draining = False
                        continue
                    # No label-based early stopping: unfinished user script is a driver mismatch.
                    journal.timing.settle()
                    capture.termination = "settled" if driver or index == len(inputs.steps) else "driver_mismatch"
                    break
                time.sleep(0.01)
            else:
                capture.termination = "timeout"
            journal.release()
            # Save partial evidence before shutdown; supervisor may kill a hung SDK thread.
            journal.timing.stop(capture.termination)
            save()
            worker.close()
            worker = None
            journal.refresh()
            with business.lock:
                capture.observations["objects"] = [dict(state_key=k, **deepcopy(v)) for k, v in business.objects.items()]
                if business_state is not None:
                    business_state.update(objects=deepcopy(business.objects), receipts=deepcopy(business.receipts))
            finished = all(r.get("finished") for name in ("models", "tool_calls") for r in capture.observations[name])
            capture.complete = {s: not journal.collection_error and bool(finished) for s in COLLECTIONS}
            save()
    except Exception as exc:
        capture.error = type(exc).__name__
        capture.termination = ("budget" if isinstance(exc, BudgetExceeded) else "error") if capture.started else "not_started"
        journal.timing.stop(capture.termination)
        journal.release()
        save()
    if driver:
        capture.metadata["simulation"] = driver.summary()
    return capture


def save_capture(path: Path, capture):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(capture.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)

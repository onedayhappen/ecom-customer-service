"""Task confirmation contracts, including races and uncertain business outcomes."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.support.store import SupportError, SupportStore
from app.tasks.operations import TaskOperations
from app.tasks.repository import TaskRepository
from app.tasks.intake import MessageIntake
from app.tools.request_context import get_current_user_id
from tests.test_task_candidates import task
from tests.test_task_storage import empty_store, message
from tests.test_message_association import store


class Business:
    def __init__(self):
        self.writes, self.queries, self.receipts = [], [], {}
        self.before_write = self.before_preview = None
        self.uncertain = False

    def preview(self, record, action):
        assert get_current_user_id() == record.user_id
        from app.execution.task_proposals import fixed_arguments, verify_whole_order
        args = fixed_arguments(record, action)
        items = verify_whole_order(record, dict(order_id=args["order_id"], items=[dict(name="耳机", quantity=1)]))
        if self.before_preview:
            self.before_preview()
        return dict(action=action, arguments=args, business_version="business-v1", review=dict(
            title="申请整单退款", order_id=args["order_id"], items=items, quantity=1,
            reason=args.get("reason"), amount_minor=12999, amount_kind="order_reference", is_demo=True,
            impact="退款金额以审核为准"))

    def write(self, user, action, args, oid, version):
        assert get_current_user_id() == user
        self.writes.append(oid)
        if self.before_write:
            self.before_write()
        if self.uncertain:
            raise TimeoutError("unknown transport outcome")
        result = dict(success=True, request_id=oid, order_id=args["order_id"], status="submitted")
        self.receipts[oid] = dict(status="succeeded", action=action, result=result)
        return result

    def query(self, user, oid):
        assert get_current_user_id() == user
        self.queries.append(oid)
        return self.receipts.get(oid, dict(status="unknown"))


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setattr(settings, "task_operations_enabled", True)
    monkeypatch.setattr(settings, "api_jwt_secret", "isolated-test-confirmation-signing-key-long-enough")
    monkeypatch.setattr(settings, "api_concurrency_redis_url", "")


def ready(store, business=None, *, order="ORD-A", sid="s"):
    record, source = task(store, sid, goal="申请整单退款", details=dict(order_id=order, reason="不想要了"))
    service = TaskOperations(store, "alice", business=business or Business())
    preparation = service.prepare(sid, record.task_id, record.version, "apply_refund")
    assert "confirmation_token" not in preparation
    card = next(c for c in service.list(sid) if c["operation_id"] == preparation["operation_id"])
    return service, record, source, card


def confirm(service, card, sid="s"):
    return service.confirm(sid, card["operation_id"], card["confirmation_token"])


def test_retired_backend_preserves_old_cards_but_cannot_submit(store):
    service, record, _, card = ready(store)
    disabled = TaskOperations(store, "alice")
    view = disabled.list("s")[0]
    assert view["status"] == "prepared" and view["can_confirm"] is False
    assert view["confirmation_token"] is None and "已移除" in view["error"]
    with pytest.raises(SupportError, match="已移除"):
        disabled.prepare("s", record.task_id, record.version, "apply_refund")
    with pytest.raises(SupportError, match="已移除"):
        confirm(disabled, card)
    assert disabled.list("s")[0]["status"] == "prepared"
    assert service.business.writes == []


def test_retired_backend_keeps_completed_receipts_readable(store):
    service, _, _, card = ready(store)
    confirm(service, card)
    disabled = TaskOperations(store, "alice")
    assert disabled.list("s")[0]["status"] == "succeeded"
    assert confirm(disabled, card)["status"] == "succeeded"
    assert len(service.business.writes) == 1


def change(store, record, patch, sid="s"):
    return TaskRepository(store, "alice").update(record.task_id, sid, expected_version=record.version,
        change_id=str(uuid4()), source=message(store, sid, "alice", "修改需求"), patch=patch)


def test_prepare_confirm_and_duplicate_recover_same_receipt(store):
    service, record, _, card = ready(store)
    assert card["can_confirm"] and card["review"]["amount_kind"] == "order_reference"
    result = confirm(service, card)
    assert result["status"] == "succeeded" and not result["can_confirm"]
    assert confirm(service, card) == result
    assert service.business.writes == [card["operation_id"]]
    assert get_current_user_id() is None
    current = TaskRepository(store, "alice").get(record.task_id)
    assert current.status == "waiting_external" and current.waiting_for["operation_id"] == card["operation_id"]
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM support_operations")["n"] == 1
        assert tx.one("SELECT COUNT(*) AS n FROM support_events WHERE kind='task_operation_result'")["n"] == 1
        assert not tx.one("SELECT * FROM task_operation_resources")
    assert sum("退款申请已登记" in m["content"] for m in store.public_history("s", "alice")) == 1


@pytest.mark.parametrize("mutation", ["object", "quantity", "cancel", "pause", "handoff", "resumed", "pending", "expiry", "key", "token", "payload"])
def test_changed_or_unresolved_inputs_prevent_old_confirmation(store, monkeypatch, mutation):
    service, record, _, card = ready(store)
    if mutation in {"object", "quantity"}:
        change(store, record, dict(details={**record.details, **({"order_id": "ORD-B"} if mutation == "object" else {"quantity": 2})}))
    elif mutation in {"cancel", "pause"}:
        change(store, record, dict(status="cancelled" if mutation == "cancel" else "paused"))
    elif mutation in {"handoff", "resumed"}:
        store.handoff("s", "alice")
        if mutation == "resumed":
            store.resume("s", "alice")
    elif mutation == "pending":
        MessageIntake(store).receive("old", "alice", request_id="wait", content="先别退")
    elif mutation == "expiry":
        from app.tasks.operations import clock
        monkeypatch.setattr("app.tasks.operations.clock", lambda tx: clock(tx) + 601)
    elif mutation == "key":
        monkeypatch.setattr(settings, "api_jwt_secret", "rotated-confirmation-signing-secret-long-enough")
    elif mutation == "token":
        card["confirmation_token"] = "untrusted-token"
    else:
        with store.transaction() as tx:
            row = tx.one("SELECT payload FROM task_operation_proposals")
            value = json.loads(row["payload"])
            value["prepared"]["arguments"]["order_id"] = "ORD-B"
            tx.execute("UPDATE task_operation_proposals SET payload=?", (json.dumps(value),))
    if mutation in {'handoff', 'resumed'}:
        assert confirm(service, card)['status'] == 'cancelled'
    else:
        with pytest.raises(SupportError):
            confirm(service, card)
    assert service.business.writes == []
    if mutation != "token":
        assert service.list("s")[0]["can_confirm"] is False


def test_unrelated_matter_change_keeps_confirmation_valid(store):
    service, _, _, card = ready(store)
    other, _ = task(store, "old", details=dict(order_id="ORD-B"))
    change(store, other, dict(details=dict(order_id="ORD-B", reason="慢了")), "old")
    assert confirm(service, card)["status"] == "succeeded"


def test_cross_session_duplicate_and_foreign_user_cannot_act(store):
    service, record, _, _ = ready(store)
    record = change(store, record, dict(details=record.details), "old")
    service.prepare("old", record.task_id, record.version, "apply_refund")
    card = service.list("old")[0]
    assert confirm(service, card, "old")["status"] == "succeeded"
    assert confirm(service, card, "s")["status"] == "succeeded"
    assert len(service.business.writes) == 1
    with pytest.raises(SupportError):
        TaskOperations(store, "bob", business=service.business).confirm("other", card["operation_id"], card["confirmation_token"])
    with pytest.raises(SupportError):
        service.cancel("old2", card["operation_id"])


def test_cancel_before_start_and_after_start_are_distinct(store):
    service, _, _, card = ready(store)
    assert service.cancel("s", card["operation_id"])["status"] == "cancelled"
    assert confirm(service, card)["status"] == "cancelled"
    assert not service.business.writes
    service, _, _, card = ready(store, order="ORD-B")
    service.business.before_write = lambda: service.cancel("s", card["operation_id"])
    assert confirm(service, card)["status"] == "succeeded"
    assert store.state("s", "alice")["mode"] == "handoff_pending"
    assert len(service.business.queries) == 1


def test_parallel_duplicate_confirms_only_one_write_and_no_transaction_during_io(store):
    service, _, _, card = ready(store)
    started, release = threading.Event(), threading.Event()
    def write():
        started.set()
        assert release.wait(10)
    service.business.before_write = write
    with ThreadPoolExecutor(2) as workers:
        future = workers.submit(confirm, service, card)
        assert started.wait(5)
        try:
            replay = workers.submit(confirm, service, card).result(timeout=5)
            assert replay["status"] == "running"
            # Independent intake can commit while the business system waits.
            MessageIntake(store).receive("s", "alice", request_id="late", content="先别退")
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "succeeded"
    assert len(service.business.writes) == 1


def test_unknown_is_queried_and_never_resubmitted_even_after_restart(store):
    service, _, _, card = ready(store)
    service.business.uncertain = True
    assert confirm(service, card)["status"] == "unknown"
    assert len(service.business.queries) == 1
    assert store.state("s", "alice")["mode"] == "handoff_pending"
    restarted = TaskOperations(SupportStore(store.dsn), "alice", business=service.business)
    assert confirm(restarted, card)["status"] == "unknown"
    assert restarted.reconcile(card["operation_id"])["status"] == "unknown"
    assert len(service.business.writes) == 1
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT * FROM task_operation_resources")


def test_timeout_after_business_commit_recovers_receipt_without_retry(store):
    service, _, _, card = ready(store)
    original = service.business.write
    def committed(*args):
        original(*args)
        raise TimeoutError()
    service.business.write = committed
    assert confirm(service, card)["status"] == "succeeded"
    assert len(service.business.writes) == 1 and len(service.business.queries) == 1


def test_crash_after_business_commit_recovered_by_query(store, monkeypatch):
    service, _, _, card = ready(store)
    monkeypatch.setattr(service, "finish", lambda *a: (_ for _ in ()).throw(RuntimeError("process lost")))
    with pytest.raises(RuntimeError):
        confirm(service, card)
    assert service.list("s")[0]["status"] == "running"
    with store.transaction() as tx:
        tx.execute("UPDATE task_operation_proposals SET run_deadline=0")
    restarted = TaskOperations(SupportStore(store.dsn), "alice", business=service.business)
    assert restarted.reconcile(card["operation_id"])["status"] == "succeeded"
    assert len(service.business.writes) == 1


def test_related_changes_during_preview_prevent_preparing_card(store):
    business = Business()
    record, _ = task(store, "s", details=dict(order_id="ORD-A", reason="不想要了"))
    business.before_preview = lambda: change(store, record, dict(status="cancelled"))
    with pytest.raises(SupportError):
        TaskOperations(store, "alice", business=business).prepare("s", record.task_id, 1, "apply_refund")
    with store.transaction(read_only=True) as tx:
        assert not tx.one("SELECT * FROM support_operations")


def test_later_user_change_is_not_overwritten_by_success_receipt(store):
    service, record, _, card = ready(store)
    service.business.before_write = lambda: change(store, record, dict(details=dict(order_id="ORD-B")))
    assert confirm(service, card)["status"] == "succeeded"
    current = TaskRepository(store, "alice").get(record.task_id)
    assert current.details == dict(order_id="ORD-B") and current.status == "in_progress"
    assert "ORD-A" in store.public_history("s", "alice")[-1]["content"]


def test_success_for_wrong_object_never_becomes_success(store):
    service, _, _, card = ready(store)
    service.business.write = lambda *args: dict(success=True, request_id=card["operation_id"], order_id="ORD-B")
    assert confirm(service, card)["status"] == "unknown"


def test_shared_order_lock_blocks_other_task_but_not_other_order(store):
    service, _, _, card = ready(store)
    other, _, _, same = ready(store, service.business)
    different, _, _, unrelated = ready(store, service.business, order="ORD-B")
    def inside_write():
        with pytest.raises(SupportError):
            confirm(other, same)
        # Do not recurse the test hook.
        service.business.before_write = None
        assert confirm(different, unrelated)["status"] == "succeeded"
    service.business.before_write = inside_write
    assert confirm(service, card)["status"] == "succeeded"
    assert len(service.business.writes) == 2


def test_flag_disabled_blocks_all_preparation(store, monkeypatch):
    monkeypatch.setattr(settings, "task_operations_enabled", False)
    record, _ = task(store, "s")
    with pytest.raises(SupportError) as exc:
        TaskOperations(store, "alice", business=Business()).prepare("s", record.task_id, 1, "apply_refund")
    assert exc.value.status == 404


def test_confirm_cancel_race_has_a_single_winner(store):
    service, _, _, card = ready(store)
    barrier = threading.Barrier(2)
    def action(kind):
        barrier.wait(timeout=10)
        return confirm(service, card) if kind == "confirm" else service.cancel("s", card["operation_id"])
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(action, ["confirm", "cancel"]))
    status = service.list("s")[0]["status"]
    assert status in {"cancelled", "succeeded"}
    assert len(service.business.writes) == (1 if status == "succeeded" else 0)
    if results[1]["status"] == "cancelled":
        assert status == "cancelled"


def test_recovery_worker_queries_expired_reservation_without_repeating_write(store, monkeypatch):
    from app.tasks.conversation_worker import ConversationWorker
    from tests.test_task_execution import engine, Client
    service, _, _, card = ready(store)
    with store.transaction() as tx:
        tx.execute("UPDATE support_operations SET status='running'")
        tx.execute("UPDATE task_operation_proposals SET run_deadline=0")
    monkeypatch.setattr("app.tasks.operations.backend", lambda: service.business)
    worker = ConversationWorker(store, engine(Client()))
    try:
        worker.recover_operations()
        assert worker.receipt_future is not None
        worker.receipt_future.result(timeout=15)
        assert service.list("s")[0]["status"] == "unknown"
        worker.recover_operations()
        assert worker.receipt_future is None
        assert not service.business.writes and len(service.business.queries) == 1
    finally:
        worker.close()


def test_atomic_receipt_failure_rolls_back_progress_and_public_message(store, monkeypatch):
    service, record, _, card = ready(store)
    original = store._event
    def fail(tx, control, kind, *args, **kw):
        if kind == "message" and kw.get("key", "").startswith("operation-public:"):
            raise RuntimeError("notification unavailable")
        return original(tx, control, kind, *args, **kw)
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        confirm(service, card)
    assert TaskRepository(store, "alice").get(record.task_id).version == 1
    with store.transaction(read_only=True) as tx:
        assert not tx.one("SELECT * FROM support_events WHERE kind='task_operation_result'")
        assert tx.one("SELECT status FROM support_operations")["status"] == "running"
    monkeypatch.setattr(store, "_event", original)
    with store.transaction() as tx:
        tx.execute("UPDATE task_operation_proposals SET run_deadline=0")
    assert service.reconcile(card["operation_id"])["status"] == "succeeded"
    assert len(service.business.writes) == 1


@pytest.mark.parametrize("structured", [True, False])
def test_cancel_message_after_start_saves_request_and_hands_off_without_claiming_undo(store, structured):
    from tests.test_message_association import batch_for, service as associate, update, proposal
    operations, record, _, card = ready(store)
    def cancel_after_start():
        if structured:
            MessageIntake(store).receive("s", "alice", request_id="late-cancel", content="ORD-A 先别退",
                action="cancel", task_id=record.task_id, task_version=record.version)
        else:
            batch = batch_for(store, "ORD-A 先别退")
            association = associate(store)
            result = association.commit(batch, association.snapshot(batch), proposal(update(batch, record, effect="cancel")))
            assert result["needs_human"]
    operations.business.before_write = cancel_after_start
    assert confirm(operations, card)["status"] == "succeeded"
    assert store.state("s", "alice")["mode"] == "handoff_pending"
    assert TaskRepository(store, "alice").get(record.task_id).status == "cancelled"
    assert len(operations.business.writes) == 1


def test_v17_migration_rolls_back_and_preserves_original_receipts(store, monkeypatch):
    from app.tasks import migrations
    with store.transaction() as tx:
        tx.execute("INSERT INTO support_operations VALUES ('legacy','s',0,'apply_refund','succeeded','old','{}')")
        tx.execute("DROP TABLE task_external_receipts")
        tx.execute("DROP TABLE task_external_waits")
        tx.execute("DROP TABLE task_handoffs")
        tx.execute("DROP TABLE task_operation_resources")
        tx.execute("DROP TABLE task_operation_proposals")
        tx.execute("DELETE FROM support_schema_versions WHERE version>16")
        tx.execute("INSERT INTO support_schema_versions VALUES (16,'before-upgrade') ON CONFLICT DO NOTHING")
    upgraded = SupportStore(store.dsn)
    original = migrations.migrate_task_operations
    def fail(tx):
        original(tx)
        raise RuntimeError("migration interrupted")
    monkeypatch.setattr(migrations, "migrate_task_operations", fail)
    with pytest.raises(RuntimeError):
        upgraded.initialize()
    with upgraded._connection() as tx:
        assert tx.one("SELECT MAX(version) AS v FROM support_schema_versions")["v"] == 16
        assert tx.one("SELECT status FROM support_operations WHERE operation_id='legacy'")["status"] == "succeeded"
    monkeypatch.setattr(migrations, "migrate_task_operations", original)
    upgraded.initialize()
    SupportStore(store.dsn).initialize()
    with upgraded.transaction(read_only=True) as tx:
        from app.support.store import SCHEMA_VERSION
        assert tx.one("SELECT MAX(version) AS v FROM support_schema_versions")["v"] == SCHEMA_VERSION
        assert tx.one("SELECT COUNT(*) AS n FROM task_operation_proposals")["n"] == 0


def test_manual_resolution_releases_resource_and_keeps_private_notes_out_of_card(store, monkeypatch):
    service, _, _, card = ready(store)
    service.business.uncertain = True
    assert confirm(service, card)["status"] == "unknown"
    monkeypatch.setattr(store, "_role", lambda tx, actor: "admin" if actor == "admin" else "customer")
    with pytest.raises(SupportError):
        store.reconcile(card["operation_id"], "alice", "failed", "not authorized")
    store.reconcile(card["operation_id"], "admin", "failed", "private support investigation notes")
    view = service.list("s")[0]
    assert view["status"] == "failed" and view["result"]["verification_source"] == "human"
    assert "private support" not in json.dumps(view)
    with store.transaction(read_only=True) as tx:
        assert not tx.one("SELECT * FROM task_operation_resources")
    # A late response is still evidence and must reach human review.
    store.resume("s", "alice")
    service.finish(card["operation_id"], dict(success=True, request_id=card["operation_id"], order_id="ORD-A", status="submitted"))
    assert store.state("s", "alice")["mode"] == "handoff_pending"
    assert service.list("s")[0]["status"] == "unknown"
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT * FROM support_events WHERE kind='operation_late_receipt'")
        assert tx.one("SELECT * FROM task_operation_resources")
    service.business.receipts[card["operation_id"]] = dict(status="succeeded", action="apply_refund",
        result=dict(success=True, request_id=card["operation_id"], order_id="ORD-A", status="submitted"))
    assert service.reconcile(card["operation_id"])["status"] == "succeeded"


def test_confirmation_rechecks_related_scheduling_evidence_not_just_own_version(store):
    from tests.test_task_execution import Client, engine, request, response, order_result
    from app.tasks.execution_models import TaskExecutionInput
    from app.tasks.scheduling import TaskSchedule
    parent, source = task(store, "s")
    from tests.test_coordinated_replies import ready as seed_evidence
    receipt = {"job_id": seed_evidence(store, parent, source)}
    child, source = task(store, "s", details=dict(order_id="ORD-B", reason="不想要了"))
    req = TaskExecutionInput.model_validate(request(child, source))
    TaskSchedule(store, "alice").enqueue(req.model_dump(), dependencies=[dict(
        kind="query", job_id=receipt["job_id"], tool="query_order", field="order.status", value="已发货")])
    service = TaskOperations(store, "alice", business=Business())
    reservation, _ = service.runs.begin(req)
    service.prepare("s", child.task_id, child.version, "apply_refund", fence=(req, reservation["payload"]))
    card = service.list("s")[0]
    assert card["can_confirm"]
    change(store, parent, dict(details={**parent.details, "reason": "之前信息不准确"}))
    with pytest.raises(SupportError):
        confirm(service, card)
    assert not service.business.writes


def test_handoff_in_another_linked_session_wins_before_confirmation_or_preserves_actual_receipt(store):
    record, _ = task(store, "s", details=dict(order_id="ORD-A", reason="不想要了"))
    record = change(store, record, dict(details=record.details), "old")
    operations = TaskOperations(store, "alice", business=Business())
    operations.prepare("s", record.task_id, record.version, "apply_refund")
    card = operations.list("s")[0]
    barrier = threading.Barrier(2)
    def submit():
        barrier.wait(10)
        try:
            return confirm(operations, card)["status"]
        except SupportError:
            return "blocked"
    def handoff():
        barrier.wait(10)
        return store.handoff("old", "alice")
    with ThreadPoolExecutor(2) as pool:
        future, control = pool.submit(submit), pool.submit(handoff)
        result = future.result(timeout=15)
        control.result(timeout=15)
    assert result in {"blocked", "cancelled", "succeeded"}
    assert len(operations.business.writes) == (1 if result == "succeeded" else 0)


def test_handoff_during_business_io_retains_receipt_without_automatic_task_progress(store):
    record, _ = task(store, "s", details=dict(order_id="ORD-A", reason="不想要了"))
    record = change(store, record, dict(details=record.details), "old")
    operations = TaskOperations(store, "alice", business=Business())
    operations.prepare("s", record.task_id, record.version, "apply_refund")
    card = operations.list("s")[0]
    operations.business.before_write = lambda: store.handoff("old", "alice")
    assert confirm(operations, card)["status"] == "succeeded"
    assert TaskRepository(store, "alice").get(record.task_id).version == record.version


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1.001", -1, True, None])
def test_money_must_be_exact_nonnegative_minor_units(value):
    from app.execution.task_proposals import minor_units, PreparationRejected
    with pytest.raises(PreparationRejected):
        minor_units(value)


@pytest.mark.parametrize("value,expected", [("129.99", 12999), (0, 0), (1.01, 101)])
def test_money_exact_values(value, expected):
    from app.execution.task_proposals import minor_units
    assert minor_units(value) == expected

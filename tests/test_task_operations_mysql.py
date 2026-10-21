"""Real MySQL transactions on a fixed dedicated test database, never ORDER_DB_DSN."""
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
import os
from uuid import uuid4

import pytest

from app.data.commerce_dao import MySqlCommerceDAO
from app.data.order_dao import MySqlOrderDAO
from app.execution.business_operations import BusinessOperations
from app.tasks.operations import TaskOperations
from app.tools.request_context import set_current_user_id, reset_current_user_id
from tests.test_task_candidates import task
from tests.test_task_storage import empty_store
from tests.test_message_association import store
from tests.test_task_operations import flags

DSN = "mysql://test_runner:isolated_test_only@127.0.0.1:23306/ecom_operations_test"
pytestmark = pytest.mark.skipif(os.environ.get("ECOM_INTEGRATION") != "1", reason="dedicated MySQL only")


@pytest.fixture
def business_db(monkeypatch):
    from scripts.init_commerce_db import demo_records
    db = MySqlCommerceDAO(DSN)
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS db")
        assert cur.fetchone()["db"] == "ecom_operations_test"
        MySqlOrderDAO.ensure_schema(conn)
    db.ensure_schema()
    policies = [r for r in demo_records([], []) if r["kind"] == "policies"]
    db.insert_seed(policies)
    order = "ORD-" + uuid4().hex
    with db._connection() as conn:
        MySqlOrderDAO.upsert(conn, dict(order_id=order, user_id="alice", status="shipped", total="129.99",
            items=[dict(name="耳机", quantity=1)]), "pdd")
    monkeypatch.setattr("app.tools.commerce_service.database", lambda: db)
    try:
        yield db, order
    finally:
        with db._connection() as conn, conn.cursor() as cur:
            for table in ("commerce_mutations",):
                cur.execute(f"DELETE FROM {table} WHERE user_id='alice' AND order_id=%s", (order,))
            cur.execute("DELETE FROM commerce_invoices WHERE user_id='alice' AND object_id=%s", (order,))
            cur.execute("DELETE FROM orders WHERE user_id='alice' AND order_id=%s", (order,))
        while not db._pool.empty():
            db._pool.get_nowait().close()


def prepared(store, order, action="apply_refund"):
    details = dict(order_id=order, reason="不想要了") if action == "apply_refund" else dict(
        order_id=order, invoice_title="张三", invoice_type="个人")
    record, _ = task(store, "s", details=details)
    operations = TaskOperations(store, "alice")
    operations.prepare("s", record.task_id, 1, action)
    return operations, operations.list("s")[0]


def test_refund_commits_with_business_receipt_and_exact_replay(store, business_db):
    db, order = business_db
    operations, card = prepared(store, order)
    first = operations.confirm("s", card["operation_id"], card["confirmation_token"])
    assert first["status"] == "succeeded", first
    assert first["result"]["is_demo"] and first["result"]["status"] == "submitted"
    identity = set_current_user_id("alice")
    try:
        backend = BusinessOperations()
        with store.transaction(read_only=True) as tx:
            import json
            p = json.loads(tx.one("SELECT payload FROM task_operation_proposals")["payload"])["prepared"]
        again = backend.write("alice", "apply_refund", p["arguments"], card["operation_id"], p["business_version"])
        assert again["success"] and again["replayed"]
        assert backend.query("alice", card["operation_id"])["status"] == "succeeded"
    finally:
        reset_current_user_id(identity)
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM commerce_mutations WHERE order_id=%s", (order,))
        assert cur.fetchone()["n"] == 1


def test_changed_business_state_rejects_stale_confirmation(store, business_db):
    db, order = business_db
    operations, card = prepared(store, order)
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET status='cancelled' WHERE order_id=%s", (order,))
    assert operations.confirm("s", card["operation_id"], card["confirmation_token"])["status"] == "failed"
    assert db.mutation_status("alice", card["operation_id"])["status"] == "unknown"


def test_real_invoice_and_refund_competing_previews_recheck_same_order(store, business_db):
    db, order = business_db
    operations, refund = prepared(store, order)
    _, invoice = prepared(store, order, "issue_invoice")
    assert invoice["review"]["amount_kind"] == "invoice_amount"
    assert operations.confirm("s", refund["operation_id"], refund["confirmation_token"])["status"] == "succeeded"
    # Task resource lock is released after success. MySQL still rejects stale qualification.
    assert operations.confirm("s", invoice["operation_id"], invoice["confirmation_token"])["status"] == "failed"


def test_invoice_write_uses_same_operation_id_and_local_demo_only(store, business_db):
    db, order = business_db
    operations, card = prepared(store, order, "issue_invoice")
    result = operations.confirm("s", card["operation_id"], card["confirmation_token"])
    assert result["status"] == "succeeded", result
    assert result["result"]["invoice"]["status"] == "issued_local"
    assert db.mutation_status("alice", card["operation_id"])["status"] == "succeeded"


def test_reissue_preserves_original_invoice_and_replays_receipt(store, business_db):
    db, order = business_db
    operations, card = prepared(store, order, "issue_invoice")
    first = operations.confirm("s", card["operation_id"], card["confirmation_token"])
    original = first["result"]["invoice"]["invoice_id"]
    record, _ = task(store, "s", details=dict(order_id=order, invoice_title="李四", invoice_type="个人", invoice_id=original))
    new = operations.prepare("s", record.task_id, 1, "reissue_invoice")
    card = next(c for c in operations.list("s") if c["operation_id"] == new["operation_id"])
    assert card["review"]["original_invoice_id"] == original
    result = operations.confirm("s", card["operation_id"], card["confirmation_token"])
    assert result["status"] == "succeeded", result
    assert operations.confirm("s", card["operation_id"], card["confirmation_token"]) == result
    rows = db.records("invoices", user_id="alice", object_id=order)
    assert sorted(r["payload"]["status"] for r in rows) == ["issued_local", "replaced_local"]


def test_business_row_lock_serializes_distinct_operation_ids(store, business_db):
    db, order = business_db
    operations, card = prepared(store, order)
    with store.transaction(read_only=True) as tx:
        import json
        p = json.loads(tx.one("SELECT payload FROM task_operation_proposals")["payload"])["prepared"]
    def write(oid):
        identity = set_current_user_id("alice")
        try:
            return BusinessOperations().write("alice", "apply_refund", p["arguments"], oid, p["business_version"])
        finally:
            reset_current_user_id(identity)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, [str(uuid4()), str(uuid4())]))
    assert sum(r["success"] is True for r in results) == 1, results
    assert any(r.get("error_code") == "confirmation_invalid" for r in results)


def process_confirm(dsn, oid, token, crash):
    """A separate application process with its own business connection pool."""
    from app.config.settings import settings
    from app.support.store import SupportStore
    from app.tools import commerce_service
    settings.task_operations_enabled = True
    settings.short_term_memory_enabled = settings.memory_enabled = False
    settings.api_jwt_secret = "isolated-test-confirmation-signing-key-long-enough"
    commerce_service.database = lambda: MySqlCommerceDAO(DSN)
    operations = TaskOperations(SupportStore(dsn), "alice")
    if crash == "after_commit":
        operations.finish = lambda *args: os._exit(0)
    elif crash == "after_reservation":
        operations.business.write = lambda *args: os._exit(0)
    operations.confirm("s", oid, token)


@pytest.mark.parametrize("crash", ["after_reservation", "after_commit"])
def test_real_process_exit_recovers_only_original_business_receipt(store, business_db, crash):
    db, order = business_db
    operations, card = prepared(store, order)
    child = get_context("spawn").Process(target=process_confirm,
        args=(store.dsn, card["operation_id"], card["confirmation_token"], crash))
    child.start()
    child.join(timeout=40)
    if child.is_alive():
        child.terminate()
        child.join(5)
        pytest.fail("operation process did not finish")
    assert child.exitcode == 0
    with store.transaction() as tx:
        assert tx.one("SELECT status FROM support_operations")["status"] == "running"
        tx.execute("UPDATE task_operation_proposals SET run_deadline=0")
    result = operations.reconcile(card["operation_id"])
    assert result["status"] == ("succeeded" if crash == "after_commit" else "unknown"), result
    assert operations.confirm("s", card["operation_id"], card["confirmation_token"])["status"] == result["status"]
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM commerce_mutations WHERE order_id=%s", (order,))
        assert cur.fetchone()["n"] == (1 if crash == "after_commit" else 0)


def test_two_application_processes_share_one_business_submission(store, business_db):
    db, order = business_db
    operations, card = prepared(store, order)
    children = [get_context("spawn").Process(target=process_confirm,
        args=(store.dsn, card["operation_id"], card["confirmation_token"], None)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=40)
            assert not child.is_alive(), "application process did not finish"
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
    assert operations.list("s")[0]["status"] == "succeeded"
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM commerce_mutations WHERE order_id=%s", (order,))
        assert cur.fetchone()["n"] == 1

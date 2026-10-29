"""Real v3 memory processing and read-path observation inside disposable stores."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import json
import sqlite3
import time

from .contracts import digest
from .runtime import BudgetExceeded, plain


def clone_store(source, destination):
    """Backup after workers close; preserve sources, tasks, receipts and memory IDs."""
    from app.support.store import SupportStore
    if destination.exists():
        raise ValueError("Snapshot destination must be new")
    with sqlite3.connect(source.dsn[len("sqlite:///"):]) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    return SupportStore("sqlite:///" + str(destination))


def snapshot_digest(store, business):
    with store.transaction(read_only=True) as tx:
        tables = tx.all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        contents = {}
        for table in tables:
            name = table["name"]
            # Names originate only from SQLite's schema, quoted as identifiers.
            rows = tx.all('SELECT * FROM "' + name.replace('"', '""') + '"')
            contents[name] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return digest(dict(tables=contents, business=business))


class MemoryObserver:
    def __init__(self, enabled, calls):
        self.enabled, self.calls = enabled, calls
        self.records, self.phase_id = [], ""
        self.journal = None

    def add(self, kind, **data):
        self.records.append(dict(kind=kind, phase_id=self.phase_id, **deepcopy(plain(data))))

    @contextmanager
    def installed(self):
        from app.memory.ltm_manager import LongTermMemoryManager
        from app.memory import task_context
        with ExitStack() as stack:
            for method, empty in (("retrieve_relevant_long_term_memory", []),
                                  ("build_memory_prompt_sections", []),
                                  ("build_memory_views", {"profile": {"refs": []}, "topics": {}}),
                                  ("topic_summary", None)):
                original = getattr(LongTermMemoryManager, method)
                def wrapper(manager, *args, _method=method, _original=original, _empty=empty, **kwargs):
                    row = dict(method=_method, user_id=getattr(manager.ltm, "user_id", ""),
                               query=args[0] if args else kwargs.get("query"), blocked=not self.enabled)
                    try:
                        result = _original(manager, *args, **kwargs) if self.enabled else deepcopy(_empty)
                        if _method == "retrieve_relevant_long_term_memory":
                            row["facts"] = [manager.fact_record(f) for f in result]
                        else:
                            row["result"] = plain(result)
                        self.add("read", **row)
                        return result
                    except Exception as exc:
                        self.add("read", **row, error=type(exc).__name__)
                        raise
                stack.enter_context(patch.object(LongTermMemoryManager, method, wrapper))
            original_history = task_context.task_history
            def history(runs, record):
                result = original_history(runs, record)
                self.add("task_history", task_id=record.task_id, result=result)
                return result
            stack.enter_context(patch.object(task_context, "task_history", history))
            yield

    def snapshot(self, store, stage):
        with store.transaction(read_only=True) as tx:
            records = tx.all("SELECT user_id,memory_id,status,payload FROM memory_v3_records ORDER BY user_id,memory_id")
            forgets = tx.all("SELECT * FROM memory_v3_forgets")
        self.add("snapshot", stage=stage,
                 facts=[dict(r, payload=json.loads(r["payload"])) for r in records], forgets=forgets)

    def archive(self, store, user, sid):
        from app.memory.v3 import jobs
        from app.memory.v3.extractor import PROMPT
        from app.memory.v3.models import Plan
        barrier = jobs.archive(store, user, sid)
        def extract(messages, existing):
            plan = self.calls.json(PROMPT, dict(messages=messages, existing=existing), Plan, "memory_extract")
            self.add("extraction", user_id=user, session_id=sid, messages=messages,
                     existing=existing, plan=plan.model_dump(mode="json"))
            return plan
        while True:
            if time.monotonic() >= self.calls.deadline:
                raise BudgetExceeded("memory_archive")
            counts = jobs.process(store, extract_fn=extract)
            barrier = jobs.archive_status(store, user, barrier["archive_id"])
            self.add("archive", user_id=user, session_id=sid, counts=counts, barrier=barrier)
            if barrier["status"] == "completed":
                self.snapshot(store, "after_archive")
                return
            if barrier["status"] in {"failed", "retrying", "completed_with_issues"} or counts["failed"]:
                self.snapshot(store, "archive_error")
                if barrier.get("last_error") == "BudgetExceeded":
                    raise BudgetExceeded("memory_archive")
                raise RuntimeError("memory_archive_" + barrier["status"])
            time.sleep(min(.05, max(0, self.calls.deadline-time.monotonic())))


@contextmanager
def memory_environment(directory, enabled):
    from app.config.settings import settings
    overrides = dict(memory_enabled=enabled, memory_engine="v3", memory_storage_backend="json",
        memory_dir=str(Path(directory) / "legacy"), memory_retrieval_enabled=False,
        memory_extraction_debounce_seconds=0, short_term_memory_enabled=False)
    with ExitStack() as stack:
        for name, value in overrides.items():
            stack.enter_context(patch.object(settings, name, value))
        yield

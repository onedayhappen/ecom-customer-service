"""Sequential, temporary SQLite scenarios. Never use a configured business store."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


@contextmanager
def isolated_store(*, sessions=None):
    from app.config.settings import settings
    from app.support.store import SupportStore
    # Overrides restored even on failure; run in a dedicated CLI process, not a worker.
    overrides = dict(short_term_memory_enabled=False, memory_enabled=False,
        observability_enabled=False, audit_enabled=False,
        api_concurrency_redis_url="", memory_redis_url="", order_cache_redis_url="",
        order_db_dsn="", api_session_postgres_dsn="", memory_postgres_dsn="",
        retrieval_service_url="", commerce_provider_config="{}")
    with TemporaryDirectory(prefix="ecom-agent-eval-") as directory, ExitStack() as stack:
        for key, value in overrides.items():
            stack.enter_context(patch.object(settings, key, value))
        store = SupportStore("sqlite:///" + str(Path(directory) / "evaluation.db"))
        for sid, user in (sessions if sessions is not None else (("s", "alice"), ("old", "alice"), ("other", "bob"))):
            store.ensure(sid, user)
        yield store


def message(store, text, sid="s", user="alice"):
    with store.transaction() as tx:
        control = store._control(tx, sid, user)
        sequence = store._event(tx, control, "message", user, dict(role="user", content=text))
    return dict(kind="message", session_id=sid, sequence=sequence)

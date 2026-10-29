"""Failed database cleanup must preserve the original query error."""
import pymysql
import pytest

from app.data.business_store import BusinessStore


@pytest.mark.parametrize('rollback_fails', [False, True])
def test_failed_transaction_preserves_original_error_and_closes(monkeypatch, rollback_fails):
    events = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            events.append(sql)

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            events.append('rollback')
            if rollback_fails:
                raise pymysql.err.InterfaceError(0, '')

        def close(self):
            events.append('close')

    monkeypatch.setattr('app.data.business_store.pymysql.connect', lambda **kwargs: Connection())
    original = pymysql.err.OperationalError(2013, 'query timed out')
    with pytest.raises(pymysql.err.OperationalError) as error:
        with BusinessStore('mysql://test:test@127.0.0.1/isolated').transaction():
            raise original
    assert error.value is original
    assert events[-2:] == ['rollback', 'close']

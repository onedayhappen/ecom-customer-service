"""MySQL-only production business transactions; no fixture fallback."""
from contextlib import contextmanager, suppress
import re
import pymysql
from app.data.order_dao import parse_mysql_dsn

class Transaction:
    def __init__(self, connection, write=False):
        self.connection = connection
        self.write = write
        self.tables = set()
    def all(self, sql, args=()):
        self.tables.update(re.findall(r'\b(?:FROM|JOIN)\s+([a-z_][a-z_0-9]*)',sql,re.I))
        with self.connection.cursor(pymysql.cursors.SSDictCursor) as c:
            c.execute(sql, args)
            rows=list(c.fetchmany(10001))
            if len(rows)>10000:
                from app.services.commerce.common import BusinessError
                raise BusinessError('insufficient_data','结果过多，请缩小查询范围')
            return rows
    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None
    def execute(self, sql, args=()):
        with self.connection.cursor() as c:
            return c.execute(sql, args)

class BusinessStore:
    def __init__(self, dsn):
        self.dsn = dsn
    @contextmanager
    def transaction(self, *, write=False):
        if not self.dsn:
            from app.services.commerce.common import BusinessError
            raise BusinessError('temporarily_unavailable', '业务数据库未配置')
        conn = pymysql.connect(**parse_mysql_dsn(self.dsn),charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,connect_timeout=3,read_timeout=10,write_timeout=10,autocommit=False)
        try:
            with conn.cursor() as c:
                c.execute("SET time_zone = '+00:00'")
                c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                c.execute('START TRANSACTION' if write else 'START TRANSACTION READ ONLY')
            yield Transaction(conn,write)
            conn.commit() if write else conn.rollback()
        except BaseException:
            # A timed-out query may already have closed the socket. Preserve
            # that original failure instead of replacing it with rollback's
            # uninformative InterfaceError.
            with suppress(Exception):
                conn.rollback()
            raise
        finally:
            conn.close()

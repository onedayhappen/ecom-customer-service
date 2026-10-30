# 文件说明：保留旧业务 DAO 的兼容读写封装，并限制退役表结构的使用。
"""Legacy commerce DAO retained temporarily for tool refactoring.

Its former ``commerce_*`` tables have been dropped; schema creation is disabled.
"""
import hashlib
import json
from datetime import datetime, timezone

from app.data.order_dao import MySqlOrderDAO, parse_mysql_dsn

TABLES = {
    'offers': 'commerce_offers', 'promotions': 'commerce_promotions',
    'coupons': 'commerce_coupons', 'delivery': 'commerce_delivery',
    'history': 'commerce_price_history', 'after_sales': 'commerce_after_sales',
    'invoices': 'commerce_invoices', 'payments': 'commerce_payments',
    'policies': 'commerce_order_policies',
}

DDL = [f'''CREATE TABLE IF NOT EXISTS {table} (
    platform_code VARCHAR(16) NOT NULL,
    user_id VARCHAR(128) NOT NULL DEFAULT '',
    record_id VARCHAR(128) NOT NULL,
    object_id VARCHAR(128) NOT NULL,
    channel VARCHAR(64) NOT NULL DEFAULT '',
    region VARCHAR(128) NOT NULL DEFAULT '',
    recorded_at DATETIME(6) NOT NULL,
    is_demo BOOLEAN NOT NULL DEFAULT FALSE,
    payload JSON NOT NULL,
    PRIMARY KEY (platform_code,user_id,record_id),
    KEY idx_object (platform_code,user_id,object_id,recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''' for table in TABLES.values()]
DDL.append('''CREATE TABLE IF NOT EXISTS commerce_mutations (
    user_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    action VARCHAR(64) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    arguments_hash CHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    result JSON NOT NULL,
    PRIMARY KEY (user_id,request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')


def decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class CommerceRejection(Exception):
    def __init__(self, message, code='business_rejection'):
        super().__init__(message)
        self.code = code


class MySqlCommerceDAO(MySqlOrderDAO):
    """Reuse the existing bounded pool and transaction/rollback discipline."""
    def _connect(self):
        import pymysql
        return pymysql.connect(**parse_mysql_dsn(self._dsn), charset='utf8mb4',
                               cursorclass=pymysql.cursors.DictCursor,
                               connect_timeout=3, read_timeout=10, write_timeout=10)

    def ensure_schema(self):
        raise RuntimeError("commerce_* 旧表已退役；请使用新版 MySQL 业务表迁移")

    def insert_seed(self, records):
        """Insert missing demo fixtures only; never overwrite business records."""
        inserted = 0
        with self._connection() as conn, conn.cursor() as cur:
            for r in records:
                table = TABLES[r['kind']]
                cur.execute(f'''INSERT INTO {table}
                    (platform_code,user_id,record_id,object_id,channel,region,recorded_at,is_demo,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
                    ON DUPLICATE KEY UPDATE record_id=record_id''',
                    (r['platform_code'], r.get('user_id', ''), r['record_id'], r['object_id'],
                     r.get('channel', ''), r.get('region', ''), r['recorded_at'],
                     json.dumps(r['payload'], ensure_ascii=False)))
                inserted += int(cur.rowcount == 1)
        return inserted

    def records(self, kind, *, user_id='', platform='pdd', object_id=None,
                record_id=None, channel=None, region=None, start=None, end=None):
        table = TABLES[kind]  # Never derived from user SQL or an arbitrary table name.
        where = ['platform_code=%s', 'user_id=%s']
        params = [platform, user_id]
        for column, value in [('object_id', object_id), ('record_id', record_id),
                              ('channel', channel), ('region', region)]:
            if value is not None:
                where.append(f'{column}=%s')
                params.append(value)
        for op, value in [('>=', start), ('<=', end)]:
            if value is not None:
                where.append(f'recorded_at {op} %s')
                params.append(value.astimezone(timezone.utc).replace(tzinfo=None))
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT record_id,is_demo,payload FROM {table} WHERE "
                        + ' AND '.join(where) + ' ORDER BY recorded_at,record_id LIMIT 10001', params)
            rows = cur.fetchall()
        if len(rows) > 10000:
            raise CommerceRejection('记录过多，请缩小查询范围', 'validation_error')
        return [{**r, 'payload': decode(r['payload'])} for r in rows]

    def owned_order(self, user_id, order_id, *, cursor=None, lock=False):
        if cursor is None:
            with self._connection() as conn, conn.cursor() as cur:
                return self.owned_order(user_id, order_id, cursor=cur)
        cursor.execute('SELECT * FROM orders WHERE platform_code=%s AND order_id=%s AND user_id=%s'
                       + (' FOR UPDATE' if lock else ''), ('pdd', order_id, user_id))
        row = cursor.fetchone()
        if not row:
            raise CommerceRejection('未找到当前用户的订单', 'evidence_unavailable')
        return row

    def invoice_snapshot(self, user_id, order_id, cur, *, lock=False):
        order = self.owned_order(user_id, order_id, cursor=cur, lock=lock)
        cur.execute('SELECT payload,is_demo FROM commerce_order_policies '
                    "WHERE platform_code='pdd' AND user_id='' AND record_id='invoice'"
                    + (' FOR UPDATE' if lock else ''))
        policy = cur.fetchone()
        if not policy:
            raise CommerceRejection('缺少本地开票政策', 'evidence_unavailable')
        cur.execute('SELECT record_id,payload,is_demo FROM commerce_invoices '
                    "WHERE platform_code='pdd' AND user_id=%s AND object_id=%s ORDER BY record_id"
                    + (' FOR UPDATE' if lock else ''), (user_id, order_id))
        invoices = [{**r, 'payload': decode(r['payload'])} for r in cur.fetchall()]
        policy = {**policy, 'payload': decode(policy['payload'])}
        version = fingerprint({'order': order, 'policy': policy, 'invoices': invoices})
        return order, policy, invoices, version

    def preview_invoice(self, user_id, order_id):
        with self._connection() as conn, conn.cursor() as cur:
            return self.invoice_snapshot(user_id, order_id, cur)

    def write_invoice(self, user_id, action, args, request_id, create):
        """Lock the owned order, revalidate preview and commit ledger+receipt atomically."""
        args_hash = fingerprint({'action': action, 'args': args})
        with self._connection() as conn, conn.cursor() as cur:
            # Serialize invoice mutations for this order before checking replay.
            snapshot = self.invoice_snapshot(user_id, args['order_id'], cur, lock=True)
            cur.execute('SELECT arguments_hash,result FROM commerce_mutations '
                        'WHERE user_id=%s AND request_id=%s FOR UPDATE', (user_id, request_id))
            previous = cur.fetchone()
            if previous:
                if previous['arguments_hash'] != args_hash:
                    raise CommerceRejection('幂等请求参数不一致', 'confirmation_invalid')
                return {**decode(previous['result']), 'replayed': True}
            if args['preview_version'] != snapshot[3]:
                raise CommerceRejection('订单或发票状态已变化，请重新预览并确认', 'confirmation_invalid')
            payload, original_id = create(snapshot)
            invoice_id = 'LOCAL-' + fingerprint({'user': user_id, 'request': request_id})[:24]
            payload['invoice_id'] = invoice_id
            if original_id:
                original = next(r for r in snapshot[2] if r['record_id'] == original_id)
                old = {**original['payload'], 'status': 'replaced_local', 'replacement_id': invoice_id}
                cur.execute('UPDATE commerce_invoices SET payload=%s WHERE '
                            "platform_code='pdd' AND user_id=%s AND record_id=%s AND object_id=%s",
                            (json.dumps(old, ensure_ascii=False), user_id, original_id, args['order_id']))
            cur.execute('INSERT INTO commerce_invoices '
                        '(platform_code,user_id,record_id,object_id,recorded_at,is_demo,payload) '
                        "VALUES ('pdd',%s,%s,%s,%s,TRUE,%s)",
                        (user_id, invoice_id, args['order_id'], datetime.now(timezone.utc).replace(tzinfo=None),
                         json.dumps(payload, ensure_ascii=False)))
            result = {'success': True, 'source': 'mysql_demo', 'is_demo': True,
                      'observed_at': datetime.now(timezone.utc).isoformat(),
                      'request_id': request_id, 'invoice': payload,
                      'message': '已创建本地演示开票记录，未开具税务发票'}
            cur.execute('INSERT INTO commerce_mutations VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (user_id, request_id, action, args['order_id'], args_hash,
                         datetime.now(timezone.utc).replace(tzinfo=None), json.dumps(result, ensure_ascii=False)))
            return result

    def mutation_status(self, user_id, request_id):
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT action,order_id,result FROM commerce_mutations '
                        'WHERE user_id=%s AND request_id=%s', (user_id, request_id))
            row = cur.fetchone()
        if not row:
            return {'status': 'unknown', 'note': '本地无成功回执不等于可以安全重试'}
        return {'status': 'succeeded', 'action': row['action'], 'order_id': row['order_id'],
                'result': decode(row['result'])}

    def refund_snapshot(self, user_id, order_id, cursor=None, *, lock=False):
        if cursor is None:
            with self._connection() as conn, conn.cursor() as cur:
                return self.refund_snapshot(user_id, order_id, cur, lock=lock)
        order = self.owned_order(user_id, order_id, cursor=cursor, lock=lock)
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute("""SELECT payload,is_demo FROM commerce_order_policies
            WHERE platform_code='pdd' AND user_id='' AND record_id='refund'""" + suffix)
        policy = cursor.fetchone()
        if not policy:
            raise CommerceRejection("缺少退款申请的业务预检规则", "evidence_unavailable")
        policy = {**policy, "payload": decode(policy["payload"])}
        cursor.execute("""SELECT record_id,payload,is_demo FROM commerce_after_sales
            WHERE platform_code='pdd' AND user_id=%s AND object_id=%s ORDER BY record_id""" + suffix,
            (user_id, order_id))
        applications = [{**r, "payload": decode(r["payload"])} for r in cursor.fetchall()]
        version = fingerprint(dict(order=order, policy=policy, applications=applications))
        return order, policy, applications, version

    def write_refund(self, user_id, args, request_id, preview_version, validate):
        """One order lock covers qualification, conflicting writes and the receipt."""
        args_hash = fingerprint({"action": "apply_refund", "args": args})
        with self._connection() as conn, conn.cursor() as cur:
            snapshot = self.refund_snapshot(user_id, args["order_id"], cur, lock=True)
            cur.execute("""SELECT arguments_hash,result FROM commerce_mutations
                WHERE user_id=%s AND request_id=%s FOR UPDATE""", (user_id, request_id))
            previous = cur.fetchone()
            if previous:
                if previous["arguments_hash"] != args_hash:
                    raise CommerceRejection("同一操作编号的参数不一致", "confirmation_invalid")
                return {**decode(previous["result"]), "replayed": True}
            if snapshot[3] != preview_version:
                raise CommerceRejection("订单或售后状态已变化，请重新预检并确认", "confirmation_invalid")
            validate(snapshot)
            cur.execute("""UPDATE orders SET refund_status='submitted',refund_reason=%s,
                refund_requested_at=NOW(),status=CASE WHEN status='pending' THEN 'refund_processing' ELSE status END
                WHERE platform_code='pdd' AND order_id=%s AND user_id=%s""",
                (args["reason"], args["order_id"], user_id))
            demo = bool(snapshot[1].get("is_demo"))
            result = dict(success=True, request_id=request_id, order_id=args["order_id"],
                          status="submitted", source="mysql_demo" if demo else "mysql", is_demo=demo,
                          message="已登记本地演示退款申请。" if demo else "整单退款申请已登记，需等待业务审核。")
            cur.execute("INSERT INTO commerce_mutations VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, request_id, "apply_refund", args["order_id"], args_hash,
                 datetime.now(timezone.utc).replace(tzinfo=None), json.dumps(result, ensure_ascii=False)))
            return result

# 文件说明：原子调整演示用户标识，并重新绑定受身份加密保护的地址。
"""Rekey the ten imported demo identities and rebind encrypted addresses atomically.

Default performs all validation then rolls back. --apply commits the data migration.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pymysql
from dotenv import dotenv_values
from app.data.order_dao import parse_mysql_dsn
from app.services.commerce.common import Cipher

NAMES = dict(zip([1, 3, 4, 5, 6, 7, 8, 9, 10, 11],
                 ['William', 'James', 'John', 'Michael', 'David', 'Lily', 'Emma', 'Sarah', 'Jessica', 'Olivia']))


def ident(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', value):
        raise ValueError('Unexpected SQL identifier')
    return '`' + value + '`'


def migrate(dsn, cipher, *, commit=False):
    conn = pymysql.connect(**parse_mysql_dsn(dsn), charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=5)
    mapping = {f'mall_user_{n}': (f'user_{n}', name) for n, name in NAMES.items()}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if cur.fetchone()['acquired'] != 1:
                raise TimeoutError('Migration lock busy')
            cur.execute('SELECT * FROM users ORDER BY user_id FOR UPDATE')
            users = {r['user_id']: r for r in cur.fetchall()}
            if all(new in users and users[new]['username'] == name for new, name in mapping.values()) and not (set(users) & set(mapping)):
                conn.rollback()
                return {'status': 'already_applied'}
            if not set(mapping) <= set(users) or any(new in users for new, _ in mapping.values()):
                raise ValueError('Missing old identity or target ID collision')
            if any(r['username_key'] in {name.casefold() for _, name in mapping.values()} for r in users.values()):
                raise ValueError('Target username collision')
            cur.execute("SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND COLUMN_NAME IN ('user_id','granted_by') ORDER BY TABLE_NAME,ORDINAL_POSITION")
            identity_columns = list(cur.fetchall())
            cur.execute('SELECT * FROM user_credentials ORDER BY user_id FOR UPDATE')
            credentials_before = {r['user_id']: (r['password_hash'], r['changed_at']) for r in cur.fetchall()}
            # Fail safely if this seed has acquired other identity-bound encrypted
            # workflows that require a separate payload-specific migration.
            cur.execute("SELECT DISTINCT TABLE_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND COLUMN_NAME LIKE %s", ('%ciphertext%',))
            for record in cur.fetchall():
                table = record['TABLE_NAME']
                if table in {'user_addresses', 'order_address_snapshots'}:
                    continue
                cur.execute('SELECT COUNT(*) n FROM ' + ident(table))
                if cur.fetchone()['n']:
                    raise ValueError(f'Encrypted workflow {table} needs a dedicated rekey migration')
            cur.execute('SELECT platform_code,order_id,user_id FROM orders ORDER BY platform_code,order_id FOR UPDATE')
            owners = {(r['platform_code'], r['order_id']): r['user_id'] for r in cur.fetchall()}
            cur.execute('SELECT * FROM user_addresses ORDER BY address_id FOR UPDATE')
            addresses = [(r, r['user_id']) for r in cur.fetchall() if r['user_id'] in mapping]
            cur.execute('SELECT * FROM order_address_snapshots ORDER BY platform_code,order_id FOR UPDATE')
            snapshots = [(r, owners[(r['platform_code'], r['order_id'])]) for r in cur.fetchall()
                         if owners[(r['platform_code'], r['order_id'])] in mapping]
            # Decrypt everything before changing identities. Round-trip each newly
            # sealed value in memory; never log plaintext or encryption keys.
            for table, records in [('user_addresses', addresses), ('order_address_snapshots', snapshots)]:
                for row, old in records:
                    new = mapping[old][0]
                    ciphertext = []
                    for field in ('recipient_name', 'phone', 'detail'):
                        plain = cipher.open(row[field + '_ciphertext'], f'address:{old}:{field}', row['key_version'])
                        sealed = cipher.seal(plain, f'address:{new}:{field}')
                        if cipher.open(sealed, f'address:{new}:{field}', cipher.version) != plain:
                            raise ValueError('Encryption round trip failed')
                        ciphertext.append(sealed)
                    where, key = ('address_id=%s', (row['address_id'],)) if table == 'user_addresses' else (
                        'platform_code=%s AND order_id=%s', (row['platform_code'], row['order_id']))
                    cur.execute(f'UPDATE {ident(table)} SET recipient_name_ciphertext=%s,phone_ciphertext=%s,detail_ciphertext=%s,key_version=%s WHERE {where}', (*ciphertext, cipher.version, *key))
            # MySQL has no deferred FK checking. Disable checks on this connection
            # only, perform transactional rekeying, then validate every FK before commit.
            cur.execute('SET FOREIGN_KEY_CHECKS=0')
            for old, (new, name) in mapping.items():
                for col in identity_columns:
                    table, field = col['TABLE_NAME'], col['COLUMN_NAME']
                    if table != 'users':
                        cur.execute(f'UPDATE {ident(table)} SET {ident(field)}=%s WHERE {ident(field)}=%s', (new, old))
                cur.execute('UPDATE users SET user_id=%s,username=%s,username_key=%s,updated_at=UTC_TIMESTAMP(6) WHERE user_id=%s', (new, name, name.casefold(), old))
                cur.execute('UPDATE user_profiles SET display_name=%s,updated_at=UTC_TIMESTAMP(6) WHERE user_id=%s', (name, new))
                cur.execute('UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at,UTC_TIMESTAMP(6)) WHERE user_id=%s', (new,))
                cur.execute('UPDATE auth_login_attempts SET username_key=%s WHERE username_key=%s', (name.casefold(), users[old]['username_key']))
            cur.execute('''SELECT TABLE_NAME,COLUMN_NAME,CONSTRAINT_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE()
                AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION''')
            foreign_keys = defaultdict(list)
            for r in cur.fetchall():
                foreign_keys[(r['TABLE_NAME'], r['CONSTRAINT_NAME'], r['REFERENCED_TABLE_NAME'])].append(r)
            for (table, constraint, parent), columns in foreign_keys.items():
                join = ' AND '.join('c.' + ident(c['COLUMN_NAME']) + '=p.' + ident(c['REFERENCED_COLUMN_NAME']) for c in columns)
                nonnull = ' AND '.join('c.' + ident(c['COLUMN_NAME']) + ' IS NOT NULL' for c in columns)
                cur.execute(f'SELECT COUNT(*) n FROM {ident(table)} c LEFT JOIN {ident(parent)} p ON {join} WHERE {nonnull} AND p.{ident(columns[0]["REFERENCED_COLUMN_NAME"])} IS NULL')
                if cur.fetchone()['n']:
                    raise ValueError(f'Foreign key validation failed: {constraint}')
            for col in identity_columns:
                cur.execute(f'SELECT COUNT(*) n FROM {ident(col["TABLE_NAME"])} WHERE {ident(col["COLUMN_NAME"])} IN (' + ','.join(['%s'] * len(mapping)) + ')', tuple(mapping))
                if cur.fetchone()['n']:
                    raise ValueError('Stale identity reference')
            cur.execute('SELECT * FROM user_credentials')
            for row in cur.fetchall():
                old = next((old for old, (new, _) in mapping.items() if new == row['user_id']), row['user_id'])
                if credentials_before[old] != (row['password_hash'], row['changed_at']):
                    raise ValueError('Password credentials unexpectedly changed')
            cur.execute('SET FOREIGN_KEY_CHECKS=1')
        conn.commit() if commit else conn.rollback()
        return {'status': 'committed' if commit else 'validated_and_rolled_back', 'users': len(mapping),
                'reencrypted_addresses': len(addresses), 'reencrypted_order_snapshots': len(snapshots),
                'validated_foreign_keys': len(foreign_keys)}
    except BaseException:
        conn.rollback()
        raise
    finally:
        with conn.cursor() as cur:
            cur.execute('SET FOREIGN_KEY_CHECKS=1')
            cur.execute("SELECT RELEASE_LOCK('ecom_mysql_business_schema')")
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = {**dotenv_values(ROOT / '.env'), **os.environ}
    if not config.get('ORDER_DB_DSN'):
        parser.error('Configure ORDER_DB_DSN')
    cipher = Cipher(config.get('BUSINESS_DATA_KEYS', '{}'), int(config.get('BUSINESS_DATA_KEY_VERSION', '1')))
    print(json.dumps(migrate(config['ORDER_DB_DSN'], cipher, commit=args.apply), ensure_ascii=False))


if __name__ == '__main__':
    main()

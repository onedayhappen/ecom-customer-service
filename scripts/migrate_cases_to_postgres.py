"""Validate/export local Case snapshots into shared PostgreSQL storage.

Stop all Case writers before --apply. Default mode only validates local files.
Existing nonempty snapshots are never overwritten; source files are retained.
"""
import argparse
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path


def read_snapshot(path):
    from app.memory.case_manager import CaseRecord
    data = json.loads(path.read_text(encoding='utf-8'))
    user = data.get('user_id')
    if not isinstance(user, str) or not user or path.stem != sha256(user.encode()).hexdigest():
        raise ValueError(f'Invalid ownership or snapshot filename: {path.name}')
    cases = [asdict(CaseRecord(**item)) for item in data.get('cases', [])]
    events, pending = data.get('events', []), data.get('pending_order_selections', {})
    if any(case['user_id'] != user for case in cases) or any(event.get('user_id') != user for event in events):
        raise ValueError(f'Cross-user records in {path.name}')
    if not isinstance(pending, dict) or any(not isinstance(value, dict) for value in pending.values()):
        raise ValueError(f'Invalid pending choices in {path.name}')
    payload = {'user_id': user, 'cases': cases, 'events': events, 'pending_order_selections': pending}
    archives = []
    archive_path = path.parent / 'archive' / (path.stem + '.jsonl')
    if archive_path.exists():
        with archive_path.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip(): continue
                item = json.loads(line)
                case = CaseRecord(**item['case'])
                if case.user_id != user or any(event.get('user_id') != user for event in item.get('events', [])):
                    raise ValueError(f'Cross-user archive in {archive_path.name}')
                archives.append(item)
    return payload, archives


def import_snapshot(store, payload, archives):
    if not store.postgres:
        raise ValueError('Case migration requires PostgreSQL')
    user = payload['user_id']
    with store.transaction() as tx:
        tx.execute('INSERT INTO case_snapshots VALUES (?,?) ON CONFLICT(user_id) DO NOTHING', (user, '{}'))
        existing = json.loads(tx.one('SELECT payload FROM case_snapshots WHERE user_id=? FOR UPDATE', (user,))['payload'])
        if existing and existing != payload:
            raise ValueError('Target already contains different Case state; migration stopped without overwriting it')
        for item in archives:
            cid = item['case']['case_id']
            previous = tx.one('SELECT user_id,payload FROM case_archives WHERE case_id=?', (cid,))
            if previous and (previous['user_id'] != user or json.loads(previous['payload']) != item):
                raise ValueError('Conflicting archive; migration stopped without overwriting it')
            tx.execute('INSERT INTO case_archives VALUES (?,?,?) ON CONFLICT(case_id) DO NOTHING',
                       (cid, user, json.dumps(item, ensure_ascii=False)))
        tx.execute('UPDATE case_snapshots SET payload=? WHERE user_id=?', (json.dumps(payload, ensure_ascii=False), user))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error('--source must be an existing Case directory')
    # Validate every source before making the first database change.
    snapshots = [read_snapshot(path) for path in sorted(args.source.glob('*.json'))]
    if args.apply:
        from app.support.runtime import get_store
        store = get_store()
        for payload, archives in snapshots:
            import_snapshot(store, payload, archives)
    print(f'{"Imported" if args.apply else "Validated"} {len(snapshots)} user snapshots; source files retained')


if __name__ == '__main__':
    main()

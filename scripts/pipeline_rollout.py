# 文件说明：通过命令行查询、暂停、放量或导入数据库控制的事项流程。
"""Inspect, stop, selectively enable, or import the single task pipeline."""
import argparse
import hashlib
import json
from pathlib import Path

from app.support.runtime import get_store
from app.tasks.rollout import Rollout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    switch = commands.add_parser('set')
    switch.add_argument('mode', choices=['all', 'selected', 'stopped', 'enabled', 'disabled'])
    switch.add_argument('--user')
    switch.add_argument('--operator', required=True)
    switch.add_argument('--reason', required=True)
    migrate = commands.add_parser('import')
    migrate.add_argument('--task', required=True)
    migrate.add_argument('--user', required=True)
    migrate.add_argument('--version', type=int, required=True)
    migrate.add_argument('--operator', required=True)
    migrate.add_argument('--reason', required=True)
    migrate.add_argument('--backup', type=Path, required=True,
                         help='Previously restored and verified database backup; never overwritten')
    args = parser.parse_args()
    service = Rollout(get_store())
    if args.command == 'status':
        result = service.status()
    elif args.command == 'set':
        result = service.set_mode(args.mode, user=args.user, actor=args.operator, reason=args.reason)
    else:
        if not args.backup.is_file() or args.backup.stat().st_size == 0:
            parser.error('A nonempty, verified backup is required before import')
        with args.backup.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        record = service.import_legacy(args.task, user=args.user, expected_version=args.version,
            actor=args.operator, reason=args.reason + '; backup sha256=' + digest)
        result = dict(task_id=record.task_id, status=record.status, version=record.version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

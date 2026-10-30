# 文件说明：从持久诊断事件重新导出按交互组织的 JSONL 日志。
"""Rebuild interaction JSONL snapshots from the durable diagnostic journal."""
import argparse

from app.observability.interactions import export_session
from app.support.runtime import get_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="Only rebuild this session; omit for all recorded sessions")
    args = parser.parse_args()
    store = get_store()
    with store.transaction(read_only=True) as tx:
        sessions = tx.all("SELECT session_id FROM interaction_trace_sessions" +
                          (" WHERE session_id=?" if args.session else ""), (args.session,) if args.session else ())
    failed = 0
    for item in sessions:
        path = export_session(store, item["session_id"])
        if path:
            print(path)
        else:
            failed += 1
    print(f"Exported {len(sessions) - failed}/{len(sessions)} sessions")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

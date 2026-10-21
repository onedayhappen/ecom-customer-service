"""Compact old terminal job bodies outside the API/dispatcher hot paths."""
import argparse
import logging
import signal
import threading

from app.support.runtime import get_store
from app.runtime.maintenance import archive_jobs, backfill_ticket_snapshots
from app.session.short_term import process_context_jobs
from app.memory.runtime import process_memory_jobs
from app.memory.maintenance import process_memory_invalidations
from app.config.settings import settings


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retention-days', type=int, default=30)
    parser.add_argument('--batch-size', type=int, default=200)
    parser.add_argument('--interval', type=float, default=300)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.retention_days < 1 or not 1 <= args.batch_size <= 1000 or args.interval < 1:
        parser.error('retention>=1 day, batch=1..1000, interval>=1 second required')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            count = archive_jobs(get_store(), args.retention_days, args.batch_size)
            logging.info('Archived %s terminal jobs; execution tombstones retained', count)
            backfill_ticket_snapshots(get_store())
            if settings.task_external_progress_enabled:
                from app.tasks.external_progress import ExternalProgress
                logging.info('External business progress: %s', ExternalProgress(get_store()).tick(limit=min(args.batch_size, 4)))
            invalidations = process_memory_invalidations(get_store(), limit=min(args.batch_size, 20))
            logging.info('Memory source maintenance: %s', invalidations)
            if settings.short_term_memory_enabled:
                contexts = process_context_jobs(get_store(), limit=min(args.batch_size, 20))
                logging.info('Short-term context maintenance: %s', contexts)
            if settings.memory_enabled:
                memories = process_memory_jobs(get_store(), limit=min(args.batch_size, 20))
                logging.info('Long-term memory maintenance: %s', memories)
        except Exception:
            if args.once:
                raise
            logging.exception('Maintenance failed; retry on next interval')
        if args.once:
            return
        stop.wait(args.interval)


if __name__ == '__main__':
    main()

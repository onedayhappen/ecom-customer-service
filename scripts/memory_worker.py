"""Dedicated memory worker; does not wait for the general maintenance interval."""
import argparse
import logging
import signal
import threading

from app.memory.v3.jobs import process
from app.support.runtime import get_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--batch-size', type=int, default=20)
    args = parser.parse_args()
    if args.interval < .1 or not 1 <= args.batch_size <= 100:
        parser.error('interval >= .1 and 1 <= batch-size <= 100 required')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    while not stop.is_set():
        try:
            logging.info('Memory v3: %s', process(get_store(), limit=args.batch_size))
        except Exception:
            logging.exception('Memory worker failed')
            if args.once:
                raise
        if args.once:
            return
        stop.wait(args.interval)


if __name__ == '__main__':
    main()

"""Lifecycle scan entry point — invoked by the Render Cron Job daily.

Usage:
    python -m lifecycle              # run the scan with current env
    python -m lifecycle --dry-run    # force LIFECYCLE_DRY_RUN=true regardless of env

The cron job command on Render is just `python -m lifecycle`. Schedule
recommendation: `30 13 * * *` (UTC) = 7:00 PM IST, start of MDB shift.

Exit codes:
    0  scan completed (with or without per-row errors — see counters)
    1  fatal error (DB unreachable, etc.) — Render will alert
"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Force LIFECYCLE_DRY_RUN=true regardless of env. Logs only.',
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Enable DEBUG logging.',
    )
    args = parser.parse_args()

    if args.dry_run:
        os.environ['LIFECYCLE_DRY_RUN'] = 'true'

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)-30s %(message)s',
    )

    # Defer imports until after logging is set up so module-load logs
    # use the chosen format. Also defer until after we may have flipped
    # LIFECYCLE_DRY_RUN — Config reads env at import time.
    try:
        # Re-evaluate Config defaults if dry-run flag flipped env vars.
        from importlib import reload
        from config import Config
        if args.dry_run:
            import config as _cfg
            reload(_cfg)
            Config = _cfg.Config

        from inventory import store
        from lifecycle.scan import run_scan

        store.init_db()
        counters = run_scan()
        print(counters)
        return 0
    except Exception:
        logging.getLogger(__name__).exception('lifecycle scan crashed')
        return 1


if __name__ == '__main__':
    sys.exit(main())

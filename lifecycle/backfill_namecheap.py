"""One-shot Namecheap expiry backfill for the lifecycle bot.

Walks every domain in our table, calls namecheap.domains.getInfo to
populate `expire_at`, and stamps `last_namecheap_sync_at` so the
classifier's expiry cascade has data to act on.

Usage:
    # 1. Make sure REDTRACK_API_KEY + NAMECHEAP_* are set in env.
    # 2. Dry run first — lists what it would update, no writes:
    python -m lifecycle.backfill_namecheap
    # 3. Real run:
    python -m lifecycle.backfill_namecheap --apply

Pacing: ~1.3s between calls to stay under Namecheap's ~50 req/min.
A 743-row backfill takes about 16 minutes wall-clock. Fine for a
one-shot — re-running daily is what the regular cron will do later
(in smaller, 50-row batches, via get_domains_due_for_namecheap_sync).

Resilience:
  • Already-synced rows (last_namecheap_sync_at < 7d ago AND not near
    expiry) are skipped — re-running after a partial failure resumes.
  • Per-row failures (domain not in the Namecheap account, transport
    blip, etc.) stamp last_namecheap_sync_at anyway so we don't loop
    on the same dead row forever.
  • Ctrl-C is safe — the script processes one row at a time and commits
    after each call.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from config import Config
from domain_assistant.namecheap_check import get_domain_info
from inventory import store


# Pace under Namecheap's ~50 req/min ceiling. 1.3s gives ~46 req/min,
# enough headroom that a transient retry doesn't push us over.
_CALL_INTERVAL_SECONDS = 1.3

# Pull this many rows per pass. Smaller batches = lower memory
# footprint + better progress visibility on long runs. Each pass
# refetches the "still due" list, so already-synced rows drop out
# automatically.
_BATCH_SIZE = 50

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--apply', action='store_true',
        help='Actually write to the DB (default is dry run).',
    )
    parser.add_argument(
        '--max', type=int, default=None,
        help='Stop after N rows (smoke testing).',
    )
    parser.add_argument(
        '--max-age-days', type=int, default=7,
        help='Skip rows synced within this many days.',
    )
    parser.add_argument(
        '--near-expiry-days', type=int, default=60,
        help='Re-sync rows expiring within this window even if recently synced.',
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(message)s',
    )

    if not Config.NAMECHEAP_API_USER or not Config.NAMECHEAP_API_KEY:
        sys.exit('Namecheap creds not configured — set NAMECHEAP_API_USER + '
                 'NAMECHEAP_API_KEY in .env first.')

    store.init_db()

    print('Mode:', 'APPLY' if args.apply else 'DRY RUN')
    print(f'Pace: ~{60 / _CALL_INTERVAL_SECONDS:.0f} req/min')
    print(f'Batch size: {_BATCH_SIZE}, max_age_days: {args.max_age_days}, '
          f'near_expiry_days: {args.near_expiry_days}')
    print('-' * 64)

    total = 0
    saved = 0
    skipped_unknown = 0
    failed = 0

    # Track domains processed in this run. The store's "due for sync"
    # query re-returns near-expiry rows on every batch (by design — for
    # the daily cron, that's correct behaviour). For a one-shot backfill
    # we want each row at most once, otherwise the loop never terminates.
    # Caught 2026-05-10 on a real prod run that did 3x the inventory
    # before hitting a connection timeout.
    seen: set = set()

    while True:
        rows = store.get_domains_due_for_namecheap_sync(
            limit=_BATCH_SIZE,
            max_age_days=args.max_age_days,
            near_expiry_days=args.near_expiry_days,
        )
        if not rows:
            break

        # Drop rows we've already processed this run. If after filtering
        # the batch is empty, every remaining due row is a repeat — we're
        # done. (Real daily cron uses a different code path and benefits
        # from the re-fetch behaviour; this opt-out is backfill-specific.)
        new_rows = [r for r in rows if r['domain'] not in seen]
        if not new_rows:
            break

        for row in new_rows:
            seen.add(row['domain'])
            domain = row['domain']
            total += 1

            try:
                info = get_domain_info(domain)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    '[%4d] %s — get_domain_info crashed: %s',
                    total, domain, e,
                )
                failed += 1
                info = None

            if info is None:
                # Either the domain isn't in this Namecheap account, or
                # the call failed. Stamp anyway so we don't loop on it.
                print(f'[{total:4d}] {domain:50s} (unknown — not in account or fetch failed)')
                skipped_unknown += 1
                if args.apply:
                    store.update_namecheap_sync(
                        domain, expire_at=None, auto_renew_enabled=None,
                    )
            else:
                expire = info['expire_at'].date() if info.get('expire_at') else 'unknown'
                print(f'[{total:4d}] {domain:50s} expires={expire}')
                if args.apply:
                    store.update_namecheap_sync(
                        domain,
                        expire_at=info.get('expire_at'),
                        auto_renew_enabled=info.get('auto_renew_enabled'),
                    )
                    saved += 1

            if args.max is not None and total >= args.max:
                break

            time.sleep(_CALL_INTERVAL_SECONDS)

        if args.max is not None and total >= args.max:
            break

    print('-' * 64)
    print(f'total={total}  saved={saved}  unknown={skipped_unknown}  failed={failed}')
    if not args.apply:
        print('\nDRY RUN — no rows were updated. Re-run with --apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Reset lifecycle_state on domains the DRY_RUN cron wrongly advanced.

Background (bug caught 2026-05-14): `_prompt_mdb_idle` /
`_prompt_mdb_expiring` / `_handle_expired` in lifecycle/scan.py advanced
lifecycle_state to AWAITING_* (and stamped last_prompted_at) even under
LIFECYCLE_DRY_RUN — so a dry-run "would DM" log line still mutated the
state machine. Because the classifier skips any AWAITING_* row, those
domains would be stuck: once we flip live the REAL DM never fires.

The bot has been in DRY_RUN since launch and has never sent a real DM,
so EVERY domain currently in an AWAITING_* state is a dry-run artifact —
none of them have a genuine pending human response. This script clears
them back to a clean slate:
  • lifecycle_state -> NULL  (classifier re-evaluates fresh next run)
  • last_prompted_at -> NULL (otherwise the 23h dedup guard suppresses
                              the real prompt once live)

The scan.py fix means dry-run no longer does this, so this is a
one-shot recovery — not something to run regularly.

Usage:
    python -m lifecycle.reset_dryrun_state            # dry run — counts
    python -m lifecycle.reset_dryrun_state --apply    # writes

Idempotent: re-running after --apply matches 0 rows. Records a
`dryrun_state_reset` event per domain for /domain-history.
"""
from __future__ import annotations

import argparse
import logging
import sys

from inventory import store
from lifecycle import states as S

logger = logging.getLogger(__name__)


def _find_awaiting():
    """Return [(domain, lifecycle_state), ...] for every AWAITING_* row."""
    placeholders = ', '.join('?' for _ in S.AWAITING_STATES)
    sql = (
        f'SELECT domain, lifecycle_state FROM domains '
        f'WHERE lifecycle_state IN ({placeholders})'
    )
    with store._conn() as c:
        cur = store._execute(c, sql, tuple(S.AWAITING_STATES))
        rows = cur.fetchall()
        if store._is_postgres():
            cur.close()
    out = []
    for r in rows:
        domain = r['domain'] if hasattr(r, 'keys') else r[0]
        state = r['lifecycle_state'] if hasattr(r, 'keys') else r[1]
        out.append((domain, state))
    return out


def _reset(domain: str, from_state: str) -> None:
    """Clear lifecycle_state + last_prompted_at, record the audit event."""
    with store._conn() as c:
        cur = store._execute(
            c,
            'UPDATE domains SET lifecycle_state = NULL, '
            'last_prompted_at = NULL, updated_at = CURRENT_TIMESTAMP '
            'WHERE domain = ?',
            (domain,),
        )
        if store._is_postgres():
            cur.close()

    store.record_event(
        domain, 'dryrun_state_reset', actor='reset_dryrun_state_2026_05_14',
        from_state=from_state, to_state=None,
        metadata={
            'reason': ('dry-run cron advanced this row to an AWAITING_* '
                       'state without sending a real DM — cleared so the '
                       'classifier re-evaluates and the real prompt fires '
                       'once live'),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--apply', action='store_true',
                        help='Actually clear the rows. Default is dry run.')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s')

    store.init_db()
    rows = _find_awaiting()
    print(f'found {len(rows)} domains in an AWAITING_* state')

    if not rows:
        print('nothing to reset.')
        return 0

    by_state: dict = {}
    for _, state in rows:
        by_state[state] = by_state.get(state, 0) + 1
    print('breakdown:')
    for state, n in sorted(by_state.items(), key=lambda kv: -kv[1]):
        print(f'  {state:35s} {n}')

    if not args.apply:
        print(f'\nDRY RUN — would reset {len(rows)} rows '
              '(lifecycle_state + last_prompted_at -> NULL). '
              'Re-run with --apply to commit.')
        return 0

    done = 0
    for domain, from_state in rows:
        try:
            _reset(domain, from_state)
            done += 1
        except Exception:
            logger.exception('failed to reset %s', domain)

    print(f'\nreset {done} domains — classifier will re-evaluate them '
          'cleanly on the next cron run')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Release unresolved "zombie" domains to the inventory pool.

A zombie = a `domains` row whose legacy `assigned_to` holds free text
that doesn't resolve to any current Slack user, AND which has no active
row in `domain_assignments`. The lifecycle bot can't DM anyone for these
(the name resolves to nobody), and because the legacy column is
non-empty they ALSO don't appear in the inventory digest — so they are
silent orphans the bot will never act on.

This script clears `assigned_to` (sets it NULL) for every such row, so
they surface in the daily inventory digest in #developers, where the
team claims them via /reassign-domain.

Background: after Phase E's backfill + alias passes, ~353 rows remained
unresolved — genuine ex-employee names (Neeraj/Nitin/Tanish/Sagar Rana/
Aradhna/etc.), non-MDB labels ('Renew', 'advertiser'), and a few
deactivated Slack users. TL (Shubham) approved on 2026-05-14: release
them to the pool, team does manual RedTrack lookup on Saturday.

Usage:
    python -m lifecycle.release_zombies            # dry run — counts only
    python -m lifecycle.release_zombies --apply    # writes

Idempotent: only touches rows that are still zombies. Re-running after
--apply is a no-op. Records a `released_to_inventory` event per domain
so /domain-history shows what was cleared and why.
"""
from __future__ import annotations

import argparse
import logging
import sys

from inventory import store

logger = logging.getLogger(__name__)

# Bucket C — legacy names that MIGHT still be current employees (the
# fuzzy matcher couldn't place them, and we haven't gotten a yes/no from
# Utkarsh yet). HOLD these — do NOT release until verified, otherwise we
# orphan a domain that actually has a live owner. Compared case-folded.
# Once Utkarsh confirms each one (alias it, or "ex-employee"), drop it
# from this set.
_HOLD_NAMES = {
    'anup', 'swayam', 'parmeet', 'swayam saini', 'rajat', 'chetan sir',
    'mohit', 'parmeet singh', 'vansh sourav', 'swayan', 'shubam', 'rishi',
    'tanishq', 'mohit bhandari', 'shubham chandrabansi', 'vansh', 'anurag',
    'rajat sir', 'aniket',
}

_ZOMBIE_SQL = (
    'SELECT d.domain, d.assigned_to '
    'FROM domains d '
    "WHERE d.assigned_to IS NOT NULL AND d.assigned_to != '' "
    '  AND NOT EXISTS ('
    '    SELECT 1 FROM domain_assignments a '
    '    WHERE a.domain = d.domain AND a.ended_at IS NULL'
    '  )'
)


def _find_zombies():
    """Return [(domain, legacy_assigned_to), ...] for every zombie row."""
    with store._conn() as c:
        cur = store._execute(c, _ZOMBIE_SQL)
        rows = cur.fetchall()
        if store._is_postgres():
            cur.close()
    out = []
    held = []
    for r in rows:
        domain = r['domain'] if hasattr(r, 'keys') else r[0]
        legacy = r['assigned_to'] if hasattr(r, 'keys') else r[1]
        if (legacy or '').strip().lower() in _HOLD_NAMES:
            held.append((domain, legacy))
        else:
            out.append((domain, legacy))
    return out, held


def _release(domain: str, legacy: str) -> None:
    """Clear assigned_to for one domain + record the audit event."""
    with store._conn() as c:
        cur = store._execute(
            c,
            'UPDATE domains SET assigned_to = NULL, '
            'updated_at = CURRENT_TIMESTAMP WHERE domain = ?',
            (domain,),
        )
        if store._is_postgres():
            cur.close()

    store.record_event(
        domain, 'released_to_inventory',
        actor='zombie_release_2026_05_14',
        from_state=None, to_state=None,
        metadata={
            'cleared_legacy_assigned_to': legacy,
            'reason': ('unresolved ex-employee / non-MDB name — TL approved '
                       'release to inventory pool for manual cleanup'),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--apply', action='store_true',
                        help='Actually clear assigned_to. Default is dry run.')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s')

    store.init_db()
    zombies, held = _find_zombies()
    print(f'found {len(zombies) + len(held)} zombie rows total')
    print(f'  releasing:  {len(zombies)}  (confirmed ex-employee / non-MDB label)')
    print(f'  HOLDING:    {len(held)}  (bucket C — unverified, awaiting Utkarsh)')

    if not zombies:
        print('\nnothing to release.')
        return 0

    # Show a sample so the operator can sanity-check before --apply.
    print('\nsample of what WOULD be released (first 15):')
    for domain, legacy in zombies[:15]:
        print(f'  {domain:45s}  assigned_to={legacy!r}')
    if len(zombies) > 15:
        print(f'  ... and {len(zombies) - 15} more')

    if not args.apply:
        print(f'\nDRY RUN — would clear assigned_to on {len(zombies)} rows '
              f'({len(held)} held back). Re-run with --apply to commit.')
        return 0

    released = 0
    for domain, legacy in zombies:
        try:
            _release(domain, legacy)
            released += 1
        except Exception:
            logger.exception('failed to release %s', domain)

    print(f'\nreleased {released} domains to the inventory pool')
    print('they surface in the next inventory digest (#developers)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

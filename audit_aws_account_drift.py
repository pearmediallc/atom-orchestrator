"""Detect and (optionally) fix aws_account drift in the inventory.

Background — 2026-05-11 incident on mymedicareexperts.online:
  Path B's confirm_purchased used to call add_domain WITHOUT aws_account,
  letting init_db's NULL-backfill quietly stamp every NULL row to
  'auto-insurance' at boot. The actual buckets, however, were created
  in whichever AWS account the user manually picked at the time —
  typically 'other-vertical' for several verticals. The end result is
  ~760 inventory rows claiming auto-insurance for buckets that actually
  live in other-vertical, which:
    • makes every Mark Deployed silently AccessDenied (the credentials
      for auto-insurance can't write to a bucket owned by other-vertical)
    • silently 404'd post-deploy URLs (incident URL:
      https://mymedicareexperts.online/cons-td/)

This script asks ATOM for the authoritative list of buckets per account
(via /api/buckets/<account_key>) and compares against our inventory's
aws_account column. Drifted rows are listed; with --apply they are
corrected to match reality.

Usage:
    # Dry run — print drift, change nothing:
    python audit_aws_account_drift.py

    # Apply fixes — update each drifted row's aws_account in place:
    python audit_aws_account_drift.py --apply

Safety:
    • READ-ONLY against ATOM. We only call GETs.
    • Idempotent against inventory — running twice is harmless.
    • Skips rows whose bucket doesn't exist in ANY known account
      (flagged separately as 'orphaned' so an operator can investigate).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict

from config import Config
from orchestrator.atom_client import AtomClient


def _list_buckets_per_account(atom: AtomClient) -> dict[str, set[str]]:
    """Return {account_key: {bucket_name, ...}} by polling ATOM's per-account
    listing endpoint. ATOM's account keys come from Config.AWS_ACCOUNT_OPTIONS
    so adding a new account requires no change here.
    """
    by_account = {}
    for account_key in Config.AWS_ACCOUNT_OPTIONS:
        resp = atom._get_json(f'/api/buckets/{account_key}', timeout=30)
        # ATOM returns {'buckets': [{'name': '...', ...}, ...]} on success.
        raw = resp.get('buckets') or []
        names = {b.get('name', '') for b in raw if b.get('name')}
        by_account[account_key] = names
        print(f'  ATOM/{account_key}: {len(names)} buckets')
    return by_account


def _classify(domain: str, current_account: str,
              buckets_per_account: dict[str, set[str]]) -> str:
    """Return one of: 'ok', 'drift:<correct_account>', 'orphan', 'duplicate'."""
    owning_accounts = [
        acct for acct, names in buckets_per_account.items()
        if domain in names
    ]
    if not owning_accounts:
        return 'orphan'  # no AWS account claims this bucket
    if len(owning_accounts) > 1:
        return f'duplicate:{",".join(sorted(owning_accounts))}'
    real = owning_accounts[0]
    return 'ok' if real == current_account else f'drift:{real}'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='Update drifted rows in-place. Without this '
                             'flag, the script only reports.')
    parser.add_argument('--sqlite-path', default='./inventory.db')
    args = parser.parse_args()

    print(f'Loading bucket map from ATOM at {Config.ATOM_BASE_URL}...')
    atom = AtomClient()
    atom.login(Config.ATOM_USERNAME, Config.ATOM_PASSWORD)
    buckets_per_account = _list_buckets_per_account(atom)

    con = sqlite3.connect(args.sqlite_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        'SELECT domain, aws_account FROM domains WHERE domain IS NOT NULL'
    ).fetchall()
    print(f'\nLoaded {len(rows)} inventory rows.')

    counts = defaultdict(int)
    drift_targets = []  # (domain, current, correct)
    orphans = []
    duplicates = []
    for r in rows:
        verdict = _classify(r['domain'], r['aws_account'] or '',
                            buckets_per_account)
        if verdict == 'ok':
            counts['ok'] += 1
        elif verdict.startswith('drift:'):
            correct = verdict.split(':', 1)[1]
            drift_targets.append((r['domain'], r['aws_account'], correct))
            counts['drift'] += 1
        elif verdict == 'orphan':
            orphans.append(r['domain'])
            counts['orphan'] += 1
        elif verdict.startswith('duplicate:'):
            duplicates.append((r['domain'], verdict.split(':', 1)[1]))
            counts['duplicate'] += 1

    print('\nSummary:')
    for k in ('ok', 'drift', 'orphan', 'duplicate'):
        print(f'  {k:10s}: {counts[k]}')

    if drift_targets:
        print(f'\nDrifted ({len(drift_targets)}):')
        for d, cur, real in drift_targets[:20]:
            print(f'  {d!r:50s}  inventory={cur!r:18s}  actual={real!r}')
        if len(drift_targets) > 20:
            print(f'  …and {len(drift_targets) - 20} more.')

    if orphans:
        print(f'\nOrphan domains (no bucket in any known account, '
              f'{len(orphans)}):')
        for d in orphans[:10]:
            print(f'  {d}')
        if len(orphans) > 10:
            print(f'  …and {len(orphans) - 10} more.')

    if duplicates:
        print(f'\nDuplicates (bucket name exists in multiple accounts):')
        for d, accts in duplicates:
            print(f'  {d}: {accts}')

    if args.apply and drift_targets:
        print(f'\nApplying {len(drift_targets)} corrections...')
        for d, _cur, correct in drift_targets:
            con.execute(
                'UPDATE domains SET aws_account = ?, '
                'updated_at = CURRENT_TIMESTAMP '
                'WHERE domain = ?',
                (correct, d),
            )
        con.commit()
        print('done — re-run without --apply to verify counts match.')
    elif drift_targets and not args.apply:
        print(f'\nDry run — no changes written. Re-run with --apply to fix '
              f'the {len(drift_targets)} drifted rows.')

    con.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

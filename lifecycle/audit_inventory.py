"""One-shot full-inventory audit — Namecheap + RedTrack cross-reference.

For every domain in atom-orchestrator's inventory, look up:
  • Namecheap ownership + expire date (one getInfo call per domain)
  • RedTrack 30d spend / revenue (one bulk fetch up front)
Then classify each row so a human can decide what to do with it.

Categories:
  HEALTHY_ACTIVE   — owned by us, has spend, expiry > 30 days
  EXPIRING_ACTIVE  — owned by us, has spend, expiry ≤ 30 days  ← URGENT
  EXPIRING_IDLE    — owned by us, no spend, expiry ≤ 30 days
  IDLE_OWNED       — owned by us, no spend, expiry > 30 days
  ZOMBIE_LIKELY    — NOT owned by us, no spend (probably dead row)
  ANOMALY          — NOT owned by us, has spend (tracking external domain?)
  UNKNOWN          — Namecheap call failed (transport / proxy)

Output:
  audit/inventory_audit_<YYYY-MM-DD>.csv  — one row per domain (streaming)
  audit/inventory_audit_<YYYY-MM-DD>.md   — summary written at the end

Resumable: if killed mid-run, re-run picks up where it left off (CSV is
appended; we skip domains already written).

Pacing: ~1.3s between Namecheap calls = ~46 req/min, under the ~50/min
ceiling. Full 743-row run ≈ 16 minutes.

Usage:
    python -m lifecycle.audit_inventory          # full run, writes audit/
    python -m lifecycle.audit_inventory --max 50 # smoke test
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import sys
import time
from collections import Counter
from typing import Optional

from config import Config
from domain_assistant.namecheap_check import (
    _request_namecheap, _local_name, _parse_namecheap_date,
)
from inventory import store
from redtrack_client import get_domain_spend_revenue_30d


_CALL_INTERVAL_SECONDS = 1.3
_AUDIT_DIR = 'audit'

_CSV_FIELDS = [
    'domain',
    'inv_vertical',
    'inv_requested_by',
    'inv_assigned_to',
    'inv_lifecycle_state',
    'nc_status',         # OWNED | NOT_OWNED | TRANSPORT_FAIL | ERROR
    'nc_expire_at',
    'nc_days_until_expiry',
    'nc_error_number',
    'nc_error_text',
    'rt_cost_30d',
    'rt_revenue_30d',
    'rt_clicks_30d',
    'rt_active',         # cost ≥ LIFECYCLE_ACTIVE_SPEND_USD
    'category',
    'recommendation',
]

logger = logging.getLogger(__name__)


def _today() -> dt.date:
    return dt.date.today()


def _open_csv(path: str):
    """Open the CSV, write header if file is new. Returns (writer, file)."""
    is_new = not os.path.exists(path)
    f = open(path, 'a', newline='', encoding='utf-8')
    w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
    if is_new:
        w.writeheader()
        f.flush()
    return w, f


def _already_audited(path: str) -> set:
    """Read existing CSV (if any) and return the set of domains already
    written. Lets re-runs skip past completed work."""
    if not os.path.exists(path):
        return set()
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return {row['domain'] for row in reader if row.get('domain')}


# ─── Namecheap lookup (read-only — no DB writes) ──────────────────────────

def _check_namecheap(domain: str) -> dict:
    """Return a dict describing what Namecheap thinks of this domain.
       Never raises — failures are reported as TRANSPORT_FAIL/ERROR."""
    try:
        root = _request_namecheap({
            'Command': 'namecheap.domains.getInfo', 'DomainName': domain,
        })
    except Exception as e:
        return {'nc_status': 'TRANSPORT_FAIL', 'nc_error_text': str(e)[:120]}

    if root is None:
        return {'nc_status': 'TRANSPORT_FAIL'}

    api_status = root.get('Status', '?')
    is_owner = None
    expire = None
    err_num = None
    err_text = None

    for el in root.iter():
        tag = _local_name(el.tag)
        if tag == 'Error':
            err_num = el.get('Number')
            err_text = (el.text or '').strip()[:120]
        elif tag == 'DomainGetInfoResult':
            is_owner = el.get('IsOwner')
        elif tag == 'ExpiredDate' and expire is None:
            expire = _parse_namecheap_date(el.text)

    if api_status == 'OK' and is_owner == 'true' and expire:
        return {
            'nc_status': 'OWNED',
            'nc_expire_at': expire.date().isoformat(),
            'nc_days_until_expiry': (expire.date() - _today()).days,
        }
    if err_num:
        return {
            'nc_status': 'NOT_OWNED',
            'nc_error_number': err_num,
            'nc_error_text': err_text or '',
        }
    return {
        'nc_status': 'ERROR',
        'nc_error_text': f'api_status={api_status} is_owner={is_owner}',
    }


# ─── Categorisation ───────────────────────────────────────────────────────

def _categorise(nc: dict, rt_active: bool, rt_cost: float) -> tuple:
    """Returns (category, recommendation). Pure, easy to test."""
    nc_status = nc.get('nc_status')
    days = nc.get('nc_days_until_expiry')

    if nc_status == 'TRANSPORT_FAIL':
        return ('UNKNOWN', 'Namecheap call failed; will retry on next audit')
    if nc_status == 'ERROR':
        return ('UNKNOWN', 'Unexpected Namecheap response; investigate')

    if nc_status == 'OWNED':
        if days is not None and days <= 30:
            if rt_active:
                return (
                    'EXPIRING_ACTIVE',
                    f'URGENT: active campaign, expires in {days} days — '
                    'renew now',
                )
            return (
                'EXPIRING_IDLE',
                f'Expires in {days} days, no spend last 30d — let it lapse '
                'or push to inventory',
            )
        if rt_active:
            return (
                'HEALTHY_ACTIVE',
                f'Active campaign, expires in {days} days. No action.',
            )
        return (
            'IDLE_OWNED',
            'Owned but no spend last 30d — candidate for inventory pool',
        )

    # NOT_OWNED
    if rt_active:
        return (
            'ANOMALY',
            f'Tracking ${rt_cost:.2f} in RedTrack but NOT in our '
            'Namecheap account — different account or different registrar?',
        )
    return (
        'ZOMBIE_LIKELY',
        'Not in our Namecheap, no recent spend — probably a stale row '
        'we can delete from inventory',
    )


# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--max', type=int, default=None,
                        help='Stop after N rows (smoke testing).')
    parser.add_argument('--out-dir', default=_AUDIT_DIR,
                        help='Directory for CSV + markdown output.')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if not args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(message)s',
    )
    # Quiet noisy libraries even in verbose mode
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    if not (Config.NAMECHEAP_API_USER and Config.NAMECHEAP_API_KEY):
        sys.exit('Namecheap creds not configured — cannot run audit.')

    os.makedirs(args.out_dir, exist_ok=True)
    today_iso = _today().isoformat()
    csv_path = os.path.join(args.out_dir, f'inventory_audit_{today_iso}.csv')
    md_path = os.path.join(args.out_dir, f'inventory_audit_{today_iso}.md')

    print(f'Writing CSV  : {csv_path}')
    print(f'Writing MD   : {md_path}')

    store.init_db()
    all_rows = store.list_domains()
    print(f'Inventory rows: {len(all_rows)}')

    print('Pulling RedTrack 30d spend (one bulk call) ...')
    spend_by_host = get_domain_spend_revenue_30d()
    print(f'RedTrack hosts with traffic: {len(spend_by_host)}')

    already = _already_audited(csv_path)
    if already:
        print(f'Resuming — {len(already)} domains already audited, skipping.')
    todo = [r for r in all_rows if r['domain'] not in already]
    if args.max is not None:
        todo = todo[:args.max]
    print(f'Domains to audit this run: {len(todo)}')
    print('-' * 100)

    writer, fp = _open_csv(csv_path)
    counts = Counter()
    started = time.time()

    try:
        for i, row in enumerate(todo, 1):
            domain = row['domain']
            spend = spend_by_host.get(domain.lower(), {})
            cost = float(spend.get('cost') or 0)
            revenue = float(spend.get('revenue') or 0)
            clicks = int(spend.get('clicks') or 0)
            rt_active = cost >= Config.LIFECYCLE_ACTIVE_SPEND_USD

            nc = _check_namecheap(domain)
            cat, rec = _categorise(nc, rt_active, cost)
            counts[cat] += 1

            record = {
                'domain': domain,
                'inv_vertical': row.get('vertical') or '',
                'inv_requested_by': row.get('requested_by') or '',
                'inv_assigned_to': row.get('assigned_to') or '',
                'inv_lifecycle_state': row.get('lifecycle_state') or '',
                'nc_status': nc.get('nc_status', ''),
                'nc_expire_at': nc.get('nc_expire_at', ''),
                'nc_days_until_expiry': nc.get('nc_days_until_expiry', ''),
                'nc_error_number': nc.get('nc_error_number', ''),
                'nc_error_text': nc.get('nc_error_text', ''),
                'rt_cost_30d': f'{cost:.2f}',
                'rt_revenue_30d': f'{revenue:.2f}',
                'rt_clicks_30d': clicks,
                'rt_active': rt_active,
                'category': cat,
                'recommendation': rec,
            }
            writer.writerow(record)
            fp.flush()

            if i % 25 == 0 or i == len(todo):
                pct = 100 * i / len(todo) if todo else 0
                elapsed = time.time() - started
                eta = elapsed / i * (len(todo) - i) if i else 0
                print(f'[{i:4d}/{len(todo)}] {pct:5.1f}%   '
                      f'elapsed {elapsed/60:5.1f}m   eta {eta/60:5.1f}m   '
                      f'{dict(counts.most_common())}')

            time.sleep(_CALL_INTERVAL_SECONDS)
    finally:
        fp.close()

    print('-' * 100)
    print(f'Done. counts: {dict(counts.most_common())}')
    _write_summary(md_path, csv_path, counts)
    print(f'\nSummary written to {md_path}')
    return 0


def _write_summary(md_path: str, csv_path: str, counts: Counter) -> None:
    """Read the CSV back and write a human-readable markdown summary."""
    rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    by_cat: dict = {}
    for r in rows:
        by_cat.setdefault(r['category'], []).append(r)

    total = len(rows)

    def _bullets_for(cat: str, limit: int = 25) -> str:
        items = by_cat.get(cat, [])
        if not items:
            return '_(none)_'
        out = []
        # sort by days_until_expiry asc when available, else by domain
        def _sort_key(r):
            days = r.get('nc_days_until_expiry') or ''
            try:
                return (0, int(days))
            except ValueError:
                return (1, r.get('domain', ''))
        items = sorted(items, key=_sort_key)
        for r in items[:limit]:
            days = r.get('nc_days_until_expiry')
            d = r.get('domain')
            mdb = r.get('inv_requested_by') or '_no MDB_'
            cost = r.get('rt_cost_30d') or '0'
            extra = []
            if days != '':
                extra.append(f'expires in {days} days')
            if float(cost) > 0:
                extra.append(f'30d spend ${cost}')
            extra_s = (' — ' + ', '.join(extra)) if extra else ''
            out.append(f'- `{d}` ({mdb}){extra_s}')
        if len(items) > limit:
            out.append(f'- _… and {len(items) - limit} more (see CSV)_')
        return '\n'.join(out)

    md = f"""# Inventory Audit — {_today().isoformat()}

Cross-reference of every atom-orchestrator inventory row against
Namecheap (ownership + expiry) and RedTrack (30d spend).

## Totals

| Category | Count | % |
|---|---:|---:|
"""
    for cat, n in counts.most_common():
        pct = 100 * n / total if total else 0
        md += f'| `{cat}` | {n} | {pct:.1f}% |\n'
    md += f'| **TOTAL** | **{total}** | 100% |\n\n'

    md += f"""## What each category means

- **HEALTHY_ACTIVE** — owned by us in Namecheap, RedTrack shows spend, expiry >30 days. Bot will leave alone.
- **EXPIRING_ACTIVE** — owned by us, has spend, expiry ≤30 days. **URGENT — needs renewal.**
- **EXPIRING_IDLE** — owned by us, no spend, expiry ≤30 days. Candidate to let lapse.
- **IDLE_OWNED** — owned by us, no spend last 30d. Candidate to push to inventory pool / reuse for rotation.
- **ZOMBIE_LIKELY** — NOT in our Namecheap account, no RedTrack spend. Most likely a stale inventory row that should be removed.
- **ANOMALY** — NOT in our Namecheap account, but has RedTrack spend. Means we're tracking a domain we don't appear to own. Could be a different registrar, or a campaign run by someone outside the bot's flow.
- **UNKNOWN** — Namecheap call failed (proxy / network). Will retry on next audit.

## 🔴 EXPIRING_ACTIVE — needs renewal NOW

Sorted by days-until-expiry, soonest first.

{_bullets_for('EXPIRING_ACTIVE', limit=50)}

## 🟡 EXPIRING_IDLE — let lapse or renew

{_bullets_for('EXPIRING_IDLE', limit=50)}

## ⚠️ ANOMALY — tracking but not owned by us

{_bullets_for('ANOMALY', limit=50)}

## ZOMBIE_LIKELY — candidates to remove from inventory

{_bullets_for('ZOMBIE_LIKELY', limit=50)}

## IDLE_OWNED — candidates for inventory pool

{_bullets_for('IDLE_OWNED', limit=25)}

## HEALTHY_ACTIVE — no action

_({len(by_cat.get('HEALTHY_ACTIVE', []))} domains. See CSV for full list.)_

## UNKNOWN — Namecheap call failed

{_bullets_for('UNKNOWN', limit=25)}

---

Data source: `{csv_path}`. Re-run `python -m lifecycle.audit_inventory`
to regenerate.
"""

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)


if __name__ == '__main__':
    sys.exit(main())

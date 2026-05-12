"""One-shot backfill — translate legacy domains.assigned_to (free-text
human names) into rows in the new domain_assignments table, keyed by
verified Slack user IDs.

Prerequisite: slack_users must be populated first. Run the daily cron
once (which runs slack_users sync as its first pass) or call
lifecycle.slack_sync.run_slack_users_sync directly.

Resolution strategy per value in domains.assigned_to:

  1. Already a Slack ID format (U_XXX or 'Slack:UXXX')      → use as-is
  2. Multi-MDB string (commas, slashes, etc.)                 → split,
                                                                resolve each
                                                                independently
  3. Non-MDB label ('Renew', 'Company domain renewal', etc.)  → skip
                                                                (clears
                                                                assigned_to)
  4. Exact match in slack_users (real_name / display_name)    → that uid
  5. First-name match (single word, unique match)             → that uid
  6. Fuzzy match (Levenshtein ≥ 0.85, single match)           → that uid
                                                                + cache as alias
  7. Otherwise                                                → leave unresolved

For every successful resolution, an `added_assignment` event is
written to domain_events for /domain-history visibility.

Usage:
    python -m lifecycle.backfill_assignments              # dry run
    python -m lifecycle.backfill_assignments --apply      # writes

The script is idempotent — re-running won't create duplicate
assignment rows for the same (domain, slack_user_id) pair.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import sys
from collections import Counter
from typing import Iterable, List, Optional, Tuple

from inventory import store

logger = logging.getLogger(__name__)


# Tokens we recognise as multi-MDB separators inside an assigned_to value.
# Order matters when splitting: longer separators first to avoid breaking
# 'Anand and Sumit' on the first 'and' incorrectly.
_MULTI_DELIMS = (', ', ',', '/', ';', ' & ', ' and ', '+')

# Strings we know are not people. These get the domain pushed to the
# inventory pool (assigned_to cleared) rather than attempting any match.
_NON_MDB_LABELS = {
    'renew', 'renew for renew', 'renewal', 'company domain renewal',
    'advertiser', 'advertisor', 'keitaro',
    'ad account(by sunny)', 'external(rachit)',
    'rachit sir(for freelance aniket)',
}

# Fuzzy match cutoff. 0.85 = "very close" per difflib's SequenceMatcher.
# Lower would resolve more matches but with more false positives.
_FUZZY_CUTOFF = 0.85


def _looks_like_slack_id(s: str) -> bool:
    s = s.strip()
    if s.startswith('Slack:'):
        s = s[6:].strip()
    if not s.startswith('U'):
        return False
    if len(s) < 9 or len(s) > 14:
        return False
    return s[1:].isalnum() and s[1:].isupper() and any(
        c.isdigit() for c in s[1:]
    )


def _strip_slack_prefix(s: str) -> str:
    return s[6:].strip() if s.startswith('Slack:') else s


def _split_multi_mdb(value: str) -> List[str]:
    """Split a value on any of the delimiters. Returns the cleaned parts.
    Single-name values return as a 1-element list."""
    parts: List[str] = [value]
    for d in _MULTI_DELIMS:
        next_parts = []
        for p in parts:
            next_parts.extend(p.split(d))
        parts = next_parts
    return [p.strip() for p in parts if p.strip()]


def _build_fuzzy_pool(slack_users: List[dict]) -> List[Tuple[str, str]]:
    """Returns [(lowercased_name, slack_user_id)] for fuzzy matching."""
    pool: List[Tuple[str, str]] = []
    for u in slack_users:
        for key in (u.get('real_name'), u.get('display_name')):
            if key:
                pool.append((key.strip().lower(), u['slack_user_id']))
    return pool


def _build_alias_map(slack_users: List[dict]) -> dict:
    """{lowercased_name_or_alias: slack_user_id} — in-memory equivalent of
    store.lookup_slack_id_by_alias, used to avoid per-row DB round trips
    when running the backfill from outside Render's network."""
    import json as _json
    m: dict = {}
    for u in slack_users:
        for key in (u.get('real_name'), u.get('display_name')):
            if key and key.strip():
                m.setdefault(key.strip().lower(), u['slack_user_id'])
        aliases = u.get('name_aliases')
        if isinstance(aliases, str):
            try:
                aliases = _json.loads(aliases)
            except Exception:
                aliases = []
        for a in (aliases or []):
            if a and str(a).strip():
                m.setdefault(str(a).strip().lower(), u['slack_user_id'])
    return m


def _build_firstname_index(slack_users: List[dict]) -> dict:
    """{first_word_lower: [slack_user_id, ...]} — for unique-first-name
    match attempts."""
    idx: dict = {}
    for u in slack_users:
        for key in (u.get('real_name'), u.get('display_name')):
            if not key:
                continue
            first = key.strip().split()[0].lower() if key.strip().split() else ''
            if first:
                idx.setdefault(first, set()).add(u['slack_user_id'])
    return {k: list(v) for k, v in idx.items()}


def _resolve_one(
    raw_value: str,
    *,
    fuzzy_pool: List[Tuple[str, str]],
    firstname_idx: dict,
    alias_map: Optional[dict] = None,
) -> Tuple[Optional[str], str, Optional[str]]:
    """Attempt to resolve a single non-multi MDB token to a Slack ID.

    Returns (slack_user_id, resolution_kind, alias_to_cache).
    `alias_to_cache` is the original value if it was resolved via a
    non-exact path (so the backfill can store it as an alias for next time).
    """
    v = raw_value.strip()
    vl = v.lower()
    if not vl:
        return (None, 'empty', None)

    if vl in _NON_MDB_LABELS:
        return (None, 'non_mdb_label', None)

    if _looks_like_slack_id(v):
        return (_strip_slack_prefix(v), 'already_id', None)

    if alias_map is not None:
        exact_uid = alias_map.get(vl)
    else:
        exact_uid = store.lookup_slack_id_by_alias(v)
    if exact_uid:
        return (exact_uid, 'exact_or_alias', None)

    # First-name unique match
    parts = vl.split()
    if len(parts) == 1 and parts[0] in firstname_idx:
        candidates = firstname_idx[parts[0]]
        if len(candidates) == 1:
            return (candidates[0], 'firstname_unique', v)
        return (None, 'firstname_ambiguous', None)

    # Fuzzy match
    if fuzzy_pool:
        pool_names = [n for n, _ in fuzzy_pool]
        close = difflib.get_close_matches(vl, pool_names, n=2,
                                          cutoff=_FUZZY_CUTOFF)
        if len(close) == 1:
            for name, uid in fuzzy_pool:
                if name == close[0]:
                    return (uid, 'fuzzy', v)
        elif len(close) >= 2:
            return (None, 'fuzzy_ambiguous', None)

    return (None, 'no_match', None)


def resolve_value(
    raw_value: str,
    *,
    fuzzy_pool: List[Tuple[str, str]],
    firstname_idx: dict,
    alias_map: Optional[dict] = None,
) -> Tuple[List[str], List[str], List[Tuple[str, str]]]:
    """Resolve a single raw assigned_to string to a list of Slack IDs.

    Returns:
      (resolved_uids, unresolved_tokens, alias_pairs)
        resolved_uids:     Slack IDs to insert as active assignments
        unresolved_tokens: parts we couldn't resolve (for reporting)
        alias_pairs:       [(slack_user_id, alias_to_cache), ...] —
                           non-exact resolutions worth caching as aliases
                           so future imports skip the fuzzy step
    """
    tokens = _split_multi_mdb(raw_value)
    resolved: List[str] = []
    unresolved: List[str] = []
    alias_pairs: List[Tuple[str, str]] = []
    for tok in tokens:
        uid, kind, alias = _resolve_one(
            tok, fuzzy_pool=fuzzy_pool, firstname_idx=firstname_idx,
            alias_map=alias_map,
        )
        if uid:
            if uid not in resolved:    # dedup within one value
                resolved.append(uid)
            if alias:
                alias_pairs.append((uid, alias))
        else:
            unresolved.append(tok)
    return resolved, unresolved, alias_pairs


# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--apply', action='store_true',
                        help='Actually write to domain_assignments. Default is dry run.')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(message)s',
    )

    store.init_db()

    slack_users = store.list_slack_users(include_deleted=False)
    if not slack_users:
        sys.exit(
            'slack_users table is empty. Run the daily cron first '
            "(or call lifecycle.slack_sync.run_slack_users_sync) so we "
            'have workspace data to resolve names against.'
        )
    print(f'slack_users cache: {len(slack_users)} active members')

    fuzzy_pool = _build_fuzzy_pool(slack_users)
    firstname_idx = _build_firstname_index(slack_users)
    alias_map = _build_alias_map(slack_users)

    all_rows = store.list_domains()
    print(f'Inventory rows: {len(all_rows)}')

    # Pre-load every active assignment in one query — otherwise we pay an
    # extra round trip per row, which is ~1s each when running from outside
    # Render's network.
    bulk_assignments = store.bulk_current_assignments()
    print(f'Existing active assignments: {len(bulk_assignments)} domains')

    counters = Counter()
    new_assignments = 0
    new_aliases = 0

    for row in all_rows:
        raw = (row.get('assigned_to') or '').strip()
        if not raw:
            counters['no_assigned_to'] += 1
            continue

        # Skip rows where domain_assignments already has entries —
        # makes the script safely re-runnable.
        if bulk_assignments.get(row['domain']):
            counters['already_has_assignment'] += 1
            continue

        resolved, unresolved, alias_pairs = resolve_value(
            raw, fuzzy_pool=fuzzy_pool, firstname_idx=firstname_idx,
            alias_map=alias_map,
        )

        if not resolved:
            counters['could_not_resolve'] += 1
            if args.verbose:
                print(f'  ⚪ {row["domain"]:35s}  '
                      f'raw={raw!r}  unresolved={unresolved}')
            continue

        if args.verbose:
            print(f'  ✅ {row["domain"]:35s}  raw={raw!r}  → {resolved} '
                  f'({len(unresolved)} unresolved token(s))')

        if args.apply:
            for uid in resolved:
                store.assign_domain(
                    row['domain'], uid,
                    assigned_by='cron',
                    notes=(f'backfilled from domains.assigned_to={raw!r}'
                           + (f'; unresolved tokens: {unresolved}'
                              if unresolved else '')),
                )
                new_assignments += 1
                store.record_event(
                    row['domain'], 'added_assignment',
                    actor='cron',
                    from_state=None, to_state=None,
                    metadata={
                        'slack_user_id': uid,
                        'source': 'backfill_assignments_2026_05_12',
                        'raw_legacy_value': raw,
                        'unresolved_tokens': unresolved,
                    },
                )
            for uid, alias in alias_pairs:
                store.add_alias_to_slack_user(uid, alias)
                new_aliases += 1

        counters['resolved'] += 1
        counters['resolved_with_multi'] += 1 if len(resolved) > 1 else 0

    print()
    print('-' * 50)
    print('Summary:')
    for k, v in counters.most_common():
        print(f'  {k:30s}  {v}')
    if args.apply:
        print(f'\nNew assignment rows written:  {new_assignments}')
        print(f'New aliases cached:           {new_aliases}')
    else:
        print('\nDRY RUN — no rows written. Re-run with --apply to commit.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

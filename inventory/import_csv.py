"""One-time CSV importer for the team's domain inventory.

The team maintains an "owned domains" log via a Google Form whose responses
land in a Google Sheet. This script imports an exported CSV of that sheet
into the bot's local SQLite store. Column names are auto-detected
(case-insensitive, multi-alias) so the importer works regardless of the
exact headers Utkarsh used.

Usage (from the project root):
    python -m inventory.import_csv ~/Desktop/inventory.csv

Re-running is safe — existing domains are skipped (not overwritten). To
overwrite, pass --replace.

Phase 6 will replace this with a live Google Sheets API integration so
the bot stays in sync continuously rather than on-demand.
"""
import argparse
import csv
import re
import sys
from typing import Dict, List, Optional

from inventory import store


# A "looks like a domain" check: at least one dot, valid chars, ends with
# a 2+ letter TLD. Catches obvious garbage like notes / parens / numbers.
_DOMAIN_PATTERN = re.compile(
    r'^[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)*\.[a-z]{2,}$',
    re.IGNORECASE,
)


def _is_domain_like(s: str) -> bool:
    """True if the string looks like a domain name (has a TLD, no spaces)."""
    if not s or ' ' in s or '\t' in s or '.' not in s:
        return False
    return bool(_DOMAIN_PATTERN.match(s))


def _split_domain_field(value: str) -> List[str]:
    """Split a 'domain' cell that may contain multiple domains.

    Some Google Form responses cram multiple domain names into one cell,
    separated by whitespace, commas, semicolons, or slashes. We split on
    any of those, normalise each candidate (strip http(s)://, www., paths)
    and validate it looks like a domain.
    """
    if not value:
        return []

    # Split on whitespace, commas, semicolons, newlines, or vertical bars
    candidates = re.split(r'[\s,;|]+', value)

    out: List[str] = []
    seen = set()
    for raw in candidates:
        c = raw.strip().lower()
        # Strip common URL prefixes
        for prefix in ('https://', 'http://', 'www.'):
            if c.startswith(prefix):
                c = c[len(prefix):]
        # Strip path / fragment / query
        c = c.split('/')[0].split('?')[0].split('#')[0].rstrip('.').strip()
        if not c or c in seen:
            continue
        if _is_domain_like(c):
            out.append(c)
            seen.add(c)
    return out


# When the vertical column says "Other" (or similar catch-all), look up
# the real value in one of these follow-up columns. Common with Google
# Forms that have an "Other..." text-input fallback.
_VERTICAL_OTHER_FOLLOWUP_COLUMNS: List[str] = [
    'if selected others write vertical name',
    'other vertical',
    'vertical (other)',
    'specify other',
]

_VERTICAL_OTHER_VALUES = {'other', 'others'}


# Common column-name aliases. Keys are our schema fields; values are the
# (lowercased, stripped) header strings we'll accept.
_COLUMN_ALIASES: Dict[str, List[str]] = {
    'domain': [
        'domain', 'domain name', 'url', 'website', 'site', 'name',
    ],
    'vertical': [
        'vertical', 'niche', 'category', 'type', 'industry',
    ],
    'aws_account': [
        'aws account', 'account', 'aws', 'aws_account', 'aws acct',
    ],
    'lander_url': [
        'lander url', 'lander', 'page url', 'lp url',
        'landing page', 'landing url', 'lp', 'page',
    ],
    'requested_by': [
        'owner', 'requested by', 'submitted by', 'mdb', 'requester',
        'requested_by', 'submitted by name',
        'requested by (media buyer name)',  # exact form column
    ],
    'notes': [
        'notes', 'comments', 'remarks', 'description', 'note',
    ],
}


def _build_column_map(headers: List[str]) -> Dict[str, str]:
    """Map our schema fields to the actual CSV header that fills them."""
    normalised = {h.strip().lower(): h for h in headers if h}
    out: Dict[str, str] = {}
    for field, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                out[field] = normalised[alias]
                break
    return out


def _find_vertical_followup_header(headers: List[str]) -> Optional[str]:
    """Locate the follow-up column used when vertical=='Other'."""
    normalised = {h.strip().lower(): h for h in headers if h}
    for alias in _VERTICAL_OTHER_FOLLOWUP_COLUMNS:
        if alias in normalised:
            return normalised[alias]
    return None


def _resolve_vertical(row: dict, vertical_col: Optional[str],
                      followup_col: Optional[str]) -> Optional[str]:
    """Read vertical, with fallback to the 'Other...' follow-up column."""
    if not vertical_col:
        return None
    primary = (row.get(vertical_col) or '').strip()
    if not primary:
        return None
    if primary.lower() in _VERTICAL_OTHER_VALUES and followup_col:
        followup = (row.get(followup_col) or '').strip()
        if followup:
            return followup
    return primary


def import_csv(path: str, replace: bool = False) -> dict:
    """Import a CSV into the inventory store. Returns a stats dict."""
    store.init_db()

    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        col_map = _build_column_map(headers)
        vertical_followup_col = _find_vertical_followup_header(headers)

        if 'domain' not in col_map:
            raise ValueError(
                f"No 'domain' column found in {path}. "
                f"Headers detected: {headers}. "
                f"Add an alias to _COLUMN_ALIASES if needed."
            )

        rows = list(reader)

    imported = 0
    skipped_no_domain = 0
    skipped_duplicate = 0
    rows_with_multiple_domains = 0

    for row in rows:
        raw_value = row.get(col_map['domain']) or ''
        domains_in_row = _split_domain_field(raw_value)

        if not domains_in_row:
            skipped_no_domain += 1
            continue

        if len(domains_in_row) > 1:
            rows_with_multiple_domains += 1

        # Build kwargs ONCE per row — every domain in that row inherits
        # the same vertical / requested_by / etc. since they came from
        # the same form submission.
        kwargs: Dict[str, Optional[str]] = {}
        for field, csv_col in col_map.items():
            if field in ('domain', 'vertical'):
                continue
            value = (row.get(csv_col) or '').strip()
            if value:
                kwargs[field] = value

        # Vertical needs special handling: the form's main "Vertical"
        # column says "Other" for ~half the rows, with the real value
        # in a follow-up text column.
        vertical = _resolve_vertical(
            row, col_map.get('vertical'), vertical_followup_col,
        )
        if vertical:
            kwargs['vertical'] = vertical

        for domain in domains_in_row:
            existing = store.get_domain(domain)
            if existing and not replace:
                skipped_duplicate += 1
                continue

            if existing and replace:
                from inventory.store import _conn
                with _conn() as c:
                    c.execute('DELETE FROM domains WHERE domain = ?',
                              (domain,))

            store.add_domain(domain=domain, **kwargs)
            imported += 1

    unmapped_columns = [h for h in headers if h not in col_map.values()]

    return {
        'imported': imported,
        'skipped_no_domain': skipped_no_domain,
        'skipped_duplicate': skipped_duplicate,
        'rows_with_multiple_domains': rows_with_multiple_domains,
        'columns_mapped': col_map,
        'columns_unmapped': unmapped_columns,
        'total_rows_in_csv': len(rows),
    }


def main():
    parser = argparse.ArgumentParser(
        description='Import domain inventory from a Google Sheets CSV export.',
    )
    parser.add_argument('csv_path', help='Path to the exported CSV file')
    parser.add_argument(
        '--replace', action='store_true',
        help='Overwrite domains that already exist in the store.',
    )
    args = parser.parse_args()

    try:
        stats = import_csv(args.csv_path, replace=args.replace)
    except FileNotFoundError:
        print(f'X File not found: {args.csv_path}', file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f'X {e}', file=sys.stderr)
        sys.exit(1)

    print('=' * 50)
    print(f'Total CSV rows:               {stats["total_rows_in_csv"]}')
    print(f'Imported (domains):           {stats["imported"]}')
    print(f'Rows with multiple domains:   {stats["rows_with_multiple_domains"]}')
    print(f'Skipped (already in DB):      {stats["skipped_duplicate"]}')
    print(f'Skipped (no valid domain):    {stats["skipped_no_domain"]}')
    print()
    print('Columns mapped:')
    for field, csv_col in stats['columns_mapped'].items():
        print(f'  {field:15s} <- "{csv_col}"')
    if stats['columns_unmapped']:
        print()
        print('Columns ignored (no matching schema field):')
        for col in stats['columns_unmapped']:
            print(f'  - "{col}"')
    print('=' * 50)


if __name__ == '__main__':
    main()

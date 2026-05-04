"""Tests for inventory.import_csv."""
import csv
import pytest

from inventory import import_csv


def _write_csv(path, headers, rows):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def test_imports_basic_columns(tmp_inventory, tmp_path):
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain', 'Vertical', 'Notes'],
        rows=[
            ['example1.com', 'auto-insurance', 'first lander'],
            ['example2.com', 'health', ''],
        ],
    )

    stats = import_csv.import_csv(str(csv_path))

    assert stats['imported'] == 2
    rows = tmp_inventory.list_domains()
    domains = {r['domain'] for r in rows}
    assert domains == {'example1.com', 'example2.com'}


def test_handles_aliased_column_names(tmp_inventory, tmp_path):
    """E.g. 'Domain Name' instead of 'Domain' should still map."""
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain Name', 'Niche', 'Lander URL', 'Submitted By'],
        rows=[
            ['ex1.com', 'finance', 'https://ex1.com/lander', 'utkarsh'],
        ],
    )

    import_csv.import_csv(str(csv_path))

    rec = tmp_inventory.get_domain('ex1.com')
    assert rec is not None
    assert rec['vertical'] == 'finance'
    assert rec['lander_url'] == 'https://ex1.com/lander'
    assert rec['requested_by'] == 'utkarsh'


def test_strips_url_prefixes_and_paths(tmp_inventory, tmp_path):
    """Form responses sometimes capture full URLs — normalise them."""
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain'],
        rows=[
            ['https://example1.com'],
            ['http://www.example2.com/some/path'],
            ['EXAMPLE3.COM'],
        ],
    )

    import_csv.import_csv(str(csv_path))

    domains = {r['domain'] for r in tmp_inventory.list_domains()}
    assert domains == {'example1.com', 'example2.com', 'example3.com'}


def test_skips_duplicate_domains_by_default(tmp_inventory, tmp_path):
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain'],
        rows=[['example.com']],
    )

    s1 = import_csv.import_csv(str(csv_path))
    s2 = import_csv.import_csv(str(csv_path))

    assert s1['imported'] == 1
    assert s2['imported'] == 0
    assert s2['skipped_duplicate'] == 1


def test_skips_rows_without_domain(tmp_inventory, tmp_path):
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain', 'Vertical'],
        rows=[
            ['', 'auto-insurance'],
            ['real.com', 'health'],
            ['   ', 'finance'],
        ],
    )

    stats = import_csv.import_csv(str(csv_path))
    assert stats['imported'] == 1
    assert stats['skipped_no_domain'] == 2


def test_raises_when_no_domain_column(tmp_inventory, tmp_path):
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Foo', 'Bar'],
        rows=[['a', 'b']],
    )

    with pytest.raises(ValueError, match='domain'):
        import_csv.import_csv(str(csv_path))


def test_reports_unmapped_columns(tmp_inventory, tmp_path):
    """Columns we don't know about should be listed (not silently ignored)."""
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain', 'Mystery Column', 'Another One'],
        rows=[['ex.com', 'x', 'y']],
    )

    stats = import_csv.import_csv(str(csv_path))
    assert 'Mystery Column' in stats['columns_unmapped']
    assert 'Another One' in stats['columns_unmapped']


def test_handles_utf8_bom_in_csv(tmp_inventory, tmp_path):
    """Google Sheets exports often include a BOM. utf-8-sig handles it."""
    csv_path = tmp_path / 'test.csv'
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Domain', 'Vertical'])
        w.writerow(['bom-test.com', 'auto-insurance'])

    stats = import_csv.import_csv(str(csv_path))
    assert stats['imported'] == 1
    assert tmp_inventory.get_domain('bom-test.com') is not None


def test_other_vertical_uses_followup_column(tmp_inventory, tmp_path):
    """When vertical=='Other', read the real value from the follow-up column.

    Pear Media's Google Form has this exact pattern: a 'Vertical' dropdown
    with an 'Other' option, plus a text field 'If selected others Write
    Vertical Name' for when 'Other' is picked.
    """
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain', 'Vertical',
                 'If selected others Write Vertical Name'],
        rows=[
            ['real-vertical.com', 'Auto Insurance', ''],
            ['other-with-followup.com', 'Other', 'Cryptocurrency'],
            ['other-no-followup.com', 'Other', ''],
        ],
    )

    import_csv.import_csv(str(csv_path))

    assert tmp_inventory.get_domain(
        'real-vertical.com')['vertical'] == 'Auto Insurance'
    assert tmp_inventory.get_domain(
        'other-with-followup.com')['vertical'] == 'Cryptocurrency'
    # When follow-up is empty, fall back to the original "Other"
    assert tmp_inventory.get_domain(
        'other-no-followup.com')['vertical'] == 'Other'


def test_replace_flag_overwrites_existing(tmp_inventory, tmp_path):
    """With --replace, existing rows are updated rather than skipped."""
    csv_path = tmp_path / 'test.csv'

    _write_csv(csv_path,
               headers=['Domain', 'Vertical'],
               rows=[['ex.com', 'oldvert']])
    import_csv.import_csv(str(csv_path))

    _write_csv(csv_path,
               headers=['Domain', 'Vertical'],
               rows=[['ex.com', 'newvert']])
    stats = import_csv.import_csv(str(csv_path), replace=True)

    assert stats['imported'] == 1
    assert stats['skipped_duplicate'] == 0
    assert tmp_inventory.get_domain('ex.com')['vertical'] == 'newvert'


# ─── Multi-domain cell splitting ──────────────────────────────────────────

def test_splits_whitespace_separated_domains(tmp_inventory, tmp_path):
    """Real-world bug: one form row had 20+ domains in a single cell."""
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain', 'Vertical', 'Requested By'],
        rows=[
            [
                'safetyfirstauto.pro carguardianpro.pro drivesafetyhub.pro',
                'Auto Insurance',
                'Neeraj',
            ],
        ],
    )

    stats = import_csv.import_csv(str(csv_path))

    assert stats['imported'] == 3
    assert stats['rows_with_multiple_domains'] == 1
    domains = {r['domain'] for r in tmp_inventory.list_domains()}
    assert domains == {
        'safetyfirstauto.pro',
        'carguardianpro.pro',
        'drivesafetyhub.pro',
    }
    # All inherited the same vertical + requester from the original row
    for d in domains:
        rec = tmp_inventory.get_domain(d)
        assert rec['vertical'] == 'Auto Insurance'
        assert rec['requested_by'] == 'Neeraj'


def test_splits_comma_separated_domains(tmp_inventory, tmp_path):
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain'],
        rows=[['ex1.com, ex2.com,ex3.com']],
    )

    stats = import_csv.import_csv(str(csv_path))
    assert stats['imported'] == 3


def test_filters_out_garbage_in_multi_domain_cell(tmp_inventory, tmp_path):
    """Garbage tokens between real domains should be dropped."""
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain'],
        rows=[['real1.com (note: legacy) real2.com asdf real3.com']],
    )

    stats = import_csv.import_csv(str(csv_path))
    domains = {r['domain'] for r in tmp_inventory.list_domains()}
    # 'asdf' has no dot → dropped. '(note:' / 'legacy)' → dropped.
    assert domains == {'real1.com', 'real2.com', 'real3.com'}


def test_deduplicates_within_a_single_cell(tmp_inventory, tmp_path):
    """If the same domain appears twice in one cell, only insert once."""
    csv_path = tmp_path / 'test.csv'
    _write_csv(
        csv_path,
        headers=['Domain'],
        rows=[['ex.com ex.com EX.COM']],
    )

    stats = import_csv.import_csv(str(csv_path))
    assert stats['imported'] == 1


def test_is_domain_like_basic_validation():
    assert import_csv._is_domain_like('example.com') is True
    assert import_csv._is_domain_like('sub.example.co.uk') is True
    assert import_csv._is_domain_like('with-hyphens.pro') is True
    # Invalid:
    assert import_csv._is_domain_like('') is False
    assert import_csv._is_domain_like('nothing-here') is False
    assert import_csv._is_domain_like('has spaces.com') is False
    assert import_csv._is_domain_like('(garbage)') is False
    assert import_csv._is_domain_like('asdf') is False

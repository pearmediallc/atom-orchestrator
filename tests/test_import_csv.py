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

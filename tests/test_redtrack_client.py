"""Tests for redtrack_client.

The bulk fetcher joins /landings + /report?group=landing on landing_id,
then aggregates spend/revenue per host. These tests exercise the join
+ aggregation logic with mocked HTTP, plus the host-extraction
edge cases and the stub fallback path.
"""
from unittest.mock import patch

import pytest

from config import Config
from redtrack_client import client as rt
from redtrack_client.client import extract_host


# ─── extract_host edge cases ───────────────────────────────────────────────

@pytest.mark.parametrize('url,expected', [
    ('https://safetyfirstauto.pro/h-insure-c/', 'safetyfirstauto.pro'),
    ('https://www.safetyfirstauto.pro/lander/', 'safetyfirstauto.pro'),
    ('http://example.com/foo',                  'example.com'),
    ('https://EXAMPLE.COM/foo',                 'example.com'),       # case
    ('https://abc.com:8080/path',               'abc.com'),           # port stripped
    ('https://abc.com',                         'abc.com'),           # no path
    ('rewardiffy.club/foo',                     'rewardiffy.club'),   # no scheme — recovered
])
def test_extract_host_happy_paths(url, expected):
    assert extract_host(url) == expected


@pytest.mark.parametrize('url', ['', None, '   '])
def test_extract_host_returns_none_on_empty(url):
    assert extract_host(url) is None


# ─── stub fallback ─────────────────────────────────────────────────────────

def test_stub_returns_empty_dict_when_no_creds(monkeypatch):
    """No API key → empty dict, NOT a fake-spend stub. Empty means every
    domain looks idle in the classifier — the conservative default."""
    monkeypatch.setattr(Config, 'REDTRACK_API_KEY', '')
    out = rt.get_domain_spend_revenue_30d()
    assert out == {}


# ─── join + aggregate ──────────────────────────────────────────────────────

def _patch_fetchers(monkeypatch, *, landings, report_rows):
    monkeypatch.setattr(Config, 'REDTRACK_API_KEY', 'fake-key-for-tests')
    monkeypatch.setattr(rt, '_fetch_all_landings', lambda: landings)
    monkeypatch.setattr(
        rt, '_fetch_report_grouped_by_landing',
        lambda start, end: report_rows,
    )


def test_aggregates_one_landing_per_domain(monkeypatch):
    _patch_fetchers(monkeypatch,
        landings=[{'id': 'L1', 'url': 'https://example.com/lander/'}],
        report_rows=[{
            'landing_id': 'L1', 'cost': 100.0, 'revenue': 250.0,
            'profit': 150.0, 'clicks': 1000, 'conversions': 5,
            'lp_views': 800,
        }],
    )
    out = rt.get_domain_spend_revenue_30d()
    assert out == {'example.com': {
        'cost': 100.0, 'revenue': 250.0, 'profit': 150.0,
        'clicks': 1000.0, 'conversions': 5.0, 'lp_views': 800.0,
    }}


def test_sums_multiple_landings_under_same_host(monkeypatch):
    """The whole point of the join: one domain can host many landers,
    we sum spend/revenue across all of them."""
    _patch_fetchers(monkeypatch,
        landings=[
            {'id': 'L1', 'url': 'https://example.com/lander-a/'},
            {'id': 'L2', 'url': 'https://example.com/lander-b/'},
            {'id': 'L3', 'url': 'https://other.com/lander/'},
        ],
        report_rows=[
            {'landing_id': 'L1', 'cost': 100, 'revenue': 200, 'profit': 100,
             'clicks': 10, 'conversions': 1, 'lp_views': 8},
            {'landing_id': 'L2', 'cost': 50, 'revenue': 80, 'profit': 30,
             'clicks': 5, 'conversions': 0, 'lp_views': 4},
            {'landing_id': 'L3', 'cost': 25, 'revenue': 100, 'profit': 75,
             'clicks': 3, 'conversions': 1, 'lp_views': 2},
        ],
    )
    out = rt.get_domain_spend_revenue_30d()
    assert out['example.com']['cost'] == 150
    assert out['example.com']['revenue'] == 280
    assert out['other.com']['cost'] == 25


def test_normalises_www_prefix_when_aggregating(monkeypatch):
    """www.example.com and example.com should land in the same bucket."""
    _patch_fetchers(monkeypatch,
        landings=[
            {'id': 'L1', 'url': 'https://example.com/a/'},
            {'id': 'L2', 'url': 'https://www.example.com/b/'},
        ],
        report_rows=[
            {'landing_id': 'L1', 'cost': 10, 'revenue': 20, 'profit': 10,
             'clicks': 1, 'conversions': 0, 'lp_views': 1},
            {'landing_id': 'L2', 'cost': 5, 'revenue': 7, 'profit': 2,
             'clicks': 1, 'conversions': 0, 'lp_views': 1},
        ],
    )
    out = rt.get_domain_spend_revenue_30d()
    assert set(out.keys()) == {'example.com'}
    assert out['example.com']['cost'] == 15


def test_drops_landings_with_unparseable_url(monkeypatch):
    """Empty / null landing.url → skip. Won't accidentally bucket spend
    under a None or empty-string key."""
    _patch_fetchers(monkeypatch,
        landings=[
            {'id': 'L1', 'url': ''},
            {'id': 'L2', 'url': None},
            {'id': 'L3', 'url': 'https://valid.com/lander/'},
        ],
        report_rows=[
            {'landing_id': 'L1', 'cost': 100, 'revenue': 200, 'profit': 100,
             'clicks': 0, 'conversions': 0, 'lp_views': 0},
            {'landing_id': 'L3', 'cost': 50, 'revenue': 80, 'profit': 30,
             'clicks': 0, 'conversions': 0, 'lp_views': 0},
        ],
    )
    out = rt.get_domain_spend_revenue_30d()
    assert set(out.keys()) == {'valid.com'}
    assert out['valid.com']['cost'] == 50


def test_drops_report_rows_for_unknown_landings(monkeypatch):
    """Report row references a landing_id that's not in /landings (deleted
    landing). Must drop it — don't crash, don't bucket under None."""
    _patch_fetchers(monkeypatch,
        landings=[{'id': 'L1', 'url': 'https://known.com/lander/'}],
        report_rows=[
            {'landing_id': 'L1', 'cost': 10, 'revenue': 20, 'profit': 10,
             'clicks': 0, 'conversions': 0, 'lp_views': 0},
            {'landing_id': 'L_DELETED', 'cost': 999, 'revenue': 999,
             'profit': 0, 'clicks': 0, 'conversions': 0, 'lp_views': 0},
        ],
    )
    out = rt.get_domain_spend_revenue_30d()
    assert set(out.keys()) == {'known.com'}


def test_handles_missing_metric_fields(monkeypatch):
    """Some report rows may omit certain metrics (e.g. impressions=0 not
    set). Treat as 0, don't crash."""
    _patch_fetchers(monkeypatch,
        landings=[{'id': 'L1', 'url': 'https://example.com/lander/'}],
        report_rows=[{'landing_id': 'L1', 'cost': 10}],  # no other fields
    )
    out = rt.get_domain_spend_revenue_30d()
    assert out['example.com']['cost'] == 10
    assert out['example.com']['revenue'] == 0
    assert out['example.com']['conversions'] == 0


def test_returns_empty_on_transport_failure(monkeypatch):
    """Network failure → empty dict, not exception. Classifier falls back
    to 'no spend data → treat all as idle' but we should NEVER crash the
    cron over a transient API blip."""
    import requests as _requests
    monkeypatch.setattr(Config, 'REDTRACK_API_KEY', 'fake-key')

    def raise_for_anything():
        raise _requests.RequestException('boom')

    monkeypatch.setattr(rt, '_fetch_all_landings', raise_for_anything)
    out = rt.get_domain_spend_revenue_30d()
    assert out == {}


# ─── HTTP layer (params + auth shape) ──────────────────────────────────────

def test_http_layer_attaches_api_key_and_handles_array_response(monkeypatch):
    """_get must auto-attach api_key and unwrap both bare-array and
    {items:[…]} responses. Verifies the contract one of the bugs we
    might hit later relies on."""
    monkeypatch.setattr(Config, 'REDTRACK_API_KEY', 'KEY-XYZ')

    captured = {}

    class FakeResponse:
        def __init__(self, body):
            self._body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self._body

    def fake_get(url, params, timeout):
        captured['url'] = url
        captured['params'] = params
        # Bare-array response (like /landings)
        return FakeResponse([{'id': 'L1', 'url': 'https://x.com/'}])

    monkeypatch.setattr('redtrack_client.client.requests.get', fake_get)
    rows = rt._get('/landings', {'page': 1, 'per': 100})

    assert rows == [{'id': 'L1', 'url': 'https://x.com/'}]
    assert captured['params']['api_key'] == 'KEY-XYZ'
    assert captured['params']['page'] == 1
    assert captured['url'].endswith('/landings')


def test_http_layer_unwraps_items_response(monkeypatch):
    """{items:[…]} envelope (used by /report?total=true) is unwrapped
    transparently."""
    monkeypatch.setattr(Config, 'REDTRACK_API_KEY', 'KEY')

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {'items': [{'a': 1}, {'a': 2}], 'total': 2}

    monkeypatch.setattr(
        'redtrack_client.client.requests.get',
        lambda *a, **kw: FakeResponse(),
    )
    assert rt._get('/report', {}) == [{'a': 1}, {'a': 2}]


def test_http_layer_raises_on_inline_error_response(monkeypatch):
    """RedTrack sometimes returns 200 with {"error": "..."} body. Must
    surface as RequestException so callers see the same failure path
    as a transport error."""
    import requests as _requests
    monkeypatch.setattr(Config, 'REDTRACK_API_KEY', 'KEY')

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {'error': 'rate limit exceeded'}

    monkeypatch.setattr(
        'redtrack_client.client.requests.get',
        lambda *a, **kw: FakeResponse(),
    )
    with pytest.raises(_requests.RequestException, match='rate limit'):
        rt._get('/report', {})

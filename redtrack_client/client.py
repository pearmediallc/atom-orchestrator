"""RedTrack API client.

Single bulk fetcher: pull every landing + every per-landing 30-day stat,
join on landing_id in memory, sum by host. Returns a `{host: {…}}` dict
the lifecycle classifier looks each domain up against.

Why a JOIN: RedTrack's /report groups by `landing_id`, not by domain.
The `landing` text field on report rows is a free-text label set by the
MDB ("Yash-New-Test-2", "Google Ads - Neeraj - simplecarquote.info"),
so we can't parse a domain out of it. The actual landing URL lives on
the /landings record and is keyed by id.

Auth: ?api_key=… as a query param (NOT a header — header returns 401).
Rate limit on /report: 20 RPM (well under what one daily cron uses).

Stub fallback: when REDTRACK_API_KEY is unset, returns {} so every
domain looks idle. Tests / dev environments without creds behave
deterministically. Same pattern as namecheap_check.py / chatgpt.py.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

from config import Config

logger = logging.getLogger(__name__)


# /report sortable cap is 1000 per page; /landings doesn't doc its cap
# but accepts the same per/page params. 1000 is a safe page size.
_PAGE_SIZE = 1000

# Hard upper bound on pages we'll fetch — safety guard against an
# infinite-pagination bug ever hitting prod.
_MAX_PAGES = 50

# Cap an individual HTTP call. /report can be slow on big accounts.
_HTTP_TIMEOUT_SECONDS = 60


# ─── Public API ────────────────────────────────────────────────────────────

def get_domain_spend_revenue_30d() -> Dict[str, Dict[str, float]]:
    """Returns {host: {cost, revenue, profit, clicks, conversions, lp_views}}
    aggregated across all landings whose URL host matches.

    Hosts are lowercase, www. stripped. Multiple landings under the same
    host are summed. Empty dict on missing creds (stub fallback) or on
    transport failure (conservative — the classifier should treat
    "unknown" as idle, not as active).
    """
    if not _has_redtrack_creds():
        logger.info('Redtrack creds not configured — returning empty stub')
        return {}

    end = _dt.date.today()
    start = end - _dt.timedelta(days=30)

    try:
        landings = _fetch_all_landings()
        report_rows = _fetch_report_grouped_by_landing(start, end)
    except requests.RequestException as e:
        logger.warning('Redtrack fetch failed: %s — treating as no data', e)
        return {}

    return _join_and_aggregate(landings, report_rows)


def extract_host(url: Optional[str]) -> Optional[str]:
    """Pull the host from a landing URL, normalised for matching against
    our `domains.domain` column.

    Strips scheme, www. prefix, lowercases, drops port. Returns None for
    empty / unparseable / non-http(s) inputs so callers can skip cleanly.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https'):
        # Also tolerate scheme-less inputs by retrying with https://
        parsed = urlparse('https://' + url.strip())
    host = (parsed.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return host or None


# ─── Internals ─────────────────────────────────────────────────────────────

def _has_redtrack_creds() -> bool:
    return bool(Config.REDTRACK_API_KEY)


def _base_url() -> str:
    return Config.REDTRACK_BASE_URL.rstrip('/')


def _get(path: str, params: dict) -> List[dict]:
    """GET with api_key auto-attached. Returns the parsed JSON list, or
    raises requests.RequestException on transport failure.

    /report and /landings both return either a bare JSON array or
    {"items": [...]} depending on whether `total` was requested. We
    normalise to a list here.
    """
    full_params = dict(params)
    full_params['api_key'] = Config.REDTRACK_API_KEY
    r = requests.get(
        f'{_base_url()}{path}',
        params=full_params,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    r.raise_for_status()
    body = r.json()
    if isinstance(body, dict):
        # /report?total=true wraps in {"items": [...]}; /landings without
        # total returns a bare array. Some endpoints return {"error": ...}
        # with HTTP 200 too — surface that explicitly.
        if 'error' in body:
            raise requests.RequestException(
                f'RedTrack {path} error: {body["error"]}'
            )
        body = body.get('items', [])
    if not isinstance(body, list):
        raise requests.RequestException(
            f'RedTrack {path} returned unexpected shape: {type(body).__name__}'
        )
    return body


def _fetch_all_landings() -> List[dict]:
    """Page through /landings until we've seen everything. Each row is
    {id, url, domain_id, title, type, …} — we only use id + url."""
    out: List[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        rows = _get('/landings', {'page': page, 'per': _PAGE_SIZE})
        if not rows:
            break
        out.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
    else:
        logger.warning(
            'Hit _MAX_PAGES=%d on /landings — there may be more rows',
            _MAX_PAGES,
        )
    return out


def _fetch_report_grouped_by_landing(
    start: _dt.date, end: _dt.date,
) -> List[dict]:
    """One call to /report with group=landing covers every landing
    that had any traffic in the window. Landings with zero traffic are
    simply absent from the result — we'll treat those as 0 spend / 0
    revenue when aggregating.
    """
    out: List[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        rows = _get('/report', {
            'group': 'landing',
            'date_from': start.strftime('%Y-%m-%d'),
            'date_to':   end.strftime('%Y-%m-%d'),
            'page': page,
            'per': _PAGE_SIZE,
        })
        if not rows:
            break
        out.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
    else:
        logger.warning(
            'Hit _MAX_PAGES=%d on /report — there may be more landings',
            _MAX_PAGES,
        )
    return out


_METRIC_FIELDS = ('cost', 'revenue', 'profit', 'clicks',
                  'conversions', 'lp_views')


def _join_and_aggregate(
    landings: List[dict],
    report_rows: List[dict],
) -> Dict[str, Dict[str, float]]:
    """Map landing_id → host using /landings, then walk report_rows
    summing the numeric metric fields per host.

    Drops report rows whose landing_id doesn't appear in /landings
    (deleted landing, etc.) and rows whose host can't be extracted
    (empty url, tracker-only domain, etc.).
    """
    landing_id_to_host: Dict[str, str] = {}
    for L in landings:
        host = extract_host(L.get('url'))
        if host and L.get('id'):
            landing_id_to_host[L['id']] = host

    aggregated: Dict[str, Dict[str, float]] = {}
    for r in report_rows:
        host = landing_id_to_host.get(r.get('landing_id'))
        if not host:
            continue
        bucket = aggregated.setdefault(host, {f: 0.0 for f in _METRIC_FIELDS})
        for f in _METRIC_FIELDS:
            v = r.get(f)
            if isinstance(v, (int, float)):
                bucket[f] += float(v)
    return aggregated

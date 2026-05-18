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


def add_tracker_domain(url: str) -> Dict:
    """Register a new tracker domain with RedTrack via POST /domains.

    Returns the parsed response dict on success. Detects the "domain
    already registered" case (409 status OR error body containing
    'already') and returns it as `{'_already_exists': True, ...body}`
    so callers can treat it as a no-op success.

    Raises:
      RuntimeError when REDTRACK_API_KEY isn't configured. Callers should
        gate on a creds check before calling.
      requests.HTTPError on any other 4xx/5xx.
      requests.RequestException on transport failures.

    Body sent (minimal — RedTrack's swagger schema lists many more
    optional fields like acme/ssl/fallback_url, but for our v1 we only
    need url + type + workspace_ids + auto-SSL):

      {url: 'trk.neurobloomone.com',
       type: 'tracker',
       workspace_ids: ['6597822f9284e30001617c1c'],
       use_auto_generated_ssl: true}
    """
    if not _has_redtrack_creds():
        raise RuntimeError(
            'REDTRACK_API_KEY is not configured — cannot add tracker domain'
        )
    if not Config.REDTRACK_WORKSPACE_ID:
        raise RuntimeError(
            'REDTRACK_WORKSPACE_ID is not configured — cannot add tracker domain'
        )

    body = {
        'url': url,
        'type': 'tracker',
        'workspace_ids': [Config.REDTRACK_WORKSPACE_ID],
        'use_auto_generated_ssl': True,
    }
    r = requests.post(
        f'{_base_url()}/domains',
        params={'api_key': Config.REDTRACK_API_KEY},
        json=body,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )

    # Best-effort body parse so callers can read error details even on 4xx.
    try:
        resp_body = r.json() if r.content else {}
    except ValueError:
        resp_body = {'_raw_body': r.text[:500]}

    # RedTrack hasn't publicly documented the exact "already exists" shape,
    # so we recognise BOTH a 409 status AND a 200/4xx body whose error
    # field contains 'already'. Treat both as "this is fine, the domain
    # is already on RedTrack" — caller's idempotent re-run.
    if r.status_code == 409:
        return {'_already_exists': True, '_http_status': r.status_code,
                **(resp_body if isinstance(resp_body, dict) else {})}
    if isinstance(resp_body, dict):
        err_text = (resp_body.get('error') or resp_body.get('message') or '')
        if isinstance(err_text, str) and 'already' in err_text.lower():
            return {'_already_exists': True, '_http_status': r.status_code,
                    **resp_body}

    # On non-success, raise with RedTrack's response body included in the
    # exception message. requests.HTTPError's default str() doesn't carry
    # the body, so without this we get an opaque "400 Client Error" and
    # have to dig through Render logs to learn what RedTrack rejected.
    if not r.ok:
        body_sniff = (
            str(resp_body)[:400] if resp_body
            else (r.text[:400] if r.text else '(empty body)')
        )
        raise requests.HTTPError(
            f'{r.status_code} {r.reason} from RedTrack POST /domains. '
            f'Body: {body_sniff}',
            response=r,
        )

    if not isinstance(resp_body, dict):
        # Unexpected: RedTrack returned a 2xx with a non-dict body.
        raise requests.RequestException(
            f'RedTrack POST /domains returned unexpected shape: '
            f'{type(resp_body).__name__} (body sniff: {str(resp_body)[:200]!r})'
        )
    return resp_body


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

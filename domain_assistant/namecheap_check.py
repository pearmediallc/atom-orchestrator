"""Namecheap domain-availability + pricing.

Two endpoints we use:
  • `namecheap.domains.check`   — availability for a list of domains in one call.
  • `namecheap.users.getPricing` — registration price for each TLD.

Both go through Oxylabs HTTPS proxy because Namecheap whitelists ATOM's
proxy IP (the bot's laptop / Render IP isn't whitelisted). Mirrors ATOM's
NamecheapManager pattern.

Falls back to deterministic stubs when API keys / proxy aren't configured,
so local dev without secrets still works end-to-end.

Docs:
  https://www.namecheap.com/support/api/methods/domains/check/
  https://www.namecheap.com/support/api/methods/users/get-pricing/
"""
from typing import Dict, List, Optional
import logging
import threading
import time
import xml.etree.ElementTree as ET

import requests

from config import Config

logger = logging.getLogger(__name__)


# ─── In-process pricing cache ──────────────────────────────────────────────
# users.getPricing returns prices for ALL TLDs in one ~10KB response, so we
# cache it for the process lifetime (reset on bot restart). Refreshing more
# often than once per day is wasteful — Namecheap registration prices are
# stable for weeks.

_PRICE_CACHE: Dict[str, float] = {}
_PRICE_CACHE_LOCK = threading.Lock()
_PRICE_CACHE_FETCHED_AT: float = 0.0
_PRICE_CACHE_TTL_SECONDS = 24 * 3600  # 24h


def _has_namecheap_creds() -> bool:
    return bool(
        Config.NAMECHEAP_API_USER
        and Config.NAMECHEAP_API_KEY
        and Config.NAMECHEAP_CLIENT_IP
    )


def _request_namecheap(params: dict, *, timeout: int = 15) -> Optional[ET.Element]:
    """Call Namecheap with proxy if configured. Returns parsed XML root, or
    None on transport failure. Caller checks for `<Errors>` inside.

    `timeout` defaults to 15s (good for availability checks). Pass a
    longer value for the getPricing call which returns a multi-KB XML
    response and is slow through the Oxylabs proxy.
    """
    if not _has_namecheap_creds():
        logger.info('Namecheap creds not configured — skipping API call')
        return None

    base = {
        'ApiUser': Config.NAMECHEAP_API_USER,
        'ApiKey': Config.NAMECHEAP_API_KEY,
        'UserName': Config.NAMECHEAP_API_USER,
        'ClientIp': Config.NAMECHEAP_CLIENT_IP,
    }
    base.update(params)

    proxies = Config.get_proxy()
    try:
        r = requests.get(
            Config.NAMECHEAP_API_URL,
            params=base,
            timeout=timeout,
            proxies=proxies,
        )
        r.raise_for_status()
        return ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError) as e:
        logger.warning('Namecheap request failed: %s', e)
        return None


def _local_name(tag: str) -> str:
    """Strip XML namespace prefix. Namecheap responses use a default ns."""
    return tag.split('}', 1)[-1] if '}' in tag else tag


# ─── Pricing ───────────────────────────────────────────────────────────────

def _fetch_all_tld_prices() -> Dict[str, float]:
    """Fetch register prices for all TLDs (1-year duration). Returns
    {extension_with_dot: usd_price}, e.g. {'.com': 9.18, '.pro': 4.88, ...}.

    Returns empty dict if creds aren't configured or the call fails.
    """
    # users.getPricing returns prices for every TLD Namecheap sells in one
    # ~50KB XML response; through the Oxylabs proxy this often takes
    # 30-45 seconds. Result is cached for 24h so the slow path runs once.
    root = _request_namecheap({
        'Command': 'namecheap.users.getPricing',
        'ProductType': 'DOMAIN',
        'ProductCategory': 'REGISTER',
    }, timeout=90)
    if root is None:
        return {}

    # Namecheap's getPricing response includes MULTIPLE entries per TLD —
    # one for regular/renewal pricing (YourPriceType=MULTIPLE) and one for
    # the promotional first-year price (YourPriceType=ABSOLUTE). Both have
    # Duration=1 YEAR. Marketers buying a NEW domain pay the ABSOLUTE price,
    # not the MULTIPLE one. To match their actual cost, take the MIN of all
    # 1-year YourPrice entries per TLD — that's always either the promo
    # price (when one exists) or the regular price (when no promo). Either
    # way, it's "the cheapest this domain can be bought for right now."
    by_tld: Dict[str, List[float]] = {}
    for product in root.iter():
        if _local_name(product.tag) != 'Product':
            continue
        ext_name = product.get('Name')
        if not ext_name:
            continue
        for price_el in product:
            if _local_name(price_el.tag) != 'Price':
                continue
            if price_el.get('Duration') != '1':
                continue
            if price_el.get('DurationType') != 'YEAR':
                continue
            try:
                p = float(
                    price_el.get('YourPrice')
                    or price_el.get('Price')
                    or price_el.get('RegularPrice')
                    or '0'
                )
            except ValueError:
                continue
            if p > 0:
                by_tld.setdefault(f'.{ext_name.lower()}', []).append(p)

    return {tld: min(plist) for tld, plist in by_tld.items()}


def get_tld_prices(force_refresh: bool = False) -> Dict[str, float]:
    """Return cached TLD pricing, fetching from Namecheap on first call or
    after the cache expires. Thread-safe."""
    global _PRICE_CACHE_FETCHED_AT
    now = time.time()
    with _PRICE_CACHE_LOCK:
        cache_fresh = (now - _PRICE_CACHE_FETCHED_AT) < _PRICE_CACHE_TTL_SECONDS
        if _PRICE_CACHE and cache_fresh and not force_refresh:
            return dict(_PRICE_CACHE)
        prices = _fetch_all_tld_prices()
        if prices:
            _PRICE_CACHE.clear()
            _PRICE_CACHE.update(prices)
            _PRICE_CACHE_FETCHED_AT = now
        return dict(_PRICE_CACHE)


# ─── Availability ──────────────────────────────────────────────────────────

def check_availability(domains: List[str]) -> Dict[str, bool]:
    """Returns {domain: True_if_available, ...}. Stub fallback to
    all-available when creds aren't configured (preserves Phase 1 dev UX)."""
    if not domains:
        return {}
    if not _has_namecheap_creds():
        return {d: True for d in domains}

    # Namecheap accepts up to ~50 domains per call; chunk to be safe.
    out: Dict[str, bool] = {}
    chunk_size = 30
    for i in range(0, len(domains), chunk_size):
        chunk = domains[i:i + chunk_size]
        root = _request_namecheap({
            'Command': 'namecheap.domains.check',
            'DomainList': ','.join(chunk),
        })
        if root is None:
            # Transport failure — be conservative, mark all NOT available
            # so we never accidentally tell a marketer to buy a taken domain.
            for d in chunk:
                out[d] = False
            continue
        for el in root.iter():
            if _local_name(el.tag) != 'DomainCheckResult':
                continue
            d = el.get('Domain')
            if d:
                out[d] = (el.get('Available', 'false').lower() == 'true')
        for d in chunk:
            out.setdefault(d, False)
    return out


# ─── Combined availability + price (the API the workflow uses) ────────────

def check_availability_and_price(
    domains: List[str], extension: str
) -> List[dict]:
    """For each domain, return availability and the registration price for
    its extension.

        [{'domain': 'foo.com', 'available': True, 'price': 9.18}, ...]

    Behavior matrix:
      • Real creds set → real availability + real price from Namecheap
      • No creds (dev / tests) → stub: all-available, fake price set
        just below Config.price_cap_for(extension) so the workflow's
        price filter still lets stubbed candidates through
      • Real creds + Namecheap transport fails → all marked NOT available
        (conservative; we'd rather suggest nothing than suggest a
        domain we can't actually buy)
    """
    if not domains:
        return []

    ext = extension if extension.startswith('.') else f'.{extension}'

    if not _has_namecheap_creds():
        # Stub fallback — fake everything so dev / tests work without
        # real credentials. Price is just-under the cap so the workflow's
        # price filter doesn't reject stubbed candidates.
        from config import Config as _Cfg  # avoid circular at import time
        fake_price = max(0.01, _Cfg.price_cap_for(ext) - 0.01)
        return [
            {'domain': d, 'available': True, 'price': fake_price}
            for d in domains
        ]

    availability = check_availability(domains)
    prices = get_tld_prices()
    tld_price = prices.get(ext.lower())

    return [
        {
            'domain': d,
            'available': availability.get(d, False),
            'price': tld_price,
        }
        for d in domains
    ]

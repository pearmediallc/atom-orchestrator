"""Namecheap domain-availability check.

This is a DIFFERENT API endpoint than the DNS-management calls already in
ATOM. We're using `namecheap.domains.check`, which returns availability for
1+ domains in a single call.

Docs: https://www.namecheap.com/support/api/methods/domains/check/
"""
from typing import List, Dict
import requests
import xml.etree.ElementTree as ET
from config import Config


def check_availability(domains: List[str]) -> Dict[str, bool]:
    """Returns {domain: True_if_available, ...}.

    Phase 5 wiring. Phase 1 fallback returns all-available so upstream
    code can develop without hitting Namecheap.
    """
    if not (Config.NAMECHEAP_API_USER and Config.NAMECHEAP_API_KEY
            and Config.NAMECHEAP_CLIENT_IP):
        # Phase 1 fallback — pretend everything is available.
        return {d: True for d in domains}

    params = {
        'ApiUser': Config.NAMECHEAP_API_USER,
        'ApiKey': Config.NAMECHEAP_API_KEY,
        'UserName': Config.NAMECHEAP_API_USER,
        'ClientIp': Config.NAMECHEAP_CLIENT_IP,
        'Command': 'namecheap.domains.check',
        'DomainList': ','.join(domains),
    }
    r = requests.get(
        'https://api.namecheap.com/xml.response',
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out: Dict[str, bool] = {}
    for el in root.iter():
        # Namecheap responses are XML-namespaced; match on the local name.
        if el.tag.endswith('}DomainCheckResult') or el.tag == 'DomainCheckResult':
            domain = el.get('Domain')
            available = el.get('Available', 'false').lower() == 'true'
            if domain:
                out[domain] = available
    return out

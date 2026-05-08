"""Tests for namecheap_check.get_domain_info.

The bulk of namecheap_check is already covered by test_domain_assistant.py
(stub fallback + parser tests). These add coverage of the new
get_domain_info path: XML parsing for ExpiredDate, error-shape handling,
and the credentialless-stub case.
"""
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pytest

from config import Config
from domain_assistant import namecheap_check as nc


# Realistic-shape Namecheap getInfo XML response (default namespace
# stripped for test clarity — _local_name() handles namespaces in prod).
_OK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ApiResponse Status="OK">
  <CommandResponse>
    <DomainGetInfoResult Status="Ok" ID="123" DomainName="example.com">
      <DomainDetails>
        <CreatedDate>06/16/2009</CreatedDate>
        <ExpiredDate>06/16/2027</ExpiredDate>
        <NumYears>0</NumYears>
      </DomainDetails>
      <LockDetails />
    </DomainGetInfoResult>
  </CommandResponse>
</ApiResponse>"""

_NOT_FOUND_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ApiResponse Status="ERROR">
  <Errors>
    <Error Number="2019166">Domain not found</Error>
  </Errors>
</ApiResponse>"""


def _set_creds(monkeypatch):
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', 'user')
    monkeypatch.setattr(Config, 'NAMECHEAP_API_KEY', 'key')
    monkeypatch.setattr(Config, 'NAMECHEAP_CLIENT_IP', '1.2.3.4')


def _fake_request_returning(xml: str):
    """Build a stub _request_namecheap that returns the parsed XML root."""
    def _stub(params, *, timeout=15):
        return ET.fromstring(xml)
    return _stub


# ─── Stub fallback when no creds ──────────────────────────────────────────

def test_returns_none_when_no_creds(monkeypatch):
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', '')
    monkeypatch.setattr(Config, 'NAMECHEAP_API_KEY', '')
    assert nc.get_domain_info('example.com') is None


# ─── Happy path ───────────────────────────────────────────────────────────

def test_parses_expired_date_from_ok_response(monkeypatch):
    _set_creds(monkeypatch)
    monkeypatch.setattr(nc, '_request_namecheap',
                        _fake_request_returning(_OK_XML))

    result = nc.get_domain_info('example.com')

    assert result is not None
    assert result['expire_at'].year == 2027
    assert result['expire_at'].month == 6
    assert result['expire_at'].day == 16
    # Auto-renew not in getInfo — explicit None per Phase A scope.
    assert result['auto_renew_enabled'] is None


# ─── Error-shape handling ─────────────────────────────────────────────────

def test_returns_none_on_namecheap_error_response(monkeypatch):
    _set_creds(monkeypatch)
    monkeypatch.setattr(nc, '_request_namecheap',
                        _fake_request_returning(_NOT_FOUND_XML))

    # Error responses don't contain DomainDetails — get_domain_info logs
    # and returns None rather than crashing.
    assert nc.get_domain_info('not-our-domain.com') is None


def test_returns_none_on_transport_failure(monkeypatch):
    """When _request_namecheap returns None (network/proxy error),
    get_domain_info propagates that as None, not as an exception."""
    _set_creds(monkeypatch)
    monkeypatch.setattr(nc, '_request_namecheap',
                        lambda params, *, timeout=15: None)
    assert nc.get_domain_info('example.com') is None


# ─── Date parser ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,expected_y_m_d', [
    ('06/16/2027',          (2027, 6, 16)),
    (' 06/16/2027 ',        (2027, 6, 16)),       # whitespace tolerant
    ('2027-06-16',          (2027, 6, 16)),       # ISO fallback
    ('06/16/2027 12:34:56', (2027, 6, 16)),
])
def test_date_parser_accepts_known_formats(text, expected_y_m_d):
    out = nc._parse_namecheap_date(text)
    assert out is not None
    assert (out.year, out.month, out.day) == expected_y_m_d


@pytest.mark.parametrize('text', ['', None, 'garbage', '2027/06/16'])
def test_date_parser_returns_none_on_garbage(text):
    assert nc._parse_namecheap_date(text) is None

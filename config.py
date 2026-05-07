"""Centralised env loading. All other modules import settings from here."""
import json
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Phase 1 — required to boot
    FLASK_SECRET_KEY = os.getenv('FLASK_SECRET_KEY')
    PORT = int(os.getenv('PORT', '5600'))
    ATOM_BASE_URL = os.getenv('ATOM_BASE_URL', 'http://localhost:5500')

    # Credentials this orchestrator uses to log into ATOM.
    # Phase 6 TODO: replace with an ATOM service-account / API token
    # instead of borrowing a human user's password.
    ATOM_USERNAME = os.getenv('ATOM_USERNAME', 'sunny')
    ATOM_PASSWORD = os.getenv('ATOM_PASSWORD', 'test123')

    # Phase 2 — Slack
    SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
    SLACK_SIGNING_SECRET = os.getenv('SLACK_SIGNING_SECRET')
    SLACK_APP_TOKEN = os.getenv('SLACK_APP_TOKEN')

    # Phase 3 — inventory
    # SQLite is the default for local dev / tests (zero setup, one file).
    INVENTORY_DB_PATH = os.getenv('INVENTORY_DB_PATH', './inventory.db')

    # Production override: when set to a Postgres connection string
    # (postgres://… or postgresql://…), inventory.store uses Postgres
    # instead of SQLite. Required for Render deploy because Render's
    # free tier has no persistent disk for SQLite.
    DATABASE_URL = (os.getenv('DATABASE_URL', '') or '').strip()

    # Phase 4/5 — LLM provider (OpenAI-compatible API: works with OpenAI,
    # Grok/xAI, any other provider that exposes the OpenAI Chat Completions
    # spec). To use Grok instead of OpenAI:
    #   OPENAI_BASE_URL=https://api.x.ai/v1
    #   OPENAI_API_KEY=xai-...
    #   OPENAI_MODEL=grok-2-1212    (or whichever Grok model is current)
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', '').strip() or None
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')

    # Phase 4/8 — Namecheap availability + pricing
    # Same credentials ATOM uses (aws_automation/.env). The bot calls Namecheap
    # directly for /new-domain availability + price filtering rather than
    # routing through ATOM, to keep ATOM's API surface small.
    NAMECHEAP_API_USER = os.getenv('NAMECHEAP_API_USER', '').strip()
    NAMECHEAP_API_KEY = os.getenv('NAMECHEAP_API_KEY', '').strip()
    NAMECHEAP_CLIENT_IP = os.getenv('NAMECHEAP_CLIENT_IP', '').strip()
    NAMECHEAP_API_URL = os.getenv(
        'NAMECHEAP_API_URL', 'https://api.namecheap.com/xml.response'
    ).strip()

    # Oxylabs HTTPS proxy — Namecheap whitelists this proxy's IP, so all
    # Namecheap API calls must go through it. Same creds as ATOM uses.
    PROXY_USERNAME = os.getenv('PROXY_USERNAME', '').strip()
    PROXY_PASSWORD = os.getenv('PROXY_PASSWORD', '').strip()
    PROXY_HOST = os.getenv('PROXY_HOST', 'ddc.oxylabs.io').strip()
    PROXY_PORT = os.getenv('PROXY_PORT', '8001').strip()

    @classmethod
    def get_proxy(cls) -> Optional[dict]:
        """Returns a requests-compatible proxy dict, or None if proxy creds
        aren't configured. Mirrors ATOM's Config.get_proxy() pattern."""
        if not (cls.PROXY_USERNAME and cls.PROXY_PASSWORD):
            return None
        url = (
            f'https://{cls.PROXY_USERNAME}:{cls.PROXY_PASSWORD}@'
            f'{cls.PROXY_HOST}:{cls.PROXY_PORT}'
        )
        return {'http': url, 'https': url}

    # Per-extension price cap for /new-domain suggestions. The bot only
    # suggests domains whose Namecheap price is at-or-below this cap.
    # Override per-vertical or per-extension via env var if needed; for now
    # hard-coded to TL's spec on 2026-05-05.
    DOMAIN_PRICE_CAP_USD = {
        '.com': 15.00,    # .com under $15
        '_default': 5.00,  # everything else under or equal to $5
    }

    @classmethod
    def price_cap_for(cls, extension: str) -> float:
        ext = extension if extension.startswith('.') else f'.{extension}'
        return cls.DOMAIN_PRICE_CAP_USD.get(ext, cls.DOMAIN_PRICE_CAP_USD['_default'])

    # Phase 5 — approvers
    APPROVER_SLACK_USER_IDS = [
        uid.strip()
        for uid in os.getenv('APPROVER_SLACK_USER_IDS', '').split(',')
        if uid.strip()
    ]

    # Slack user-ID of the person who buys domains on Namecheap (per TL: manual
    # purchase via Utkarsh, not automated). Bot DMs this user when an MDB picks
    # a suggested domain. Falls back to DMing the requester themselves so the
    # flow is self-testable without a second Slack user.
    UTKARSH_SLACK_USER_ID = os.getenv('UTKARSH_SLACK_USER_ID', '').strip()

    # Dev override — when set to a Slack user ID, every TL approval card,
    # Utkarsh purchase/deploy DM, and worker progress message gets rerouted
    # to this single user instead of the real recipients. Lets a solo dev
    # walk the whole flow alone without spamming TL/Utkarsh in production.
    # Empty in real use.
    DEV_REROUTE_DMS_TO = os.getenv('DEV_REROUTE_DMS_TO', '').strip()

    # Identifies the deployment environment. Drives the production
    # safeguards below — set to 'production' on the live Render
    # service so misconfiguration (e.g. forgetting to clear
    # DEV_REROUTE_DMS_TO after a solo test) refuses to boot instead
    # of silently absorbing real users' approvals (audit #15).
    ENVIRONMENT = os.getenv('ENVIRONMENT', '').strip().lower()

    @classmethod
    def assert_production_safe(cls) -> None:
        """Refuse to boot with dev-only knobs left on in production.

        Called once from create_app() at startup. Any failure here is a
        fatal misconfiguration — better to die loudly than serve traffic
        with TL approvals routed to one developer.
        """
        if cls.ENVIRONMENT != 'production':
            return
        if cls.DEV_REROUTE_DMS_TO:
            raise RuntimeError(
                'DEV_REROUTE_DMS_TO is set in a production deployment. '
                'This redirects TL approvals + Utkarsh DMs to a single '
                'user, which would silently break the workflow for real '
                'users. Unset DEV_REROUTE_DMS_TO in the production '
                'environment, then redeploy.'
            )

    @classmethod
    def route_recipient(cls, real_recipient: str) -> str:
        """Return DEV_REROUTE_DMS_TO if set, else the real recipient unchanged.

        Use this everywhere we'd send a DM to a TL/approver/Utkarsh — never
        for the requester themselves (their own DMs should always reach
        them, not get hijacked by the dev override).
        """
        return cls.DEV_REROUTE_DMS_TO or real_recipient

    # ─── Phase 7 — Mark Done click triggers ATOM ───────────────
    # Master switch. When False, Mark Purchased/Deployed clicks behave like
    # Phase 2.8 — update inventory only, no ATOM trigger. Lets you ship the
    # bot without breaking the demo if ATOM is unreachable or AWS is broken.
    ENABLE_PHASE_7 = os.getenv('ENABLE_PHASE_7', 'false').lower() in ('1', 'true', 'yes', 'on')

    # How long to wait for ATOM's setup_domain task to reach a terminal
    # state (completed | failed). Default is 30 minutes — enough for a
    # FRESH domain to go through ACM cert validation (the slow step,
    # 5–30 min depending on DNS propagation) plus CloudFront build.
    # Path A (idempotent reuse on already-set-up domains) typically
    # finishes in <30 sec, so the timeout is a safety ceiling, not a
    # baseline. The previous hardcoded 600s was too short for fresh
    # domains and caused false "did not complete in time" failures
    # while ATOM was still legitimately working (2026-05-08 audit).
    PHASE7_SETUP_TIMEOUT_SEC = int(os.getenv('PHASE7_SETUP_TIMEOUT_SEC', '1800'))

    # Per-vertical defaults that tell run_existing_domain_workflow which
    # source bucket / folder to copy lander files from. The Slack flow only
    # collects a lander URL, not bucket+folder, so these defaults fill the
    # gap. Override per-vertical via PHASE7_LANDER_DEFAULTS_JSON, e.g.:
    #   PHASE7_LANDER_DEFAULTS_JSON='{"auto-insurance": {"source_account": "auto-insurance", "source_bucket": "pearmedia-default-lander-auto", "source_folders": ["lander/"]}}'
    PHASE7_LANDER_DEFAULTS = json.loads(os.getenv('PHASE7_LANDER_DEFAULTS_JSON', '') or '{}')

    # Single global fallback used when a vertical isn't in the JSON map above.
    PHASE7_DEFAULT_SOURCE_ACCOUNT = os.getenv('PHASE7_DEFAULT_SOURCE_ACCOUNT', 'auto-insurance')
    PHASE7_DEFAULT_SOURCE_BUCKET = os.getenv('PHASE7_DEFAULT_SOURCE_BUCKET', '').strip()
    PHASE7_DEFAULT_SOURCE_FOLDERS = [
        f.strip() for f in os.getenv('PHASE7_DEFAULT_SOURCE_FOLDERS', '').split(',') if f.strip()
    ]

    @classmethod
    def phase7_defaults_for(cls, vertical: str) -> dict:
        """Resolve source-* defaults for a given vertical, falling back to global."""
        by_vert = cls.PHASE7_LANDER_DEFAULTS.get(vertical or '', {}) or {}
        return {
            'source_account': by_vert.get('source_account') or cls.PHASE7_DEFAULT_SOURCE_ACCOUNT,
            'source_bucket': by_vert.get('source_bucket') or cls.PHASE7_DEFAULT_SOURCE_BUCKET,
            'source_folders': by_vert.get('source_folders') or cls.PHASE7_DEFAULT_SOURCE_FOLDERS,
            'source_files': by_vert.get('source_files') or [],
        }

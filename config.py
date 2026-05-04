"""Centralised env loading. All other modules import settings from here."""
import os
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
    INVENTORY_DB_PATH = os.getenv('INVENTORY_DB_PATH', './inventory.db')

    # Phase 4/5 — LLM provider (OpenAI-compatible API: works with OpenAI,
    # Grok/xAI, any other provider that exposes the OpenAI Chat Completions
    # spec). To use Grok instead of OpenAI:
    #   OPENAI_BASE_URL=https://api.x.ai/v1
    #   OPENAI_API_KEY=xai-...
    #   OPENAI_MODEL=grok-2-1212    (or whichever Grok model is current)
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', '').strip() or None
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')

    # Phase 4 — Namecheap availability
    NAMECHEAP_API_USER = os.getenv('NAMECHEAP_API_USER')
    NAMECHEAP_API_KEY = os.getenv('NAMECHEAP_API_KEY')
    NAMECHEAP_CLIENT_IP = os.getenv('NAMECHEAP_CLIENT_IP')

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

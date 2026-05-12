"""Daily Slack workspace sync — pulls users.list, UPSERTs into slack_users.

Why: Slack workspace is the source of truth for "who is on the team
and what's their Slack ID". The bot needs to resolve legacy free-text
MDB names (from CSV imports) into stable Slack IDs to DM the right
person. We cache the workspace locally so day-to-day classifier runs
do fast DB lookups instead of one HTTP call per match.

Self-healing — every cron run:
  • New hires automatically show up
  • Renamed people get their new real_name in our cache
  • People who leave get flagged deleted=true (we don't delete the row
    because old domain_assignments still reference them — the audit
    trail stays valid).

Wired in as the 4th pass of `python -m lifecycle`. Honours
LIFECYCLE_DRY_RUN by always running (the sync is read-only on Slack
and the writes are to our DB only — there's no "spam users" risk to
gate).
"""
from __future__ import annotations

import logging
from typing import Dict, Set

from config import Config
from inventory import store

logger = logging.getLogger(__name__)


# Internal Slack bot accounts to skip even though they're not flagged
# as is_bot=True. Slackbot itself is a deactivated bot ID.
_NON_HUMAN_USER_IDS = {'USLACKBOT'}


def run_slack_users_sync(slack_client=None) -> Dict[str, int]:
    """Pull every active workspace member from Slack, UPSERT into
    slack_users, and mark anyone we previously cached but no longer
    see as deleted.

    Returns counters: {'fetched', 'upserted', 'newly_deleted', 'errors'}.
    """
    counters = {'fetched': 0, 'upserted': 0, 'newly_deleted': 0, 'errors': 0}

    if not Config.SLACK_BOT_TOKEN:
        logger.info('slack sync skipped — SLACK_BOT_TOKEN not configured')
        return counters

    if slack_client is None:
        slack_client = _make_slack_client()

    try:
        members = _fetch_all_workspace_members(slack_client)
    except Exception:
        logger.exception('slack users.list call failed')
        counters['errors'] = 1
        return counters

    counters['fetched'] = len(members)
    logger.info('slack sync: fetched %d active members', len(members))

    # 1. UPSERT every active member.
    seen_ids: Set[str] = set()
    for m in members:
        uid = m['id']
        seen_ids.add(uid)
        try:
            store.upsert_slack_user(
                slack_user_id=uid,
                real_name=m.get('real_name') or m.get('profile', {}).get('real_name'),
                display_name=m.get('profile', {}).get('display_name') or m.get('name'),
                email=m.get('profile', {}).get('email'),
                deleted=False,
            )
            counters['upserted'] += 1
        except Exception:
            logger.exception('upsert failed for slack user %s', uid)
            counters['errors'] += 1

    # 2. Mark previously-cached users who are no longer in the workspace
    #    as deleted. They might have been disabled, removed, or have left.
    #    The row stays for audit; future DM attempts route to TL.
    try:
        cached = store.list_slack_users(include_deleted=False)
    except Exception:
        logger.exception('failed to read existing slack_users')
        return counters

    for u in cached:
        if u['slack_user_id'] not in seen_ids:
            try:
                store.mark_slack_user_deleted(u['slack_user_id'])
                counters['newly_deleted'] += 1
                logger.info(
                    'slack sync: marked %s (%s) deleted — no longer in workspace',
                    u['slack_user_id'], u.get('real_name'),
                )
            except Exception:
                logger.exception(
                    'failed to mark slack user %s deleted', u['slack_user_id'],
                )
                counters['errors'] += 1

    logger.info('slack sync finished — %s', counters)
    return counters


# ─── Internals ────────────────────────────────────────────────────────────

def _make_slack_client():
    from slack_sdk import WebClient
    return WebClient(token=Config.SLACK_BOT_TOKEN)


def _fetch_all_workspace_members(client) -> list:
    """Paginated users.list. Returns active humans only — drops bots,
    deactivated users (Slack returns those too), and Slackbot itself."""
    members = []
    cursor = None
    while True:
        resp = client.users_list(cursor=cursor, limit=200)
        members.extend(resp.get('members', []))
        cursor = (resp.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break

    active = []
    for m in members:
        if m.get('deleted'):
            continue
        if m.get('is_bot'):
            continue
        if m.get('id') in _NON_HUMAN_USER_IDS:
            continue
        active.append(m)
    return active

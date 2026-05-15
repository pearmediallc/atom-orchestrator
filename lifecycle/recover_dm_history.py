"""One-shot recovery — reconstruct the prompt fan-out ledger from the
bot's own DM history.

Why this exists (2026-05-15 incident): the first manually-triggered live
cron actually sent its DMs successfully, but a `isinstance(resp, dict)`
check on the SlackResponse return value (SlackResponse is not a dict
subclass — it just has .get()) treated every send as failed. So:

  • real DMs landed in MDBs' inboxes — Anusree complained
  • the bot's DB shows 0 ledger rows, 0 state changes, 0 events
  • re-triggering the cron WOULD re-DM everyone about the same domains
    because the DB has no record of who already got what

Slack still has the messages the bot sent. This script asks Slack:
"for every workspace member's DM channel with the bot, show me what
the bot posted in the last N hours" — then parses the domain name out
of each message ("Heads up — `xyz.com` had no spend..." / "`xyz.com`
expires in...") and writes:

  • domain_prompt_recipients rows (so sibling-sync still works if
    someone clicks)
  • lifecycle_state = AWAITING_MDB_INVENTORY_RESPONSE (idle) or
    AWAITING_MDB_USAGE_RESPONSE (expiring) — the classifier will then
    skip these on the next cron, preventing duplicate prompts
  • last_prompted_at = now (belt + braces dedup)
  • a 'recovered_from_dm_history' audit event

Requires the bot's `im:history` scope (it almost certainly has it
since it DMs users; if missing, Slack returns missing_scope and the
script prints a clear add-this-scope message).

Usage:
    python -m lifecycle.recover_dm_history            # dry run — count only
    python -m lifecycle.recover_dm_history --apply    # writes the ledger
    python -m lifecycle.recover_dm_history --hours 6  # look back 6 hours
                                                       (default 6, covers the
                                                       killed run easily)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import re
import sys
import time

from config import Config
from inventory import store
from lifecycle import dm as _dm
from lifecycle import states as S

logger = logging.getLogger(__name__)

# Each prompt message embeds the domain as `xyz.com`. The leading
# backtick + a TLD-suffix anchor keeps us off random backticked phrases.
_DOMAIN_RE = re.compile(r'`([a-z0-9][a-z0-9-]*\.[a-z]{2,})`')
# Phrases that uniquely mark an IDLE prompt (TL Flow 2). The first
# matches the fallback text; the second matches the block-kit card
# itself, since Slack sometimes returns bot messages with the text
# field stripped when blocks are present.
_IDLE_FALLBACK_RE = re.compile(r'had no spend in the last 30 days',
                               re.IGNORECASE)
_IDLE_BLOCK_RE = re.compile(r'has gone quiet', re.IGNORECASE)
# Phrase that uniquely marks an EXPIRING prompt (TL Flow 1) — matches
# both the fallback text and the block card.
_EXPIRING_RE = re.compile(r'expires in ~\d+ day', re.IGNORECASE)


def _make_client():
    from slack_sdk import WebClient  # local import — module is optional
    return WebClient(token=Config.SLACK_BOT_TOKEN)


def _parse_msg(msg: dict):
    """Return (domain, mode) for a bot prompt message, or None if the
    message isn't a lifecycle prompt."""
    text = msg.get('text') or ''
    # Slack may push the prompt content into blocks instead of text.
    # Concatenate the text from every section block so we can match.
    for b in (msg.get('blocks') or []):
        if b.get('type') == 'section':
            inner = (b.get('text') or {}).get('text') or ''
            text = text + '\n' + inner

    m = _DOMAIN_RE.search(text)
    if not m:
        return None
    domain = m.group(1).lower()

    if _IDLE_FALLBACK_RE.search(text) or _IDLE_BLOCK_RE.search(text):
        return (domain, 'idle')
    if _EXPIRING_RE.search(text):
        return (domain, 'expiring')
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--apply', action='store_true',
                        help='Actually write the ledger + advance state. '
                             'Default is dry run.')
    parser.add_argument('--hours', type=int, default=6,
                        help='How far back to look in DM history (default 6).')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-7s %(message)s',
    )

    if not Config.SLACK_BOT_TOKEN:
        sys.exit('SLACK_BOT_TOKEN not configured.')

    store.init_db()
    client = _make_client()

    # Bot's own Slack user id — we filter history to messages we sent.
    try:
        auth = client.auth_test()
    except Exception as e:
        sys.exit(f'auth_test failed: {e}')
    bot_user_id = auth.get('user_id')
    print(f'bot user id: {bot_user_id}')

    # Recipients = every active slack_users row + TL (in case TL isn't
    # mirrored into slack_users for some reason).
    users = store.list_slack_users(include_deleted=False)
    recipient_ids = {u['slack_user_id'] for u in users}
    tl_norm = _dm.normalise_slack_id(Config.TL_SLACK_USER_ID)
    if tl_norm:
        recipient_ids.add(tl_norm)
    print(f'recipients to scan: {len(recipient_ids)}')

    since_ts = (_dt.datetime.now() - _dt.timedelta(hours=args.hours)).timestamp()
    print(f'looking back {args.hours} hours (since unix={since_ts:.0f})')

    found: dict = {}  # (recipient_uid, domain) -> {channel, ts, mode}
    api_calls = 0

    for uid in sorted(recipient_ids):
        # Open the DM channel — Slack returns the channel id even if no
        # messages are in it (cheap, harmless).
        try:
            dm_open = client.conversations_open(users=uid)
            api_calls += 1
            channel = dm_open['channel']['id']
        except Exception as e:
            err = getattr(getattr(e, 'response', None), 'get',
                          lambda *_: None)('error')
            if err == 'missing_scope':
                sys.exit(
                    'Bot missing `im:write` scope — add it in the Slack '
                    'app config (Features → OAuth & Permissions), then '
                    're-install the app and re-run.'
                )
            print(f'  open DM with {uid} failed: {err or e}')
            continue

        try:
            hist = client.conversations_history(
                channel=channel, oldest=str(since_ts), limit=200,
            )
            api_calls += 1
        except Exception as e:
            err = getattr(getattr(e, 'response', None), 'get',
                          lambda *_: None)('error')
            if err == 'missing_scope':
                sys.exit(
                    'Bot missing `im:history` scope — add it in the '
                    'Slack app config (Features → OAuth & Permissions), '
                    'then re-install the app and re-run.'
                )
            print(f'  history for {uid} failed: {err or e}')
            continue

        for msg in (hist.get('messages') or []):
            # Only messages the BOT sent.
            sender = msg.get('user') or msg.get('bot_id')
            if msg.get('user') != bot_user_id and not msg.get('bot_id'):
                continue
            parsed = _parse_msg(msg)
            if not parsed:
                continue
            domain, mode = parsed
            found[(uid, domain)] = {
                'channel_id': channel,
                'message_ts': msg['ts'],
                'mode': mode,
            }

        # Pace under conversations.history Tier 3 (~50/min). 1.2s per
        # recipient = ~50/min for the open+history pair averaged.
        time.sleep(0.6)

    domains = {d for (_, d) in found.keys()}
    print(f'\nfound {len(found)} (recipient, domain) pairs across '
          f'{len(domains)} distinct domains')
    print(f'  api calls: {api_calls}')

    if not domains:
        print('\nnothing to recover.')
        return 0

    # Group by domain so we can write one ledger batch + one state move
    # per domain.
    by_domain: dict = {}
    for (uid, domain), meta in found.items():
        by_domain.setdefault(domain, []).append({
            'recipient_slack_id': uid,
            'channel_id': meta['channel_id'],
            'message_ts': meta['message_ts'],
            'is_tl': (uid == tl_norm),
            'mode': meta['mode'],
        })

    print('\nsample of recovered domains (first 10):')
    for i, (domain, recs) in enumerate(list(by_domain.items())[:10]):
        modes = ','.join(sorted({r['mode'] for r in recs}))
        print(f'  {domain:42s}  recipients={len(recs)}  mode={modes}')

    if not args.apply:
        print(f'\nDRY RUN — would write the ledger + advance state for '
              f'{len(by_domain)} domains. Re-run with --apply to commit.')
        return 0

    done = 0
    for domain, recipients in by_domain.items():
        try:
            store.record_prompt_recipients(
                domain,
                [{k: v for k, v in r.items() if k != 'mode'}
                 for r in recipients],
            )
            # State is determined by the dominant mode across recipients
            # for this domain. In practice all are the same mode (one
            # cron run only sends one type per domain) but be defensive.
            modes = [r['mode'] for r in recipients]
            mode = max(set(modes), key=modes.count)
            target_state = (
                S.AWAITING_MDB_INVENTORY_RESPONSE if mode == 'idle'
                else S.AWAITING_MDB_USAGE_RESPONSE
            )
            store.set_lifecycle_state(domain, target_state)
            store.bump_last_prompted_at(domain)
            store.record_event(
                domain, 'recovered_from_dm_history',
                actor='dm_history_recovery_2026_05_15',
                from_state=None, to_state=target_state,
                metadata={
                    'recipients': [r['recipient_slack_id'] for r in recipients],
                    'mode': mode,
                    'reason': ('isinstance(resp, dict) bug — DMs landed '
                               'but ledger never wrote; reconstructed '
                               'from bot DM history'),
                },
            )
            done += 1
        except Exception:
            logger.exception('failed to recover %s', domain)

    print(f'\nrecovered {done} domains — they are now in AWAITING_* state. '
          'The classifier will skip them on the next cron, so re-running '
          'will NOT re-DM Anusree (or anyone else who already got a DM).')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Slack button handlers for the lifecycle bot.

Plugs into the existing bolt app via register(app). slack_bot.routes
calls this once at boot — keeping the handlers in their own module so
slack_bot/routes.py doesn't grow another 500 lines.

Handlers:
  • MDB clicks on the EXPIRING DM:
      lifecycle_using_yes   → DM Utkarsh "renew"          → AWAITING_UTKARSH_RENEW
      lifecycle_using_no    → DM Utkarsh "disable autorenew" → AWAITING_UTKARSH_DISABLE_RENEW
                                (also: contradiction guard — if last 7d
                                 spend > threshold, escalate to TL instead)
  • Utkarsh clicks on his DM:
      lifecycle_renewed                → re-sync expiry, DM MDB+TL,  RENEWED
      lifecycle_disable_renew_done     → DM MDB+TL,  state cleared (will re-classify)
  • MDB clicks on the IDLE DM:
      lifecycle_keep_30                → EXTENDED_30, snooze, DM TL FYI
      lifecycle_keep_15                → EXTENDED_15, snooze, DM TL FYI
      lifecycle_push_inventory         → assigned_to=NULL, INVENTORY,
                                          DM TL FYI
  • TL escalation buttons (Phase B-next: 48h SLA escalator builds these
    cards). Defined here so the action_ids exist:
      lifecycle_tl_force_renew         → DM Utkarsh renew
      lifecycle_tl_force_disable_renew → DM Utkarsh disable autorenew
      lifecycle_tl_force_push          → push to inventory
      lifecycle_tl_force_keep_30       → snooze 30 days

Authz: every handler checks body['user']['id'] against the expected
actor for that button. Wrong-user clicks get an ephemeral "this isn't
for you" reply rather than silently working.

DRY_RUN: all DMs go through lifecycle.dm.dm(), so flipping
LIFECYCLE_DRY_RUN=true neuters this whole flow without code changes.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from config import Config
from inventory import store
from lifecycle import dm as _dm, states as S

logger = logging.getLogger(__name__)


# ─── Authz helpers ────────────────────────────────────────────────────────

def _payload(body) -> Optional[dict]:
    """Pull the JSON payload out of a Slack action body. Returns None on
    malformed input — handler then bails silently."""
    try:
        return json.loads(body['actions'][0]['value'])
    except (KeyError, json.JSONDecodeError, IndexError, TypeError):
        return None


def _enforce(actor_id: str, allowed_id: Optional[str],
             ack, *, role: str) -> bool:
    """True if `actor_id` is allowed to click this button.

    Honours DEV_REROUTE_DMS_TO — if the dev override is on, the dev's
    own user ID counts as the allowed actor too (so you can solo-test
    every button without 5 fake accounts).

    `role` is just a label for the "not for you" reply.
    """
    if not allowed_id:
        return True   # no expected actor configured → don't gate
    expected = _dm.normalise_slack_id(allowed_id)
    if actor_id == expected:
        return True
    if Config.DEV_REROUTE_DMS_TO and actor_id == Config.DEV_REROUTE_DMS_TO:
        return True
    ack({
        'response_action': 'errors',
        'errors': {},
    })
    logger.info(
        'authz reject: user=%s tried %s but expected=%s',
        actor_id, role, expected,
    )
    return False


def _ephemeral_not_for_you(client, channel: str, user: str, role: str):
    try:
        client.chat_postEphemeral(
            channel=channel, user=user,
            text=f':no_entry: This {role} button is not for you.',
        )
    except Exception:
        logger.exception('chat_postEphemeral failed in authz reject')


def _replace_card(client, body, header_text: str, context_text: str):
    """Update the original message so the buttons disappear and the
    state of the click is visible to anyone scrolling back."""
    try:
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            text=header_text,
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text', 'text': header_text,
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn', 'text': context_text,
                }]},
            ],
        )
    except Exception:
        logger.exception('chat_update failed (non-fatal)')


# ─── Contradiction guard ──────────────────────────────────────────────────

def _recent_spend(domain: str) -> float:
    """Last-7d cost for the contradiction guard. Best-effort: if
    redtrack is down, return 0 so we don't false-positive an escalation.
    Pulls live data — not cached — because the user's just-clicked
    decision deserves the freshest signal we can get."""
    try:
        from redtrack_client import client as rt
        # Quick re-use: the existing 30d fetcher gives us cost, but
        # we want last-7d here. Until we add a 7-day variant, use the
        # 30d data as a coarse proxy — if 30d cost > threshold the
        # domain has been spending at SOME point in the last month.
        # TODO(phase-c): swap for a proper 7d fetcher.
        data = rt.get_domain_spend_revenue_30d().get(domain.lower(), {})
        return float(data.get('cost') or 0)
    except Exception:
        logger.exception('contradiction-guard spend lookup failed')
        return 0.0


# ─── Phase F — atomic first-click-wins + sibling-card sync ────────────────

# Events that mark a prompt as resolved. Used to name the winner when a
# later click loses the race.
_RESOLVING_EVENTS = frozenset({
    'mdb_said_using_yes', 'mdb_said_using_no',
    'mdb_extended_30', 'mdb_extended_15', 'pushed_to_inventory',
})


def _last_resolver(domain: str) -> Optional[str]:
    """Slack ID of whoever resolved this domain's last prompt — read off
    the most recent resolving domain_event. None if not found."""
    try:
        events = store.list_domain_events(domain, limit=15)  # newest first
    except Exception:
        logger.exception('list_domain_events failed in _last_resolver')
        return None
    for ev in events:
        if ev.get('event_type') in _RESOLVING_EVENTS and ev.get('actor'):
            return ev['actor']
    return None


def _already_handled(client, body, domain: str) -> None:
    """The click lost the race — someone else already resolved this
    prompt. Name the winner, replace the loser's own card so it stops
    showing buttons, and send them an ephemeral."""
    winner = _last_resolver(domain)
    who = f'<@{winner}>' if winner else 'someone else'
    _replace_card(
        client, body,
        header_text=f':information_source: {domain} — already handled',
        context_text=(f'{who} already actioned this. Your click was ignored '
                      'to avoid conflicting decisions.'),
    )
    try:
        client.chat_postEphemeral(
            channel=body['channel']['id'], user=body['user']['id'],
            text=(f':information_source: `{domain}` was already actioned by '
                  f'{who}. Your click was ignored.'),
        )
    except Exception:
        logger.exception('chat_postEphemeral failed in race-guard')


def _claim(client, body, domain: str, from_state: str, to_state: str) -> bool:
    """Atomic first-click-wins. Moves the domain from_state -> to_state,
    but ONLY if it's still in from_state — the DB decides the winner, so
    two near-simultaneous clicks can't both proceed. Returns True if this
    click won (caller does the work); False if it lost (loser's card +
    ephemeral already handled, caller should return)."""
    if store.transition_lifecycle_state(domain, from_state, to_state):
        return True
    _already_handled(client, body, domain)
    return False


def _sync_siblings(client, body, domain: str,
                   header_text: str, sibling_context: str) -> None:
    """Phase F — after a resolution, update every OTHER recipient's card
    (the fan-out siblings) so nobody else sees stale buttons. The
    clicker's own card is handled separately by _replace_card.

    Best-effort per card: a deleted message or API blip on one sibling
    must not block the rest. Clears the fan-out ledger when done.
    """
    clicker_channel = (body.get('channel') or {}).get('id')
    clicker_ts = (body.get('message') or {}).get('ts')
    try:
        recipients = store.get_prompt_recipients(domain)
    except Exception:
        logger.exception('get_prompt_recipients failed for %s', domain)
        recipients = []
    for r in recipients:
        ch, ts = r.get('channel_id'), r.get('message_ts')
        if not ch or not ts:
            continue
        if ch == clicker_channel and ts == clicker_ts:
            continue  # the clicker's own card — _replace_card already did it
        try:
            client.chat_update(
                channel=ch, ts=ts, text=header_text,
                blocks=[
                    {'type': 'header', 'text': {
                        'type': 'plain_text', 'text': header_text}},
                    {'type': 'context', 'elements': [{
                        'type': 'mrkdwn', 'text': sibling_context}]},
                ],
            )
        except Exception:
            logger.exception(
                'sibling chat_update failed (non-fatal) — channel=%s ts=%s',
                ch, ts,
            )
    try:
        store.clear_prompt_recipients(domain)
    except Exception:
        logger.exception('clear_prompt_recipients failed for %s', domain)


# ─── Handler registration ─────────────────────────────────────────────────

def register(app) -> None:
    """Wire all lifecycle handlers onto the given bolt App."""

    # ─ MDB on EXPIRING card ──────────────────────────────────────────────

    @app.action('lifecycle_using_yes')
    def using_yes(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        domain = data['domain']
        assigned = data.get('assigned_to') or ''
        actor = body['user']['id']

        if not _enforce(actor, assigned, ack, role='MDB usage'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor,
                                   'MDB usage')
            return

        # Atomic first-click-wins across every fan-out recipient (MDBs+TL).
        if not _claim(client, body, domain,
                      S.AWAITING_MDB_USAGE_RESPONSE, S.AWAITING_UTKARSH_RENEW):
            return

        store.record_event(
            domain, 'mdb_said_using_yes', actor=actor,
            from_state=S.AWAITING_MDB_USAGE_RESPONSE,
            to_state=S.AWAITING_UTKARSH_RENEW,
            metadata={'expiring_state': data.get('expiring_state')},
        )
        _replace_card(
            client, body,
            header_text=f':white_check_mark: Yes, using {domain}',
            context_text=(f'<@{actor}> confirmed in use. '
                          'Forwarding to Utkarsh for renewal.'),
        )
        _sync_siblings(
            client, body, domain,
            header_text=f':white_check_mark: Yes, using {domain}',
            sibling_context=(f'<@{actor}> confirmed it\'s in use — resolved. '
                             'Forwarded to Utkarsh for renewal.'),
        )
        _dm.dm(
            client, real_recipient=Config.UTKARSH_SLACK_USER_ID,
            text=f'Please renew `{domain}` on Namecheap',
            blocks=[
                {'type': 'section', 'text': {
                    'type': 'mrkdwn',
                    'text': (
                        f':moneybag: *Renewal needed: `{domain}`*\n'
                        f'MDB <@{actor}> confirmed it\'s in use. '
                        'Renew on Namecheap, then click below.'
                    ),
                }},
                {'type': 'actions', 'elements': [{
                    'type': 'button', 'action_id': 'lifecycle_renewed',
                    'text': {'type': 'plain_text', 'text': ':white_check_mark: Renewed'},
                    'style': 'primary',
                    'value': json.dumps({
                        'domain': domain, 'requester': actor,
                    }),
                }]},
            ],
            dry_run_label=f'utkarsh_renew_request:{domain}',
        )

    @app.action('lifecycle_using_no')
    def using_no(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        domain = data['domain']
        assigned = data.get('assigned_to') or ''
        actor = body['user']['id']

        if not _enforce(actor, assigned, ack, role='MDB usage'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor,
                                   'MDB usage')
            return

        # Stale-click guard: if the row already left AWAITING, a sibling
        # recipient already resolved it — name the winner and bail.
        current = (store.get_domain(domain) or {}).get('lifecycle_state')
        if current != S.AWAITING_MDB_USAGE_RESPONSE:
            _already_handled(client, body, domain)
            return

        # Contradiction guard: MDB says "not using" but recent spend
        # disagrees → don't auto-disable; loop in TL with both signals.
        # This does NOT resolve the prompt — state stays AWAITING and the
        # other recipients' cards stay live (only the clicker's is replaced).
        cost = _recent_spend(domain)
        if cost >= Config.LIFECYCLE_ACTIVE_SPEND_USD:
            store.record_event(
                domain, 'mdb_no_but_recent_spend', actor=actor,
                from_state=S.AWAITING_MDB_USAGE_RESPONSE,
                to_state=S.AWAITING_MDB_USAGE_RESPONSE,
                metadata={'recent_cost': cost},
            )
            _replace_card(
                client, body,
                header_text=f':warning: Conflict on {domain}',
                context_text=(f'<@{actor}> said "not using" but recent '
                              f'30d spend is ${cost:.2f}. Escalated to TL '
                              'for verification — auto-renew NOT touched.'),
            )
            _dm.dm(
                client, real_recipient=Config.TL_SLACK_USER_ID,
                text=(f':warning: <@{actor}> said `{domain}` is not in use, '
                      f'but RedTrack shows ${cost:.2f} spend in the last 30d. '
                      'Please verify before we ask Utkarsh to disable auto-renew.'),
                dry_run_label=f'contradiction:{domain}',
            )
            return

        # No contradiction → resolve. Atomic first-click-wins.
        if not _claim(client, body, domain, S.AWAITING_MDB_USAGE_RESPONSE,
                      S.AWAITING_UTKARSH_DISABLE_RENEW):
            return

        store.record_event(
            domain, 'mdb_said_using_no', actor=actor,
            from_state=S.AWAITING_MDB_USAGE_RESPONSE,
            to_state=S.AWAITING_UTKARSH_DISABLE_RENEW,
        )
        _replace_card(
            client, body,
            header_text=f':x: Not using {domain}',
            context_text=(f'<@{actor}> confirmed not in use. '
                          'Asking Utkarsh to disable auto-renew so it lapses cleanly.'),
        )
        _sync_siblings(
            client, body, domain,
            header_text=f':x: Not using {domain}',
            sibling_context=(f'<@{actor}> confirmed not in use — resolved. '
                             'Utkarsh asked to disable auto-renew.'),
        )
        _dm.dm(
            client, real_recipient=Config.UTKARSH_SLACK_USER_ID,
            text=f'Please disable auto-renew on `{domain}`',
            blocks=[
                {'type': 'section', 'text': {
                    'type': 'mrkdwn',
                    'text': (
                        f':no_entry_sign: *Auto-renew off: `{domain}`*\n'
                        f'MDB <@{actor}> said not in use. Disable auto-renew '
                        'on Namecheap so it lapses cleanly, then click below.'
                    ),
                }},
                {'type': 'actions', 'elements': [{
                    'type': 'button', 'action_id': 'lifecycle_disable_renew_done',
                    'text': {'type': 'plain_text',
                             'text': ':white_check_mark: Auto-renew disabled'},
                    'value': json.dumps({
                        'domain': domain, 'requester': actor,
                    }),
                }]},
            ],
            dry_run_label=f'utkarsh_disable_renew:{domain}',
        )

    # ─ Utkarsh on his DM ─────────────────────────────────────────────────

    @app.action('lifecycle_renewed')
    def renewed(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        domain = data['domain']
        requester = data.get('requester')
        actor = body['user']['id']

        if not _enforce(actor, Config.UTKARSH_SLACK_USER_ID, ack, role='Utkarsh'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor,
                                   'Utkarsh')
            return

        # Re-sync expiry from Namecheap so the new expire_at is current.
        # Best-effort: failure here is fine, the next nightly sync will
        # pick it up. Don't block the click on a Namecheap call.
        try:
            from domain_assistant.namecheap_check import get_domain_info
            info = get_domain_info(domain)
            if info and info.get('expire_at'):
                store.update_namecheap_sync(
                    domain, info['expire_at'],
                    auto_renew_enabled=info.get('auto_renew_enabled'),
                )
        except Exception:
            logger.exception(
                'post-renewal namecheap re-sync failed for %s', domain,
            )

        store.set_lifecycle_state(domain, S.RENEWED)
        store.record_event(
            domain, 'renewed', actor=actor,
            from_state=S.AWAITING_UTKARSH_RENEW, to_state=S.RENEWED,
        )
        _replace_card(
            client, body,
            header_text=f':white_check_mark: Renewed: {domain}',
            context_text=f'<@{actor}> renewed it. Good for another year.',
        )
        if requester:
            _dm.dm(
                client, real_recipient=requester,
                text=f':tada: `{domain}` was renewed by <@{actor}>. Good for another year.',
                dry_run_label=f'mdb_renewal_confirmation:{domain}',
            )
        _dm.dm(
            client, real_recipient=Config.TL_SLACK_USER_ID,
            text=f':white_check_mark: `{domain}` renewed by <@{actor}>.',
            dry_run_label=f'tl_renewal_confirmation:{domain}',
        )

    @app.action('lifecycle_disable_renew_done')
    def disable_renew_done(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        domain = data['domain']
        requester = data.get('requester')
        actor = body['user']['id']

        if not _enforce(actor, Config.UTKARSH_SLACK_USER_ID, ack, role='Utkarsh'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor,
                                   'Utkarsh')
            return

        # Clear lifecycle_state so cron re-classifies cleanly. The domain
        # is now on a path to natural expiry; classifier will mark it
        # EXPIRED on the day if it hasn't been picked back up.
        store.set_lifecycle_state(domain, None)
        store.record_event(
            domain, 'auto_renew_disabled', actor=actor,
            from_state=S.AWAITING_UTKARSH_DISABLE_RENEW, to_state=None,
        )
        _replace_card(
            client, body,
            header_text=f':no_entry_sign: Auto-renew off: {domain}',
            context_text=(f'<@{actor}> disabled auto-renew. Domain will '
                          'lapse on its expire date.'),
        )
        if requester:
            _dm.dm(
                client, real_recipient=requester,
                text=(f':information_source: `{domain}` auto-renew disabled '
                      f'by <@{actor}>. Domain will lapse naturally — let me '
                      'know if plans change.'),
                dry_run_label=f'mdb_disable_renew_confirm:{domain}',
            )
        _dm.dm(
            client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':information_source: `{domain}` auto-renew disabled by '
                  f'<@{actor}>. Will lapse on expire date.'),
            dry_run_label=f'tl_disable_renew_confirm:{domain}',
        )

    # ─ MDB on IDLE card ──────────────────────────────────────────────────

    def _keep_for(days: int):
        """Factory: returns a handler that snoozes for `days` days."""
        def _handler(ack, body, client):
            ack()
            data = _payload(body)
            if data is None:
                return
            domain = data['domain']
            assigned = data.get('assigned_to') or ''
            actor = body['user']['id']

            if not _enforce(actor, assigned, ack, role='MDB inventory'):
                _ephemeral_not_for_you(client, body['channel']['id'], actor,
                                       'MDB inventory')
                return

            new_state = S.EXTENDED_30 if days == 30 else S.EXTENDED_15
            # Atomic first-click-wins across every fan-out recipient.
            if not _claim(client, body, domain,
                          S.AWAITING_MDB_INVENTORY_RESPONSE, new_state):
                return

            store.record_event(
                domain, f'mdb_extended_{days}', actor=actor,
                from_state=S.AWAITING_MDB_INVENTORY_RESPONSE,
                to_state=new_state,
                metadata={'snooze_days': days},
            )
            _replace_card(
                client, body,
                header_text=f':zzz: Snoozed {domain} for {days} days',
                context_text=f'<@{actor}> kept it. Will re-check after {days} days.',
            )
            _sync_siblings(
                client, body, domain,
                header_text=f':zzz: Snoozed {domain} for {days} days',
                sibling_context=(f'<@{actor}> kept it — resolved. '
                                 f'Will re-check after {days} days.'),
            )
            _dm.dm(
                client, real_recipient=Config.TL_SLACK_USER_ID,
                text=(f':zzz: <@{actor}> snoozed `{domain}` for {days} days.'),
                dry_run_label=f'tl_extended_{days}:{domain}',
            )
        return _handler

    app.action('lifecycle_keep_30')(_keep_for(30))
    app.action('lifecycle_keep_15')(_keep_for(15))

    @app.action('lifecycle_push_inventory')
    def push_inventory(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        domain = data['domain']
        assigned = data.get('assigned_to') or ''
        actor = body['user']['id']

        if not _enforce(actor, assigned, ack, role='MDB inventory'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor,
                                   'MDB inventory')
            return

        # Atomic first-click-wins across every fan-out recipient.
        if not _claim(client, body, domain,
                      S.AWAITING_MDB_INVENTORY_RESPONSE, S.INVENTORY):
            return

        # Per design: leave AWS resources alive (cert / R53 / CF / S3)
        # so rotation reuse is fast. We only release ownership.
        # Phase E: end ALL active assignments (multi-MDB safe) AND clear
        # the legacy assigned_to column for backwards-compat reads.
        # (lifecycle_state is already INVENTORY — _claim did that.)
        previous_assignments = [
            a['slack_user_id']
            for a in store.current_assignments_for_domain(domain)
        ]
        for uid in previous_assignments:
            store.end_assignment(domain, uid, by=actor)
        store.assign_to(domain, None)
        store.record_event(
            domain, 'pushed_to_inventory', actor=actor,
            from_state=S.AWAITING_MDB_INVENTORY_RESPONSE,
            to_state=S.INVENTORY,
            metadata={
                'previous_assigned_to_legacy': assigned,
                'previous_assignments': previous_assignments,
            },
        )
        _replace_card(
            client, body,
            header_text=f':package: Pushed to inventory: {domain}',
            context_text=(f'<@{actor}> released it. AWS resources stay alive '
                          'for rotation reuse.'),
        )
        _sync_siblings(
            client, body, domain,
            header_text=f':package: Pushed to inventory: {domain}',
            sibling_context=(f'<@{actor}> released it to inventory — resolved. '
                             'AWS resources stay alive for rotation reuse.'),
        )
        _dm.dm(
            client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':package: <@{actor}> pushed `{domain}` to inventory. '
                  'AWS resources kept alive for reuse.'),
            dry_run_label=f'tl_pushed_inventory:{domain}',
        )

    # ─ TL escalation buttons (action_ids defined now; the SLA escalator
    #   that posts these cards lands in Phase B-next). Each one is a
    #   thin wrapper around the matching MDB-side action — TL is just
    #   making the decision on the MDB's behalf.

    @app.action('lifecycle_tl_force_renew')
    def tl_force_renew(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        actor = body['user']['id']
        if not _enforce(actor, Config.TL_SLACK_USER_ID, ack, role='TL'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor, 'TL')
            return
        domain = data['domain']
        # SLA escalator may have advanced state to AWAITING_TL_OVERRIDE_USAGE,
        # OR TL might be clicking before MDB responded — read whatever's
        # there so the audit log reflects the real transition.
        from_state = (store.get_domain(domain) or {}).get('lifecycle_state')
        store.set_lifecycle_state(domain, S.AWAITING_UTKARSH_RENEW)
        store.record_event(
            domain, 'tl_forced_renew', actor=actor,
            from_state=from_state,
            to_state=S.AWAITING_UTKARSH_RENEW,
        )
        _replace_card(
            client, body,
            header_text=f':warning: TL override: renew {domain}',
            context_text=f'<@{actor}> forced renewal. Asking Utkarsh.',
        )
        _dm.dm(
            client, real_recipient=Config.UTKARSH_SLACK_USER_ID,
            text=(f':moneybag: TL <@{actor}> escalated `{domain}` for renewal '
                  '(MDB ghosted the prompt). Please renew on Namecheap.'),
            blocks=[
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text':
                    f'*Renewal needed (TL escalation): `{domain}`*'}},
                {'type': 'actions', 'elements': [{
                    'type': 'button', 'action_id': 'lifecycle_renewed',
                    'text': {'type': 'plain_text', 'text': ':white_check_mark: Renewed'},
                    'style': 'primary',
                    'value': json.dumps({'domain': domain, 'requester': actor}),
                }]},
            ],
            dry_run_label=f'tl_force_renew:{domain}',
        )

    @app.action('lifecycle_tl_force_disable_renew')
    def tl_force_disable_renew(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        actor = body['user']['id']
        if not _enforce(actor, Config.TL_SLACK_USER_ID, ack, role='TL'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor, 'TL')
            return
        domain = data['domain']
        from_state = (store.get_domain(domain) or {}).get('lifecycle_state')
        store.set_lifecycle_state(domain, S.AWAITING_UTKARSH_DISABLE_RENEW)
        store.record_event(
            domain, 'tl_forced_disable_renew', actor=actor,
            from_state=from_state,
            to_state=S.AWAITING_UTKARSH_DISABLE_RENEW,
        )
        _replace_card(
            client, body,
            header_text=f':warning: TL override: lapse {domain}',
            context_text=f'<@{actor}> chose to let it lapse. Asking Utkarsh.',
        )
        _dm.dm(
            client, real_recipient=Config.UTKARSH_SLACK_USER_ID,
            text=(f':no_entry_sign: TL <@{actor}> escalated `{domain}` to lapse '
                  '(MDB ghosted). Please disable auto-renew on Namecheap.'),
            blocks=[
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text':
                    f'*Auto-renew off (TL escalation): `{domain}`*'}},
                {'type': 'actions', 'elements': [{
                    'type': 'button', 'action_id': 'lifecycle_disable_renew_done',
                    'text': {'type': 'plain_text',
                             'text': ':white_check_mark: Auto-renew disabled'},
                    'value': json.dumps({'domain': domain, 'requester': actor}),
                }]},
            ],
            dry_run_label=f'tl_force_disable:{domain}',
        )

    @app.action('lifecycle_tl_force_push')
    def tl_force_push(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        actor = body['user']['id']
        if not _enforce(actor, Config.TL_SLACK_USER_ID, ack, role='TL'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor, 'TL')
            return
        domain = data['domain']
        prev = data.get('assigned_to') or ''
        from_state = (store.get_domain(domain) or {}).get('lifecycle_state')
        store.assign_to(domain, None)
        store.set_lifecycle_state(domain, S.INVENTORY)
        store.record_event(
            domain, 'tl_forced_push_inventory', actor=actor,
            from_state=from_state,
            to_state=S.INVENTORY,
            metadata={'previous_assigned_to': prev},
        )
        _replace_card(
            client, body,
            header_text=f':package: TL override: pushed {domain}',
            context_text=f'<@{actor}> pushed it to inventory.',
        )

    @app.action('lifecycle_tl_force_keep_30')
    def tl_force_keep_30(ack, body, client):
        ack()
        data = _payload(body)
        if data is None:
            return
        actor = body['user']['id']
        if not _enforce(actor, Config.TL_SLACK_USER_ID, ack, role='TL'):
            _ephemeral_not_for_you(client, body['channel']['id'], actor, 'TL')
            return
        domain = data['domain']
        from_state = (store.get_domain(domain) or {}).get('lifecycle_state')
        store.set_lifecycle_state(domain, S.EXTENDED_30)
        store.record_event(
            domain, 'tl_forced_keep_30', actor=actor,
            from_state=from_state,
            to_state=S.EXTENDED_30,
        )
        _replace_card(
            client, body,
            header_text=f':zzz: TL override: keep {domain}',
            context_text=f'<@{actor}> snoozed it for 30 days.',
        )

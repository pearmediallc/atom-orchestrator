"""Lifecycle state values used by the daily classifier and Slack handlers.

Stored as TEXT in `domains.lifecycle_state`. Centralised here so SQL
queries, Slack handlers, and tests can't drift on spelling.

State machine (see docs/flow):

  cron classifier sets:
    ACTIVE          — has spend in last 30d, expire >30d away
    IDLE            — no spend in last 30d, past assignment grace
    EXPIRING_30     — has spend, 14-30 days to expiry
    EXPIRING_14     — has spend, 7-14 days to expiry
    EXPIRING_7      — has spend, 1-7 days to expiry
    EXPIRING_1      — has spend, ≤1 day to expiry
    INVENTORY       — unassigned, in the rotation pool

  set when a prompt is posted, cleared by the button click handler:
    AWAITING_MDB_USAGE_RESPONSE     — DM'd MDB on EXPIRING_*
    AWAITING_MDB_INVENTORY_RESPONSE — DM'd MDB on IDLE
    AWAITING_UTKARSH_RENEW          — MDB said "yes using"
    AWAITING_UTKARSH_DISABLE_RENEW  — MDB said "no, not using"

  terminal-ish (cron will re-classify on next pass):
    RENEWED         — Utkarsh marked renewed
    EXPIRED         — past expire_at and not renewed
    EXTENDED_30     — MDB asked for 30 more days on an idle domain
    EXTENDED_15     — MDB asked for 15 more days
"""

ACTIVE     = 'ACTIVE'
IDLE       = 'IDLE'
INVENTORY  = 'INVENTORY'
RENEWED    = 'RENEWED'
EXPIRED    = 'EXPIRED'

EXPIRING_30 = 'EXPIRING_30'
EXPIRING_14 = 'EXPIRING_14'
EXPIRING_7  = 'EXPIRING_7'
EXPIRING_1  = 'EXPIRING_1'

EXTENDED_30 = 'EXTENDED_30'
EXTENDED_15 = 'EXTENDED_15'

AWAITING_MDB_USAGE_RESPONSE     = 'AWAITING_MDB_USAGE_RESPONSE'
AWAITING_MDB_INVENTORY_RESPONSE = 'AWAITING_MDB_INVENTORY_RESPONSE'
AWAITING_UTKARSH_RENEW          = 'AWAITING_UTKARSH_RENEW'
AWAITING_UTKARSH_DISABLE_RENEW  = 'AWAITING_UTKARSH_DISABLE_RENEW'

# Cron classifier never re-touches domains in these states — they're
# waiting on a human click. Prevents re-prompting / race conditions.
AWAITING_STATES = frozenset({
    AWAITING_MDB_USAGE_RESPONSE,
    AWAITING_MDB_INVENTORY_RESPONSE,
    AWAITING_UTKARSH_RENEW,
    AWAITING_UTKARSH_DISABLE_RENEW,
})

EXPIRING_STATES = frozenset({
    EXPIRING_30, EXPIRING_14, EXPIRING_7, EXPIRING_1,
})

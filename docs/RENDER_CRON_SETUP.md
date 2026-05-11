# Render Cron Job — Lifecycle Bot Setup

One-time dashboard configuration that wires the lifecycle bot to run
once a day on Render. After this is set up, the cron handles:

1. **Classifier pass** — scans all 744 domains, classifies each as
   ACTIVE / IDLE / EXPIRING_30/14/7/1 / EXPIRED / INVENTORY.
2. **SLA escalator** — any MDB-side AWAITING row past
   `LIFECYCLE_MDB_RESPONSE_SLA_HOURS` (default 48h) gets escalated to
   TL with override buttons.
3. **Inventory digest** — when `DEVELOPERS_CHANNEL_ID` is set, posts
   the day's "available for grabs" list to that channel.

## Render dashboard steps

1. Render dashboard → **New +** → **Cron Job**
2. Configure:

   | Field | Value |
   |---|---|
   | **Name** | `atom-orchestrator-lifecycle-cron` |
   | **Repository** | `pearmediallc/atom-orchestrator` (same repo) |
   | **Branch** | `phase-7-mark-done-wires-atom` (or `main` after PR merge) |
   | **Region** | same as the web service (`Oregon`) |
   | **Runtime** | `Python 3` |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Command** | `python -m lifecycle` |
   | **Schedule** | `30 13 * * *` |
   | **Plan** | Starter (free is OK for now) |

   The schedule `30 13 * * *` is UTC. That's **7:00 PM IST** — start
   of the MDB shift, so prompts land when MDBs are actually online.

3. **Environment Variables** — copy ALL of these from the web service.
   Most can be linked directly via Render's "Sync from another service"
   feature; the lifecycle-specific ones may need to be added manually.

   Required (lifecycle won't run without these):

   ```
   DATABASE_URL              (same as web service)
   SLACK_BOT_TOKEN           (same as web service)
   SLACK_SIGNING_SECRET      (same as web service)
   REDTRACK_API_KEY          (Q6bS31RtDkG15tHj396n)
   REDTRACK_BASE_URL         https://api.redtrack.io
   NAMECHEAP_API_USER        (same as web service)
   NAMECHEAP_API_KEY         (same as web service)
   NAMECHEAP_CLIENT_IP       (same as web service)
   PROXY_USERNAME            (same as web service)
   PROXY_PASSWORD            (same as web service)
   PROXY_HOST                ddc.oxylabs.io
   PROXY_PORT                8001
   TL_SLACK_USER_ID          U09U534JS2F
   UTKARSH_SLACK_USER_ID     U0AMR5SN81E
   ```

   First-deploy safety:

   ```
   LIFECYCLE_DRY_RUN=true              (CRITICAL — leave on for the first 48h)
   DEV_REROUTE_DMS_TO=U0B0UBF8H7X      (also good to leave on initially —
                                         routes all DMs to your account so
                                         you can see what the bot would send)
   ```

   Tuning knobs (set explicitly even though they have defaults — easier
   to find later in the dashboard):

   ```
   LIFECYCLE_ACTIVE_SPEND_USD=1.0
   LIFECYCLE_ASSIGNMENT_GRACE_DAYS=14
   LIFECYCLE_MDB_RESPONSE_SLA_HOURS=48
   LIFECYCLE_PROMPT_DEDUP_HOURS=23
   LIFECYCLE_EXPIRY_CASCADE_DAYS=30,14,7,1
   ```

   Optional — enables the daily inventory pool digest to `#developers`:

   ```
   DEVELOPERS_CHANNEL_ID=  (get from Slack: right-click channel → details → bottom)
   ```

4. **Click Save & Deploy.** First deploy pulls the repo, installs deps,
   and parks the cron. The next 7 PM IST tick is when it actually runs.

## Verifying it works

After the first scheduled run (or trigger one manually from the Render
dashboard → cron job → **Manual Run**):

1. Render's cron logs should end with a line like:
   ```
   {'scan': {'classified': 12, 'prompted': 0, 'unchanged': 728, 'skipped': 4, 'errors': 0},
    'sla': {'escalated': 0, 'errors': 0},
    'digest': {'unassigned': 100, 'posted': 0, 'skipped': 1}}
   ```
   - `digest: posted=0, skipped=1` is expected during DRY_RUN — it logs intent.
   - Once `LIFECYCLE_DRY_RUN=false`, you'd see `posted=1` instead.

2. Check the `domain_events` table for newly-written audit rows:
   ```sql
   SELECT domain, event_type, occurred_at
     FROM domain_events
    ORDER BY occurred_at DESC LIMIT 20;
   ```

3. Open Slack and run `/list-domains :expiring` — you should now see
   real expiry data (populated by the backfill).

## 48h dry-run observation period

Leave `LIFECYCLE_DRY_RUN=true` for at least 48 hours after the first
cron run. During this window:

- Cron runs daily but sends **no real DMs**.
- All state transitions + events are still written (so you have an
  audit trail to verify).
- Spot-check the `prompted_mdb_*` events: are the right MDBs being
  flagged for the right domains?
- Spot-check the `classified_*` events: any surprises in IDLE vs
  ACTIVE classification?

If everything looks correct after 48h, flip the switch:

1. Render dashboard → atom-orchestrator-lifecycle-cron → Environment
2. Change `LIFECYCLE_DRY_RUN` to `false`
3. Save
4. Also clear `DEV_REROUTE_DMS_TO` so real recipients get real DMs
   (instead of all DMs landing in your account)
5. Next 7 PM IST run starts sending real DMs.

## Operational notes

- **Cold starts**: Render cron jobs spin up fresh each run, so there's
  no warmup cost during the run itself. (The web service's cold-start
  delay is unrelated.)
- **Free tier vs paid**: cron jobs on Render's free tier run for up to
  15 minutes. Our `python -m lifecycle` takes ~30-60 seconds. Plenty
  of headroom.
- **Failure recovery**: if a cron run fails mid-pass, the next run
  picks up automatically. `last_prompted_at` dedup prevents
  double-prompts even if a row's classification ran twice.
- **Logs**: each run's stdout is captured in Render's logs UI. Failed
  runs also send an email alert by default.

## Rollback

If something goes wrong after `LIFECYCLE_DRY_RUN=false`:

1. **Stop the bleeding**: Render → cron job → Environment → flip
   `LIFECYCLE_DRY_RUN` back to `true` → Save. Next run sends no DMs.
2. **Investigate**: `domain_events` has the full audit trail of what
   the bot did and when.
3. **Optional code revert**: `git revert <bad-commit-sha> && git push`
   — Render auto-deploys the rollback.

The state machine + audit log means there's no "ghost actions"
unaccounted for. Anything the bot did can be traced and undone
manually.

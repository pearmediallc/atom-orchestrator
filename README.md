# ATOM Orchestrator

A Slack-bot interface that automates Pear Media's domain-provisioning
workflow end-to-end. Talks to ATOM (existing Flask app) over HTTP for the
AWS heavy lifting; handles the human-facing workflow (Slack, ChatGPT,
Namecheap availability, approval, inventory) itself.

> **Status:** Phase 1 scaffold. Not production-ready. Local development only.

## What this service does

Today, provisioning a new campaign domain involves 5+ people across 6 tools
(Slack, email, ChatGPT, Namecheap, Google Forms, ATOM). This service
collapses that into one Slack conversation:

```
MDB types `/new-domain` in Slack
   │
   ▼
   bot asks: vertical? + 2-3 example names? + lander URL? + extension?
   │
   ▼
   ChatGPT generates name suggestions
   │
   ▼
   Namecheap availability filters down
   │
   ▼
   MDB picks one  →  TL approves (Slack button)  →  domain purchased
   │
   ▼
   bot calls ATOM `/api/setup-domain`  →  AWS resources created
   │
   ▼
   bot calls ATOM `/api/copy-files`  →  lander deployed to new bucket
   │
   ▼
   Utkarsh verifies  →  bot DMs MDB: "domain X is ready"
```

For a flow with an existing inventory domain, the ChatGPT/Namecheap/purchase
steps are skipped — bot just looks up an owned domain and runs setup + copy.

## Architecture

This service deliberately does NOT replicate ATOM's AWS code. It calls
ATOM's existing HTTP API. ATOM stays the AWS engine; this service is the
workflow engine.

```
   Slack         OpenAI       Namecheap        Google Sheets
     │             │              │                  │
     ▼             ▼              ▼                  ▼
  ┌────────────────────────────────────────────────────────┐
  │              ATOM Orchestrator (this app)              │
  │  • slack_bot/        — slash commands, modals          │
  │  • orchestrator/     — workflow state machine          │
  │  • domain_assistant/ — ChatGPT + availability          │
  │  • inventory/        — owned-domain CRUD               │
  └─────────────────┬──────────────────────────────────────┘
                    │ HTTP
                    ▼
         ┌────────────────────────┐
         │  ATOM (existing app)   │
         │  /api/setup-domain     │
         │  /api/copy-files       │
         │  /api/check-existing/* │
         └────────────────────────┘
                    │ boto3
                    ▼
                  AWS
```

## Project layout

```
atom-orchestrator/
├── app.py                     # Flask entry, blueprint registration, /health
├── config.py                  # env loading
├── requirements.txt
├── .env.example
├── slack_bot/
│   ├── __init__.py
│   └── routes.py              # slash commands, interactive callbacks
├── orchestrator/
│   ├── __init__.py
│   ├── workflow.py            # state machine for /new-domain
│   └── atom_client.py         # HTTP wrapper around ATOM's APIs
├── domain_assistant/
│   ├── __init__.py
│   ├── chatgpt.py             # OpenAI domain-suggestion calls
│   └── namecheap_check.py     # availability lookups
└── inventory/
    ├── __init__.py
    └── store.py               # SQLite-backed CRUD over owned domains
```

## Running locally

Prerequisites:
- Python 3.9+
- The existing ATOM app running on `http://localhost:5500` (so this service
  can call it)

```bash
cd /Users/pear/Desktop/Projects/atom-orchestrator
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill in real values
python app.py
```

By default this service runs on `http://localhost:5600`. Verify with:

```bash
curl http://localhost:5600/health
# → {"status": "healthy", "service": "atom-orchestrator"}
```

## Environment variables

See `.env.example` for the full list. At minimum to boot:

| Var | Purpose |
|---|---|
| `FLASK_SECRET_KEY` | Flask session key |
| `ATOM_BASE_URL` | Where ATOM is reachable, e.g. `http://localhost:5500` |

Optional (per phase):
| Var | Phase |
|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | Phase 2 (Slack integration) |
| `OPENAI_API_KEY` | Phase 4 (ChatGPT suggestions) |
| `NAMECHEAP_*` | Phase 4 (availability check) |
| `INVENTORY_DB_PATH` | Phase 3 (defaults to `./inventory.db`) |

## Phased delivery

| Phase | Goal | Time |
|---|---|---|
| 1 | Scaffold + `/health` endpoint (this PR) | ✅ done |
| 2 | Slack app skeleton + `/new-domain` slash command stub | 3–5 days |
| 3 | Inventory CRUD (SQLite for now) + `/list-domains` command | 2–3 days |
| 4 | Path A — existing-domain → ATOM domain setup + file copy | 4–6 days |
| 5 | Path B — ChatGPT + Namecheap availability + TL approval | 5–7 days |
| 6 | Polish: error handling, audit log, retries | 3–5 days |
| | **Total** | **~3 weeks** |

## What this is NOT

- **Not a replacement for ATOM.** ATOM keeps its UI for direct admin use.
- **Not a domain registrar.** Purchase still happens via Namecheap (manual
  or API-driven, TBD).
- **Not multi-tenant.** Single team, single Slack workspace.
- **Not production-ready** until Phase 6 lands and a security review passes.

## Security

- No production credentials in this repo. `.env` is gitignored.
- All secrets come from env vars, never hardcoded.
- The service should run in a VPC or behind authentication when deployed.

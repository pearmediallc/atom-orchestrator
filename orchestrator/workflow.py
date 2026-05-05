"""High-level workflows.

Two workflows mirror Utkarsh's two flows:
  • run_existing_domain_workflow — Path A (this phase)
  • run_new_domain_workflow      — Path B (Phase 5)
"""
from dataclasses import dataclass, field
from typing import Optional, List

from config import Config
from inventory import store
from orchestrator.atom_client import AtomClient
from domain_assistant import chatgpt, namecheap_check


# ---------- Request / Result types ----------

@dataclass
class ExistingDomainRequest:
    """Inputs for Path A — deploying a lander to an already-owned domain."""
    target_domain: str               # the existing domain we're deploying to
    source_account: str              # AWS account that holds the lander files
    source_bucket: str               # bucket that holds the lander files
    source_folders: List[str] = field(default_factory=list)
    source_files: List[str] = field(default_factory=list)
    requested_by: Optional[str] = None  # Slack user id, for audit


@dataclass
class WorkflowRequest:
    """Inputs for Path B (new domain). Phase 5 fleshes this out."""
    requester_slack_id: str
    vertical: Optional[str] = None
    example_domains: List[str] = field(default_factory=list)
    extension: Optional[str] = None
    lander_url: Optional[str] = None
    chosen_domain: Optional[str] = None


@dataclass
class WorkflowResult:
    status: str               # 'completed' | 'failed'
    message: str              # human-readable summary for Slack
    details: dict = field(default_factory=dict)


# ---------- Workflows ----------

def run_existing_domain_workflow(
    req: ExistingDomainRequest,
    client: Optional[AtomClient] = None,
) -> WorkflowResult:
    """Path A — MDB picked a domain we already own. Deploy the lander.

    Steps:
      1. Look up the target domain in our inventory store.
      2. Trigger ATOM domain setup (idempotent — reuses cert/zone if present).
      3. Wait for setup to finish (or fail).
      4. Copy lander files from source bucket to the target domain's bucket.
      5. Mark setup_complete in inventory.
    """
    # 1. Inventory lookup
    record = store.get_domain(req.target_domain)
    if not record:
        return WorkflowResult(
            status='failed',
            message=f"Domain '{req.target_domain}' is not in our inventory.",
            details={'reason': 'not_in_inventory'},
        )

    target_account = record.get('aws_account') or 'auto-insurance'

    # Inject-or-create AtomClient. Tests pass a mock; real callers get login.
    owns_client = client is None
    if owns_client:
        client = AtomClient()
        try:
            client.login(Config.ATOM_USERNAME, Config.ATOM_PASSWORD)
        except Exception as e:
            return WorkflowResult(
                status='failed',
                message=f'Could not log in to ATOM: {e}',
                details={'reason': 'atom_login_failed'},
            )

    # 2. Trigger ATOM domain setup
    try:
        setup_response = client.setup_domain(
            req.target_domain,
            account_key=target_account,
        )
        task_id = setup_response['tasks'][0]['task_id']
    except Exception as e:
        return WorkflowResult(
            status='failed',
            message=f'Could not start ATOM domain setup: {e}',
            details={'reason': 'atom_setup_kickoff_failed'},
        )

    # 3. Wait for setup to finish
    try:
        setup_result = client.wait_for_setup(task_id, timeout=600)
    except TimeoutError as e:
        return WorkflowResult(
            status='failed',
            message=f'ATOM setup did not complete in time: {e}',
            details={'reason': 'atom_setup_timeout', 'task_id': task_id},
        )

    if setup_result.get('status') != 'completed':
        # Forward ATOM's structured error so the caller sees AWS error code,
        # request id, etc.
        return WorkflowResult(
            status='failed',
            message=(
                f"ATOM domain setup failed at step "
                f"'{setup_result.get('failed_at_step', 'unknown')}'."
            ),
            details={
                'reason': 'atom_setup_failed',
                'setup_result': setup_result,
            },
        )

    # 4. Copy lander files from source → target with domain rewrite
    if not (req.source_folders or req.source_files):
        return WorkflowResult(
            status='failed',
            message='No source_folders or source_files specified — nothing to copy.',
            details={'reason': 'no_source_specified'},
        )

    try:
        copy_result = client.copy_files(
            source_account=req.source_account,
            source_bucket=req.source_bucket,
            target_account=target_account,
            target_bucket=req.target_domain,
            selected_folders=req.source_folders,
            selected_files=req.source_files,
        )
    except Exception as e:
        return WorkflowResult(
            status='failed',
            message=f'File copy failed: {e}',
            details={'reason': 'copy_files_exception'},
        )

    if copy_result.get('error'):
        return WorkflowResult(
            status='failed',
            message=f"File copy reported an error: {copy_result['error']}",
            details={'reason': 'copy_files_error', 'copy_result': copy_result},
        )

    # 5. Mark complete in inventory
    store.mark_setup_complete(req.target_domain)

    # 6. Done
    return WorkflowResult(
        status='completed',
        message=f'Lander deployed. Live at https://{req.target_domain}',
        details={
            'live_url': f'https://{req.target_domain}',
            'setup_result': setup_result,
            'copy_result': copy_result,
        },
    )


def suggest_new_domains(
    vertical: str,
    example_domains: List[str],
    extension: str = '.com',
    count: int = 5,
    max_attempts: int = 4,
) -> List[dict]:
    """Path B step 1 — suggest *available* + *price-filtered* new domain names.

    Composes:
      • ChatGPT-style LLM for naming
      • Namecheap `domains.check` for availability
      • Namecheap `users.getPricing` for register price
      • Per-extension price cap from Config.DOMAIN_PRICE_CAP_USD
        (TL spec 2026-05-05: .com under $15, others under-or-equal $5)

    Tries up to `max_attempts` LLM batches to assemble exactly `count`
    domains that are (a) available on Namecheap AND (b) price-capped.
    Returns whatever it found if the cap can't be met after max_attempts.

    Each result row:
        {'domain': str, 'available': True, 'price': 9.18}

    The returned list is suitable to render directly as Pick this buttons.
    Stubs in domain_assistant/ keep the function working end-to-end in
    local dev without any real API keys.
    """
    if not vertical:
        raise ValueError("'vertical' is required")
    if extension and not extension.startswith('.'):
        extension = '.' + extension

    cap = Config.price_cap_for(extension)
    qualifying: List[dict] = []
    seen: set = set()
    # Generate ~3x the count we need per attempt — most candidates are
    # taken or premium-priced, so we need a buffer.
    candidates_per_attempt = max(15, count * 3)

    for attempt in range(max_attempts):
        if len(qualifying) >= count:
            break

        candidates = chatgpt.suggest_domains(
            vertical=vertical,
            example_domains=example_domains or [],
            extension=extension,
            count=candidates_per_attempt,
        )
        # Dedupe across attempts (LLMs sometimes repeat)
        candidates = [d for d in candidates if d not in seen]
        seen.update(candidates)
        if not candidates:
            continue

        checked = namecheap_check.check_availability_and_price(
            candidates, extension=extension,
        )
        for r in checked:
            if not r['available']:
                continue
            price = r.get('price')
            # Unknown price (no creds / API failure) — exclude. We can't
            # confirm it's under the cap.
            if price is None:
                continue
            if price > cap:
                continue
            qualifying.append(r)
            if len(qualifying) >= count:
                break

    return qualifying[:count]


def run_new_domain_workflow(req: WorkflowRequest) -> WorkflowResult:
    """Path B — full new-domain workflow (suggest → pick → approve → buy →
    setup → copy). Phase 5+. The orchestration of these multi-step gates
    requires Slack interactivity (Phase 2) so it lives there.

    For just the suggestion step, see suggest_new_domains() above.
    """
    return WorkflowResult(
        status='failed',
        message='Full Path B workflow not implemented yet — Phase 5+.',
        details={'reason': 'not_implemented'},
    )

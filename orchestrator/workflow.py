"""High-level workflows.

Two workflows mirror Utkarsh's two flows:
  • run_existing_domain_workflow — Path A (this phase)
  • run_new_domain_workflow      — Path B (Phase 5)
"""
import re
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
    audience: Optional[str] = None
    extension: Optional[str] = None
    lander_url: Optional[str] = None
    chosen_domain: Optional[str] = None


@dataclass
class WorkflowResult:
    status: str               # 'completed' | 'failed'
    message: str              # human-readable summary for Slack
    details: dict = field(default_factory=dict)


# ---------- Workflows ----------

def _fail(req: ExistingDomainRequest, message: str, *,
          reason: str, task_id: Optional[str] = None,
          extra_details: Optional[dict] = None) -> WorkflowResult:
    """Build a WorkflowResult(status='failed') and stamp the inventory
    row with STATUS_FAILED + latest_error so /list-domains and any
    future re-deploy logic can see the last failure cause.

    Inventory write is best-effort — if the DB itself is the failure
    we don't want to mask the original error with a secondary one.
    """
    details = {'reason': reason}
    if task_id is not None:
        details['task_id'] = task_id
    if extra_details:
        details.update(extra_details)
    try:
        store.transition_status(
            req.target_domain,
            to_status=store.STATUS_FAILED,
            task_id=task_id,
            error=message[:500],  # cap to keep DB rows small
        )
    except Exception:
        pass  # original error wins; don't mask it.
    return WorkflowResult(status='failed', message=message, details=details)


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

    Status state machine: the inventory row's `status` column is
    transitioned to STATUS_DEPLOYING right before kicking off ATOM,
    and to STATUS_DEPLOYED / STATUS_FAILED at the end of every return
    path. /list-domains can then surface the live state of any in-
    flight deploy without polling Slack threads.
    """
    # 1. Inventory lookup
    record = store.get_domain(req.target_domain)
    if not record:
        # No row = nothing to update; build the result inline (can't call
        # _fail because there's no row to stamp).
        return WorkflowResult(
            status='failed',
            message=f"Domain '{req.target_domain}' is not in our inventory.",
            details={'reason': 'not_in_inventory'},
        )

    # aws_account must be explicitly set on the row. Legacy NULL rows
    # were backfilled to 'auto-insurance' by init_db at boot time, and
    # newly inserted rows are required to set this column. A NULL we
    # see at runtime now indicates a real bug — better to fail loudly
    # than silently route to the wrong AWS account (audit #6 fix).
    target_account = record.get('aws_account')
    if not target_account:
        return _fail(
            req,
            f"Domain '{req.target_domain}' has no aws_account set in "
            'inventory. Refusing to silently default — set the column '
            'explicitly via SQL or re-import the row.',
            reason='aws_account_missing',
        )

    # Inject-or-create AtomClient. Tests pass a mock; real callers get login.
    owns_client = client is None
    if owns_client:
        client = AtomClient()
        try:
            client.login(Config.ATOM_USERNAME, Config.ATOM_PASSWORD)
        except Exception as e:
            return _fail(
                req,
                f'Could not log in to ATOM: {e}',
                reason='atom_login_failed',
            )

    # Mark the row as deploying BEFORE we kick off ATOM — so an
    # external observer (/list-domains, dashboards) can see the
    # in-flight state from the moment the worker starts. We pass
    # task_id=None for now and overwrite with the real ID after the
    # kickoff response below.
    try:
        store.transition_status(
            req.target_domain,
            to_status=store.STATUS_DEPLOYING,
        )
    except Exception:
        pass  # status is observability only; never block the deploy on it.

    # 2. Trigger ATOM domain setup
    try:
        setup_response = client.setup_domain(
            req.target_domain,
            account_key=target_account,
        )
        task_id = setup_response['tasks'][0]['task_id']
    except Exception as e:
        return _fail(
            req,
            f'Could not start ATOM domain setup: {e}',
            reason='atom_setup_kickoff_failed',
        )

    # Now record the real ATOM task_id on the row so it's correlatable
    # with ATOM's logs / status endpoint without re-deriving it.
    try:
        store.transition_status(
            req.target_domain,
            to_status=store.STATUS_DEPLOYING,
            task_id=task_id,
        )
    except Exception:
        pass

    # 3. Wait for setup to finish. Timeout is configured via
    # Config.PHASE7_SETUP_TIMEOUT_SEC (default 30 min) — long enough
    # for a fresh-domain ACM cert validation to complete. The previous
    # hardcoded 600s aborted legitimate runs while cert was still
    # propagating (2026-05-08 audit fix).
    try:
        setup_result = client.wait_for_setup(
            task_id, timeout=Config.PHASE7_SETUP_TIMEOUT_SEC,
        )
    except TimeoutError as e:
        return _fail(
            req,
            f'ATOM setup did not complete in time: {e}',
            reason='atom_setup_timeout', task_id=task_id,
        )

    if setup_result.get('status') != 'completed':
        # Forward ATOM's structured error so the caller sees AWS error code,
        # request id, etc.
        return _fail(
            req,
            "ATOM domain setup failed at step "
            f"'{setup_result.get('failed_at_step', 'unknown')}'.",
            reason='atom_setup_failed', task_id=task_id,
            extra_details={'setup_result': setup_result},
        )

    # 4. Copy lander files from source → target with domain rewrite
    if not (req.source_folders or req.source_files):
        return _fail(
            req,
            'No source_folders or source_files specified — nothing to copy.',
            reason='no_source_specified', task_id=task_id,
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
        return _fail(
            req,
            f'File copy failed: {e}',
            reason='copy_files_exception', task_id=task_id,
        )

    if copy_result.get('error'):
        return _fail(
            req,
            f"File copy reported an error: {copy_result['error']}",
            reason='copy_files_error', task_id=task_id,
            extra_details={'copy_result': copy_result},
        )

    # ATOM returns 200 with "Successfully copied 0 files from ..." when the
    # source path has no content. Treat that as failure — otherwise the bot
    # reports "deployed" on a no-op (caught 2026-05-06 on safetyfirstauto.pro).
    msg = copy_result.get('message', '')
    m = re.search(r'copied\s+(\d+)\s+files', msg, re.IGNORECASE)
    if m and int(m.group(1)) == 0:
        return _fail(
            req,
            f'File copy returned 0 files — source has no content at '
            f"folders={req.source_folders or '(none)'} files={req.source_files or '(none)'} "
            f'in s3://{req.source_bucket}.',
            reason='copy_files_zero', task_id=task_id,
            extra_details={'copy_result': copy_result},
        )

    # Build the live URL with the folder path included. The lander typically
    # lives under the first source folder (e.g. https://target.com/h-insure-c/),
    # not the apex.
    if req.source_folders:
        folder = req.source_folders[0].strip('/')
        live_url = f'https://{req.target_domain}/{folder}/' if folder else f'https://{req.target_domain}'
    else:
        live_url = f'https://{req.target_domain}'

    # 5. Mark complete in inventory + persist the lander URL we just deployed
    store.mark_setup_complete(req.target_domain, lander_url=live_url)

    # 6. Move the row into its terminal STATUS_DEPLOYED state.
    # Best-effort — mark_setup_complete already stamped setup_at, so a
    # transition_status failure here only loses the status field, not
    # the deploy itself.
    try:
        store.transition_status(
            req.target_domain,
            to_status=store.STATUS_DEPLOYED,
            task_id=task_id,
        )
    except Exception:
        pass

    # 7. Done
    return WorkflowResult(
        status='completed',
        message=f'Lander deployed. Live at {live_url}',
        details={
            'live_url': live_url,
            'setup_result': setup_result,
            'copy_result': copy_result,
        },
    )


# When the user picks "Any" extension, we sweep across these cheap TLDs
# and return the cheapest available results. Order matters — earlier
# entries get tried first per LLM batch, so the cheapest TLDs surface
# faster. .com is included so .com names still show up in mixed mode.
_ANY_EXTENSION_SWEEP = [
    '.site', '.icu', '.top', '.live', '.pro', '.info', '.com',
]


def suggest_new_domains(
    vertical: str,
    audience: str = '',
    extension: str = 'any',
    count: int = 5,
    max_attempts: int = 4,
    examples: Optional[List[str]] = None,
) -> List[dict]:
    """Path B step 1 — suggest *available* + *price-filtered* new domain names.

    Composes:
      • LLM for naming (vertical + optional audience/angle + optional
        user-supplied stylistic example domains)
      • Namecheap `domains.check` for availability
      • Namecheap `users.getPricing` for register price
      • Per-extension price cap from Config.DOMAIN_PRICE_CAP_USD
        (TL spec 2026-05-05: .com under $15, others under-or-equal $5)

    Two modes based on `extension`:
      • `'any'` (default) — sweep across cheap TLDs (.site, .icu, .top,
        .live, .pro, .info, .com) and return the 5 cheapest available.
      • `'.com'` / `'.pro'` / etc. — restrict to that TLD only.

    `audience` is the marketer's free-text description of WHO the
    campaign is for (e.g. "seniors looking for medigap"). Optional —
    pass an empty string when no audience info is provided.

    `examples` is an optional list of domain names whose stylistic feel
    the AI should match (NOT reuse). Useful when the vertical's
    vocabulary differs from what the prompt's generic defaults assume.

    Returns up to `count` rows shaped like
        {'domain': str, 'available': True, 'price': 9.18, 'extension': '.com'}.
    Empty list if nothing qualifies after max_attempts.
    """
    if not vertical:
        raise ValueError("'vertical' is required")
    if not extension:
        extension = 'any'

    if extension == 'any':
        return _suggest_across_extensions(
            vertical, audience, count, max_attempts, examples=examples,
        )

    if not extension.startswith('.'):
        extension = '.' + extension
    return _suggest_for_extension(
        vertical, audience, extension, count, max_attempts,
        examples=examples,
    )


def _suggest_for_extension(
    vertical: str, audience: str, extension: str,
    count: int, max_attempts: int,
    examples: Optional[List[str]] = None,
) -> List[dict]:
    """Single-extension search — generate, filter to available + price-capped,
    retry until count is satisfied or attempts exhausted."""
    cap = Config.price_cap_for(extension)
    qualifying: List[dict] = []
    seen: set = set()
    # Most "obvious" candidates (cheapauto.com, lowrate.com, etc.) are
    # squatter-owned. Generate ~6x the count we need per attempt — only
    # 1-2 in 30 typical LLM suggestions actually clear Namecheap.
    candidates_per_attempt = max(30, count * 6)

    for attempt in range(max_attempts):
        if len(qualifying) >= count:
            break
        candidates = chatgpt.suggest_domains(
            vertical=vertical,
            audience=audience,
            extension=extension,
            count=candidates_per_attempt,
            examples=examples,
        )
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
            if price is None or price > cap:
                continue
            qualifying.append(r)
            if len(qualifying) >= count:
                break

    return qualifying[:count]


def _suggest_across_extensions(
    vertical: str, audience: str, count: int, max_attempts: int,
    examples: Optional[List[str]] = None,
) -> List[dict]:
    """Mixed-extension search — try each cheap TLD until we have `count`
    available + price-capped domains, then sort by price ascending so
    the cheapest options show first.
    """
    qualifying: List[dict] = []

    for ext in _ANY_EXTENSION_SWEEP:
        if len(qualifying) >= count:
            break
        per_ext_target = min(2, count - len(qualifying))

        results = _suggest_for_extension(
            vertical, audience, ext, per_ext_target, max_attempts=2,
            examples=examples,
        )
        for r in results:
            r['extension'] = ext
        qualifying.extend(results)

    qualifying.sort(key=lambda r: r.get('price') or 999.0)
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

"""High-level workflows. Phase 1: stubs only. Each workflow returns a result
dict that the caller (Slack handler) can post back to the channel.

Two workflows mirror Utkarsh's two flows:
  • run_existing_domain_workflow — Path A
  • run_new_domain_workflow      — Path B
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WorkflowRequest:
    """Inputs from the Slack modal that started this run."""
    requester_slack_id: str
    vertical: Optional[str] = None
    example_domains: list = field(default_factory=list)
    extension: Optional[str] = None
    lander_url: Optional[str] = None
    chosen_domain: Optional[str] = None  # filled in after MDB selects


@dataclass
class WorkflowResult:
    status: str               # 'pending_approval' | 'completed' | 'failed'
    message: str              # human-readable summary for Slack
    details: dict = field(default_factory=dict)


def run_existing_domain_workflow(req: WorkflowRequest) -> WorkflowResult:
    """Path A — MDB picked a domain we already own. Just deploy the lander."""
    # Phase 4 TODO:
    #   1. Look up the domain in the inventory store
    #   2. Resolve which AWS account + bucket
    #   3. Call AtomClient.setup_domain (idempotent — reuses cert/zone/etc.)
    #   4. Call AtomClient.copy_files (deploy the lander)
    #   5. Return success with the live URL
    return WorkflowResult(
        status='pending',
        message='Path A workflow not implemented yet — Phase 4.',
    )


def run_new_domain_workflow(req: WorkflowRequest) -> WorkflowResult:
    """Path B — buy a fresh domain, then deploy the lander."""
    # Phase 5 TODO:
    #   1. Call domain_assistant.chatgpt to suggest names
    #   2. Filter via domain_assistant.namecheap_check for availability
    #   3. Reply in Slack with shortlist; wait for MDB selection
    #   4. Send TL approval card; wait for click
    #   5. Trigger purchase (manual ping to Utkarsh OR automated)
    #   6. Run setup_domain + copy_files (same as Path A from step 3 onward)
    #   7. Write inventory record
    #   8. Notify MDB
    return WorkflowResult(
        status='pending',
        message='Path B workflow not implemented yet — Phase 5.',
    )

"""Thin HTTP client wrapping ATOM's existing Flask APIs.

Why this exists: this orchestrator is a separate service from ATOM. We don't
import ATOM's Python directly — we call its HTTP endpoints. That keeps both
codebases independently deployable and testable.

ATOM endpoints we wrap (see ATOM repo at /Users/pear/Desktop/Projects/aws_automation):
  POST /api/setup-domain         → start domain setup
  GET  /api/status/<task_id>     → poll progress
  GET  /api/check-existing/<dom> → existing-AWS-resources lookup
  POST /api/copy-files           → S3-to-S3 copy + domain rewrite
"""
import time
import requests
from config import Config


class AtomClient:
    def __init__(self, base_url: str = None, session: requests.Session = None):
        self.base_url = (base_url or Config.ATOM_BASE_URL).rstrip('/')
        # Session reuse keeps the cookie jar so /login persists across calls.
        self.session = session or requests.Session()

    def login(self, username: str, password: str) -> bool:
        """ATOM uses cookie-based auth. Most endpoints require a logged-in
        session. We post the form and rely on the session jar.

        Phase 2 TODO: replace this with a service-account token in ATOM
        instead of borrowing a human user's credentials.
        """
        r = self.session.post(
            f'{self.base_url}/login',
            data={'username': username, 'password': password},
            allow_redirects=True,
            timeout=10,
        )
        r.raise_for_status()
        return True

    def health(self) -> dict:
        return self.session.get(f'{self.base_url}/api/health', timeout=5).json()

    def check_existing(self, domain: str) -> dict:
        return self.session.get(
            f'{self.base_url}/api/check-existing/{domain}',
            timeout=15,
        ).json()

    def setup_domain(self, domain: str, account_key: str = 'auto-insurance',
                     cname_name: str = 'track') -> dict:
        """Kicks off the 8-step domain setup. Returns task_id(s) immediately."""
        r = self.session.post(
            f'{self.base_url}/api/setup-domain',
            json={'domain': domain, 'account': account_key, 'cname_name': cname_name},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def status(self, task_id: str) -> dict:
        return self.session.get(
            f'{self.base_url}/api/status/{task_id}',
            timeout=5,
        ).json()

    def copy_files(self, source_account: str, source_bucket: str,
                   target_account: str, target_bucket: str,
                   selected_folders: list = None,
                   selected_files: list = None) -> dict:
        """Cross-account S3 copy with automatic source→target domain rewrite
        in HTML/CSS/JS files."""
        r = self.session.post(
            f'{self.base_url}/api/copy-files',
            json={
                'sourceAccount': source_account,
                'sourceBucket': source_bucket,
                'targetAccount': target_account,
                'targetBucket': target_bucket,
                'selectedFolders': selected_folders or [],
                'selectedFiles': selected_files or [],
            },
            timeout=300,
        )
        return r.json()

    def wait_for_setup(self, task_id: str, timeout: int = 1800,
                       poll_interval: int = 5) -> dict:
        """Block until setup completes or fails. Returns the final status dict.

        Phase 4 TODO: instead of blocking, push progress updates back to the
        Slack thread as they arrive.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.status(task_id)
            if s.get('status') in ('completed', 'failed'):
                return s
            time.sleep(poll_interval)
        raise TimeoutError(f'Setup task {task_id} did not complete within {timeout}s')

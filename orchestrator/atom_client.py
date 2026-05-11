"""Thin HTTP client wrapping ATOM's existing Flask APIs.

Why this exists: this orchestrator is a separate service from ATOM. We don't
import ATOM's Python directly — we call its HTTP endpoints. That keeps both
codebases independently deployable and testable.

ATOM endpoints we wrap (see ATOM repo at /Users/pear/Desktop/Projects/aws_automation):
  POST /api/setup-domain         → start domain setup
  GET  /api/status/<task_id>     → poll progress
  GET  /api/check-existing/<dom> → existing-AWS-resources lookup
  POST /api/copy-files           → S3-to-S3 copy + domain rewrite

Error model — every public method either returns the parsed JSON dict
or raises one of the AtomError subclasses below. Callers can branch on
type to decide whether to retry (transient) vs. surface to the user
(programmer error / auth issue).
"""
import logging
import time
import requests

from config import Config
from orchestrator.log_setup import log_event


logger = logging.getLogger(__name__)


# ─── Public exception types ────────────────────────────────────────────────
# A typed hierarchy so callers can distinguish "ATOM is down, retry later"
# from "you sent a bad request, fix your code". Every leaf type carries
# enough context (HTTP status, body sniff, request URL) to debug from
# Render logs alone.

class AtomError(Exception):
    """Base class for ATOM-client failures. Always carries enough context
    to debug without needing the underlying Response object.
    """


class AtomConnectionError(AtomError):
    """Raised when we couldn't reach ATOM at all — DNS failure, TLS
    failure, connection refused, request timeout. Almost always
    transient; retrying with backoff is appropriate.
    """


class AtomServerError(AtomError):
    """Raised when ATOM returned 5xx. Usually transient (Render restart,
    upstream Namecheap proxy, etc.). Carries the status code and a
    response-body sniff for debugging.
    """


class AtomClientError(AtomError):
    """Raised when ATOM returned 4xx. Indicates programmer error
    (missing required field, invalid account_key, malformed payload)
    OR auth failure (cookie expired, login() never run). NOT transient
    — retrying without fixing the input will fail again.
    """


class AtomInvalidResponse(AtomError):
    """Raised when ATOM returned a successful HTTP status but the body
    wasn't valid JSON — typically the login redirect HTML leaking
    through because the session cookie wasn't actually authenticated
    (the bug we hit on 2026-05-08). Diagnostic value is high: the body
    sniff tells you exactly what came back instead of JSON.
    """


class AtomAuthenticationError(AtomClientError):
    """Specific subclass of AtomClientError raised by login() when the
    POSTed credentials weren't accepted — distinguishable from the
    generic 4xx case so callers can prompt for fresh creds.
    """


# Internal helper: pull a short, log-safe sniff of a response body so
# raised exceptions carry diagnostic context without dumping kilobytes.
_BODY_SNIFF = 240


def _sniff(text: str) -> str:
    if not text:
        return '(empty body)'
    text = text.replace('\n', '\\n').replace('\r', '\\r')
    if len(text) > _BODY_SNIFF:
        return text[:_BODY_SNIFF] + f'... ({len(text)} bytes total)'
    return text


def _translate(method: str, url: str, response: requests.Response) -> dict:
    """Validate `response` and return its JSON. Raises a typed AtomError
    on every failure mode with enough context to debug from logs.

    The single chokepoint that turns 'I called requests' into 'I called
    a typed RPC' — every public method routes through here.
    """
    status = response.status_code

    if status >= 500:
        raise AtomServerError(
            f'{method} {url} -> HTTP {status} (server error). '
            f'Body: {_sniff(response.text)}'
        )

    if status >= 400:
        # 401/403 are auth-specific — surface them with a tighter type
        # so callers can retry login() instead of giving up.
        if status in (401, 403):
            raise AtomAuthenticationError(
                f'{method} {url} -> HTTP {status} (auth required). '
                f'Body: {_sniff(response.text)}'
            )
        raise AtomClientError(
            f'{method} {url} -> HTTP {status} (client error). '
            f'Body: {_sniff(response.text)}'
        )

    try:
        return response.json()
    except ValueError as e:
        # 200 OK but non-JSON body — typically the login HTML when our
        # session cookie isn't authenticated. The body sniff makes this
        # immediately obvious from the exception text.
        raise AtomInvalidResponse(
            f'{method} {url} -> HTTP {status} but body was not JSON: '
            f'{type(e).__name__}: {e}. Body sniff: {_sniff(response.text)}'
        ) from e


class AtomClient:
    def __init__(self, base_url: str = None, session: requests.Session = None):
        self.base_url = (base_url or Config.ATOM_BASE_URL).rstrip('/')
        # Session reuse keeps the cookie jar so /login persists across calls.
        self.session = session or requests.Session()

    # ─── Internal request helpers ──────────────────────────────────────────

    def _get_json(self, path: str, *, timeout: int) -> dict:
        url = f'{self.base_url}{path}'
        try:
            r = self.session.get(url, timeout=timeout)
        except requests.RequestException as e:
            raise AtomConnectionError(
                f'GET {url} could not reach ATOM: '
                f'{type(e).__name__}: {e}'
            ) from e
        return _translate('GET', url, r)

    def _post_json(self, path: str, json_body: dict, *, timeout: int) -> dict:
        url = f'{self.base_url}{path}'
        try:
            r = self.session.post(url, json=json_body, timeout=timeout)
        except requests.RequestException as e:
            raise AtomConnectionError(
                f'POST {url} could not reach ATOM: '
                f'{type(e).__name__}: {e}'
            ) from e
        return _translate('POST', url, r)

    # ─── Public API ─────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> bool:
        """ATOM uses cookie-based auth. Most endpoints require a logged-in
        session.

        Returns True iff the credentials were accepted. Raises
        AtomAuthenticationError when ATOM rejected them (i.e. the form
        re-rendered with HTTP 200 instead of 302-redirecting to home).
        Raises AtomConnectionError on transport failures, AtomServerError
        on 5xx.

        Why we don't follow redirects: ATOM's login flow uses POST /login
        and either 302→home on success or 200→login.html on failure.
        Allowing redirects collapses both cases to HTTP 200 and we lose
        the signal — exactly the bug we hit 2026-05-08, where a wrong
        password caused all subsequent /api/setup-domain calls to receive
        the login HTML and crash on .json(). Inspecting the unfollowed
        response status is the root-cause fix, not a probe-after-login
        patch.
        """
        url = f'{self.base_url}/login'
        try:
            r = self.session.post(
                url,
                data={'username': username, 'password': password},
                allow_redirects=False,
                timeout=10,
            )
        except requests.RequestException as e:
            raise AtomConnectionError(
                f'POST {url} could not reach ATOM: '
                f'{type(e).__name__}: {e}'
            ) from e

        if r.status_code == 302:
            # Successful auth — Flask-Login redirects to home (or 'next').
            # The session cookie is now in self.session.cookies.
            location = r.headers.get('Location', '')
            if location.endswith('/login') or '/login?' in location:
                # Defensive: a 302 BACK to /login means the redirect is
                # the post-login bounce, not the success case.
                log_event(
                    'atom_login_failed', level=logging.ERROR,
                    username=username,
                    failure_reason='redirected_back_to_login',
                    location=location,
                )
                raise AtomAuthenticationError(
                    f'POST {url} returned 302 -> {location} (redirect '
                    f'back to login means credentials were rejected)'
                )
            log_event('atom_login_succeeded', username=username)
            return True

        if r.status_code == 200:
            # The form was re-rendered, which Flask-Login does on bad
            # credentials. Treat as auth failure — never as success.
            log_event(
                'atom_login_failed', level=logging.ERROR,
                username=username,
                failure_reason='form_re_rendered_http_200',
            )
            raise AtomAuthenticationError(
                f'POST {url} returned HTTP 200 with no redirect — '
                f'ATOM re-rendered the login form, meaning credentials '
                f'were rejected. Verify ATOM_USERNAME/ATOM_PASSWORD '
                f'match what production ATOM\'s users_config expects.'
            )

        # Any other status is unexpected — fall through to the typed
        # translator so 5xx vs 4xx get the right exception type.
        return _translate('POST', url, r) and True  # always raises before True

    def health(self) -> dict:
        return self._get_json('/api/health', timeout=5)

    def check_existing(self, domain: str) -> dict:
        return self._get_json(f'/api/check-existing/{domain}', timeout=15)

    def list_buckets(self, account_key: str) -> list:
        """List bucket NAMES in the given AWS account, via ATOM.

        Returns the bare list of strings (just the names — ATOM also
        returns creation_date, which we don't need for ownership
        resolution). Empty list on any response shape we don't
        understand — the caller treats that as "this account doesn't
        own the bucket I'm looking for" rather than crashing.
        """
        resp = self._get_json(f'/api/buckets/{account_key}', timeout=30)
        return [
            b.get('name')
            for b in (resp.get('buckets') or [])
            if b.get('name')
        ]

    def setup_domain(self, domain: str, account_key: str = 'auto-insurance',
                     cname_name: str = 'track') -> dict:
        """Kicks off the 8-step domain setup. Returns task_id(s) immediately."""
        return self._post_json(
            '/api/setup-domain',
            {'domain': domain, 'account': account_key, 'cname_name': cname_name},
            timeout=10,
        )

    def status(self, task_id: str) -> dict:
        return self._get_json(f'/api/status/{task_id}', timeout=5)

    def copy_files(self, source_account: str, source_bucket: str,
                   target_account: str, target_bucket: str,
                   selected_folders: list = None,
                   selected_files: list = None) -> dict:
        """Cross-account S3 copy with automatic source→target domain rewrite
        in HTML/CSS/JS files."""
        return self._post_json(
            '/api/copy-files',
            {
                'sourceAccount': source_account,
                'sourceBucket': source_bucket,
                'targetAccount': target_account,
                'targetBucket': target_bucket,
                'selectedFolders': selected_folders or [],
                'selectedFiles': selected_files or [],
            },
            timeout=300,
        )

    def wait_for_setup(self, task_id: str, timeout: int = 1800,
                       poll_interval: int = 5,
                       on_progress=None) -> dict:
        """Block until setup completes or fails. Returns the final status dict.

        Transient AtomConnectionError / AtomServerError during polling
        do NOT abort the wait — they're expected (Render redeploy, edge
        hiccup) and resolve on the next poll. Persistent failures still
        bubble up via the deadline expiring.

        ``on_progress`` (optional) is invoked with the full status dict on
        every poll (running OR terminal). The callback runs in this
        thread; failures are caught and logged so a buggy reporter never
        prevents the worker from observing the terminal status.

        ATOM's status dict during a running task carries:
          • status:   'running' | 'completed' | 'failed'
          • progress: human-readable string about the current step
          • steps:    { step_key: { status: pending|in_progress|completed|failed } }
        See aws_automation/app.py::setup_domain_async for the source.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = self.status(task_id)
            except (AtomConnectionError, AtomServerError) as e:
                # Transient — don't kill the wait, just log and retry.
                logger.warning(
                    'wait_for_setup transient error polling task %s: %s. '
                    'Will retry in %ds.', task_id, e, poll_interval,
                )
                time.sleep(poll_interval)
                continue
            if on_progress is not None:
                try:
                    on_progress(s)
                except Exception:
                    # Progress reporting is decorative — never let a
                    # callback bug abort the actual wait.
                    logger.exception(
                        'wait_for_setup progress callback raised for '
                        'task %s; continuing wait', task_id,
                    )
            if s.get('status') in ('completed', 'failed'):
                return s
            time.sleep(poll_interval)
        raise TimeoutError(f'Setup task {task_id} did not complete within {timeout}s')

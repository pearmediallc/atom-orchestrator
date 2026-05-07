"""DB-backed Phase 7 task queue.

Replaces the previous fire-and-forget daemon-Thread pattern in
slack_bot/routes.py. Every Slack click that needs ATOM work now
enqueues a row in the `phase7_tasks` table; an in-process worker
thread claims and processes it. If the bot process restarts mid-task
(Render redeploy, OOM kill, scaling event), the task stays in
`status='running'` with a stale `heartbeat_at`; on the next boot
`recover_stale_running_tasks()` requeues it so the work resumes
instead of leaking AWS resources behind a Slack thread that goes
silent.

Why a DB queue and not Celery / RQ:
  • adds zero new infra (no Redis subscription),
  • runs at the volume this bot actually sees (~3 tasks/day),
  • keeps the entire workflow state queryable from the same Postgres
    we already use for inventory, so /list-domains and any future
    dashboard can join domain ↔ task without crossing systems.

Single-process semantics: only one bot instance runs in production.
Atomic task claim is implemented via a UPDATE ... WHERE status='queued'
guard — no FOR UPDATE SKIP LOCKED needed at this scale, but the same
guard would extend cleanly to multi-worker if we ever scale out.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from inventory import store


logger = logging.getLogger(__name__)


# ─── Task lifecycle constants ──────────────────────────────────────────────

TASK_QUEUED = 'queued'
TASK_RUNNING = 'running'
TASK_DONE = 'done'
TASK_FAILED = 'failed'

_VALID_TASK_STATUSES = {TASK_QUEUED, TASK_RUNNING, TASK_DONE, TASK_FAILED}

# Task `kind` discriminator — one entry per Slack-button → workflow
# call site so workers can dispatch to the right runner without
# inspecting payload shape.
TASK_KIND_PATH_A = 'path_a_deploy_lander'  # confirm_deployed click
TASK_KIND_PATH_B = 'path_b_purchased_setup'  # confirm_purchased click

_VALID_TASK_KINDS = {TASK_KIND_PATH_A, TASK_KIND_PATH_B}


# Recovery thresholds — a task whose worker hasn't heartbeat in this
# long is treated as dead. 90s is well above the worker's 30s
# heartbeat cadence, so a slightly delayed heartbeat doesn't trigger
# a false recovery.
_HEARTBEAT_INTERVAL_SEC = 30
_STALE_HEARTBEAT_SEC = 90
# A task that was claimed but never wrote a single heartbeat (worker
# crashed during the very first ATOM call) needs a separate timer so
# we don't wait 90s on the heartbeat that will never arrive.
_NO_HEARTBEAT_GRACE_SEC = 60


# ─── Public exception types ────────────────────────────────────────────────

class TaskError(Exception):
    """Base class for queue-related failures."""


class TaskClaimLost(TaskError):
    """Raised when a worker attempts to claim a task that another
    worker (or the recovery sweeper) has already moved out of
    'queued'. Caller should silently exit — its work has been or is
    being handled elsewhere."""


# ─── Internal helpers ──────────────────────────────────────────────────────

def _worker_id() -> str:
    """A short identifier for the worker that claimed a task — used
    purely for debugging ('which process / thread did this?'). The
    hostname + thread name is enough to correlate with Render logs."""
    host = os.getenv('HOSTNAME') or socket.gethostname()
    return f'{host}/{threading.current_thread().name}'


# ─── Public API: enqueue + claim + lifecycle ───────────────────────────────

@dataclass
class ClaimedTask:
    id: int
    domain: str
    kind: str
    request: dict
    attempt: int
    max_attempts: int


def enqueue(domain: str, kind: str, request: dict, *,
            max_attempts: int = 1) -> int:
    """Insert a queued task and return its id.

    Caller is expected to spawn a worker (run_in_thread) immediately
    so the click feels responsive. The task row is the durability
    contract — even if the worker thread never starts, the recovery
    sweeper will pick this up on next boot.
    """
    if kind not in _VALID_TASK_KINDS:
        raise ValueError(f'kind={kind!r} not in {sorted(_VALID_TASK_KINDS)}')
    sql = (
        'INSERT INTO phase7_tasks '
        '(domain, kind, request_json, status, max_attempts, '
        ' created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)'
    )
    with store._conn() as c:
        if store._is_postgres():
            cur = c.cursor()
            cur.execute(
                store._q(sql) + ' RETURNING id',
                (domain, kind, json.dumps(request),
                 TASK_QUEUED, max_attempts),
            )
            row = cur.fetchone()
            cur.close()
            return row['id']
        cur = c.execute(
            sql,
            (domain, kind, json.dumps(request),
             TASK_QUEUED, max_attempts),
        )
        return cur.lastrowid


def claim(task_id: int) -> ClaimedTask:
    """Atomically transition `task_id` from queued → running.

    Returns the claimed task with parsed request_json. Raises
    TaskClaimLost if another worker beat us to it (status had already
    moved to running / done / failed). The atomic guard is the
    ``WHERE status = 'queued'`` clause — Postgres + SQLite both
    enforce it via the row-level write lock taken by UPDATE.
    """
    update_sql = (
        "UPDATE phase7_tasks SET status = ?, started_at = CURRENT_TIMESTAMP, "
        "heartbeat_at = CURRENT_TIMESTAMP, worker_id = ?, "
        "attempt = attempt + 1, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = ?"
    )
    select_sql = (
        'SELECT id, domain, kind, request_json, attempt, max_attempts '
        'FROM phase7_tasks WHERE id = ?'
    )
    worker_id = _worker_id()
    with store._conn() as c:
        cur = store._execute(
            c, update_sql,
            (TASK_RUNNING, worker_id, task_id, TASK_QUEUED),
        )
        rowcount = cur.rowcount
        if store._is_postgres():
            cur.close()
        if rowcount == 0:
            raise TaskClaimLost(
                f'Task {task_id} was not in queued state; '
                'another worker likely claimed it'
            )
        cur = store._execute(c, select_sql, (task_id,))
        row = cur.fetchone()
        if store._is_postgres():
            cur.close()
    return ClaimedTask(
        id=row['id'],
        domain=row['domain'],
        kind=row['kind'],
        request=json.loads(row['request_json']),
        attempt=row['attempt'],
        max_attempts=row['max_attempts'],
    )


def heartbeat(task_id: int) -> None:
    """Update heartbeat_at = NOW(). Called periodically during long-
    running ATOM polls so the recovery sweeper can distinguish a
    legitimately-slow worker from a dead one."""
    sql = (
        'UPDATE phase7_tasks SET heartbeat_at = CURRENT_TIMESTAMP, '
        'updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = ?'
    )
    with store._conn() as c:
        cur = store._execute(c, sql, (task_id, TASK_RUNNING))
        if store._is_postgres():
            cur.close()


def mark_done(task_id: int, *, atom_task_id: Optional[str] = None) -> None:
    sql = (
        "UPDATE phase7_tasks SET status = ?, "
        "atom_task_id = COALESCE(?, atom_task_id), "
        "finished_at = CURRENT_TIMESTAMP, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?"
    )
    with store._conn() as c:
        cur = store._execute(c, sql, (TASK_DONE, atom_task_id, task_id))
        if store._is_postgres():
            cur.close()


def mark_failed(task_id: int, error: str, *,
                atom_task_id: Optional[str] = None) -> None:
    sql = (
        "UPDATE phase7_tasks SET status = ?, error = ?, "
        "atom_task_id = COALESCE(?, atom_task_id), "
        "finished_at = CURRENT_TIMESTAMP, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?"
    )
    with store._conn() as c:
        cur = store._execute(
            c, sql,
            (TASK_FAILED, error[:1000], atom_task_id, task_id),
        )
        if store._is_postgres():
            cur.close()


# ─── Recovery sweeper (called once at boot) ────────────────────────────────

def find_stale_running_task_ids(
    *, heartbeat_threshold_sec: int = _STALE_HEARTBEAT_SEC,
    no_heartbeat_grace_sec: int = _NO_HEARTBEAT_GRACE_SEC,
) -> list:
    """Return task IDs that look dead.

    A task is "dead" if EITHER:
      • status='running' AND heartbeat_at IS NULL AND
        started_at < NOW() - no_heartbeat_grace_sec
        (worker crashed before its first heartbeat)
      • status='running' AND heartbeat_at < NOW() - heartbeat_threshold_sec
        (worker stopped heartbeating long enough that we trust it's
        gone, not just slow)

    Implementation note: cutoff timestamps are formatted as
    ``YYYY-MM-DD HH:MM:SS`` to match SQLite's CURRENT_TIMESTAMP format
    exactly. Python's datetime.isoformat() produces ``T``-separated
    strings with timezone offsets which would NOT compare correctly
    against SQLite's space-separated, timezone-naïve column values.
    Postgres's TIMESTAMPTZ accepts both formats so the same string
    round-trips on either backend.
    """
    now = datetime.now(timezone.utc)
    heartbeat_cutoff = now - timedelta(seconds=heartbeat_threshold_sec)
    started_cutoff = now - timedelta(seconds=no_heartbeat_grace_sec)
    fmt = '%Y-%m-%d %H:%M:%S'
    sql = (
        'SELECT id FROM phase7_tasks WHERE status = ? AND ('
        '(heartbeat_at IS NULL AND started_at < ?) OR '
        '(heartbeat_at IS NOT NULL AND heartbeat_at < ?))'
    )
    with store._conn() as c:
        cur = store._execute(
            c, sql,
            (TASK_RUNNING,
             started_cutoff.strftime(fmt),
             heartbeat_cutoff.strftime(fmt)),
        )
        rows = cur.fetchall()
        if store._is_postgres():
            cur.close()
    return [r['id'] for r in rows]


def requeue(task_id: int) -> bool:
    """Move a stale 'running' task back to 'queued' so a fresh worker
    can claim it. Returns True if exactly one row was updated.

    Bumps `attempt` to remain accurate after requeue (claim() also
    increments, so we DON'T double-bump here — claim handles it).
    Clears heartbeat / worker / started fields so the next claim sees
    a clean state.
    """
    sql = (
        "UPDATE phase7_tasks SET status = ?, "
        "heartbeat_at = NULL, worker_id = NULL, started_at = NULL, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = ?"
    )
    with store._conn() as c:
        cur = store._execute(
            c, sql, (TASK_QUEUED, task_id, TASK_RUNNING),
        )
        rowcount = cur.rowcount
        if store._is_postgres():
            cur.close()
    return rowcount == 1


def recover_stale_running_tasks() -> list:
    """Walk every stale 'running' task and requeue it, dispatching a
    fresh worker for each. Called once from create_app() at boot.

    Returns the list of task IDs that were requeued, for logging /
    test assertions.
    """
    stale = find_stale_running_task_ids()
    requeued = []
    for tid in stale:
        if requeue(tid):
            requeued.append(tid)
            logger.warning(
                'Phase 7 task %s was stale (worker died mid-task); '
                'requeued for retry', tid,
            )
    if requeued:
        # Dispatch workers for each requeued task. Lazy-import the
        # runner to avoid a circular dependency between tasks.py and
        # routes.py / workflow.py.
        from orchestrator.tasks_runner import dispatch_worker_for
        for tid in requeued:
            dispatch_worker_for(tid)
    return requeued


# ─── Heartbeat helper for long-running workers ─────────────────────────────

class HeartbeatThread(threading.Thread):
    """Background thread that bumps heartbeat_at every 30s while a
    Phase 7 task is in flight. Started by the worker; stopped via
    .stop() when the work completes."""

    def __init__(self, task_id: int):
        super().__init__(daemon=True, name=f'phase7-heartbeat-{task_id}')
        self.task_id = task_id
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                heartbeat(self.task_id)
            except Exception as e:
                # Heartbeat failure is non-fatal — the work continues.
                # Worst case the recovery sweeper requeues the task,
                # which is the same outcome as a real crash.
                logger.warning(
                    'Heartbeat failed for task %s: %s',
                    self.task_id, e,
                )
            # Sleep with periodic stop-check so .stop() returns
            # within 1s instead of waiting the full interval.
            for _ in range(_HEARTBEAT_INTERVAL_SEC):
                if self._stop.is_set():
                    return
                time.sleep(1)


def format_traceback(exc: BaseException) -> str:
    """Compact traceback string for the `error` column.

    Caller should pass the exception caught at the worker boundary;
    we read its __traceback__ via traceback.format_exception so the
    column captures both the type/value and the call site.
    """
    return ''.join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )

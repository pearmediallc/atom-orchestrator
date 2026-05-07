"""Worker dispatch + execution for the Phase 7 task queue.

Glues `orchestrator.tasks` (the queue) to `slack_bot.routes`'s
existing Phase 7 worker logic. The runner is the single entrypoint
for both:

  • Live clicks (Mark Purchased / Mark Deployed) — routes.py enqueues
    a task and immediately calls `dispatch_worker_for(task_id)` so
    the user sees the bot working within seconds.

  • Boot-time recovery — `tasks.recover_stale_running_tasks()` finds
    tasks that were running when the previous process died and
    requeues them, then calls `dispatch_worker_for(task_id)` for each
    so the deploy resumes instead of stalling forever.

Worker invariants:
  • Always claims the task exclusively before doing real work
    (TaskClaimLost → silent exit; another worker has it).
  • Spawns a heartbeat thread so the recovery sweeper can tell live
    workers from dead ones.
  • Marks the task done / failed unconditionally on every exit path.
  • Catches all exceptions at the boundary so a worker crash never
    leaks an unhandled exception into the daemon — Python would log
    it and the task would stay in 'running' forever.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from orchestrator import tasks


logger = logging.getLogger(__name__)


def dispatch_worker_for(task_id: int) -> None:
    """Spawn a daemon thread that claims and processes the given
    task. Idempotent w.r.t. re-dispatches — the claim's atomic guard
    means at most one worker ever owns a task at a time.

    Daemon thread is intentional: when the process is asked to shut
    down, in-flight workers stop with it. The persisted task row is
    the durability contract — recovery will pick up where this left
    off on the next boot.
    """
    t = threading.Thread(
        target=_run_task,
        args=(task_id,),
        daemon=True,
        name=f'phase7-worker-{task_id}',
    )
    t.start()


def _run_task(task_id: int) -> None:
    """Worker entrypoint. Claims the task and routes it to the
    appropriate handler based on `kind`.

    All exceptions are caught and translated to mark_failed so the
    task can never get stuck in 'running' due to an unhandled error.
    """
    try:
        try:
            claimed = tasks.claim(task_id)
        except tasks.TaskClaimLost:
            logger.info(
                'Worker for task %s exiting: another worker '
                'already claimed this task', task_id,
            )
            return

        hb = tasks.HeartbeatThread(task_id)
        hb.start()
        try:
            if claimed.kind == tasks.TASK_KIND_PATH_A:
                _run_path_a(claimed)
            elif claimed.kind == tasks.TASK_KIND_PATH_B:
                _run_path_b(claimed)
            else:
                tasks.mark_failed(
                    task_id,
                    f'unknown task kind {claimed.kind!r}',
                )
                return
            tasks.mark_done(task_id)
        finally:
            hb.stop()
    except Exception as e:
        logger.exception('Phase 7 worker for task %s crashed', task_id)
        try:
            tasks.mark_failed(task_id, tasks.format_traceback(e))
        except Exception:
            # If even the mark_failed write fails (DB unavailable),
            # the task stays in 'running' and the recovery sweeper
            # picks it up on the next healthy boot. That's the
            # whole point of having recovery.
            logger.exception(
                'mark_failed also crashed for task %s — relying on '
                'recovery sweeper', task_id,
            )


def _build_slack_client():
    """Create a fresh Slack WebClient using the configured bot token.

    Workers must build their own client (rather than receive one as
    an argument) because boot-time recovery has no upstream caller —
    it runs before any Slack interaction has happened.
    """
    # Lazy import — keeps the module importable in environments
    # where slack_sdk isn't installed (e.g. unit tests of the queue
    # mechanics that mock _run_task).
    from slack_sdk import WebClient
    from config import Config

    if not Config.SLACK_BOT_TOKEN:
        raise RuntimeError(
            'SLACK_BOT_TOKEN not set — cannot post Phase 7 progress. '
            'Set it in the bot environment and redeploy.'
        )
    return WebClient(token=Config.SLACK_BOT_TOKEN)


# ─── Per-kind runners ──────────────────────────────────────────────────────

def _run_path_a(claimed: tasks.ClaimedTask) -> None:
    """Path A — Mark Deployed click. The serialized request payload
    carries everything the existing _phase7_run_atom_setup function
    in slack_bot/routes.py needs."""
    # Lazy import to avoid the routes.py → tasks_runner cycle.
    from slack_bot.routes import _phase7_run_atom_setup

    p = claimed.request
    client = _build_slack_client()
    _phase7_run_atom_setup(
        client,
        p['channel'],
        p['message_ts'],
        p['target_domain'],
        p.get('vertical', ''),
        p['requester'],
        p.get('lander_url', ''),
    )


def _run_path_b(claimed: tasks.ClaimedTask) -> None:
    """Path B — Mark Purchased click. Same _phase7_run_atom_setup
    function as Path A; the only difference is that Path B's
    inventory row was inserted moments ago via add_domain.
    """
    from slack_bot.routes import _phase7_run_atom_setup

    p = claimed.request
    client = _build_slack_client()
    _phase7_run_atom_setup(
        client,
        p['channel'],
        p['message_ts'],
        p['target_domain'],
        p.get('vertical', ''),
        p['requester'],
        p.get('lander_url', ''),
    )


# ─── Public enqueue helpers used by routes.py ──────────────────────────────

def enqueue_path_a(*, channel: str, message_ts: str, target_domain: str,
                   vertical: str, requester: str,
                   lander_url: str) -> int:
    """Enqueue a Path A 'Mark Deployed' workflow and dispatch a worker
    immediately. Returns the task id.
    """
    task_id = tasks.enqueue(
        domain=target_domain,
        kind=tasks.TASK_KIND_PATH_A,
        request={
            'channel': channel,
            'message_ts': message_ts,
            'target_domain': target_domain,
            'vertical': vertical,
            'requester': requester,
            'lander_url': lander_url,
        },
    )
    dispatch_worker_for(task_id)
    return task_id


def enqueue_path_b(*, channel: str, message_ts: str, target_domain: str,
                   vertical: str, requester: str,
                   lander_url: str) -> int:
    """Enqueue a Path B 'Mark Purchased' workflow and dispatch a worker
    immediately. Returns the task id.
    """
    task_id = tasks.enqueue(
        domain=target_domain,
        kind=tasks.TASK_KIND_PATH_B,
        request={
            'channel': channel,
            'message_ts': message_ts,
            'target_domain': target_domain,
            'vertical': vertical,
            'requester': requester,
            'lander_url': lander_url,
        },
    )
    dispatch_worker_for(task_id)
    return task_id

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
            if claimed.kind in (
                tasks.TASK_KIND_PATH_A, tasks.TASK_KIND_PATH_B,
            ):
                _run_phase7(claimed)
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


# ─── Per-kind runner ───────────────────────────────────────────────────────

def _run_phase7(claimed: tasks.ClaimedTask) -> None:
    """Run a Phase 7 deploy task — same code path for Path A
    (Mark Deployed) and Path B (Mark Purchased).

    Both paths invoke the SAME _phase7_run_atom_setup function in
    slack_bot/routes.py (it's idempotent, so reusing already-set-up
    AWS resources for Path A on a known domain is a fast no-op,
    while Path B on a fresh domain runs the full 7-step build).

    Audit #13 cleanup: previously two near-identical _run_path_a /
    _run_path_b functions; merged here because the only difference
    was the docstring. Leaving the kind discriminator on the task
    row preserves observability — `task_kind` is grep-able in logs
    + queryable in inventory.
    """
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


# Backward-compat aliases — the dispatcher in _run_task and any
# external callers still reference _run_path_a / _run_path_b. Both
# point at the unified runner; new code should call _run_phase7
# directly.
_run_path_a = _run_phase7
_run_path_b = _run_phase7


# ─── Public enqueue helper used by routes.py ───────────────────────────────

def enqueue_phase7(*, kind: str, channel: str, message_ts: str,
                   target_domain: str, vertical: str, requester: str,
                   lander_url: str) -> int:
    """Enqueue a Phase 7 task and dispatch a worker immediately.

    Returns the task id. `kind` is one of tasks.TASK_KIND_PATH_A
    (Mark Deployed) or tasks.TASK_KIND_PATH_B (Mark Purchased). The
    discriminator is recorded on the task row so grepping logs by
    kind shows which click triggered the deploy without inspecting
    the request payload.

    Audit #13 cleanup: replaces the previous enqueue_path_a +
    enqueue_path_b functions, which had identical bodies except for
    the kind constant.
    """
    task_id = tasks.enqueue(
        domain=target_domain,
        kind=kind,
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


# Backward-compat shims so existing imports (and any in-flight
# tests) keep working while callers migrate to enqueue_phase7.
def enqueue_path_a(**kwargs) -> int:
    return enqueue_phase7(kind=tasks.TASK_KIND_PATH_A, **kwargs)


def enqueue_path_b(**kwargs) -> int:
    return enqueue_phase7(kind=tasks.TASK_KIND_PATH_B, **kwargs)

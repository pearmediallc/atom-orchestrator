"""Unit tests for the Phase 7 DB-backed task queue (audit #2 fix).

Covers:
  • enqueue inserts a queued row and returns its id
  • claim atomically transitions queued → running once
  • re-claiming the same task raises TaskClaimLost
  • heartbeat updates heartbeat_at without changing status
  • mark_done / mark_failed transitions to terminal states
  • find_stale_running_task_ids picks up tasks whose worker died
  • requeue moves stale running back to queued (clears worker fields)
  • recover_stale_running_tasks integrates the above + dispatches
    a worker for each (worker dispatch is monkey-patched in tests)
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from inventory import store
from orchestrator import tasks


# Reuse the tmp_inventory fixture from conftest.py — it sets DATABASE_URL
# to '' (force SQLite), creates a temp DB, and runs init_db() which now
# also creates the phase7_tasks table.


# ─── enqueue + claim ──────────────────────────────────────────────────────

def test_enqueue_returns_id_and_creates_queued_row(tmp_inventory):
    task_id = tasks.enqueue(
        domain='enq.com',
        kind=tasks.TASK_KIND_PATH_A,
        request={'channel': 'C1', 'target_domain': 'enq.com'},
    )
    assert isinstance(task_id, int) and task_id > 0

    # Read back the row
    with store._conn() as c:
        cur = store._execute(
            c,
            'SELECT id, domain, kind, status, attempt FROM phase7_tasks '
            'WHERE id = ?',
            (task_id,),
        )
        row = cur.fetchone()
    assert row['domain'] == 'enq.com'
    assert row['kind'] == tasks.TASK_KIND_PATH_A
    assert row['status'] == tasks.TASK_QUEUED
    assert row['attempt'] == 0


def test_enqueue_rejects_unknown_kind(tmp_inventory):
    with pytest.raises(ValueError):
        tasks.enqueue(
            domain='x.com', kind='garbage', request={'foo': 'bar'},
        )


def test_claim_transitions_queued_to_running(tmp_inventory):
    task_id = tasks.enqueue(
        domain='claim.com', kind=tasks.TASK_KIND_PATH_B,
        request={'target_domain': 'claim.com'},
    )
    claimed = tasks.claim(task_id)
    assert claimed.id == task_id
    assert claimed.domain == 'claim.com'
    assert claimed.kind == tasks.TASK_KIND_PATH_B
    assert claimed.attempt == 1  # bumped on claim

    with store._conn() as c:
        cur = store._execute(
            c,
            'SELECT status, started_at, heartbeat_at, worker_id '
            'FROM phase7_tasks WHERE id = ?',
            (task_id,),
        )
        row = cur.fetchone()
    assert row['status'] == tasks.TASK_RUNNING
    assert row['started_at'] is not None
    assert row['heartbeat_at'] is not None
    assert row['worker_id'] is not None


def test_claim_lost_when_already_claimed(tmp_inventory):
    """Second claim of the same task must raise — the atomic guard
    prevents double-execution of a single workflow."""
    task_id = tasks.enqueue(
        domain='race.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'race.com'},
    )
    tasks.claim(task_id)  # first wins
    with pytest.raises(tasks.TaskClaimLost):
        tasks.claim(task_id)


def test_claim_returns_request_dict(tmp_inventory):
    payload = {
        'channel': 'C42',
        'message_ts': '1234.5',
        'target_domain': 'payload.com',
        'lander_url': 'https://src.com/lander/',
    }
    task_id = tasks.enqueue(
        domain='payload.com', kind=tasks.TASK_KIND_PATH_A,
        request=payload,
    )
    claimed = tasks.claim(task_id)
    assert claimed.request == payload


# ─── heartbeat ────────────────────────────────────────────────────────────

def test_heartbeat_updates_heartbeat_at_without_changing_status(tmp_inventory):
    task_id = tasks.enqueue(
        domain='hb.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'hb.com'},
    )
    tasks.claim(task_id)
    with store._conn() as c:
        cur = store._execute(
            c, 'SELECT heartbeat_at FROM phase7_tasks WHERE id = ?',
            (task_id,),
        )
        first_hb = cur.fetchone()['heartbeat_at']

    # Sleep enough that SQLite's second-resolution timestamp ticks.
    time.sleep(1.1)
    tasks.heartbeat(task_id)

    with store._conn() as c:
        cur = store._execute(
            c,
            'SELECT status, heartbeat_at FROM phase7_tasks WHERE id = ?',
            (task_id,),
        )
        after = cur.fetchone()
    assert after['status'] == tasks.TASK_RUNNING
    assert after['heartbeat_at'] >= first_hb


def test_heartbeat_no_op_on_non_running_task(tmp_inventory):
    """Heartbeating a queued or done task is a silent no-op — the
    UPDATE's WHERE status='running' clause makes it impossible to
    accidentally resurrect a finished task."""
    task_id = tasks.enqueue(
        domain='hb-noop.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'hb-noop.com'},
    )
    # Don't claim — task is still queued.
    tasks.heartbeat(task_id)  # must not raise

    with store._conn() as c:
        cur = store._execute(
            c, 'SELECT status, heartbeat_at FROM phase7_tasks WHERE id = ?',
            (task_id,),
        )
        row = cur.fetchone()
    assert row['status'] == tasks.TASK_QUEUED
    assert row['heartbeat_at'] is None  # untouched


# ─── mark_done / mark_failed ──────────────────────────────────────────────

def test_mark_done_records_atom_task_id_and_finishes(tmp_inventory):
    task_id = tasks.enqueue(
        domain='done.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'done.com'},
    )
    tasks.claim(task_id)
    tasks.mark_done(task_id, atom_task_id='atom-task-abc')

    with store._conn() as c:
        cur = store._execute(
            c,
            'SELECT status, atom_task_id, finished_at, error '
            'FROM phase7_tasks WHERE id = ?',
            (task_id,),
        )
        row = cur.fetchone()
    assert row['status'] == tasks.TASK_DONE
    assert row['atom_task_id'] == 'atom-task-abc'
    assert row['finished_at'] is not None
    assert row['error'] is None


def test_mark_failed_records_error(tmp_inventory):
    task_id = tasks.enqueue(
        domain='fail.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'fail.com'},
    )
    tasks.claim(task_id)
    tasks.mark_failed(task_id, 'Cert validation timed out')

    with store._conn() as c:
        cur = store._execute(
            c,
            'SELECT status, error, finished_at FROM phase7_tasks '
            'WHERE id = ?',
            (task_id,),
        )
        row = cur.fetchone()
    assert row['status'] == tasks.TASK_FAILED
    assert 'Cert validation timed out' in row['error']
    assert row['finished_at'] is not None


def test_mark_failed_truncates_long_errors(tmp_inventory):
    """The `error` column is bounded — a 100k-char traceback shouldn't
    fill the row."""
    task_id = tasks.enqueue(
        domain='big-err.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'big-err.com'},
    )
    tasks.claim(task_id)
    huge = 'x' * 100_000
    tasks.mark_failed(task_id, huge)

    with store._conn() as c:
        cur = store._execute(
            c, 'SELECT error FROM phase7_tasks WHERE id = ?', (task_id,),
        )
        row = cur.fetchone()
    assert len(row['error']) <= 1000


# ─── stale recovery ───────────────────────────────────────────────────────

def _set_started_at_and_heartbeat(task_id, *, started_at, heartbeat_at):
    """Helper for tests — directly mutate the timestamps to simulate
    a worker that died at a known point in the past.

    Uses the same ``YYYY-MM-DD HH:MM:SS`` format the production code
    uses in find_stale_running_task_ids so SQLite text comparisons
    line up correctly.
    """
    fmt = '%Y-%m-%d %H:%M:%S'
    started = started_at.strftime(fmt) if started_at is not None else None
    hb = heartbeat_at.strftime(fmt) if heartbeat_at is not None else None
    sql = (
        'UPDATE phase7_tasks SET started_at = ?, heartbeat_at = ? '
        'WHERE id = ?'
    )
    with store._conn() as c:
        store._execute(c, sql, (started, hb, task_id))


def test_find_stale_picks_up_running_task_with_old_heartbeat(tmp_inventory):
    task_id = tasks.enqueue(
        domain='stale-hb.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'stale-hb.com'},
    )
    tasks.claim(task_id)

    # Simulate a worker that's been silent for 5 minutes.
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    _set_started_at_and_heartbeat(
        task_id,
        started_at=five_min_ago,
        heartbeat_at=five_min_ago,
    )

    stale_ids = tasks.find_stale_running_task_ids()
    assert task_id in stale_ids


def test_find_stale_picks_up_running_task_with_no_heartbeat_after_grace(
    tmp_inventory,
):
    """Worker that crashed BEFORE its first heartbeat but long enough
    ago that we trust it's dead."""
    task_id = tasks.enqueue(
        domain='no-hb.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'no-hb.com'},
    )
    tasks.claim(task_id)
    two_min_ago = datetime.now(timezone.utc) - timedelta(minutes=2)
    _set_started_at_and_heartbeat(
        task_id, started_at=two_min_ago, heartbeat_at=None,
    )
    stale_ids = tasks.find_stale_running_task_ids()
    assert task_id in stale_ids


def test_find_stale_does_not_pick_up_recent_running_task(tmp_inventory):
    """A worker whose heartbeat is fresh must NOT be requeued."""
    task_id = tasks.enqueue(
        domain='alive.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'alive.com'},
    )
    tasks.claim(task_id)
    # Default state from claim: heartbeat_at = NOW(). Should not be stale.
    stale_ids = tasks.find_stale_running_task_ids()
    assert task_id not in stale_ids


def test_find_stale_does_not_pick_up_done_or_failed(tmp_inventory):
    """Terminal states (done / failed) are out of scope for recovery —
    they finished, even if a long time ago."""
    done_id = tasks.enqueue(
        domain='old-done.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'old-done.com'},
    )
    tasks.claim(done_id)
    tasks.mark_done(done_id)

    failed_id = tasks.enqueue(
        domain='old-fail.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'old-fail.com'},
    )
    tasks.claim(failed_id)
    tasks.mark_failed(failed_id, 'something blew up')

    stale_ids = tasks.find_stale_running_task_ids()
    assert done_id not in stale_ids
    assert failed_id not in stale_ids


def test_requeue_moves_running_back_to_queued_clearing_worker_fields(
    tmp_inventory,
):
    task_id = tasks.enqueue(
        domain='requeue.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'requeue.com'},
    )
    tasks.claim(task_id)
    assert tasks.requeue(task_id) is True

    with store._conn() as c:
        cur = store._execute(
            c,
            'SELECT status, worker_id, heartbeat_at, started_at '
            'FROM phase7_tasks WHERE id = ?',
            (task_id,),
        )
        row = cur.fetchone()
    assert row['status'] == tasks.TASK_QUEUED
    assert row['worker_id'] is None
    assert row['heartbeat_at'] is None
    assert row['started_at'] is None


def test_requeue_only_affects_running_rows(tmp_inventory):
    """Requeueing a queued task is a no-op — the WHERE clause guards
    against accidentally re-requeueing the same task forever."""
    task_id = tasks.enqueue(
        domain='already-q.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'already-q.com'},
    )
    assert tasks.requeue(task_id) is False


def test_recover_stale_running_tasks_dispatches_workers(
    tmp_inventory, monkeypatch,
):
    """Integration: stale row → recovery requeues + dispatches a fresh
    worker. We monkey-patch dispatch_worker_for to record calls without
    actually spawning threads."""
    # Insert a stale 'running' task.
    task_id = tasks.enqueue(
        domain='recover.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'recover.com'},
    )
    tasks.claim(task_id)
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    _set_started_at_and_heartbeat(
        task_id, started_at=five_min_ago, heartbeat_at=five_min_ago,
    )

    dispatched = []
    from orchestrator import tasks_runner
    monkeypatch.setattr(
        tasks_runner, 'dispatch_worker_for',
        lambda tid: dispatched.append(tid),
    )

    requeued = tasks.recover_stale_running_tasks()

    assert task_id in requeued
    assert task_id in dispatched
    # And the row is back in queued state.
    with store._conn() as c:
        cur = store._execute(
            c, 'SELECT status FROM phase7_tasks WHERE id = ?', (task_id,),
        )
        assert cur.fetchone()['status'] == tasks.TASK_QUEUED


def test_recover_is_idempotent_on_empty_queue(tmp_inventory):
    """No queued / running tasks → recovery returns []."""
    assert tasks.recover_stale_running_tasks() == []


def test_recover_does_not_touch_already_done_tasks(tmp_inventory, monkeypatch):
    """Even if a task's heartbeat is ancient, status='done' rows must
    stay terminal — recovery is for in-flight work only."""
    task_id = tasks.enqueue(
        domain='ancient-done.com', kind=tasks.TASK_KIND_PATH_A,
        request={'target_domain': 'ancient-done.com'},
    )
    tasks.claim(task_id)
    tasks.mark_done(task_id)

    from orchestrator import tasks_runner
    monkeypatch.setattr(
        tasks_runner, 'dispatch_worker_for',
        lambda tid: pytest.fail(f'should NOT dispatch worker for done task {tid}'),
    )
    requeued = tasks.recover_stale_running_tasks()
    assert requeued == []

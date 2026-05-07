"""Tests for orchestrator.log_setup — structured JSON logging
(audit #10 fix).

Covers:
  • JSONFormatter emits valid JSON with the standard top-level fields
    (ts, level, logger, msg) on every record
  • log_event(...) merges the `event` field plus arbitrary kwargs
  • logger.exception() captures traceback into the `exc` field
  • configure_logging() is idempotent (no duplicate handlers)
  • non-serialisable extras don't crash the formatter
  • the `logger` field reflects the calling module's __name__
"""
import io
import json
import logging

import pytest

from orchestrator.log_setup import (
    JSONFormatter,
    configure_logging,
    log_event,
)


# ─── helpers ──────────────────────────────────────────────────────────────

def _capture_logs():
    """Return (stream, handler) where handler writes JSON to stream.

    Tests use this to assert the exact format of emitted log lines
    without depending on caplog's plain-text default formatting.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    return stream, handler


def _last_line_json(stream):
    lines = [ln for ln in stream.getvalue().split('\n') if ln.strip()]
    if not lines:
        raise AssertionError(
            f'no log lines captured. raw stream: {stream.getvalue()!r}'
        )
    return json.loads(lines[-1])


# ─── JSONFormatter shape ──────────────────────────────────────────────────

def test_formatter_emits_top_level_fields():
    stream, handler = _capture_logs()
    log = logging.getLogger('test_log_setup_basic')
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.info('hello world')

    record = _last_line_json(stream)
    assert record['msg'] == 'hello world'
    assert record['level'] == 'INFO'
    assert record['logger'] == 'test_log_setup_basic'
    # ts must be ISO-8601 with timezone offset.
    assert 'T' in record['ts']
    assert record['ts'].endswith('+00:00')


def test_formatter_includes_extras_passed_via_extra_kwarg():
    stream, handler = _capture_logs()
    log = logging.getLogger('test_log_setup_extras')
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.info('msg', extra={'event': 'demo', 'domain': 'x.com', 'count': 42})

    record = _last_line_json(stream)
    assert record['event'] == 'demo'
    assert record['domain'] == 'x.com'
    assert record['count'] == 42


def test_formatter_captures_traceback_on_exception():
    stream, handler = _capture_logs()
    log = logging.getLogger('test_log_setup_exc')
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        raise ValueError('boom')
    except ValueError:
        log.exception('caught it')

    record = _last_line_json(stream)
    assert record['msg'] == 'caught it'
    assert record['level'] == 'ERROR'
    assert 'exc' in record
    assert 'ValueError: boom' in record['exc']
    assert 'Traceback' in record['exc']


def test_formatter_handles_non_serialisable_extra_values():
    """An extra value like an open socket or a custom class must NOT
    crash the formatter — it should fall back to repr/str rather than
    silently dropping the line."""
    stream, handler = _capture_logs()
    log = logging.getLogger('test_log_setup_nonser')
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    class NotSerialisable:
        def __repr__(self):
            return 'NotSer()'

    log.info('x', extra={'event': 'e', 'thing': NotSerialisable()})
    record = _last_line_json(stream)
    # default=str converts via str() which falls through to __repr__
    # for plain objects.
    assert record['thing'] == 'NotSer()'


# ─── log_event helper ─────────────────────────────────────────────────────

def test_log_event_emits_event_field_with_calling_module_name():
    stream, handler = _capture_logs()
    # Attach handler to root so log_event's getLogger(__name__) reaches it.
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        log_event('phase7_started', domain='x.com', task_id=42)

        record = _last_line_json(stream)
        assert record['event'] == 'phase7_started'
        assert record['msg'] == 'phase7_started'
        assert record['domain'] == 'x.com'
        assert record['task_id'] == 42
        # Calling module is THIS file under pytest's normalised path.
        assert record['logger'].endswith('test_log_setup')
    finally:
        root.removeHandler(handler)


def test_log_event_supports_warning_level():
    stream, handler = _capture_logs()
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        log_event('weird_thing', level=logging.WARNING, detail='x')
        record = _last_line_json(stream)
        assert record['level'] == 'WARNING'
    finally:
        root.removeHandler(handler)


def test_log_event_rejects_reserved_field_name():
    """Using a key that collides with a LogRecord internal (e.g.
    'message') must raise — better to fail loud during dev than silently
    emit a corrupted log line."""
    root = logging.getLogger()
    with pytest.raises(KeyError):
        log_event('boom', message='should-not-be-allowed')


# ─── configure_logging idempotency ────────────────────────────────────────

def test_configure_logging_is_idempotent():
    """Calling configure_logging() multiple times must NOT install
    duplicate handlers — otherwise tests + repeat boots would emit
    every line N times."""
    configure_logging()
    configure_logging()
    configure_logging()

    root = logging.getLogger()
    json_handlers = [
        h for h in root.handlers
        if getattr(h, '_atom_orch_json_handler', False)
    ]
    assert len(json_handlers) == 1


def test_configure_logging_does_not_remove_other_handlers():
    """A test framework's caplog handler (or any other handler)
    must survive configure_logging — we only own the one we marked."""
    foreign = logging.StreamHandler(io.StringIO())  # unmarked
    root = logging.getLogger()
    root.addHandler(foreign)
    try:
        configure_logging()
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)

"""Structured JSON logging for atom-orchestrator (audit #10 fix).

Every meaningful workflow event becomes a single line of JSON on
stdout. Render's log viewer aggregates stdout per-service so the
output is grep-able and (when piped to a log drain) feeds metrics /
alerts off a single source of truth.

Every record emits (at minimum):
  ts      — ISO 8601 UTC, millisecond precision
  level   — DEBUG | INFO | WARNING | ERROR | CRITICAL
  logger  — caller's module name
  msg     — human-readable message (or the event name when emitted
            via log_event)

Records emitted via :func:`log_event` also carry:
  event   — machine-grepable category (e.g. 'workflow_failed',
            'task_enqueued', 'atom_login_failed'). Operators can
            jq-filter on this field to slice logs by event type.
  + every extra kwarg you pass.

Records emitted via the standard ``logger.exception(...)`` path
include ``exc`` with the formatted traceback so a single log line
contains the entire failure context.

Usage:
  # at module top:
  from orchestrator.log_setup import log_event
  log_event('phase7_started', domain=req.target_domain, task_id=tid)

  # in error paths:
  try:
      ...
  except Exception:
      logger.exception('phase7 worker crashed', extra={'task_id': tid})
"""
from __future__ import annotations

import inspect
import json
import logging
import sys
from datetime import datetime, timezone


# Sentinel attribute used to detect a previously installed handler so
# configure_logging is idempotent across tests + repeat boots.
_HANDLER_MARKER = '_atom_orch_json_handler'


# Standard LogRecord attribute names — anything in this set is
# already serialised by the top-level fields and must NOT be
# re-emitted as a caller-supplied extra. Anything OUTSIDE this set
# (and not starting with '_') is treated as a structured field
# from the caller and merged into the JSON line.
_LOGRECORD_RESERVED = {
    'name', 'msg', 'args', 'levelname', 'levelno',
    'pathname', 'filename', 'module', 'exc_info', 'exc_text',
    'stack_info', 'lineno', 'funcName', 'created', 'msecs',
    'relativeCreated', 'thread', 'threadName', 'processName',
    'process', 'message', 'asctime', 'taskName',
}


class JSONFormatter(logging.Formatter):
    """One-line JSON formatter for stdout.

    Defensively handles non-serialisable extras (uses ``default=str``)
    and dumps a fallback marker if even that fails — a logger should
    NEVER drop a line silently because a kwarg was unhashable.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        ts = datetime.fromtimestamp(
            record.created, tz=timezone.utc,
        ).isoformat(timespec='milliseconds')
        payload = {
            'ts': ts,
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in _LOGRECORD_RESERVED or k.startswith('_'):
                continue
            payload[k] = v
        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)

        try:
            return json.dumps(payload, default=str)
        except Exception:  # pragma: no cover — defensive only
            return json.dumps({
                'ts': ts, 'level': record.levelname,
                'logger': record.name,
                'msg': record.getMessage(),
                'log_format_error': True,
            })


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON handler on the root logger.

    Idempotent — removes any previously installed JSON handler before
    adding the new one so tests + repeated calls don't duplicate
    output. Leaves non-JSON handlers (e.g. pytest's caplog) untouched.

    Call once from app.py:create_app() at boot, BEFORE the first
    log_event call so even the migration / boot sequence emits in
    JSON form.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_MARKER, False):
            root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)
    root.setLevel(level)


def log_event(event: str, *, level: int = logging.INFO, **fields) -> None:
    """Emit a structured event line.

    The ``event`` argument doubles as the human-readable message AND
    the machine-grepable category. Caller passes any number of
    structured kwargs; they all appear as top-level JSON fields next
    to the standard ts/level/logger/msg/event.

    The logger name is taken from the caller's ``__name__`` so log
    lines correctly attribute the event to the originating module.

    Field-name collisions with LogRecord internals (e.g. ``message``,
    ``asctime``) raise a TypeError from the stdlib — the wrapper
    deliberately doesn't shadow that to keep mistakes loud during
    development.
    """
    # `event` itself goes into extras so JSONFormatter merges it
    # alongside the user-supplied fields. The msg arg is set to the
    # same value so non-JSON consumers (legacy log inspectors, the
    # repr in a debugger) still see something meaningful.
    extras = {'event': event, **fields}
    logger_name = _caller_module_name()
    logging.getLogger(logger_name).log(level, event, extra=extras)


def _caller_module_name() -> str:
    """Return the ``__name__`` of the module that invoked log_event.

    Falls back to a stable string when introspection fails (e.g. when
    called from an interactive REPL).
    """
    frame = inspect.currentframe()
    # Walk back two frames: 1) _caller_module_name, 2) log_event,
    # 3) the actual caller.
    try:
        if frame is None or frame.f_back is None or frame.f_back.f_back is None:
            return 'atom_orchestrator'
        caller = frame.f_back.f_back
        return caller.f_globals.get('__name__', 'atom_orchestrator')
    finally:
        del frame

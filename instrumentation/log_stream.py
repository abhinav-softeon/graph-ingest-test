"""In-memory tail of the build's own log records, for display in the UI.

The backend already logs every step to stdout (sail_core.logger -> logging.
basicConfig), which is what `docker compose logs` shows. The UI had no equivalent:
it rendered a single `stage_state["text"]` line that each new step OVERWROTE, so
by the time a build finished — or failed — everything it had said was gone. On a
run measured in tens of minutes that is the difference between watching progress
and watching a spinner.

This attaches a second handler to the root logger that keeps the last N formatted
records in a ring buffer the UI can poll. Design constraints that matter here:

* BOUNDED, always. This process runs against a hard container memory cap, so an
  unbounded log list is not an option — a `deque(maxlen=...)` drops the oldest
  record for free rather than growing. Long messages are truncated too, since a
  single log call can carry a large repr.
* Thread-safe by construction. The build runs on a worker thread while Streamlit
  renders on the main one. `deque.append` with a maxlen is atomic, so the reader
  never needs a lock and the writer never blocks on one.
* Additive. It does not replace, reconfigure or re-level the stdout handler —
  terminal output is unchanged, and losing this buffer would not lose any logging.
"""
from __future__ import annotations

import logging
from collections import deque

# ~4000 lines is minutes of build output at the rate _beat/resolve emit, and costs
# well under a megabyte even with long lines. Old records fall off the left.
_MAX_LINES = 4000
_MAX_LINE_CHARS = 400

_BUFFER: deque[str] = deque(maxlen=_MAX_LINES)
_HANDLER: "_RingHandler | None" = None

# Chatty third-party loggers that would drown the build's own output. Streamlit's
# own logger is already levelled down in ui/app.py; these are the rest.
_MUTED_PREFIXES = ("streamlit", "watchdog", "urllib3", "PIL", "matplotlib", "numba")


class _RingHandler(logging.Handler):
    """Formats each record and appends it to the module-level ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name or ""
            if name.startswith(_MUTED_PREFIXES):
                return
            msg = self.format(record)
            if len(msg) > _MAX_LINE_CHARS:
                msg = msg[:_MAX_LINE_CHARS] + " …[truncated]"
            _BUFFER.append(msg)
        except Exception:  # noqa: BLE001
            # A logging handler must never raise into the code that logged. A
            # broken log line is not worth failing a build over.
            pass


def install(level: int = logging.INFO) -> None:
    """Attach the ring handler to the root logger. Idempotent.

    Root, not a specific logger, so it captures graph_core, ingest, analysis and
    anything else that logs during a build without needing to enumerate them.
    """
    global _HANDLER
    if _HANDLER is not None:
        return
    handler = _RingHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger()
    root.addHandler(handler)
    # Only ever RAISE verbosity: root may already be at DEBUG, and lowering it
    # here would silently reduce what the terminal gets. This handler is additive
    # and must not change existing output.
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    _HANDLER = handler


def clear() -> None:
    """Drop buffered lines — call at the start of a run so the panel shows that
    run, not the previous one's tail."""
    _BUFFER.clear()


def tail(limit: int = 400) -> list[str]:
    """The most recent ``limit`` lines, oldest first.

    Snapshotted into a list rather than returned lazily: the worker thread keeps
    appending while the UI renders, and iterating a deque that is being mutated
    can raise.
    """
    if limit <= 0:
        return []
    snapshot = list(_BUFFER)
    return snapshot[-limit:]


def line_count() -> int:
    """Lines currently buffered (capped at _MAX_LINES)."""
    return len(_BUFFER)


def dropped_hint() -> str:
    """Whether the ring has wrapped, so the UI can say so instead of implying the
    panel holds the whole run."""
    return (f"buffer full ({_MAX_LINES} lines) — older lines dropped; "
            f"full history is in the container logs"
            if len(_BUFFER) >= _MAX_LINES else "")

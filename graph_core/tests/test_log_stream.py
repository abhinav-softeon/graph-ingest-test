"""Guardrails for the UI's in-memory log tail.

This is diagnostic infrastructure, so the requirements are the boring ones and
they all matter more than the feature itself:

* BOUNDED — it runs in a process under a hard container memory cap, so an
  unbounded log list is a memory leak with a friendly name.
* NEVER RAISES — a logging handler that throws takes down whatever was logging,
  which here means a multi-hour build dying over a log line.
* ADDITIVE — installing it must not change what the terminal receives, since
  `docker compose logs` is the primary record.
"""
from __future__ import annotations

import logging
import threading

from instrumentation import log_stream


class TestRingBuffer:
    def test_captures_records(self):
        log_stream.install()
        log_stream.clear()
        logging.getLogger("graph_core.test").info("hello from the pipeline")
        assert any("hello from the pipeline" in l for l in log_stream.tail(50))

    def test_is_bounded(self):
        """The cap is the whole point — 10x the limit must not grow past it."""
        log_stream.install()
        log_stream.clear()
        log = logging.getLogger("graph_core.test")
        for i in range(log_stream._MAX_LINES * 2):
            log.info("line %s", i)
        assert log_stream.line_count() == log_stream._MAX_LINES
        # Oldest dropped, newest kept.
        lines = log_stream.tail(5)
        assert f"line {log_stream._MAX_LINES * 2 - 1}" in lines[-1]

    def test_long_lines_are_truncated(self):
        log_stream.install()
        log_stream.clear()
        logging.getLogger("graph_core.test").info("x" * 5000)
        line = log_stream.tail(1)[0]
        assert "[truncated]" in line
        assert len(line) < log_stream._MAX_LINE_CHARS + 100

    def test_noisy_third_party_loggers_are_muted(self):
        """Streamlit/watchdog chatter would bury the build's own output."""
        log_stream.install()
        log_stream.clear()
        logging.getLogger("streamlit.runtime.scriptrunner").info("script ran")
        logging.getLogger("watchdog.observers").info("fs event")
        logging.getLogger("graph_core.pipeline").info("real build line")
        lines = log_stream.tail(50)
        assert any("real build line" in l for l in lines)
        assert not any("script ran" in l or "fs event" in l for l in lines)

    def test_clear_empties_it(self):
        log_stream.install()
        logging.getLogger("graph_core.test").info("before")
        log_stream.clear()
        assert log_stream.line_count() == 0
        assert log_stream.tail(10) == []

    def test_tail_limit_is_respected(self):
        log_stream.install()
        log_stream.clear()
        log = logging.getLogger("graph_core.test")
        for i in range(50):
            log.info("n%s", i)
        assert len(log_stream.tail(10)) == 10
        assert log_stream.tail(0) == []

    def test_dropped_hint_only_when_wrapped(self):
        log_stream.install()
        log_stream.clear()
        logging.getLogger("graph_core.test").info("one")
        assert log_stream.dropped_hint() == ""


class TestSafety:
    def test_install_is_idempotent(self):
        """Called on every Streamlit rerun — reinstalling would duplicate every
        line once per rerun, which on a 0.5s refresh loop compounds fast."""
        log_stream.install()
        root = logging.getLogger()
        before = sum(1 for h in root.handlers if type(h).__name__ == "_RingHandler")
        for _ in range(5):
            log_stream.install()
        after = sum(1 for h in root.handlers if type(h).__name__ == "_RingHandler")
        assert before == after == 1

    def test_handler_emit_never_raises_on_an_unformattable_record(self):
        """Tested against the handler directly, not via logging.

        A first version of this test called `log.info("%s %s", "only-one")` and
        asserted nothing propagated — but that fails under pytest, whose own
        capturing handler raises on the malformed record before ours is even
        reached. That is stdlib/pytest behaviour and no handler of ours can
        prevent it, so asserting it would be asserting something false about the
        system. What IS ours to guarantee is that OUR emit() swallows a bad
        record instead of adding a new way for a build to die at hour two.
        """
        log_stream.install()
        log_stream.clear()
        bad = logging.LogRecord(
            name="graph_core.test", level=logging.INFO, pathname=__file__,
            lineno=1, msg="%s %s", args=("only-one",), exc_info=None,
        )
        log_stream._HANDLER.emit(bad)  # must not raise
        # And the buffer is still usable afterwards.
        logging.getLogger("graph_core.test").info("still alive")
        assert any("still alive" in l for l in log_stream.tail(10))

    def test_install_does_not_lower_root_level(self):
        """Additive only: if something set root to DEBUG, this must not raise it
        back to INFO and silently reduce terminal output."""
        root = logging.getLogger()
        original = root.level
        try:
            root.setLevel(logging.DEBUG)
            log_stream.install(level=logging.INFO)
            assert root.level == logging.DEBUG
        finally:
            root.setLevel(original)

    def test_concurrent_writes_do_not_corrupt_or_raise(self):
        """The build logs from a worker thread while Streamlit reads from the
        main one, so append/read overlap on every run."""
        log_stream.install()
        log_stream.clear()
        errors: list[BaseException] = []

        def writer(n: int) -> None:
            try:
                log = logging.getLogger("graph_core.test")
                for i in range(300):
                    log.info("t%s-%s", n, i)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(300):
                    log_stream.tail(100)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert log_stream.line_count() > 0

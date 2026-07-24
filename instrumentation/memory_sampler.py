"""Background memory sampler for this experimentation app.

There's no Docker/cgroup stats available on the target machine, so the app
has to watch its own memory. Samples RSS of the MAIN process *and* every
child worker process (ProcessPoolExecutor workers spawned during extraction/
resolve) every ``interval_s`` seconds on a daemon thread — sampling only the
main process would systematically under-report peak memory during any
parallel stage, which is exactly the number this app exists to measure.
"""
from __future__ import annotations

import threading

import psutil


class MemorySampler:
    """Tracks running-peak RSS (this process + recursive children).

    Usage::
        sampler = MemorySampler(interval_s=0.5)
        sampler.start()
        ...
        sampler.stop()
        sampler.peak_mb()
    """

    def __init__(self, interval_s: float = 0.5):
        self.interval_s = interval_s
        self._proc = psutil.Process()
        self._peak_mb = 0.0
        self._current_mb = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _sample_once(self) -> float:
        try:
            total = self._proc.memory_info().rss
            for child in self._proc.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return self._current_mb
        return total / (1024 * 1024)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            mb = self._sample_once()
            with self._lock:
                self._current_mb = mb
                if mb > self._peak_mb:
                    self._peak_mb = mb
            self._stop_event.wait(self.interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        # One synchronous sample immediately so peak_mb()/current_mb() are
        # meaningful even if stop() is called before the loop's first tick.
        mb = self._sample_once()
        self._current_mb = mb
        self._peak_mb = mb
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="graph-memory-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 2)
            self._thread = None

    def peak_mb(self) -> float:
        with self._lock:
            return self._peak_mb

    def current_mb(self) -> float:
        with self._lock:
            return self._current_mb

    def reset_peak(self) -> None:
        """Reset the running peak to the current reading — lets a caller
        additionally track a peak PER STAGE (call at each stage boundary) on
        top of the overall run peak (tracked separately by the caller)."""
        with self._lock:
            self._peak_mb = self._current_mb

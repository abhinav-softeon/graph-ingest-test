"""Per-run experiment report: per-stage time+memory, overall totals, the
exact config/toggles used, and result counts — one timestamped JSON file per
run plus a summary row appended to an index, so raw/chunked/parallel/
checkpointed runs can be compared against each other later.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from .memory_sampler import MemorySampler

_RUNS_DIR_DEFAULT = "runs"


@dataclass
class StageRecord:
    name: str
    start_ts: float
    end_ts: float = 0.0
    mem_start_mb: float = 0.0
    mem_end_mb: float = 0.0
    mem_peak_mb: float = 0.0

    @property
    def duration_s(self) -> float:
        return round((self.end_ts or time.time()) - self.start_ts, 3)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "duration_s": self.duration_s,
            "mem_start_mb": round(self.mem_start_mb, 1),
            "mem_end_mb": round(self.mem_end_mb, 1),
            "mem_peak_mb": round(self.mem_peak_mb, 1),
        }


class RunReport:
    """Wraps an ``on_stage(stage, detail)`` callback: records one
    StageRecord per distinct stage name (first call -> start, every call ->
    running end/mem), samples the shared MemorySampler each time, and writes
    the final JSON + index.jsonl summary on ``finish()``.
    """

    def __init__(
        self,
        run_id: str,
        project: str,
        config: dict[str, Any],
        sampler: MemorySampler,
        runs_dir: str = _RUNS_DIR_DEFAULT,
        inner_on_stage: Optional[Callable[[str, dict], None]] = None,
    ):
        self.run_id = run_id
        self.project = project
        self.config = config
        self.sampler = sampler
        self.runs_dir = runs_dir
        self.inner_on_stage = inner_on_stage
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.result_counts: dict[str, Any] = {}
        self._stages: dict[str, StageRecord] = {}
        self._order: list[str] = []

    def on_stage(self, stage: str, detail: dict) -> None:
        mb = self.sampler.current_mb()
        rec = self._stages.get(stage)
        if rec is None:
            rec = StageRecord(name=stage, start_ts=time.time(), mem_start_mb=mb, mem_peak_mb=mb)
            self._stages[stage] = rec
            self._order.append(stage)
        rec.end_ts = time.time()
        rec.mem_end_mb = mb
        if mb > rec.mem_peak_mb:
            rec.mem_peak_mb = mb
        if self.inner_on_stage:
            self.inner_on_stage(stage, {
                **detail,
                "current_mb": round(mb, 1),
                "stage_peak_mb": round(rec.mem_peak_mb, 1),
                "overall_peak_mb": round(self.sampler.peak_mb(), 1),
                "started_at": datetime.fromtimestamp(self.started_at).strftime("%H:%M:%S"),
                "elapsed_s": round(time.time() - self.started_at, 1),
            })

    def set_result_counts(self, **counts: Any) -> None:
        self.result_counts.update(counts)

    def finish(self, error: Optional[str] = None) -> dict:
        self.finished_at = time.time()
        self.error = error
        report = {
            "run_id": self.run_id,
            "project": self.project,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_duration_s": round(self.finished_at - self.started_at, 3),
            "overall_peak_mb": round(self.sampler.peak_mb(), 1),
            "error": self.error,
            "config": self.config,
            "result_counts": self.result_counts,
            "stages": [self._stages[name].to_dict() for name in self._order],
        }
        os.makedirs(self.runs_dir, exist_ok=True)
        path = os.path.join(self.runs_dir, f"{self.run_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)

        summary = {
            "run_id": self.run_id,
            "project": self.project,
            "started_at": self.started_at,
            "total_duration_s": report["total_duration_s"],
            "overall_peak_mb": report["overall_peak_mb"],
            "error": self.error,
            **{f"cfg.{k}": v for k, v in self.config.items()},
            **{f"result.{k}": v for k, v in self.result_counts.items() if not isinstance(v, dict)},
        }
        index_path = os.path.join(self.runs_dir, "index.jsonl")
        with open(index_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, default=str) + "\n")
        return report

"""CLI entrypoint: ingest a zip into Neo4j with every mode/concurrency
setting exposed as a flag. Sets the corresponding GRAPH_*/NEO4J_* env vars
(same env-var-driven config graph_core already uses) before calling into
ingest.build, then prints the resulting run report.

Example (raw / no chunking / sequential / no checkpoint):
    python cli.py --zip repo.zip --project demo --no-chunking --no-parallel-extraction

Example (chunked + parallel + checkpointed, comparing against the above):
    python cli.py --zip repo.zip --project demo --chunking --parallel-extraction --checkpointing
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ingest.build import build_graph_from_zip
from ingest.toggles import apply_ingestion_toggles


def _bool_flag(parser: argparse.ArgumentParser, name: str, default: bool, help_on: str, help_off: str = "") -> None:
    dest = name.replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=dest, action="store_true", help=help_on)
    group.add_argument(f"--no-{name}", dest=dest, action="store_false", help=help_off or f"disable {name}")
    parser.set_defaults(**{dest: default})


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ingest a zip of source code into Neo4j (memory/time experimentation harness).")
    p.add_argument("--zip", required=True, help="Path to a .zip of source code")
    p.add_argument("--project", required=True, help="Project/namespace name")

    _bool_flag(p, "chunking", True, "Batch discover+extract (default)", "Raw: one giant batch, no chunking")
    p.add_argument("--extract-batch-size", type=int, default=None, help="Files per batch when chunking is on (default 2000)")

    _bool_flag(p, "parallel-extraction", True, "Parallel extraction across processes (default)", "Force sequential (1 worker) extraction")
    p.add_argument("--extract-workers", type=int, default=None, help="Process-pool size when parallel-extraction is on (default: CPU count)")

    _bool_flag(p, "checkpointing", False, "Enable disk-spill checkpointing", "Disable checkpointing (default)")
    p.add_argument("--checkpoint-root", default=None, help="Directory for checkpoint spill (default ./.graph_checkpoints)")
    _bool_flag(p, "streaming-ingest", False, "Resolve against a slim node projection + stream refs from disk (requires --checkpointing)", "Disable streaming ingest (default)")

    _bool_flag(p, "javac", False,
               "Resolve Java CALLS with javac (type-precise) instead of heuristic name matching",
               "Disable the javac resolver (default, heuristic resolver only)")
    _bool_flag(p, "bytecode", False,
               "Read Java CALLS/READS/WRITES from compiled .class files and jars "
               "(exact bindings; also recovers lambdas, anonymous classes and "
               "static initializers as nodes)",
               "Disable the bytecode resolver (default)")
    _bool_flag(p, "extract-cache", True, "Enable local-disk extract cache (default)", "Disable extract cache")
    p.add_argument("--extract-cache-dir", default=None, help="Local dir for the extract cache (default ./.cache/graph_extract_cache)")

    p.add_argument("--cache-io-workers", type=int, default=None, help="Thread-pool size for extract-cache I/O (default 16)")
    p.add_argument("--zip-extract-workers", type=int, default=None, help="Thread-pool size for unzipping (default min(16, cpu*2))")
    p.add_argument("--write-workers", type=int, default=1, help="Thread-pool size for concurrent Neo4j write batches (default 1 = sequential)")
    p.add_argument("--write-batch-size", type=int, default=None, help="Rows per Neo4j write transaction (default 5000)")

    p.add_argument("--sample-interval", type=float, default=0.5, help="Memory-sampler poll interval in seconds (default 0.5)")
    p.add_argument("--runs-dir", default="runs", help="Directory for run reports (default ./runs)")

    # JOBLIB DUMP possibility (DISABLED — kept for re-enabling; see pipeline.py):

    p.add_argument("--neo4j-uri", default=None)
    p.add_argument("--neo4j-user", default=None)
    p.add_argument("--neo4j-password", default=None)
    p.add_argument("--neo4j-database", default=None)
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.neo4j_uri:
        os.environ["NEO4J_URI"] = args.neo4j_uri
    if args.neo4j_user:
        os.environ["NEO4J_USER"] = args.neo4j_user
    if args.neo4j_password:
        os.environ["NEO4J_PASSWORD"] = args.neo4j_password
    if args.neo4j_database:
        os.environ["NEO4J_DATABASE"] = args.neo4j_database
    if args.write_batch_size:
        os.environ["GRAPH_WRITE_BATCH_SIZE"] = str(args.write_batch_size)

    apply_ingestion_toggles(
        chunking=args.chunking,
        extract_batch_size=args.extract_batch_size,
        parallel_extraction=args.parallel_extraction,
        extract_workers=args.extract_workers,
        checkpointing=args.checkpointing,
        checkpoint_root=args.checkpoint_root,
        streaming_ingest=args.streaming_ingest,
        javac=args.javac,
        bytecode=args.bytecode,
        extract_cache=args.extract_cache,
        extract_cache_dir=args.extract_cache_dir,
        cache_io_workers=args.cache_io_workers,
        zip_extract_workers=args.zip_extract_workers,
        write_workers=args.write_workers,
    )

    def _print_stage(stage: str, detail: dict) -> None:
        print(f"[{stage}] {detail}")

    report = build_graph_from_zip(
        args.zip,
        args.project,
        on_stage=_print_stage,
        sample_interval_s=args.sample_interval,
        runs_dir=args.runs_dir,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

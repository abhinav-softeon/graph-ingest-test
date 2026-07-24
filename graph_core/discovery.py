"""Stage 0 — walk a repo, detect language, read + hash source files."""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

from .ids import body_hash
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

IGNORE_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "build", "target", "dist", "out", "bin", ".idea", ".gradle", ".mvn",
    ".pytest_cache", ".mypy_cache", "site-packages", "__MACOSX",
}


def _is_macos_cruft(filename: str) -> bool:
    """AppleDouble resource-fork file (``._Foo.java``) — a Mac-created zip
    mirrors every real file with one of these, same extension as the real
    file, so the EXT_LANG check alone doesn't exclude it."""
    return filename.startswith("._")

EXT_LANG = {
    ".java": "java", ".py": "python", ".sql": "sql",
    # JavaScript family (javascript grammar is JSX-aware)
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    # TypeScript (.tsx needs the tsx grammar)
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
}


@dataclass
class FileInfo:
    relpath: str
    abspath: str
    lang: str
    sha: str
    source: bytes


def discover(root: str, candidate_relpaths: list[str] | None = None) -> list[FileInfo]:
    """Walk a repo (or, if ``candidate_relpaths`` is given, skip the walk and
    read exactly that pre-computed list of relative paths instead).

    ``candidate_relpaths`` lets a caller that already walked the tree once
    (e.g. the upload-review flow, which scans for LLM-review-eligible files
    before this runs) avoid a second full directory walk over potentially
    tens of thousands of files. It must be a superset of every file this
    function would otherwise find via EXT_LANG — any extra (non-graph)
    extensions in it are simply skipped below, same as during a normal walk.
    """
    root = os.path.abspath(root)
    out: list[FileInfo] = []
    t0 = time.time()

    if candidate_relpaths is not None:
        for rel in candidate_relpaths:
            if _is_macos_cruft(rel.rsplit("/", 1)[-1]):
                continue
            ext = os.path.splitext(rel)[1]
            lang = EXT_LANG.get(ext)
            if not lang:
                continue
            abspath = os.path.join(root, *rel.split("/"))
            try:
                with open(abspath, "rb") as fh:
                    src = fh.read()
            except OSError:
                continue
            out.append(
                FileInfo(
                    relpath=rel,
                    abspath=abspath,
                    lang=lang,
                    sha=body_hash(src.decode("utf-8", "replace")),
                    source=src,
                )
            )
        _log.info("[graph_discover] read+hashed %s file(s) from %s candidate path(s) in %.3fs", len(out), len(candidate_relpaths), time.time() - t0)
        return out

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            if _is_macos_cruft(fn):
                continue
            ext = os.path.splitext(fn)[1]
            lang = EXT_LANG.get(ext)
            if not lang:
                continue
            abspath = os.path.join(dirpath, fn)
            try:
                with open(abspath, "rb") as fh:
                    src = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(abspath, root)
            out.append(
                FileInfo(
                    relpath=rel,
                    abspath=abspath,
                    lang=lang,
                    sha=body_hash(src.decode("utf-8", "replace")),
                    source=src,
                )
            )
    _log.info("[graph_discover] walked %s and found+hashed %s file(s) in %.3fs", root, len(out), time.time() - t0)
    return out


def build_manifest(root: str, candidate_relpaths: list[str] | None = None) -> dict[str, str]:
    """Discover the repo and collapse it to a ``{relpath: sha}`` map.

    Cheap to compute repeatedly (no source retained), useful for deciding
    whether anything in the codebase changed without holding file contents.
    """
    t0 = time.time()
    manifest = {
        fileinfo.relpath: fileinfo.sha
        for fileinfo in discover(root, candidate_relpaths=candidate_relpaths)
    }
    _log.info("[graph_discover] build_manifest: %s file(s) in %.3fs", len(manifest), time.time() - t0)
    return manifest


def codebase_hash(manifest: dict[str, str]) -> str:
    """Deterministic hash of a whole ``build_manifest`` result.

    Sorts by relpath so the result is stable regardless of dict/walk order,
    then hashes the same way ``body_hash`` does (sha1) so callers can do a
    single cheap comparison instead of diffing every file's sha.
    """
    lines = "\n".join(f"{relpath}:{sha}" for relpath, sha in sorted(manifest.items()))
    return hashlib.sha1(lines.encode("utf-8", "replace")).hexdigest()


def list_candidate_relpaths(root: str) -> list[str]:
    """Walk the repo and return matching relative paths only — no file reads.

    Used to enumerate files for chunked discovery (see pipeline.py's
    index_repo): knowing the full file list up front is cheap (just
    directory listing), but reading every file's content up front is what
    holds large memory for big repos — chunking calls discover() per-batch
    with a slice of this list instead, so at most one batch's worth of file
    content is resident at a time instead of the whole repo's.
    """
    root = os.path.abspath(root)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            if _is_macos_cruft(fn):
                continue
            ext = os.path.splitext(fn)[1]
            if ext not in EXT_LANG:
                continue
            abspath = os.path.join(dirpath, fn)
            out.append(os.path.relpath(abspath, root).replace("\\", "/"))
    return out

"""Stage 0 — walk a repo, detect language, read + hash source files."""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

from .artifacts import ARTIFACT_DIR_OVERRIDES, artifact_kind
from .ids import body_hash
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

IGNORE_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "build", "target", "dist", "out", "bin", ".idea", ".gradle", ".mvn",
    ".pytest_cache", ".mypy_cache", "site-packages", "__MACOSX",
}

# The artifact walk must enter the build-output directories the source walk
# excludes — `target/classes`, `build/classes`, `WEB-INF/classes` are where
# .class files live. Caches and vendored trees stay excluded either way: they
# hold third-party bytecode, which would balloon the walk without adding any
# in-repo target. See artifacts.ARTIFACT_DIR_OVERRIDES.
ARTIFACT_IGNORE_DIRS = IGNORE_DIRS - ARTIFACT_DIR_OVERRIDES


def _is_macos_cruft(filename: str) -> bool:
    """AppleDouble resource-fork file (``._Foo.java``) — a Mac-created zip
    mirrors every real file with one of these, same extension as the real
    file, so the EXT_LANG check alone doesn't exclude it."""
    return filename.startswith("._")

EXT_LANG = {
    ".java": "java", ".py": "python", ".sql": "sql",
    # JSP pages are translated to synthetic Java and run through the Java
    # extractor (extractors/jsp.py) — they are source, not artifacts.
    ".jsp": "jsp", ".jspf": "jsp", ".tag": "jsp",
    # JavaScript family (javascript grammar is JSX-aware)
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    # TypeScript (.tsx needs the tsx grammar)
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
}


def _source_lang(name: str) -> str | None:
    """Language to parse a path as, or None if it must not be parsed as source.

    The artifact check has to come first, and `.d.ts` is why: splitext sees only
    ".ts", so declaration files have always been admitted and parsed as
    TypeScript. They contain declarations with no bodies, so every Function node
    they produce is a phantom that then competes as a resolution candidate.
    Routing the decision through artifacts.artifact_kind keeps this module and
    the upload filter agreeing on what is source, which is the whole reason that
    classification lives in one place.
    """
    if artifact_kind(name):
        return None
    return EXT_LANG.get(os.path.splitext(name)[1])


@dataclass
class FileInfo:
    relpath: str
    abspath: str
    lang: str
    sha: str
    source: bytes


@dataclass(frozen=True)
class ArtifactInfo:
    """A non-source input located on disk, not read.

    Contents are deliberately NOT loaded here, unlike FileInfo: a repo's .class
    and .jar files can be larger than its source, and each resolver reads only
    the subset it needs (the bytecode pass streams class files; the JSP pass
    wants .jsp/.tld). Holding all of it resident would reintroduce exactly the
    whole-repo-in-RAM problem the streaming design removed.
    """
    relpath: str
    abspath: str
    kind: str      # see artifacts.EXT_ARTIFACT / ARTIFACT_FILENAMES


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
            lang = _source_lang(rel)
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
            lang = _source_lang(fn)
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


def discover_artifacts(root: str) -> list[ArtifactInfo]:
    """Locate non-source artifacts — bytecode, archives, JSPs, stubs, build and
    deployment config. Paths only, no reads (see ArtifactInfo).

    Separate from discover() rather than folded into it on purpose. discover()
    feeds tree-sitter, and every file it returns gets parsed as source; these
    must never take that path. Keeping them apart also means this function can
    walk the build-output directories discover() deliberately refuses to enter,
    without any risk of generated .java under target/ leaking into extraction.
    """
    root = os.path.abspath(root)
    out: list[ArtifactInfo] = []
    t0 = time.time()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ARTIFACT_IGNORE_DIRS]
        for fn in filenames:
            if _is_macos_cruft(fn):
                continue
            kind = artifact_kind(fn)
            if not kind:
                continue
            abspath = os.path.join(dirpath, fn)
            out.append(ArtifactInfo(
                relpath=os.path.relpath(abspath, root).replace("\\", "/"),
                abspath=abspath,
                kind=kind,
            ))
    if out:
        by_kind: dict[str, int] = {}
        for art in out:
            by_kind[art.kind] = by_kind.get(art.kind, 0) + 1
        _log.info(
            "[graph_discover] found %s artifact(s) in %.3fs (%s)",
            len(out), time.time() - t0,
            ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())),
        )
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
            if not _source_lang(fn):
                continue
            abspath = os.path.join(dirpath, fn)
            out.append(os.path.relpath(abspath, root).replace("\\", "/"))
    return out

"""Per-file extraction-result cache on local disk, keyed by (content sha,
repo, relpath).

Re-ingesting a codebase should only re-parse files whose content actually
changed. The key MUST include the file's identity (repo + relpath), not just
its content sha: extracted bundles embed path- and repo-dependent data (node
ids from make_id(repo, ...), Node.file/Node.repo/package fields), so a bundle
is only valid for the exact file it was extracted from. A sha-only scheme
would mean every duplicate-content file (e.g. multiple identical
`__init__.py` files) silently collides on the first writer's bundle. Keys
are namespaced under a version directory (`_CACHE_VERSION`).

BUMP _CACHE_VERSION WHENEVER AN EXTRACTOR STARTS EMITTING SOMETHING NEW.
The key is (content sha, repo, relpath) and NOTHING about the extractor, so a
file whose bytes have not changed is served from cache no matter how much the
extraction logic has moved on. Adding a ref type without bumping means every
previously-ingested file silently keeps its OLD bundle: the new edges appear
only for files that happen to be new or edited, and the run looks like the
feature half-works rather than like a stale cache. That failure is invisible —
there is no error, just missing edges.

Standalone-app note: developer_assistant's original extract_cache.py is
S3-backed (boto3 + app.core.config.settings + sail_core.aws_services) — this
is a local-disk-backed rewrite with the SAME public API (ExtractCache class,
get_extract_cache() singleton), so pipeline.py needs no changes to use it.
No AWS dependency.

This cache is purely a performance optimization: any failure (disabled,
missing file, (de)serialization error) degrades to a cache miss / no-op
rather than raising, so a caching problem can never turn into an extraction
failure.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional

import joblib

from sail_core.logger.logger import get_logger

from .canonical_ir import CanonicalBundle
from .config import get_extract_cache_dir, is_extract_cache_enabled

_log = get_logger(__name__)

# v3: the cross-language web layer (RENDERS / INCLUDES_SCRIPT / INCLUDES_PAGE /
# HANDLED_BY / CALLS_API-from-JS / @WebServlet endpoints). Bundles cached under v2
# predate every one of those refs.
#
# NOT the same lifetime as the extraction CHECKPOINT, which is a common mix-up.
# pipeline.index_repo calls checkpoint.clear_checkpoint() after a successful run,
# so `.graph_checkpoints/` is gone once the graph is ingested — correct, it exists
# only to resume a crashed run. THIS cache is the opposite: it exists precisely to
# outlive a run so an unchanged file is never re-parsed, and nothing deletes it.
# That is why a version bump is the only way to invalidate it.
_CACHE_VERSION = "v3"

# Per-extension epoch, folded into the key for that extension only.
#
# The global version above is the right lever when the PAYLOAD changes, since a
# CanonicalBundle written by an older scheme is unreadable whatever produced it.
# It is the wrong lever when one extractor's OUTPUT changes: bumping it discards
# 40k cached Java bundles to pick up a JSP-only fix, and re-running javac over
# them costs hours to arrive at byte-identical results.
#
# An unlisted extension keys exactly as before, so raising an epoch here can only
# ever cause a miss (re-extract) for the language named — never a stale hit, and
# never a miss for any other language.
#
# .jsp = 2: `<script src=<%= ... %>>` with the expression broken across lines.
# Epoch 1 matched the bare attribute as a run of non-space characters, so it
# stopped at the newline and never saw the .js filename on the continuation line
# — 906 pages in the ARAMEX tree cached with no INCLUDES_SCRIPT ref at all. Their
# content sha has not changed, so nothing but this bump re-parses them.
#
# .js = 2: `"FooServlet?"` as a HANDLED_BY target. Epoch 1 rejected any literal
# containing a `?`, so every servlet named with its query separator attached
# resolved to nothing — 3655 call sites across 1279 files.
_LANG_EPOCH = {".jsp": 2, ".js": 2}


class ExtractCache:
    """Local-disk cache mapping a file content sha -> its CanonicalBundle.

    Never raises to the caller. `get()` returns None and `put()` is a
    silent no-op on any failure (cache disabled, missing file,
    (de)serialization error).
    """

    def __init__(self) -> None:
        self._enabled = bool(is_extract_cache_enabled())
        self._dir = Path(get_extract_cache_dir() or os.path.join(".cache", "graph_extract_cache"))
        if self._enabled:
            try:
                (self._dir / _CACHE_VERSION).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _log.warning("[graph_extract_cache] failed to create cache dir %s: %s", self._dir, exc)
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _path(self, sha: str, repo: str, relpath: str) -> Path:
        # File identity folded in as a digest (relpaths can contain chars
        # awkward for filenames); the version dir isolates key/payload schemes.
        ident = hashlib.md5(f"{repo}\x1f{relpath}".encode("utf-8")).hexdigest()
        epoch = _LANG_EPOCH.get(os.path.splitext(relpath)[1].lower(), 1)
        suffix = "" if epoch == 1 else f"-e{epoch}"
        return self._dir / _CACHE_VERSION / f"{sha}-{ident}{suffix}.joblib"

    def get(self, sha: str, repo: str, relpath: str) -> Optional[CanonicalBundle]:
        if not self._enabled or not sha:
            return None
        path = self._path(sha, repo, relpath)
        if not path.exists():
            return None
        try:
            bundle = joblib.load(path)
        except Exception as exc:  # noqa: BLE001 - corrupt/incompatible cache entry
            _log.warning("[graph_extract_cache] failed to deserialize %s: %s", path, exc)
            return None
        if not isinstance(bundle, CanonicalBundle):
            _log.warning("[graph_extract_cache] unexpected cached object type at %s: %r", path, type(bundle))
            return None
        return bundle

    def put(self, sha: str, repo: str, relpath: str, bundle: CanonicalBundle) -> None:
        if not self._enabled or not sha:
            return
        path = self._path(sha, repo, relpath)
        try:
            # Write to a temp file in the same directory then atomically
            # rename into place — avoids a half-written .joblib file being
            # read by a concurrent get() if the process crashes mid-write.
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".joblib")
            try:
                with os.fdopen(fd, "wb") as fh:
                    joblib.dump(bundle, fh)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001 - any disk failure is a no-op
            _log.debug("[graph_extract_cache] failed to write %s: %s", path, exc)


@lru_cache(maxsize=1)
def get_extract_cache() -> ExtractCache:
    """Process-wide singleton so callers share one ExtractCache."""
    return ExtractCache()

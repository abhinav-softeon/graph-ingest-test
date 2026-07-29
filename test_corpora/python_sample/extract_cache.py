"""Per-file extraction-result cache on local disk, keyed by (content sha,
repo, relpath).

Re-ingesting a codebase should only re-parse files whose content actually
changed. The key MUST include the file's identity (repo + relpath), not just
its content sha: extracted bundles embed path- and repo-dependent data (node
ids from make_id(repo, ...), Node.file/Node.repo/package fields), so a bundle
is only valid for the exact file it was extracted from. A sha-only scheme
would mean every duplicate-content file (e.g. multiple identical
`__init__.py` files) silently collides on the first writer's bundle. Keys
are namespaced under `v2/` in case the key scheme ever changes again.

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
                (self._dir / "v2").mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _log.warning("[graph_extract_cache] failed to create cache dir %s: %s", self._dir, exc)
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _path(self, sha: str, repo: str, relpath: str) -> Path:
        # File identity folded in as a digest (relpaths can contain chars
        # awkward for filenames); `v2/` in case the key scheme ever changes.
        ident = hashlib.md5(f"{repo}\x1f{relpath}".encode("utf-8")).hexdigest()
        return self._dir / "v2" / f"{sha}-{ident}.joblib"

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

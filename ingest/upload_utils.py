from __future__ import annotations

from pathlib import Path
import io
import os
import tempfile
import zipfile
from typing import Callable, Dict, Iterable, List, Tuple


SUPPORTED_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go",
    ".cs", ".rb", ".php", ".rs", ".cpp", ".c", ".h",
}

DEFAULT_EXCLUDED_DIRS = {
    ".git", "node_modules", "target", "dist", "build",
    "out", "coverage", "vendor", "__pycache__",
}


def sanitize_upload_path(raw_path: str) -> str:
    """Normalize user-provided paths and reject unsafe traversal patterns."""
    p = (raw_path or "").replace("\\", "/").strip()
    if not p:
        raise ValueError("Upload path is empty")

    parts = [part for part in p.split("/") if part not in ("", ".")]
    if not parts:
        raise ValueError("Upload path is invalid")

    sanitized: List[str] = []
    for part in parts:
        if part == "..":
            raise ValueError(f"Unsafe upload path traversal: {raw_path}")
        # Reject Windows drive prefixes inside a segment like C: or D:
        if ":" in part:
            raise ValueError(f"Unsafe upload path drive specifier: {raw_path}")
        sanitized.append(part)

    rel = "/".join(sanitized)
    if rel.startswith("/"):
        raise ValueError(f"Unsafe absolute upload path: {raw_path}")
    return rel


def _path_contains_excluded_dir(rel_path: str, excluded_dirs: Iterable[str]) -> bool:
    excluded = {d.strip().lower() for d in excluded_dirs if d and d.strip()}
    if not excluded:
        return False
    segments = [seg.lower() for seg in rel_path.split("/") if seg]
    return any(seg in excluded for seg in segments)


def _is_macos_zip_cruft(rel_path: str) -> bool:
    """A zip built on macOS mirrors every real file with an AppleDouble
    resource-fork file under a __MACOSX/ tree, named ``._<original name>`` —
    same extension as the real file (e.g. ``._Foo.java``), so the extension
    allowlist alone doesn't exclude them. Left in, they double the file count
    and get parsed as if their binary resource-fork bytes were source code."""
    segments = rel_path.split("/")
    if "__MACOSX" in segments:
        return True
    return segments[-1].startswith("._")


def materialize_uploaded_sources(
    direct_files: Dict[str, bytes],
    zip_bytes: bytes | None,
    allowed_exts: Iterable[str],
    excluded_dirs: Iterable[str] | None = None,
    upload_root_dir: str | None = None,
) -> Tuple[str, List[str]]:
    """Write uploaded files to a temp source root and return selected code files.

    Parameters
    ----------
    direct_files : Mapping of filename -> raw bytes.
    zip_bytes    : Optional raw bytes of a ZIP archive.
    allowed_exts : Iterable of lower-case extensions (e.g. {".py", ".ts"}).
    """
    if upload_root_dir:
        Path(upload_root_dir).mkdir(parents=True, exist_ok=True)
        src_root = tempfile.mkdtemp(prefix="pr_review_upload_", dir=upload_root_dir)
    else:
        src_root = tempfile.mkdtemp(prefix="pr_review_upload_")
    allowed = {ext.lower() for ext in allowed_exts}
    excluded = set(DEFAULT_EXCLUDED_DIRS)
    if excluded_dirs:
        excluded |= {d.strip().lower() for d in excluded_dirs if d and d.strip()}
    selected: List[str] = []

    if zip_bytes:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    rel = sanitize_upload_path(info.filename)
                except ValueError:
                    continue
                if _path_contains_excluded_dir(rel, excluded) or _is_macos_zip_cruft(rel):
                    continue
                ext = os.path.splitext(rel)[1].lower()
                if ext not in allowed:
                    continue
                target = Path(src_root, *rel.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src_fh:
                    target.write_bytes(src_fh.read())
                selected.append(rel)

    for name, blob in direct_files.items():
        try:
            rel = sanitize_upload_path(name)
        except ValueError:
            continue
        if _path_contains_excluded_dir(rel, excluded):
            continue
        ext = os.path.splitext(rel)[1].lower()
        if ext not in allowed:
            continue
        target = Path(src_root, *rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        selected.append(rel)

    selected = sorted(set(selected))
    if not selected:
        raise RuntimeError("No supported code files found in uploaded content.")
    return src_root, selected


def materialize_uploaded_sources_from_zip_path(
    zip_path: str,
    allowed_exts: Iterable[str],
    excluded_dirs: Iterable[str] | None = None,
    upload_root_dir: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> Tuple[str, List[str]]:
    """Extract supported code files from an on-disk ZIP path.

    This avoids loading large archives fully into memory.
    """
    if upload_root_dir:
        Path(upload_root_dir).mkdir(parents=True, exist_ok=True)
        src_root = tempfile.mkdtemp(prefix="pr_review_upload_", dir=upload_root_dir)
    else:
        src_root = tempfile.mkdtemp(prefix="pr_review_upload_")

    allowed = {ext.lower() for ext in allowed_exts}
    excluded = set(DEFAULT_EXCLUDED_DIRS)
    if excluded_dirs:
        excluded |= {d.strip().lower() for d in excluded_dirs if d and d.strip()}

    import threading
    from concurrent.futures import ThreadPoolExecutor

    with zipfile.ZipFile(zip_path) as zf:
        selected_infos: List[tuple[zipfile.ZipInfo, str]] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                rel = sanitize_upload_path(info.filename)
            except ValueError:
                continue
            if _path_contains_excluded_dir(rel, excluded) or _is_macos_zip_cruft(rel):
                continue
            ext = os.path.splitext(rel)[1].lower()
            if ext not in allowed:
                continue
            selected_infos.append((info, rel))

        total = len(selected_infos)
        if on_progress:
            on_progress(0, total, "scanning archive entries")

        # Pre-create all parent directories up front (deduplicated) instead of
        # calling mkdir(parents=True, exist_ok=True) per file — collapses what
        # would be thousands of repeated per-file directory syscalls into one
        # pass over the unique set of directories.
        parent_dirs = {Path(src_root, *rel.split("/")).parent for _info, rel in selected_infos}
        for d in parent_dirs:
            d.mkdir(parents=True, exist_ok=True)

        # zipfile.ZipFile is not safe for concurrent reads from multiple threads
        # on one handle, so the archive read+decompress is serialized under a
        # lock; the actual disk write (the slow part on network/virtualized
        # mounts) happens outside the lock so it can overlap across threads.
        zip_lock = threading.Lock()
        done_count = 0
        progress_lock = threading.Lock()

        def _extract_one(item: tuple[zipfile.ZipInfo, str]) -> str:
            info, rel = item
            target = Path(src_root, *rel.split("/"))
            with zip_lock:
                data = zf.read(info)
            target.write_bytes(data)
            nonlocal done_count
            with progress_lock:
                done_count += 1
                idx = done_count
            if on_progress:
                on_progress(idx, total, rel)
            return rel

        default_workers = min(16, max(1, (os.cpu_count() or 4) * 2))
        try:
            worker_count = int(os.environ.get("GRAPH_ZIP_EXTRACT_WORKERS") or default_workers)
        except ValueError:
            worker_count = default_workers
        if worker_count <= 0:
            worker_count = default_workers
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            selected = list(pool.map(_extract_one, selected_infos))

    selected = sorted(set(selected))
    if not selected:
        raise RuntimeError("No supported code files found in uploaded content.")
    return src_root, selected

"""Extract-cache keying.

The cache outlives a run by design and nothing deletes it, so a fix in an
extractor only reaches the graph if the key changes too. That makes the key the
place where a correct fix can still ship as a no-op: the file's content sha has
not changed, so an unchanged key replays the old, wrong bundle forever.

The global version is the lever for a payload-format change. A per-extension
epoch is the lever for an output change in one extractor, and it must be exact in
both directions — invalidate that language, leave every other language alone.
"""
from __future__ import annotations

from pathlib import Path

from graph_core.extract_cache import _LANG_EPOCH, ExtractCache


def _cache(tmp: Path) -> ExtractCache:
    """An ExtractCache pinned to a temp dir, without touching real config."""
    c = ExtractCache.__new__(ExtractCache)
    c._enabled = True
    c._dir = tmp
    return c


def test_epoch_changes_the_key_for_listed_extensions_only(tmp_path):
    """Asserted against the table rather than against today's entries in it, so
    raising an epoch for a new language does not require editing this test — only
    a change to the RULE should."""
    c = _cache(tmp_path)

    for ext, epoch in _LANG_EPOCH.items():
        name = c._path("SHA", "aramex", f"scm/x/page{ext}").name
        assert name.endswith(f"-e{epoch}.joblib"), f"{ext} missing its epoch"

    # An extension absent from the table keys as it always did, so bumping one
    # language never re-parses another. .java is the one that matters: 40k files,
    # and javac is the expensive half of a run.
    for ext in (".java", ".py", ".sql"):
        assert ext not in _LANG_EPOCH, f"{ext} joined the table; pick another"
        name = c._path("SHA", "aramex", f"scm/x/thing{ext}").name
        assert name.endswith(".joblib") and "-e" not in name


def test_unlisted_extension_keys_exactly_as_before(tmp_path):
    """The epoch is opt-in. An extension absent from the table must produce the
    pre-epoch filename byte for byte, or raising an epoch for one language
    silently invalidates all of them — the cost the table exists to avoid."""
    c = _cache(tmp_path)
    import hashlib

    from graph_core.extract_cache import _CACHE_VERSION

    rel = "WEB-INF/classes/scm/A.java"
    assert Path(rel).suffix not in _LANG_EPOCH
    ident = hashlib.md5(f"aramex\x1f{rel}".encode("utf-8")).hexdigest()
    expected = tmp_path / _CACHE_VERSION / f"SHA-{ident}.joblib"
    assert c._path("SHA", "aramex", rel) == expected


def test_extension_match_is_case_insensitive(tmp_path):
    """Windows trees carry .JSP. A case-sensitive lookup would leave those pages
    on the stale key — the same silent hole, just harder to notice."""
    c = _cache(tmp_path)
    assert c._path("SHA", "r", "a/B.JSP").name.endswith("-e2.joblib")


def test_epoch_bump_is_a_miss_not_a_stale_hit(tmp_path):
    """The whole point, end to end: a bundle written under the old key must not
    be readable under the new one."""
    c = _cache(tmp_path)
    rel = "scm/x/page.jsp"
    new_key = c._path("SHA", "r", rel)
    new_key.parent.mkdir(parents=True, exist_ok=True)

    # Simulate epoch 1: the same path with the suffix stripped.
    old_key = new_key.with_name(new_key.name.replace("-e2", ""))
    old_key.write_bytes(b"stale bundle from the old extractor")

    assert c.get("SHA", "r", rel) is None, "epoch bump served a stale bundle"

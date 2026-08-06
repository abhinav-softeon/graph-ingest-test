"""The ambiguity cap on the `+unknown_recv` demotion path.

emit() has capped bare-name fan-out for a while (GRAPH_NAME_MATCH_MAX_CANDIDATES,
default 5), but the unknown-receiver demotion branch appends its edges directly
and never went through emit() — so it was the one fan-out path with no cap at all,
and by volume the largest. HANDOFF measured 8,236,060 of 8.38M REFERENCES arriving
there (`name+arity+unknown_recv`), each emitting one edge per same-named candidate
of which at most one can be right; the AMBIGUOUS bucket as a whole measured 0.1%
precision for a 1.0% recall contribution (HANDOFF 3.2).

The ref is constructed directly rather than extracted from Java source. Reaching
this branch needs `recv` set with `recv_type` UNKNOWN — a receiver whose type
could not be inferred (chained calls like `getThing().foo()`, which java.py
explicitly does not type). Coaxing the extractor into that shape makes the test
about the extractor; building the ref states the precondition outright.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from graph_core.discovery import discover
from graph_core.models import RawRef
from graph_core.pipeline import _extract_one
from graph_core.resolver import resolve

# Seven classes, each declaring common(), each in its OWN package so that no
# same-file / same-package / import narrowing can fire and resolution is forced
# down to the bare-name tier.
_N_CANDIDATES = 7


def _corpus():
    root = tempfile.mkdtemp(prefix="cap_")
    for i in range(_N_CANDIDATES):
        pkg_dir = os.path.join(root, "com", f"a{i}")
        os.makedirs(pkg_dir, exist_ok=True)
        with open(os.path.join(pkg_dir, f"Holder{i}.java"), "w", encoding="utf-8") as fh:
            fh.write(f"""
package com.a{i};

public class Holder{i} {{
    public void common() {{ }}
}}
""")
    caller_dir = os.path.join(root, "com", "z")
    os.makedirs(caller_dir, exist_ok=True)
    with open(os.path.join(caller_dir, "Caller.java"), "w", encoding="utf-8") as fh:
        fh.write("""
package com.z;

public class Caller {
    public void go() { }
}
""")

    nodes, edges, refs = [], [], []
    for fi in discover(root):
        n, e, r = _extract_one(fi, "bench")
        nodes.extend(n)
        edges.extend(e)
        refs.extend(r)

    caller = [n for n in nodes if n.fqn == "com.z.Caller#go"][0]
    # The precondition for the demotion branch: a CALLS ref with a receiver whose
    # type is unknown, whose name matches many in-repo declarations.
    refs.append(RawRef(
        type="CALLS", src=caller.id, target_name="common", kind_hint="call",
        recv="obj", recv_type="", ref_file=caller.file, ref_line=5, call_arity=0,
    ))
    return nodes, edges, refs, caller


@pytest.fixture(scope="module")
def corpus():
    return _corpus()


def _demoted(out_edges, src_id):
    return [e for e in out_edges
            if e.src == src_id and "unknown_recv" in (e.strategy or "")]


class TestUnknownRecvCap:
    def test_fanout_above_the_cap_emits_nothing(self, corpus, monkeypatch):
        """7 candidates against a cap of 5: the site is recorded as unresolved
        rather than allocating 7 edges of which 6 are false by construction."""
        nodes, edges, refs, caller = corpus
        monkeypatch.setenv("GRAPH_NAME_MATCH_MAX_CANDIDATES", "5")
        _extra, out_edges, cov = resolve(nodes, list(edges), refs, "bench")
        assert not _demoted(out_edges, caller.id)
        assert cov["REFERENCES"].unresolved >= 1, \
            "the dropped site must still be counted, not silently vanish"

    def test_disabling_the_cap_restores_the_full_fanout(self, corpus, monkeypatch):
        """Proves the previous test measures the cap and not some other filter:
        with the cap off, every candidate gets its edge."""
        nodes, edges, refs, caller = corpus
        monkeypatch.setenv("GRAPH_NAME_MATCH_MAX_CANDIDATES", "0")
        _extra, out_edges, _cov = resolve(nodes, list(edges), refs, "bench")
        demoted = _demoted(out_edges, caller.id)
        assert len(demoted) == _N_CANDIDATES
        assert all(e.type == "REFERENCES" for e in demoted)
        assert all(e.confidence == "AMBIGUOUS" for e in demoted)

    def test_fanout_at_or_below_the_cap_is_kept(self, corpus, monkeypatch):
        """The cap must not swallow small, still-plausible fan-outs — it targets
        the pathological tail, not every ambiguous site."""
        nodes, edges, refs, caller = corpus
        monkeypatch.setenv("GRAPH_NAME_MATCH_MAX_CANDIDATES", str(_N_CANDIDATES))
        _extra, out_edges, _cov = resolve(nodes, list(edges), refs, "bench")
        assert len(_demoted(out_edges, caller.id)) == _N_CANDIDATES

    def test_a_unique_match_is_never_capped(self, monkeypatch):
        """A single candidate resolves as INFERRED regardless of the cap — the
        cap is about fan-out, and one edge is not a fan-out."""
        root = tempfile.mkdtemp(prefix="cap_uniq_")
        pkg = os.path.join(root, "com", "only")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "Solo.java"), "w", encoding="utf-8") as fh:
            fh.write("""
package com.only;

public class Solo {
    public void uniqueName() { }
}
""")
        caller_dir = os.path.join(root, "com", "z2")
        os.makedirs(caller_dir, exist_ok=True)
        with open(os.path.join(caller_dir, "C2.java"), "w", encoding="utf-8") as fh:
            fh.write("""
package com.z2;

public class C2 {
    public void go() { }
}
""")
        nodes, edges, refs = [], [], []
        for fi in discover(root):
            n, e, r = _extract_one(fi, "bench")
            nodes.extend(n)
            edges.extend(e)
            refs.extend(r)
        caller = [n for n in nodes if n.fqn == "com.z2.C2#go"][0]
        refs.append(RawRef(
            type="CALLS", src=caller.id, target_name="uniqueName", kind_hint="call",
            recv="obj", recv_type="", ref_file=caller.file, ref_line=5, call_arity=0,
        ))
        monkeypatch.setenv("GRAPH_NAME_MATCH_MAX_CANDIDATES", "1")
        _extra, out_edges, _cov = resolve(nodes, edges, refs, "bench")
        demoted = _demoted(out_edges, caller.id)
        assert len(demoted) == 1
        assert demoted[0].confidence == "INFERRED"

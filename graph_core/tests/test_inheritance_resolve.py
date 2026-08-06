"""Inheritance-aware call resolution (the ancestor pre-pass in resolver.py).

methods_of_class holds a class's OWN methods, so a call to a method it INHERITS
used to find nothing and fall through to the arity-only fallback — which fans out
across every same-named method in the repo and then gets demoted to a weak
REFERENCES edge tagged `+unknown_recv`. HANDOFF measured 8,236,060 of 8.38M
REFERENCES in exactly that state: "receiver type known, method not found on it",
which in an Abstract*/I*-heavy codebase is inheritance rather than a real miss.

ONE CLASS PER FILE, deliberately. narrow_call tries same-parent and same-file
narrowing (steps 1-2) BEFORE receiver-type narrowing (step 4), so a fixture with
the whole hierarchy in one file never reaches the code under test — the same-file
step matches every same-named method in that file and returns them all as
AMBIGUOUS. Separate files is also what a real codebase looks like.

`Unrelated` declares the same method names so the fallback and the fix give
visibly different answers: without ancestry the call is ambiguous across two
classes, with it there is exactly one right answer.

No javac needed — extraction plus resolve(), no bytecode. That is the point: the
ancestor index also has to work from heuristic EXTENDS refs, the weaker of its
two sources and the one worth pinning down.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from graph_core.discovery import discover
from graph_core.models import Edge
from graph_core.pipeline import _derive_overrides, _extract_one
from graph_core.resolver import resolve

_FILES = {
    "Base.java": """
package com.acme;

class Base {
    public void doWork() { }
    public void shared() { }
}
""",
    "Impl.java": """
package com.acme;

class Impl extends Base {
}
""",
    "Deep.java": """
package com.acme;

class Deep extends Impl {
}
""",
    "Shadower.java": """
package com.acme;

class Shadower extends Base {
    public void shared() { }
}
""",
    "Unrelated.java": """
package com.acme;

class Unrelated {
    public void doWork() { }
    public void shared() { }
}
""",
    "Caller.java": """
package com.acme;

public class Caller {
    public void callInherited() {
        Impl x = new Impl();
        x.doWork();
    }

    public void callTwoHopsUp() {
        Deep d = new Deep();
        d.doWork();
    }

    public void callOverridden() {
        Shadower o = new Shadower();
        o.shared();
    }
}
""",
}


def _build(files: dict[str, str], prefix: str):
    root = tempfile.mkdtemp(prefix=prefix)
    pkg = os.path.join(root, "com", "acme")
    os.makedirs(pkg, exist_ok=True)
    for name, src in files.items():
        with open(os.path.join(pkg, name), "w", encoding="utf-8") as fh:
            fh.write(src)
    nodes, edges, refs = [], [], []
    for fi in discover(root):
        n, e, r = _extract_one(fi, "bench")
        nodes.extend(n)
        edges.extend(e)
        refs.extend(r)
    return nodes, edges, refs


@pytest.fixture(scope="module")
def resolved():
    """(nodes, edges, coverage) after extraction + resolve.

    ``edges`` is extraction's structural edges PLUS resolve's output, which is
    what index_repo hands to the derive stage. resolve() only RETURNS the edges it
    created, so a fixture using its output alone would have no CONTAINS edges at
    all — and anything depending on class->method containment (like
    _derive_overrides) would silently produce nothing.
    """
    nodes, edges, refs = _build(_FILES, "inh_")
    extra, out_edges, cov = resolve(nodes, list(edges), refs, "bench")
    return nodes + list(extra), list(edges) + out_edges, cov


def _node(nodes, fqn, arity=None):
    hits = [n for n in nodes if n.fqn == fqn
            and (arity is None or (n.param_count or 0) == arity)]
    assert hits, f"no node for {fqn}"
    return hits[0]


def _calls_between(out_edges, src_id, dst_id):
    return [e for e in out_edges
            if e.src == src_id and e.dst == dst_id and e.type == "CALLS"]


class TestInheritedCallResolution:
    def test_inherited_method_resolves_to_the_ancestor(self, resolved):
        """`Impl x; x.doWork()` where doWork is declared on Base."""
        nodes, out_edges, _cov = resolved
        caller = _node(nodes, "com.acme.Caller#callInherited")
        target = _node(nodes, "com.acme.Base#doWork")
        found = _calls_between(out_edges, caller.id, target.id)
        assert found, "inherited call did not resolve to a CALLS edge"
        assert found[0].strategy.startswith("receiver_type_hint_inherited")

    def test_it_does_not_also_hit_the_same_named_unrelated_class(self, resolved):
        """The failure mode being fixed was fanning out to every same-named
        method in the repo."""
        nodes, out_edges, _cov = resolved
        caller = _node(nodes, "com.acme.Caller#callInherited")
        decoy = _node(nodes, "com.acme.Unrelated#doWork")
        assert not _calls_between(out_edges, caller.id, decoy.id)

    def test_no_unknown_recv_demotion_for_the_inherited_call(self, resolved):
        """Resolving via ancestry means the strategy no longer starts with
        'name', so the REFERENCES+unknown_recv demotion no longer applies —
        that is what turns these 8.2M weak edges back into real calls."""
        nodes, out_edges, _cov = resolved
        caller = _node(nodes, "com.acme.Caller#callInherited")
        demoted = [e for e in out_edges
                   if e.src == caller.id and "unknown_recv" in (e.strategy or "")]
        assert not demoted

    def test_resolution_is_unambiguous(self, resolved):
        """One receiver type, one declaration — the edge must be a confident one,
        not an AMBIGUOUS fan-out that happens to include the right answer."""
        nodes, out_edges, _cov = resolved
        caller = _node(nodes, "com.acme.Caller#callInherited")
        target = _node(nodes, "com.acme.Base#doWork")
        edge = _calls_between(out_edges, caller.id, target.id)[0]
        assert edge.confidence != "AMBIGUOUS", edge.strategy

    def test_walks_more_than_one_level(self, resolved):
        """Deep -> Impl -> Base. A direct-parent-only lookup would miss this."""
        nodes, out_edges, _cov = resolved
        caller = _node(nodes, "com.acme.Caller#callTwoHopsUp")
        target = _node(nodes, "com.acme.Base#doWork")
        assert _calls_between(out_edges, caller.id, target.id), \
            "two-hop inherited call did not resolve"

    def test_an_override_shadows_the_ancestor(self, resolved):
        """`Shadower o; o.shared()` must bind to Shadower#shared, NOT Base#shared.
        This is why own-methods are tried before the hierarchy: reversing that
        order silently sends every overridden call to the wrong declaration."""
        nodes, out_edges, _cov = resolved
        caller = _node(nodes, "com.acme.Caller#callOverridden")
        own = _node(nodes, "com.acme.Shadower#shared")
        ancestor = _node(nodes, "com.acme.Base#shared")
        assert _calls_between(out_edges, caller.id, own.id), "should bind to the override"
        assert not _calls_between(out_edges, caller.id, ancestor.id), \
            "must not also bind to the shadowed ancestor method"


class TestAncestorIndexSources:
    def test_precise_hierarchy_edges_seed_the_index(self):
        """The index's other source: EXTENDS/IMPLEMENTS edges a precise tier
        already produced. bytecode_resolver emits those at EXTRACTED straight
        from the class header, and they arrive as EDGES rather than refs — so an
        inherited call must resolve from an edge alone, with no EXTENDS ref
        anywhere in the input.
        """
        files = {
            "Root.java": """
package com.acme;

class Root {
    public void fromParent() { }
}
""",
            # No `extends` in the source: extraction produces no EXTENDS ref, so
            # the only possible ancestry source is the edge injected below.
            "Leaf.java": """
package com.acme;

class Leaf {
}
""",
            "Use.java": """
package com.acme;

public class Use {
    public void go() {
        Leaf l = new Leaf();
        l.fromParent();
    }
}
""",
        }
        nodes, edges, refs = _build(files, "inh_edge_")
        root_cls = _node(nodes, "com.acme.Root")
        leaf_cls = _node(nodes, "com.acme.Leaf")
        target = _node(nodes, "com.acme.Root#fromParent")
        caller = _node(nodes, "com.acme.Use#go")

        assert not [r for r in refs if r.type == "EXTENDS"], \
            "fixture must have no EXTENDS ref, or this proves nothing"
        # Without the edge there is no ancestry at all and the call cannot resolve.
        _e, before, _c = resolve(nodes, list(edges), refs, "bench")
        assert not _calls_between(before, caller.id, target.id)

        # Synthetic precise edge: Leaf extends Root, exactly as bytecode reports it.
        seeded = list(edges) + [
            Edge("EXTENDS", leaf_cls.id, root_cls.id, "EXTRACTED",
                 origin="EXTRACTED", extractor="bytecode", strategy="bytecode"),
        ]
        _e2, after, _c2 = resolve(nodes, seeded, refs, "bench")
        assert _calls_between(after, caller.id, target.id), \
            "a precise EXTENDS edge alone must be enough to resolve an inherited call"


class TestDeriveOverridesSharedIndex:
    """index_repo now builds the class->supertypes map in the SAME traversal that
    builds parent_of, and passes it in, so _derive_overrides no longer makes a
    second full pass over a 15M-edge list in the most expensive stage. That is a
    pure work-elimination change, so the only thing worth asserting is that the
    output is bit-identical either way.
    """

    def test_passing_supers_matches_deriving_them_locally(self, resolved):
        nodes, out_edges, _cov = resolved
        by_id = {n.id: n for n in nodes}
        parent_of = {e.dst: e.src for e in out_edges if e.type == "CONTAINS"}

        # What index_repo now computes inline alongside parent_of.
        supers: dict[str, list[str]] = {}
        for e in out_edges:
            if e.type in ("EXTENDS", "IMPLEMENTS"):
                s, d = by_id.get(e.src), by_id.get(e.dst)
                if s and d and s.label == "Class" and d.label == "Class":
                    supers.setdefault(e.src, []).append(e.dst)

        local = _derive_overrides(nodes, out_edges, by_id, dict(parent_of))
        shared = _derive_overrides(nodes, out_edges, by_id, dict(parent_of),
                                   supers=supers)
        assert [(e.type, e.src, e.dst) for e in local] == \
               [(e.type, e.src, e.dst) for e in shared]
        # Guard against the fixture going empty and making this vacuous.
        assert local, "fixture produced no OVERRIDES to compare"

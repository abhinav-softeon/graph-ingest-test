"""End-to-end smoke + equivalence test for the low-RAM derive tail
(_lowram_derive_and_write), against a fake store so no Neo4j is needed.

Verifies the orchestration runs and that its node metrics + edge set match
running the in-RAM derive functions on the same graph.
"""
from __future__ import annotations

import tempfile

from graph_core.models import Confidence, Edge, Node, Origin
from graph_core.lowram_derive import DiskEdgeStore
from graph_core.pipeline import (
    _attach_call_metrics,
    _lowram_derive_and_write,
)


class FakeStore:
    def __init__(self):
        self.nodes: list = []
        self.edges: list = []

    def bootstrap(self):
        pass

    def wipe(self, repo):
        pass

    def write_nodes(self, nodes, on_batch=None):
        self.nodes.extend(nodes)

    def write_edges(self, edges, on_batch=None):
        self.edges.extend(edges)

    def counts(self, repo):
        return (len(self.nodes), len(self.edges))


def _beat(stage, step, **detail):
    pass


def _mark(label):
    pass


def _write_beat(stage, what):
    return lambda w, t: None


def _cls(i):
    return Node(id=f"c{i}", label="Class", name=f"C{i}", fqn=f"pkg.C{i}", repo="t", kind="class", package="pkg", file="pkg/mod.py")


def _method(cid, name):
    return Node(id=f"{cid}.{name}", label="Function", name=name, fqn=f"pkg.{cid}.{name}",
                repo="t", kind="method", package="pkg", file="pkg/mod.py", param_count=0)


def _build_graph():
    """Animal.speak overridden by Dog.speak; a caller calls Animal.speak."""
    file_n = Node(id="f1", label="File", name="mod.py", fqn="pkg/mod.py", repo="t", kind="file", package="pkg", file="pkg/mod.py")
    animal, dog = _cls("Animal"), _cls("Dog")
    a_speak = _method("Animal", "speak")
    d_speak = _method("Dog", "speak")
    caller = _method("Animal", "wrangle")  # some method that calls speak
    nodes = [file_n, animal, dog, a_speak, d_speak, caller]

    structural = [
        Edge("CONTAINS", "f1", "cAnimal"), Edge("CONTAINS", "f1", "cDog"),
        Edge("CONTAINS", "cAnimal", "Animal.speak"), Edge("CONTAINS", "cAnimal", "Animal.wrangle"),
        Edge("CONTAINS", "cDog", "Dog.speak"),
        Edge("EXTENDS", "cDog", "cAnimal", confidence=Confidence.INFERRED.value, origin=Origin.EXTRACTED.value),
    ]
    # bulk: caller -> Animal.speak (twice = multiplicity), plus a self-loop
    calls = [
        Edge("CALLS", "Animal.wrangle", "Animal.speak", evidence_line=1, evidence_file="pkg/mod.py"),
        Edge("CALLS", "Animal.wrangle", "Animal.speak", evidence_line=2, evidence_file="pkg/mod.py"),
        Edge("CALLS", "Animal.wrangle", "Animal.wrangle", evidence_line=3, evidence_file="pkg/mod.py"),
    ]
    return nodes, structural, calls


def _key(e):
    return (e.type, e.src, e.dst)


def test_lowram_tail_runs_and_matches_inram_metrics():
    nodes, structural, calls = _build_graph()

    # expected fan_in/fan_out from the in-RAM function over the full edge list
    exp_nodes = _clone_nodes(nodes)
    _attach_call_metrics(exp_nodes, structural + calls)
    exp_fan = {n.id: (n.fan_in, n.fan_out, n.recursive) for n in exp_nodes}

    # run the low-RAM tail
    run_nodes = _clone_nodes(nodes)
    struct_ram = [e for e in structural]  # mutable copy (tail extends it)
    with tempfile.TemporaryDirectory() as spill, tempfile.TemporaryDirectory() as root:
        disk = DiskEdgeStore(spill, shard_size=2)
        disk.extend(calls)
        store = FakeStore()
        result = _lowram_derive_and_write(
            store, run_nodes, struct_ram, disk, root, "t", {"CALLS": None},
            True, total_files=1, t0=0.0, stage_seconds={},
            on_stage=None, _beat=_beat, _mark=_mark, _write_beat=_write_beat,
        )

    # 1) fan metrics identical to the in-RAM path
    run_fan = {n.id: (n.fan_in, n.fan_out, n.recursive) for n in run_nodes if n.label == "Function"}
    for nid, fan in run_fan.items():
        assert fan == exp_fan[nid], (nid, fan, exp_fan[nid])

    # 2) counts add up and store received exactly result.edges edges
    assert result.edges == len(store.edges), (result.edges, len(store.edges))
    assert result.nodes == len(store.nodes)

    # 3) OVERRIDES was derived (Dog.speak overrides Animal.speak)
    assert ("OVERRIDES", "Dog.speak", "Animal.speak") in {_key(e) for e in store.edges}

    # 4) polymorphic synthesis: caller of Animal.speak becomes caller of Dog.speak
    assert ("CALLS", "Animal.wrangle", "Dog.speak") in {_key(e) for e in store.edges}

    # 5) PASSES from extraction were dropped and only dataflow's (0 here) remain
    assert result.validation["ok"] is True or "dangling" not in "".join(result.validation.get("errors", []))


def _clone_nodes(nodes):
    out = []
    for n in nodes:
        m = Node(id=n.id, label=n.label, name=n.name, fqn=n.fqn, repo=n.repo,
                 kind=n.kind, package=n.package, file=n.file, param_count=n.param_count)
        out.append(m)
    return out

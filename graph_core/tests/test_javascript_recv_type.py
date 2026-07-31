"""Receiver typing for JavaScript/TypeScript — the fix java.py already got.

Without recv_type every member call falls through to global name matching, so
`userService.findById()` and `orderRepo.findById()` are indistinguishable and
the resolver emits an edge to every findById in the repo. That is the exact
mechanism that produced ~62M junk edges on the Java side (HANDOFF 2.1).

The subtle requirement, and the one these tests exist to protect: a WRONG
recv_type is worse than none. It drives both the resolver's typed path and its
external-receiver suppression, so asserting the enclosing class for
`this.users.findById()` would confidently mis-resolve rather than merely fail.
"""
from __future__ import annotations

import os
import tempfile

from graph_core.discovery import discover
from graph_core.extractors import extract


def _refs(body: str, name: str = "m.js"):
    """(nodes, [CALLS refs]) — a LIST, not a dict.

    Keying by (name, recv) would collapse `this.users.findById()` and
    `this.orders.findById()` into one entry, hiding the exact distinction these
    tests exist to prove.
    """
    root = tempfile.mkdtemp(prefix="jsrecv_")
    with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
        fh.write(body)
    files = discover(root)
    assert files, "discovery found nothing"
    nodes, _edges, refs = extract(files[0], "bench")
    return nodes, [r for r in refs if r.type == "CALLS"]


def _one(calls, target: str):
    found = [r for r in calls if r.target_name == target]
    assert len(found) == 1, f"expected one call to {target}, got {len(found)}"
    return found[0]


def _types_for(calls, target: str) -> set:
    return {r.recv_type for r in calls if r.target_name == target}


class TestLocalVariables:
    def test_new_expression_types_a_local(self):
        """The only type source that works in plain JS — no annotations to read."""
        _n, calls = _refs("function run(){ const s = new UserService(); s.load(1); }")
        assert _one(calls, "load").recv_type == "UserService"

    def test_untyped_receiver_stays_empty(self):
        """An honest miss. Guessing here is what the whole change is avoiding."""
        _n, calls = _refs("function run(){ other.load(2); }")
        assert _one(calls, "load").recv_type == ""

    def test_bare_call_has_no_receiver(self):
        _n, calls = _refs("function run(){ helper(1); }")
        helper = _one(calls, "helper")
        assert (helper.recv, helper.recv_type) == ("", "")


class TestClassMembers:
    _CLS = """
class Controller {
  users = new UserService();
  handle() {
    this.users.findById(1);
    this.orders.findById(2);
    this.helper();
    mystery.findById(3);
  }
  helper() { }
  orders = new OrderRepo();
}
"""

    def test_sibling_fields_are_distinguished(self):
        """Two calls, same method name, same arity, different owners. This is
        precisely the case name-only matching cannot resolve."""
        _n, calls = _refs(self._CLS)
        this_typed = {r.recv_type for r in calls
                      if r.target_name == "findById" and r.recv == "this"}
        assert this_typed == {"UserService", "OrderRepo"}

    def test_field_declared_after_the_method_still_resolves(self):
        """`orders` is declared BELOW handle(). Collecting field types in source
        order would leave that call untyped."""
        _n, calls = _refs(self._CLS)
        assert any(r.target_name == "findById" and r.recv_type == "OrderRepo"
                   for r in calls), "field declared after the method was not resolved"

    def test_this_resolves_to_the_enclosing_class(self):
        _n, calls = _refs(self._CLS)
        assert _one(calls, "helper").recv_type == "Controller"

    def test_unknown_receiver_in_a_class_stays_empty(self):
        _n, calls = _refs(self._CLS)
        mystery = [r for r in calls if r.recv == "mystery"]
        assert mystery and all(r.recv_type == "" for r in mystery)

    def test_js_class_fields_become_field_nodes(self):
        """The JavaScript grammar names this `property`, TypeScript names it
        `name`. Checking only `name` meant plain-JS class fields produced no
        Field nodes at all."""
        nodes, _calls = _refs(self._CLS)
        assert {n.name for n in nodes if n.label == "Field"} == {"users", "orders"}


class TestDoesNotOverreach:
    def test_deep_chain_is_not_guessed(self):
        """`a.b.c.d()` needs return-type propagation to bind. Reporting the type
        of `a` here would attribute the call to the wrong owner."""
        body = """
class C {
  a = new Alpha();
  go() { this.a.b.c(1); }
}
"""
        _n, calls = _refs(body)
        assert _one(calls, "c").recv_type == ""

    def test_method_call_chain_is_not_guessed(self):
        """`svc.find().save()` — save() is on find()'s return type, which is
        not knowable here."""
        _n, calls = _refs("function r(){ const s = new Svc(); s.find().save(); }")
        # save()'s receiver is a call expression, so there is no named receiver
        # at all — and certainly no type.
        assert _one(calls, "save").recv_type == ""
        assert _one(calls, "find").recv_type == "Svc"

    def test_nested_function_scope_is_not_leaked(self):
        """A `const x = new Foo()` inside a callback belongs to that callback.
        If declarator collection crossed the scope boundary, the inner Inner
        would shadow the outer Outer and mis-type `x.go(2)`.

        Only one `go` is seen: calls inside an anonymous callback are not
        extracted at all, because _SCOPE_BOUNDARY stops the walk and the
        callback never becomes a Function node of its own. That is a separate,
        pre-existing gap — what matters here is that the OUTER call keeps its
        correct type.
        """
        body = """
function outer() {
  const x = new Outer();
  run(function () { const x = new Inner(); x.go(1); });
  x.go(2);
}
"""
        _n, calls = _refs(body)
        assert _types_for(calls, "go") == {"Outer"}

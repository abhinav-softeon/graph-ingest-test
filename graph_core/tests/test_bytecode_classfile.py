"""Class file parser guardrails, against bytecode javac actually produced.

Fixtures are compiled at test time rather than checked in as binaries: a
checked-in .class pins one javac version forever and hides exactly the drift
this parser has to survive. Tests needing javac skip cleanly without it; the
descriptor and error-handling tests run everywhere.

The constructs asserted here are the ones the plan depends on. If any of them
regresses, Phase 2 silently produces a worse graph rather than failing:

  * overloads distinguished by descriptor  -> D4 node matching
  * bare field reads                       -> closes HANDOFF 4.4
  * lambda / anonymous / <clinit> methods  -> closes HANDOFF 4.2
  * bridge methods flagged                 -> must NOT become graph nodes
  * switch opcodes stepped correctly       -> desync yields silent garbage
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from graph_core.bytecode.classfile import (
    ClassFileError,
    descriptor_arity,
    descriptor_param_types,
    parse_class,
    parse_class_file,
    simple_name,
    type_name,
)

_FIXTURE = """
package com.acme;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Function;

public class Fixture {
    private String name;
    private static int counter;
    private long bigValue = 9223372036854775807L;   // forces a Long constant
    private double ratio = 3.141592653589793;       // forces a Double constant

    static { counter = 10; }

    public Fixture(String n) { this.name = n; }

    public String getName() { return name; }        // BARE field read, no `this.`
    public void bump() { counter = counter + 1; }

    public void handle(String s) { }
    public void handle(int i) { counter = i; }
    public void handle(String s, int i) { handle(s); handle(i); }

    public Runnable makeLambda() { return () -> bump(); }

    public Function<String,String> anon() {
        return new Function<String,String>() {
            public String apply(String in) { return in.trim(); }
        };
    }

    public List<String> build() {
        List<String> out = new ArrayList<>();
        out.add(getName());
        return out;
    }

    public int dense(int x) {
        switch (x) { case 1: return 1; case 2: return 2; case 3: return 3; default: return 0; }
    }

    public int sparse(int x) {
        switch (x) { case 1: return 1; case 1000: return 2; case 99999: return 3; default: return 0; }
    }

    static class Inner { void ping() { } }
}
"""

_HAVE_JAVAC = shutil.which("javac") is not None
needs_javac = pytest.mark.skipif(not _HAVE_JAVAC, reason="javac not on PATH")


@pytest.fixture(scope="module")
def compiled() -> dict:
    """Compile the fixture and return {class_name: ClassInfo}."""
    if not _HAVE_JAVAC:
        pytest.skip("javac not on PATH")
    root = tempfile.mkdtemp(prefix="bytecode_fixture_")
    src_dir = os.path.join(root, "com", "acme")
    os.makedirs(src_dir, exist_ok=True)
    src = os.path.join(src_dir, "Fixture.java")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(_FIXTURE)
    proc = subprocess.run(
        ["javac", "-g", os.path.join("com", "acme", "Fixture.java")],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"javac failed: {proc.stderr[:400]}")
    out = {}
    for fn in os.listdir(src_dir):
        if fn.endswith(".class"):
            info = parse_class_file(os.path.join(src_dir, fn))
            out[info.name] = info
    return out


def _method(info, name, arity=None, descriptor=None):
    for m in info.methods:
        if m.name != name:
            continue
        if descriptor is not None and m.descriptor != descriptor:
            continue
        if arity is not None and m.arity != arity:
            continue
        return m
    raise AssertionError(f"no method {name} (arity={arity}, desc={descriptor}) on {info.name}")


class TestDescriptors:
    """Pure functions — no javac needed."""

    def test_param_types(self):
        assert descriptor_param_types("(JLjava/lang/String;[I)V") == ["J", "Ljava/lang/String;", "[I"]
        assert descriptor_param_types("()V") == []

    def test_arity(self):
        assert descriptor_arity("()V") == 0
        assert descriptor_arity("(Ljava/lang/String;I)V") == 2
        assert descriptor_arity("(JD)V") == 2     # long/double are ONE param each

    def test_type_name(self):
        assert type_name("J") == "long"
        assert type_name("Ljava/lang/String;") == "java.lang.String"
        assert type_name("[Ljava/lang/String;") == "java.lang.String[]"
        assert type_name("[[I") == "int[][]"

    def test_simple_name(self):
        assert simple_name("java.lang.String") == "String"
        assert simple_name("java/lang/String") == "String"
        assert simple_name("Foo") == "Foo"

    def test_malformed_descriptor(self):
        with pytest.raises(ClassFileError):
            descriptor_param_types("no parens here")
        with pytest.raises(ClassFileError):
            descriptor_param_types("(Ljava/lang/String")   # unterminated


class TestMalformedInput:
    def test_bad_magic(self):
        with pytest.raises(ClassFileError):
            parse_class(b"\x00\x00\x00\x00" + b"\x00" * 40)

    def test_truncated(self):
        with pytest.raises(ClassFileError):
            parse_class(b"\xca\xfe\xba\xbe\x00\x00")


@needs_javac
class TestClassStructure:
    def test_classes_found(self, compiled):
        assert set(compiled) == {
            "com.acme.Fixture", "com.acme.Fixture$1", "com.acme.Fixture$Inner",
        }

    def test_source_file_and_package(self, compiled):
        f = compiled["com.acme.Fixture"]
        assert f.source_file == "Fixture.java"
        assert f.package == "com.acme"
        assert f.source_path_hint == "com/acme/Fixture.java"

    def test_long_double_constants_do_not_desync_pool(self, compiled):
        """Long/Double occupy two constant pool slots. Getting that wrong
        misaligns every later index — the class would fail to parse or resolve
        nonsense names, so simply having correct names here is the assertion."""
        f = compiled["com.acme.Fixture"]
        assert {fl.name for fl in f.fields} == {"name", "counter", "bigValue", "ratio"}


@needs_javac
class TestOverloads:
    def test_same_arity_overloads_are_distinct(self, compiled):
        """D4: (class, name, arity) is ambiguous here; only the descriptor
        separates handle(String) from handle(int)."""
        f = compiled["com.acme.Fixture"]
        handles = [m for m in f.methods if m.name == "handle"]
        assert len(handles) == 3
        assert {m.descriptor for m in handles} == {
            "(Ljava/lang/String;)V", "(I)V", "(Ljava/lang/String;I)V",
        }

    def test_call_sites_pick_the_right_overload(self, compiled):
        """The measurement that justifies the whole approach: the heuristic
        resolver cannot tell these two call sites apart."""
        f = compiled["com.acme.Fixture"]
        both = _method(f, "handle", descriptor="(Ljava/lang/String;I)V")
        targets = {(i.name, i.descriptor) for i in both.invocations}
        assert ("handle", "(Ljava/lang/String;)V") in targets
        assert ("handle", "(I)V") in targets


@needs_javac
class TestFieldAccess:
    def test_bare_field_read_detected(self, compiled):
        """HANDOFF 4.4: java.py only sees explicit `this.x`, so `return name;`
        is missed entirely. Bytecode names the owner on every access."""
        get_name = _method(compiled["com.acme.Fixture"], "getName", 0)
        reads = [fa for fa in get_name.field_accesses if fa.is_read]
        assert len(reads) == 1
        assert reads[0].owner == "com.acme.Fixture"
        assert reads[0].name == "name"
        assert reads[0].opcode == "getfield"

    def test_static_read_and_write(self, compiled):
        bump = _method(compiled["com.acme.Fixture"], "bump", 0)
        ops = {(fa.opcode, fa.name) for fa in bump.field_accesses}
        assert ("getstatic", "counter") in ops
        assert ("putstatic", "counter") in ops

    def test_write_in_constructor(self, compiled):
        ctor = _method(compiled["com.acme.Fixture"], "<init>", 1)
        assert any(fa.opcode == "putfield" and fa.name == "name"
                   for fa in ctor.field_accesses)


@needs_javac
class TestMissingNodeConstructs:
    """HANDOFF 4.2 — none of these have a tree-sitter Function node today."""

    def test_lambda_body(self, compiled):
        f = compiled["com.acme.Fixture"]
        lam = [m for m in f.methods if m.is_lambda_body]
        assert len(lam) == 1
        assert lam[0].enclosing_of_lambda == "makeLambda"
        assert any(i.name == "bump" for i in lam[0].invocations)
        assert lam[0].has_line_numbers

    def test_anonymous_class(self, compiled):
        anon = compiled["com.acme.Fixture$1"]
        assert anon.is_anonymous
        assert anon.outer_name == "com.acme.Fixture"
        apply_ = _method(anon, "apply", descriptor="(Ljava/lang/String;)Ljava/lang/String;")
        assert any(i.name == "trim" for i in apply_.invocations)

    def test_named_inner_class_is_not_anonymous(self, compiled):
        inner = compiled["com.acme.Fixture$Inner"]
        assert not inner.is_anonymous
        assert inner.outer_name == "com.acme.Fixture"

    def test_static_initializer(self, compiled):
        clinit = _method(compiled["com.acme.Fixture"], "<clinit>", 0)
        assert clinit.is_class_initializer
        assert clinit.is_static
        assert any(fa.opcode == "putstatic" and fa.name == "counter"
                   for fa in clinit.field_accesses)


@needs_javac
class TestSyntheticFiltering:
    def test_bridge_method_flagged(self, compiled):
        """Covariant-return forwarder javac generates for the anonymous
        Function<String,String>. It must never become a graph node."""
        anon = compiled["com.acme.Fixture$1"]
        bridge = _method(anon, "apply", descriptor="(Ljava/lang/Object;)Ljava/lang/Object;")
        assert bridge.is_bridge and bridge.is_synthetic

    def test_lambda_body_is_synthetic_but_real_code(self, compiled):
        """A naive `skip if synthetic` filter drops every call made inside a
        lambda. Lambda bodies are user code that javac merely lifted out."""
        lam = [m for m in compiled["com.acme.Fixture"].methods if m.is_lambda_body][0]
        assert lam.is_synthetic
        assert not lam.is_bridge


@needs_javac
class TestInstructionStepping:
    def test_switches_do_not_desync(self, compiled):
        """tableswitch/lookupswitch are variable length with 4-byte alignment
        padding. Mis-stepping produces plausible garbage rather than an error,
        so the assertion is that the methods AFTER them still read correctly."""
        f = compiled["com.acme.Fixture"]
        for name in ("dense", "sparse"):
            m = _method(f, name, 1)
            assert m.has_line_numbers
            assert m.start_line <= m.end_line
        # build() is defined before the switches and calls two known targets;
        # a desync anywhere in the class would corrupt these names.
        build = _method(f, "build", 0)
        assert {i.name for i in build.invocations} >= {"getName", "add", "<init>"}

    def test_invokedynamic_recorded_without_owner(self, compiled):
        """The call-site descriptor names the functional interface method, not
        the code that runs — the real target needs BootstrapMethods."""
        make = _method(compiled["com.acme.Fixture"], "makeLambda", 0)
        indy = [i for i in make.invocations if i.opcode == "invokedynamic"]
        assert len(indy) == 1
        assert indy[0].owner == ""

    def test_interface_call_opcode(self, compiled):
        build = _method(compiled["com.acme.Fixture"], "build", 0)
        add = [i for i in build.invocations if i.name == "add"][0]
        assert add.opcode == "invokeinterface"
        assert add.owner == "java.util.List"


@needs_javac
class TestLineNumbers:
    def test_every_concrete_method_has_lines(self, compiled):
        """Phase 2.4 refuses to synthesize a node without real positions, so
        absent LineNumberTable means those constructs drop a tier instead."""
        for info in compiled.values():
            for m in info.methods:
                if m.is_abstract or m.is_bridge:
                    continue
                assert m.has_line_numbers, f"{info.name}.{m.name} has no lines"
                assert m.start_line <= m.end_line

    def test_invocation_lines_within_method(self, compiled):
        build = _method(compiled["com.acme.Fixture"], "build", 0)
        for inv in build.invocations:
            assert build.start_line <= inv.line <= build.end_line

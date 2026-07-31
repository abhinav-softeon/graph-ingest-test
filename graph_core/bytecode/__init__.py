"""Java bytecode reading — exact call bindings without a JVM.

The heuristic resolver and javac both answer "what does this call site bind
to?" by inference. Compiled bytecode does not have to: every call instruction
carries the resolved owner class, method name and descriptor, because javac
already did the work and wrote the answer down.

See IMPLEMENTATION_PLAN.md D1/D2 for why this reads as a *resolver* (edges only,
nodes still come from tree-sitter) and why the parser is pure Python.
"""
from __future__ import annotations

from .classfile import (
    ClassFileError,
    ClassInfo,
    FieldAccess,
    FieldInfo,
    Invocation,
    MethodInfo,
    descriptor_arity,
    descriptor_param_types,
    iter_jar_classes,
    parse_class,
    parse_class_file,
    simple_name,
    type_name,
)

__all__ = [
    "ClassFileError", "ClassInfo", "FieldAccess", "FieldInfo", "Invocation",
    "MethodInfo", "descriptor_arity", "descriptor_param_types",
    "iter_jar_classes", "parse_class", "parse_class_file", "simple_name",
    "type_name",
]

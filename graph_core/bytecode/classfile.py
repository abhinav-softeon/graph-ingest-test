"""Minimal Java class file reader (JVMS chapter 4).

Parses exactly what a call graph needs and skips the rest: the constant pool,
fields, methods, and — inside each method's Code attribute — only the
instructions that create an edge (invoke*, field access, `new`), plus the
LineNumberTable that maps them back to source lines.

Everything else is stepped over by declared length. That is deliberate and is
what makes this forward-compatible with future Java releases: an attribute this
code has never heard of costs one length read, not a parse failure.

Three details account for most class-file parser bugs, and each is handled
explicitly below:

  * the constant pool is **1-indexed**, and Long/Double entries occupy **two**
    slots while only being addressable at the first
  * unknown *attributes* can be skipped by length, but unknown *constant pool
    tags* cannot — their size is not self-describing, so hitting one is fatal
  * `tableswitch`/`lookupswitch` are variable length with 4-byte alignment
    padding measured from the start of the code array, and `wide` re-reads the
    following opcode; getting any of these wrong desynchronises the instruction
    stream and produces silent garbage rather than an error
"""
from __future__ import annotations

import os
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Iterator

MAGIC = 0xCAFEBABE

# Access flags (JVMS 4.1 / 4.5 / 4.6)
ACC_PUBLIC = 0x0001
ACC_PRIVATE = 0x0002
ACC_PROTECTED = 0x0004
ACC_STATIC = 0x0008
ACC_FINAL = 0x0010
ACC_INTERFACE = 0x0200
ACC_ABSTRACT = 0x0400
ACC_SYNTHETIC = 0x1000
ACC_ENUM = 0x4000
ACC_BRIDGE = 0x0040          # methods only
ACC_SUPER = 0x0020           # classes only (same bit as ACC_BRIDGE)

# Constant pool tags -> (struct format for the fixed body, slots consumed)
_CP_LAYOUT = {
    3: (">i", 1),        # Integer
    4: (">f", 1),        # Float
    5: (">q", 2),        # Long    -- two slots
    6: (">d", 2),        # Double  -- two slots
    7: (">H", 1),        # Class            -> name_index
    8: (">H", 1),        # String
    9: (">HH", 1),       # Fieldref
    10: (">HH", 1),      # Methodref
    11: (">HH", 1),      # InterfaceMethodref
    12: (">HH", 1),      # NameAndType
    16: (">H", 1),       # MethodType
    17: (">HH", 1),      # Dynamic
    18: (">HH", 1),      # InvokeDynamic
    19: (">H", 1),       # Module
    20: (">H", 1),       # Package
}
_CP_UTF8 = 1
_CP_METHOD_HANDLE = 15   # u1 + u2, irregular

_MEMBER_REF_TAGS = (9, 10, 11)

# Opcodes that produce a graph edge.
_INVOKE_OPS = {0xb6: "invokevirtual", 0xb7: "invokespecial", 0xb8: "invokestatic",
               0xb9: "invokeinterface", 0xba: "invokedynamic"}
_FIELD_OPS = {0xb2: "getstatic", 0xb3: "putstatic", 0xb4: "getfield", 0xb5: "putfield"}
_NEW_OP = 0xbb

# Operand byte count per opcode, for stepping the instruction stream. Only the
# length matters here, not the semantics. Variable-length opcodes (0xaa, 0xab,
# 0xc4) are marked -1 and handled inline.
_OPERAND_LEN = {}


def _init_operand_lengths() -> None:
    L = _OPERAND_LEN
    for op in range(0x00, 0x100):
        L[op] = 0
    for op in (0x10, 0x12, 0x15, 0x16, 0x17, 0x18, 0x19,
               0x36, 0x37, 0x38, 0x39, 0x3a, 0xa9, 0xbc):
        L[op] = 1
    for op in (0x11, 0x13, 0x14, 0x84, 0xb2, 0xb3, 0xb4, 0xb5,
               0xb6, 0xb7, 0xb8, 0xbb, 0xbd, 0xc0, 0xc1, 0xc6, 0xc7):
        L[op] = 2
    # conditional branches + goto/jsr
    for op in range(0x99, 0xa9):
        L[op] = 2
    L[0xc5] = 3                       # multianewarray
    for op in (0xb9, 0xba, 0xc8, 0xc9):
        L[op] = 4                     # invokeinterface/invokedynamic/goto_w/jsr_w
    for op in (0xaa, 0xab, 0xc4):
        L[op] = -1                    # tableswitch / lookupswitch / wide


_init_operand_lengths()

_PRIMITIVES = {
    "B": "byte", "C": "char", "D": "double", "F": "float",
    "I": "int", "J": "long", "S": "short", "Z": "boolean", "V": "void",
}


class ClassFileError(Exception):
    """Malformed, truncated, or unparseable class file.

    Raised per file so a caller can skip one bad class rather than lose a batch.
    """


# --------------------------------------------------------------------------
# descriptors
# --------------------------------------------------------------------------

def descriptor_param_types(descriptor: str) -> list[str]:
    """Raw parameter descriptors from a method descriptor.

    ``(JLjava/lang/String;[I)V`` -> ``['J', 'Ljava/lang/String;', '[I']``
    """
    try:
        i = descriptor.index("(") + 1
    except ValueError:
        raise ClassFileError(f"not a method descriptor: {descriptor!r}")
    out: list[str] = []
    while i < len(descriptor) and descriptor[i] != ")":
        start = i
        while i < len(descriptor) and descriptor[i] == "[":
            i += 1
        if i >= len(descriptor):
            raise ClassFileError(f"truncated descriptor: {descriptor!r}")
        if descriptor[i] == "L":
            end = descriptor.find(";", i)
            if end < 0:
                raise ClassFileError(f"unterminated object type: {descriptor!r}")
            i = end + 1
        else:
            i += 1
        out.append(descriptor[start:i])
    return out


def descriptor_arity(descriptor: str) -> int:
    """Parameter count. This is the primary key for matching bytecode methods
    onto tree-sitter nodes — see IMPLEMENTATION_PLAN.md D4."""
    return len(descriptor_param_types(descriptor))


def type_name(descriptor: str) -> str:
    """Field/parameter descriptor to a readable dotted type.

    ``[Ljava/lang/String;`` -> ``java.lang.String[]``; ``J`` -> ``long``.
    """
    arrays = 0
    while descriptor.startswith("["):
        arrays += 1
        descriptor = descriptor[1:]
    if descriptor.startswith("L") and descriptor.endswith(";"):
        base = descriptor[1:-1].replace("/", ".")
    else:
        base = _PRIMITIVES.get(descriptor, descriptor)
    return base + "[]" * arrays


def simple_name(dotted: str) -> str:
    """Last segment of a dotted (or internal) type name, arrays preserved.

    Used for overload disambiguation, where comparing erased simple names is
    the most that can be done without resolving imports.
    """
    return dotted.replace("/", ".").rsplit(".", 1)[-1]


# --------------------------------------------------------------------------
# parsed shapes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Invocation:
    """One call instruction, with its target already resolved by javac."""
    opcode: str          # invokevirtual | invokespecial | invokestatic | ...
    owner: str           # dotted owner class; '' for invokedynamic
    name: str
    descriptor: str
    line: int            # -1 when no LineNumberTable covers this offset
    offset: int

    @property
    def arity(self) -> int:
        return descriptor_arity(self.descriptor)

    @property
    def is_constructor(self) -> bool:
        return self.name == "<init>"


@dataclass(frozen=True)
class FieldAccess:
    """A field read or write, with the exact owning class.

    This is what closes HANDOFF 4.4: the tree-sitter extractor only sees
    explicit ``this.x``, so bare ``x`` references to fields are missed entirely
    (WRITES=51,867 vs READS=3,521). Bytecode names the owner on every access.
    """
    opcode: str          # getfield | putfield | getstatic | putstatic
    owner: str
    name: str
    descriptor: str
    line: int

    @property
    def is_read(self) -> bool:
        return self.opcode in ("getfield", "getstatic")


@dataclass(frozen=True)
class FieldInfo:
    name: str
    descriptor: str
    access_flags: int

    @property
    def is_static(self) -> bool:
        return bool(self.access_flags & ACC_STATIC)

    @property
    def is_synthetic(self) -> bool:
        return bool(self.access_flags & ACC_SYNTHETIC)


@dataclass
class MethodInfo:
    name: str
    descriptor: str
    access_flags: int
    invocations: list[Invocation] = field(default_factory=list)
    field_accesses: list[FieldAccess] = field(default_factory=list)
    instantiations: list[str] = field(default_factory=list)
    start_line: int = -1
    end_line: int = -1

    @property
    def arity(self) -> int:
        return descriptor_arity(self.descriptor)

    @property
    def param_types(self) -> list[str]:
        return [type_name(d) for d in descriptor_param_types(self.descriptor)]

    @property
    def is_static(self) -> bool:
        return bool(self.access_flags & ACC_STATIC)

    @property
    def is_abstract(self) -> bool:
        return bool(self.access_flags & ACC_ABSTRACT)

    @property
    def is_synthetic(self) -> bool:
        return bool(self.access_flags & ACC_SYNTHETIC)

    @property
    def is_bridge(self) -> bool:
        return bool(self.access_flags & ACC_BRIDGE)

    @property
    def is_constructor(self) -> bool:
        return self.name == "<init>"

    @property
    def is_class_initializer(self) -> bool:
        return self.name == "<clinit>"

    @property
    def is_lambda_body(self) -> bool:
        """javac compiles a lambda body to a synthetic method named
        ``lambda$<enclosing>$<n>``. It has no tree-sitter node, so Phase 2
        synthesizes one — which is how HANDOFF 4.2 gets closed."""
        return self.name.startswith("lambda$")

    @property
    def has_line_numbers(self) -> bool:
        return self.start_line > 0

    @property
    def enclosing_of_lambda(self) -> str:
        """For ``lambda$doWork$0`` -> ``doWork``. Empty if not a lambda body."""
        if not self.is_lambda_body:
            return ""
        return self.name[len("lambda$"):].rsplit("$", 1)[0]


@dataclass
class ClassInfo:
    name: str                    # dotted, '$' retained: com.acme.Outer$Inner
    super_name: str
    interfaces: list[str]
    access_flags: int
    major_version: int
    source_file: str             # e.g. 'Foo.java'; '' when not compiled with it
    fields: list[FieldInfo]
    methods: list[MethodInfo]

    @property
    def is_interface(self) -> bool:
        return bool(self.access_flags & ACC_INTERFACE)

    @property
    def is_synthetic(self) -> bool:
        return bool(self.access_flags & ACC_SYNTHETIC)

    @property
    def package(self) -> str:
        return self.name.rsplit(".", 1)[0] if "." in self.name else ""

    @property
    def outer_name(self) -> str:
        """Enclosing class for a nested/inner/anonymous class, else ''."""
        return self.name.rsplit("$", 1)[0] if "$" in self.name else ""

    @property
    def is_anonymous(self) -> bool:
        """``Outer$1`` — an anonymous inner class. These have no tree-sitter
        node at all (HANDOFF 4.2: object_creation_expression is not a
        _TYPE_DECLS type), and are exactly where JDBC callbacks live."""
        tail = self.name.rsplit("$", 1)[-1] if "$" in self.name else ""
        return tail.isdigit()

    @property
    def source_path_hint(self) -> str:
        """Best guess at the repo-relative source path, from the package and the
        SourceFile attribute. A *hint*: it has no source root prefix, so callers
        match it as a suffix rather than treating it as a path."""
        if not self.source_file:
            return ""
        pkg = self.package
        return f"{pkg.replace('.', '/')}/{self.source_file}" if pkg else self.source_file


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

class _Reader:
    """Bounds-checked big-endian cursor. Every short read becomes a
    ClassFileError rather than an IndexError or a silent wrap."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def _take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise ClassFileError(
                f"truncated: wanted {n} byte(s) at {self.pos}, have {len(self.data) - self.pos}"
            )
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def u1(self) -> int:
        return self._take(1)[0]

    def u2(self) -> int:
        return struct.unpack(">H", self._take(2))[0]

    def u4(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def skip(self, n: int) -> None:
        if n < 0:
            raise ClassFileError(f"negative skip: {n}")
        if self.pos + n > len(self.data):
            raise ClassFileError(f"truncated: skip {n} past end at {self.pos}")
        self.pos += n


def _parse_constant_pool(r: _Reader, count: int) -> list:
    """Entries indexed 1..count-1. Long/Double take two slots, and the second is
    unusable — placeholders keep every later index correct."""
    pool: list = [None] * count
    i = 1
    while i < count:
        tag = r.u1()
        if tag == _CP_UTF8:
            length = r.u2()
            pool[i] = r._take(length).decode("utf-8", "replace")
            i += 1
            continue
        if tag == _CP_METHOD_HANDLE:
            pool[i] = (tag, r.u1(), r.u2())
            i += 1
            continue
        layout = _CP_LAYOUT.get(tag)
        if layout is None:
            # Unknown tags cannot be skipped: entry size is not self-describing,
            # so everything after this point would be misaligned. Fail loudly.
            raise ClassFileError(f"unknown constant pool tag {tag} at index {i}")
        fmt, slots = layout
        values = struct.unpack(fmt, r._take(struct.calcsize(fmt)))
        pool[i] = (tag, *values)
        i += slots
    return pool


class _Pool:
    """Typed accessors over the raw constant pool."""

    __slots__ = ("raw",)

    def __init__(self, raw: list):
        self.raw = raw

    def utf8(self, index: int) -> str:
        if not (0 < index < len(self.raw)):
            raise ClassFileError(f"constant pool index {index} out of range")
        value = self.raw[index]
        if not isinstance(value, str):
            raise ClassFileError(f"constant pool index {index} is not Utf8")
        return value

    def class_name(self, index: int) -> str:
        """Class entry -> dotted name. '$' is retained: it distinguishes inner
        and anonymous classes, which Phase 2 needs."""
        if index == 0:
            return ""    # only legal for java/lang/Object's super_class
        entry = self.raw[index] if 0 < index < len(self.raw) else None
        if not (isinstance(entry, tuple) and entry[0] == 7):
            raise ClassFileError(f"constant pool index {index} is not a Class")
        return self.utf8(entry[1]).replace("/", ".")

    def member_ref(self, index: int) -> tuple[str, str, str]:
        """Fieldref/Methodref/InterfaceMethodref -> (owner, name, descriptor)."""
        entry = self.raw[index] if 0 < index < len(self.raw) else None
        if not (isinstance(entry, tuple) and entry[0] in _MEMBER_REF_TAGS):
            raise ClassFileError(f"constant pool index {index} is not a member ref")
        _tag, class_index, nat_index = entry
        nat = self.raw[nat_index] if 0 < nat_index < len(self.raw) else None
        if not (isinstance(nat, tuple) and nat[0] == 12):
            raise ClassFileError(f"constant pool index {nat_index} is not NameAndType")
        return self.class_name(class_index), self.utf8(nat[1]), self.utf8(nat[2])

    def invokedynamic_nat(self, index: int) -> tuple[str, str]:
        """InvokeDynamic -> (name, descriptor). The owner is unknowable without
        resolving BootstrapMethods, so callers get no owner."""
        entry = self.raw[index] if 0 < index < len(self.raw) else None
        if not (isinstance(entry, tuple) and entry[0] == 18):
            raise ClassFileError(f"constant pool index {index} is not InvokeDynamic")
        nat = self.raw[entry[2]] if 0 < entry[2] < len(self.raw) else None
        if not (isinstance(nat, tuple) and nat[0] == 12):
            raise ClassFileError("InvokeDynamic NameAndType missing")
        return self.utf8(nat[1]), self.utf8(nat[2])


def _skip_attributes(r: _Reader) -> None:
    for _ in range(r.u2()):
        r.u2()                 # name index
        r.skip(r.u4())         # body, by declared length


def _find_attributes(r: _Reader, pool: _Pool, wanted: set[str]) -> dict[str, bytes]:
    """Collect only the named attributes; step over everything else by declared
    length. This is what keeps the parser working on future class file
    versions."""
    found: dict[str, bytes] = {}
    for _ in range(r.u2()):
        name = pool.utf8(r.u2())
        length = r.u4()
        if name in wanted:
            found[name] = r._take(length)
        else:
            r.skip(length)
    return found


def _parse_line_number_table(body: bytes) -> list[tuple[int, int]]:
    r = _Reader(body)
    entries = [(r.u2(), r.u2()) for _ in range(r.u2())]
    entries.sort()
    return entries


def _line_for_offset(table: list[tuple[int, int]], offset: int) -> int:
    """Line of the last table entry at or before this bytecode offset."""
    if not table:
        return -1
    best = -1
    for start_pc, line in table:
        if start_pc > offset:
            break
        best = line
    return best


def _scan_code(code: bytes, pool: _Pool, lines: list[tuple[int, int]],
               method: MethodInfo) -> None:
    """Walk the instruction stream, recording only edge-producing opcodes.

    Correct stepping is the whole job: a single mis-sized instruction shifts
    every subsequent read and yields plausible-looking nonsense instead of an
    error. Hence the explicit handling of switch padding and `wide`.
    """
    pos = 0
    end = len(code)
    while pos < end:
        offset = pos
        op = code[pos]
        pos += 1

        if op in _INVOKE_OPS:
            index = struct.unpack(">H", code[pos:pos + 2])[0]
            opname = _INVOKE_OPS[op]
            if op == 0xba:
                # invokedynamic: lambdas, method refs, and (Java 9+) string
                # concatenation all land here. The real target lives in
                # BootstrapMethods; the call-site name/descriptor alone would
                # name the functional interface method, not the code that runs.
                # Recorded with no owner so consumers can filter it out.
                name, desc = pool.invokedynamic_nat(index)
                owner = ""
            else:
                owner, name, desc = pool.member_ref(index)
            method.invocations.append(Invocation(
                opcode=opname, owner=owner, name=name, descriptor=desc,
                line=_line_for_offset(lines, offset), offset=offset,
            ))
            pos += 4 if op in (0xb9, 0xba) else 2
            continue

        if op in _FIELD_OPS:
            owner, name, desc = pool.member_ref(struct.unpack(">H", code[pos:pos + 2])[0])
            method.field_accesses.append(FieldAccess(
                opcode=_FIELD_OPS[op], owner=owner, name=name, descriptor=desc,
                line=_line_for_offset(lines, offset),
            ))
            pos += 2
            continue

        if op == _NEW_OP:
            method.instantiations.append(
                pool.class_name(struct.unpack(">H", code[pos:pos + 2])[0])
            )
            pos += 2
            continue

        operand_len = _OPERAND_LEN[op]
        if operand_len >= 0:
            pos += operand_len
            continue

        if op == 0xc4:                       # wide
            if pos >= end:
                raise ClassFileError("truncated wide instruction")
            # wide iinc has two u2 operands; every other widened opcode has one
            pos += 5 if code[pos] == 0x84 else 3
            continue

        # tableswitch / lookupswitch: pad to the next 4-byte boundary measured
        # from the START of the code array, not from the current position.
        pos += (4 - (pos % 4)) % 4
        if op == 0xaa:                       # tableswitch
            low, high = struct.unpack(">ii", code[pos + 4:pos + 12])
            pos += 12 + max(0, high - low + 1) * 4
        else:                                # lookupswitch
            npairs = struct.unpack(">i", code[pos + 4:pos + 8])[0]
            pos += 8 + max(0, npairs) * 8


def _parse_members(r: _Reader, pool: _Pool, with_code: bool):
    """fields[] and methods[] share a layout; only methods carry Code."""
    out = []
    for _ in range(r.u2()):
        access_flags = r.u2()
        name = pool.utf8(r.u2())
        descriptor = pool.utf8(r.u2())
        if not with_code:
            _skip_attributes(r)
            out.append(FieldInfo(name=name, descriptor=descriptor, access_flags=access_flags))
            continue

        method = MethodInfo(name=name, descriptor=descriptor, access_flags=access_flags)
        attrs = _find_attributes(r, pool, {"Code"})
        code_attr = attrs.get("Code")
        if code_attr is not None:
            cr = _Reader(code_attr)
            cr.skip(4)                       # max_stack, max_locals
            code = cr._take(cr.u4())
            cr.skip(cr.u2() * 8)             # exception_table
            code_attrs = _find_attributes(cr, pool, {"LineNumberTable"})
            lines = _parse_line_number_table(code_attrs["LineNumberTable"]) \
                if "LineNumberTable" in code_attrs else []
            if lines:
                numbers = [ln for _pc, ln in lines]
                method.start_line = min(numbers)
                method.end_line = max(numbers)
            _scan_code(code, pool, lines, method)
        out.append(method)
    return out


def parse_class(data: bytes) -> ClassInfo:
    """Parse one class file's bytes."""
    r = _Reader(data)
    if r.u4() != MAGIC:
        raise ClassFileError("bad magic — not a class file")
    minor = r.u2()      # noqa: F841 - read for position, not used
    major = r.u2()
    pool = _Pool(_parse_constant_pool(r, r.u2()))

    access_flags = r.u2()
    name = pool.class_name(r.u2())
    super_name = pool.class_name(r.u2())
    interfaces = [pool.class_name(r.u2()) for _ in range(r.u2())]

    fields = _parse_members(r, pool, with_code=False)
    methods = _parse_members(r, pool, with_code=True)

    source_file = ""
    attrs = _find_attributes(r, pool, {"SourceFile"})
    if "SourceFile" in attrs:
        source_file = pool.utf8(struct.unpack(">H", attrs["SourceFile"][:2])[0])

    return ClassInfo(
        name=name, super_name=super_name, interfaces=interfaces,
        access_flags=access_flags, major_version=major, source_file=source_file,
        fields=fields, methods=methods,
    )


def parse_class_file(path: str) -> ClassInfo:
    with open(path, "rb") as fh:
        return parse_class(fh.read())


def iter_jar_classes(path: str, skip_meta_inf: bool = True) -> Iterator[tuple[str, ClassInfo]]:
    """Yield ``(entry_name, ClassInfo)`` for each class in a jar/war/ear.

    Unparseable entries are skipped rather than aborting the archive: a jar
    holds hundreds of independent classes and one bad entry should cost one
    class. Nested archives (``WEB-INF/lib/*.jar`` inside a war) are NOT
    descended into — the caller decides whether dependency jars are wanted.
    """
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.endswith(".class"):
                continue
            if skip_meta_inf and info.filename.startswith("META-INF/"):
                continue
            try:
                yield info.filename, parse_class(zf.read(info))
            except (ClassFileError, struct.error, zipfile.BadZipFile):
                continue


def iter_class_files(root: str) -> Iterator[tuple[str, ClassInfo]]:
    """Yield ``(relpath, ClassInfo)`` for every .class file under a directory."""
    root = os.path.abspath(root)
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".class"):
                continue
            abspath = os.path.join(dirpath, fn)
            try:
                info = parse_class_file(abspath)
            except (ClassFileError, struct.error, OSError):
                continue
            yield os.path.relpath(abspath, root).replace("\\", "/"), info

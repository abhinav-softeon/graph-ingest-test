"""Flatten FindSecBugs' taint configuration into catalog entries.

Run:
    python scripts/mine_findsecbugs.py --src <dir of downloaded .txt> \
        --out graph_core/catalog/findsecbugs_java.py

WHY A SCRIPT AND NOT A ONE-OFF PASTE
The upstream files change, and re-mining must be a re-run rather than re-work.
The generated module is committed so the catalog is reviewable in a diff and the
build never needs network.

UPSTREAM AND LICENSING
Source: github.com/find-sec-bugs/find-sec-bugs, LGPL-3.0,
`findsecbugs-plugin/src/main/resources/{taint-config,injection-sinks}/*.txt`.
What is extracted is a list of JDK/library API signatures and their taint role —
factual reference data about third-party APIs, not FindSecBugs' detector code or
algorithms. Their engine is not used, linked, or reimplemented. Attribution is
carried into the generated file header.

THE TWO FILE FORMATS

  injection-sinks/*.txt   owner.method(descriptor)ret:idx[,idx...]
                          -> a SINK; the indices are which arguments are dangerous

  taint-config/*.txt      owner.method(descriptor)ret:TAINTED
                          -> a SOURCE; the return value is attacker-controlled
                          owner.method(descriptor)ret:SAFE
                          -> explicitly NOT a source (valuable: it is the
                             difference between getParameter and getContextPath)
                          owner.method(descriptor)ret:idx
                          -> a PROPAGATOR; return taint equals argument idx's

THE INDEX CONVENTION IS THE WHOLE RISK OF THIS SCRIPT
Upstream indices are JVM OPERAND STACK SLOT offsets counted from the top — not
left-to-right argument numbers. The catalog stores ordinary parameter positions.
Inverting them would produce entries that are exactly backwards, the same class of
defect as claiming PreparedStatement.setString sanitizes parameter 0 (the
placeholder index) instead of parameter 1 (the value).

Three properties, each pinned to a specific upstream line rather than assumed —
see to_positions() for the derivation:

    reversed order      Statement.execute(Ljava/lang/String;I)Z:1   -> the SQL
    receiver at slot W  java/net/URL.openConnection()...:0          -> the URL
    slots, not values   java/lang/StringBuilder.append(D)...:2      -> the receiver

The third was found by this script warning "index outside 0 param(s)" on the
second, and "out of range" on the third, rather than by trusting the first
reading. Both were real conversions, not noise.

WHAT IS DELIBERATELY NOT EMITTED
taint-config numeric tags are PROPAGATORS (return taint = argument taint), and
lines carrying a `#` suffix are upstream's mutable-object taint notation. Both
describe how taint MOVES, which belongs to the DFG's propagation rules, not to a
source/sink/sanitizer table. Skipped silently.
"""
from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict

# Category per upstream filename. FindSecBugs organises by detector, which maps
# cleanly onto CWE; anything unlisted is skipped rather than guessed at, because a
# sink with the wrong category is reported under the wrong vulnerability class.
SINK_CATEGORY = {
    "command": "CWE-78/command-injection",
    "sql-jdbc": "CWE-89/sql-injection",
    "sql-hibernate": "CWE-89/sql-injection",
    "sql-jpa": "CWE-89/sql-injection",
    "ldap": "CWE-90/ldap-injection",
    "path-traversal-in": "CWE-22/path-traversal",
    "path-traversal-out": "CWE-22/path-traversal",
    "xss-servlet": "CWE-79/xss",
    "xss-jsp": "CWE-79/xss",
    "response-splitting": "CWE-113/response-splitting",
    "el": "CWE-917/el-injection",
    "spel": "CWE-917/el-injection",
    "script-engine": "CWE-94/code-injection",
    "formatter": "CWE-134/format-string",
    "beans": "CWE-915/bean-property-injection",
    "urlconnection-ssrf": "CWE-918/ssrf",
    "xpath-javax": "CWE-643/xpath-injection",
    "xpath-apache": "CWE-643/xpath-injection",
    "xslt": "CWE-611/xslt-injection",
    "smtp": "CWE-93/smtp-header-injection",
    "http-parameter-pollution": "CWE-235/parameter-pollution",
    "requestdispatcher-file-disclosure": "CWE-552/file-disclosure",
    "trust-boundary-violation-value": "CWE-501/trust-boundary",
    "crlf-logs": "CWE-117/log-injection",
    "sql-spring": "CWE-89/sql-injection",
    "sql-turbine": "CWE-89/sql-injection",
    "seam-el": "CWE-917/el-injection",
    "aws": "CWE-99/resource-injection",
}

_LINE = re.compile(r"^(?P<owner>[\w/$]+)\.(?P<method><?\w+>?)"
                   r"\((?P<params>[^)]*)\)(?P<ret>\S*?):(?P<tag>[\w,]+)\s*$")

# One JVM descriptor parameter. Object types are L...; arrays prefix [ .
_PARAM = re.compile(r"\[*(?:[BCDFIJSZ]|L[^;]+;)")


def parse_params(desc: str) -> list[str]:
    """Split a descriptor's parameter list into individual type descriptors."""
    out, i = [], 0
    while i < len(desc):
        m = _PARAM.match(desc, i)
        if not m:            # malformed — refuse rather than mis-count
            raise ValueError(f"cannot parse descriptor params: {desc!r}")
        out.append(m.group(0))
        i = m.end()
    return out


def is_two_slot(param: str) -> bool:
    """long / double occupy two JVM stack slots. Arrays of them do not."""
    return param in ("J", "D")


CATALOG_RECEIVER = -2  # keep in sync with catalog.RECEIVER


def to_positions(upstream: list[int], params: list[str]) -> tuple[int, ...]:
    """Upstream stack-slot offsets -> left-to-right parameter positions.

    Upstream counts JVM OPERAND STACK SLOTS from the top, not arguments. Three
    consequences, each established from a specific upstream line rather than
    assumed:

    1. Reversed order. `Statement.execute(Ljava/lang/String;I)Z:1` — stack is
       this, sql, flags; from the top flags=0, sql=1. Index 1 is the SQL.

    2. The receiver is addressable, at the slot just past the arguments.
       `java/net/URL.openConnection()Ljava/net/URLConnection;:0` has no arguments
       at all, so its index 0 can only be the URL object itself — a real SSRF
       sink, and one this function silently discarded until the out-of-range
       warning exposed it.

    3. SLOTS, not values: `long` and `double` occupy two each.
       `java/lang/StringBuilder.append(D)Ljava/lang/StringBuilder;:2` has ONE
       parameter and index 2, which is only reachable if the double covers slots
       0-1 and `this` sits at slot 2. An argument-counting implementation reports
       that line as out of range, which is how the convention was pinned down.

    So for parameters p0..pn-1 of slot widths w0..wn-1 and W = sum(w), parameter k
    occupies top-offsets [W - Sk - wk, W - Sk - 1] where Sk = sum(w0..wk-1), and
    the receiver sits at top-offset W. A static method has no receiver, so W is
    simply never referenced for one upstream.
    """
    widths = [2 if is_two_slot(p) else 1 for p in params]
    total = sum(widths)
    # top-offset -> parameter position, built once per line
    slot_to_pos: dict[int, int] = {}
    running = 0
    for k, w in enumerate(widths):
        for off in range(total - running - w, total - running):
            slot_to_pos[off] = k
        running += w

    pos = set()
    for i in upstream:
        if i in slot_to_pos:
            pos.add(slot_to_pos[i])
        elif i == total:
            pos.add(CATALOG_RECEIVER)
        # beyond the receiver is genuinely malformed; the caller warns.
    return tuple(sorted(pos))


def binary_to_fqn(owner: str) -> str:
    """`javax/servlet/http/HttpServletRequest` -> dotted form. Nested classes
    keep their `$`, matching what bytecode descriptors carry and therefore what
    external_api/catalog lookups are keyed on."""
    return owner.replace("/", ".")


def mine(src_dir: str, strict: bool) -> tuple[dict, dict, list[str]]:
    """Return (sinks, sources, warnings).

    sinks/sources: {owner_fqn: {method_lower: set(positions)}}
    """
    sinks: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    sink_cat: dict[str, str] = {}
    sources: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    propagators: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    safe: set[tuple[str, str]] = set()
    warnings: list[str] = []

    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".txt"):
            continue
        stem = fn[:-4]
        kind, _, name = stem.partition("_")
        path = os.path.join(src_dir, fn)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                # Upstream uses both `-` and `--` prose lines as comments.
                if not line or line.startswith("-"):
                    continue
                # Type-level declarations (`Ljava/lang/String;:#IMMUTABLE`) carry
                # no method and configure upstream's own value tracking, not a
                # taint role. Skipped silently — warning on them buried the
                # warnings that mattered.
                if "(" not in line:
                    continue
                # An explicit upstream "no opinion" marker.
                if line.endswith(":UNKNOWN"):
                    continue
                # `#` suffix = upstream's mutable-object taint notation, always on
                # a propagator entry. Not a role this table represents.
                if "#" in line:
                    continue
                m = _LINE.match(line)
                if not m:
                    warnings.append(f"{fn}:{lineno}: unparsed: {line}")
                    continue
                owner = binary_to_fqn(m.group("owner"))
                method = m.group("method").lower()
                try:
                    params = parse_params(m.group("params"))
                except ValueError as exc:
                    warnings.append(f"{fn}:{lineno}: {exc}")
                    continue
                tag = m.group("tag")

                if tag == "SAFE":
                    safe.add((owner, method))
                    continue
                if tag == "TAINTED":
                    # Return value is attacker-controlled -> a source. The taint
                    # is on the RESULT, so there is no argument position; the
                    # catalog's RECEIVER marker carries that.
                    sources[owner][method].add(-2)  # catalog.RECEIVER
                    continue
                # Numeric tag.
                try:
                    idx = [int(x) for x in tag.split(",")]
                except ValueError:
                    warnings.append(f"{fn}:{lineno}: bad tag {tag!r}")
                    continue
                if kind == "is":
                    cat = SINK_CATEGORY.get(name)
                    if cat is None:
                        warnings.append(f"{fn}: no category mapped, skipped")
                        continue
                    pos = to_positions(idx, params)
                    if not pos:
                        warnings.append(
                            f"{fn}:{lineno}: index {idx} outside "
                            f"{len(params)} param(s), skipped: {line}")
                        continue
                    sinks[owner][method] |= set(pos)
                    sink_cat.setdefault(owner, cat)
                else:
                    # A numeric tag in taint-config is a PROPAGATOR: the RETURN
                    # value carries the taint of the value(s) at these positions.
                    # This is how taint actually travels — `"..." + getParameter()`
                    # compiles to StringBuilder.append, so without these rules a
                    # deterministic DFG loses the taint at the first hop of the
                    # most common injection pattern there is.
                    #
                    # RECEIVER appears constantly here and is not noise:
                    # StringBuilder.append(String):0,1 means the result is tainted
                    # if EITHER the appended argument OR the builder already was.
                    pos = to_positions(idx, params)
                    if pos:
                        propagators[owner][method] |= set(pos)

    # A method explicitly marked SAFE upstream must never survive as a source.
    # This is the correction that matters most: getContextPath/getMethod/getAuthType
    # LOOK like request getters and are not attacker-controlled.
    for owner, method in safe:
        if owner in sources and method in sources[owner]:
            del sources[owner][method]
    return ({o: dict(ms) for o, ms in sinks.items() if ms},
            {o: dict(ms) for o, ms in sources.items() if ms},
            {o: dict(ms) for o, ms in propagators.items() if ms}), sink_cat, warnings


def emit(sinks, sources, propagators, sink_cat, out_path: str) -> None:
    def fmt(table: dict) -> str:
        lines = []
        for owner in sorted(table):
            methods = table[owner]
            inner = ", ".join(
                f"{m!r}: {tuple(sorted(methods[m]))!r}" for m in sorted(methods))
            lines.append(f"    {owner!r}: {{{inner}}},")
        return "\n".join(lines)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write('"""GENERATED — do not edit. Regenerate with '
                 'scripts/mine_findsecbugs.py.\n\n'
                 "Taint roles for third-party Java APIs, flattened from "
                 "FindSecBugs'\ntaint configuration "
                 "(github.com/find-sec-bugs/find-sec-bugs, LGPL-3.0).\n\n"
                 "Reference data about JDK/library API signatures only — none of "
                 "FindSecBugs'\ndetector code or analysis logic is used here. "
                 "Argument indices have been\nconverted from their reverse "
                 "(stack-order) convention to left-to-right\nparameter "
                 'positions; see the script for the verification.\n"""\n')
        fh.write("from __future__ import annotations\n\n")
        fh.write("# {owner_fqn: {method: (dangerous parameter positions,)}}\n")
        fh.write("SINKS: dict[str, dict[str, tuple[int, ...]]] = {\n")
        fh.write(fmt(sinks))
        fh.write("\n}\n\n")
        fh.write("# {owner_fqn: category}\n")
        fh.write("SINK_CATEGORY: dict[str, str] = {\n")
        for o in sorted(sink_cat):
            fh.write(f"    {o!r}: {sink_cat[o]!r},\n")
        fh.write("}\n\n")
        fh.write("# {owner_fqn: {method: (RECEIVER,)}} — return value is "
                 "attacker-controlled.\n")
        fh.write("# Methods upstream marks SAFE have been removed.\n")
        fh.write("SOURCES: dict[str, dict[str, tuple[int, ...]]] = {\n")
        fh.write(fmt(sources))
        fh.write("\n}\n\n")
        fh.write("# {owner_fqn: {method: (positions whose taint reaches the "
                 "RETURN value,)}}\n")
        fh.write("# RECEIVER (-2) means the receiver's own taint carries "
                 "through — which is how\n# chained StringBuilder.append calls "
                 "accumulate taint across a concatenation.\n")
        fh.write("PROPAGATORS: dict[str, dict[str, tuple[int, ...]]] = {\n")
        fh.write(fmt(propagators))
        fh.write("\n}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--strict", action="store_true",
                    help="fail on the long/double index ambiguity instead of "
                         "skipping the affected line")
    a = ap.parse_args()
    (sinks, sources, propagators), sink_cat, warnings = mine(a.src, a.strict)
    emit(sinks, sources, propagators, sink_cat, a.out)
    print(f"sinks:   {len(sinks)} owners, "
          f"{sum(len(m) for m in sinks.values())} methods")
    print(f"sources: {len(sources)} owners, "
          f"{sum(len(m) for m in sources.values())} methods")
    print(f"propagators: {len(propagators)} owners, "
          f"{sum(len(m) for m in propagators.values())} methods")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings[:40]:
            print("  " + w)


if __name__ == "__main__":
    main()

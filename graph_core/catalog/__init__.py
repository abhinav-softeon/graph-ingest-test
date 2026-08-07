"""Vulnerability catalog — external API signature -> taint role.

WHAT THIS IS, AND WHAT IT IS NOT
Reference data about THIRD-PARTY APIs, imported once and consulted at lookup
time. It says nothing about this repo's own code. It is not produced by
tree-sitter, bytecode, or any parser — those tell you a call happened; this tells
you whether that call matters.

Three roles, which is the minimum a taint analysis needs:

    SOURCE     untrusted data enters here          (request.getParameter)
    SINK       tainted data causes harm here       (Runtime.exec)
    SANITIZER  taint is neutralised here           (PreparedStatement.setString)

WHY IT IS SEPARATE FROM external_api.py
external_api.py answers "is this call worth an edge at all", in RESOURCE terms
(db_acquire / db_execute / db_release / reflection). That is a different question
with a different vocabulary, and it runs 3.8M times per ingest, so it stays as
lean as it is. This module answers "what is this call, security-wise" and is
consulted per candidate rather than per invocation.

They are coupled in one direction that matters: external_api decides whether a
CALLS_EXTERNAL edge exists, and a catalog entry with no edge to attach to is
inert. Anything given a role here must therefore also be classifiable there (or
GRAPH_EXTERNAL_ALL_CALLS must be on). missing_edge_coverage() computes that gap
by asking external_api directly, so it can never go stale.

KEYED ON THE OWNER TYPE, THEN THE METHOD — NEVER THE METHOD ALONE
Inherited directly from external_api.py's design note, for the same reason: a
table keyed on `close` tags `inputStream.close()` as a database release, and a
table keyed on `write` tags every logger as an XSS sink. The owner type is
checked first and the method only within it. This is the single most important
property of the whole file; entries that violate it produce confident garbage,
which is worse than no entry.

ARGUMENT POSITIONS ARE PART OF THE ENTRY
A sink is rarely dangerous in all of its parameters. `setString(int, String)` is
a sanitizer for parameter 1 and meaningless for parameter 0. Recording positions
is what lets a DFG ask "does tainted data reach THIS argument" instead of the
much weaker "does it reach this call".

CATALOG BREADTH IS THE DIAL
A deterministic analysis has zero general knowledge: it recognises exactly what
is in here and nothing else. So a thin catalog has WORSE recall than an LLM
reading the same code. The design is therefore hybrid — deterministic where an
entry exists, LLM everywhere else — and this file growing is what shifts work
from the expensive path to the cheap one. It is meant to be appended to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from . import findsecbugs_java as _fsb

# ---- roles ---------------------------------------------------------------
SOURCE = "source"
SINK = "sink"
SANITIZER = "sanitizer"

# Any argument position. Used for sinks whose danger is the call itself rather
# than one parameter (ObjectInputStream.readObject reads from a stream bound
# earlier, so there is no argument to point at).
ANY_ARG = -1

# Receiver position, for entries where the tainted value is the object the method
# is called ON rather than anything passed to it.
RECEIVER = -2


@dataclass(frozen=True)
class Entry:
    """One catalogued API.

    ``methods`` maps a lowercased method name to the argument positions that
    carry the role. Lowercased because bytecode and source spellings agree on
    case in Java but the heuristic path does not always, and a case-sensitive
    miss here fails silently — the same trap external_api.py's lowercased method
    sets avoid.

    ``category`` is a CWE-style label, kept as a plain string rather than an enum
    so importing a new upstream taxonomy does not require touching this file.
    """
    role: str
    category: str
    methods: dict[str, tuple[int, ...]]
    note: str = ""


# ---- mined bulk ---------------------------------------------------------
# FindSecBugs' taxonomy, flattened by scripts/mine_findsecbugs.py. This is the
# BULK of the catalog and the authority for sources and injection sinks: 42 sink
# owners and 7 source owners, with argument positions converted from upstream's
# stack-slot convention.
#
# It is authoritative over hand-written entries for a reason worth recording.
# The first version of this file hand-listed HttpServletRequest sources and got
# four of them WRONG in the same direction — getInputStream / getReader / getPart
# / getParts were listed as sources, and upstream marks them SAFE, correctly:
# they return stream OBJECTS, not attacker-controlled values. The taint arrives
# when something reads from the stream. It also missed seven real sources
# (getContentType, getLocalAddr, getLocalName, getPathTranslated, getRemoteHost,
# getServerName, getServletPath). Hand-curation of a large API surface does not
# work; mining plus review does.
_MINED_SOURCES: dict[str, Entry] = {
    owner: Entry(SOURCE, "CWE-20/untrusted-input", methods,
                 note="mined from FindSecBugs taint-config")
    for owner, methods in _fsb.SOURCES.items()
}
_MINED_SINKS: dict[str, Entry] = {
    owner: Entry(SINK, _fsb.SINK_CATEGORY.get(owner, "CWE-unknown"), methods,
                 note="mined from FindSecBugs injection-sinks")
    for owner, methods in _fsb.SINKS.items()
}


# ---- curated: what mining does not supply -------------------------------
# Kept small ON PURPOSE. Every entry here is something the upstream files do not
# express, not a second opinion about something they do.
#
# SANITIZERS are the whole reason this table exists: FindSecBugs encodes them
# inside detector logic rather than in the taint config, so ZERO were mined. A
# catalog with sinks and no sanitizers reports every correctly-parameterized query
# as SQL injection — the documented failure mode of the tools this competes with,
# and worse than reporting nothing.
_CURATED: dict[str, Entry] = {
    "java.sql.PreparedStatement": Entry(
        SANITIZER, "CWE-89/sql-injection",
        {"setstring": (1,), "setint": (1,), "setlong": (1,), "setdouble": (1,),
         "setfloat": (1,), "setboolean": (1,), "setdate": (1,),
         "settimestamp": (1,), "settime": (1,), "setbigdecimal": (1,),
         "setobject": (1,), "setnull": (1,), "setbytes": (1,)},
        note="parameter 1 is the VALUE and is bound, not interpolated. Parameter "
             "0 is the placeholder index and is irrelevant — which is exactly why "
             "positions are part of an entry. NOTE this owner is ALSO a mined "
             "sink (executeQuery et al); see the merge note on _CURATED_WINS",
    ),
    "java.sql.CallableStatement": Entry(
        SANITIZER, "CWE-89/sql-injection",
        {"setstring": (1,), "setint": (1,), "setlong": (1,), "setobject": (1,)},
    ),
    "org.apache.commons.text.StringEscapeUtils": Entry(
        SANITIZER, "CWE-79/xss",
        {"escapehtml4": (0,), "escapehtml3": (0,), "escapexml10": (0,),
         "escapexml11": (0,), "escapeecmascript": (0,), "escapejson": (0,)},
    ),
    "org.apache.commons.lang.StringEscapeUtils": Entry(
        SANITIZER, "CWE-79/xss",
        {"escapehtml": (0,), "escapexml": (0,), "escapejavascript": (0,),
         "escapesql": (0,)},
        note="escapeSql is catalogued for lookup completeness but only escapes "
             "quotes and is NOT sufficient against SQL injection — a consumer "
             "should treat it as weak rather than clearing the finding",
    ),
    "org.owasp.esapi.Encoder": Entry(
        SANITIZER, "CWE-79/xss",
        {"encodeforhtml": (0,), "encodeforhtmlattribute": (0,),
         "encodeforjavascript": (0,), "encodeforurl": (0,),
         "encodeforsql": (0,), "encodeforldap": (0,)},
    ),
    # Not injection sinks, so absent from the mined injection-sinks files: the
    # defect is the ARGUMENT VALUE ("MD5"), not tainted data arriving. Catalogued
    # here because the lookup is identical and a second mechanism buys nothing.
    "java.security.MessageDigest": Entry(
        SINK, "CWE-327/weak-crypto", {"getinstance": (0,)},
        note="flag when argument 0 is a literal MD5 or SHA-1",
    ),
    "javax.crypto.Cipher": Entry(
        SINK, "CWE-327/weak-crypto", {"getinstance": (0,)},
        note="flag DES / RC4 / ECB-mode transformations",
    ),
    # WEAK RANDOMNESS. Not a taint flow at all — the defect is the CHOICE of API,
    # so there is no source and nothing to propagate. Catalogued here because the
    # lookup is identical and a second mechanism would buy nothing.
    #
    # Found by reading OWASP Benchmark's ground truth before running it: weakrand
    # is 493 of its 2,740 cases, the second-largest category, and this catalog had
    # nothing for it. A good argument for measuring against labelled data early —
    # the gap was invisible from the inside.
    "java.util.Random": Entry(
        SINK, "CWE-330/weak-random",
        {"<init>": (ANY_ARG,), "nextint": (ANY_ARG,), "nextlong": (ANY_ARG,),
         "nextdouble": (ANY_ARG,), "nextfloat": (ANY_ARG,),
         "nextboolean": (ANY_ARG,), "nextbytes": (ANY_ARG,),
         "nextgaussian": (ANY_ARG,), "ints": (ANY_ARG,), "longs": (ANY_ARG,),
         "doubles": (ANY_ARG,)},
        note="java.util.Random is a linear congruential generator — predictable "
             "from a couple of outputs. SecureRandom is the fix. NOT flagged "
             "for non-security use (shuffling a demo list), which is why a "
             "consumer should weigh context rather than report every hit",
    ),
    "java.lang.Math": Entry(
        SINK, "CWE-330/weak-random", {"random": (ANY_ARG,)},
        note="Math.random() delegates to a shared java.util.Random",
    ),
    "org.apache.commons.lang.math.JVMRandom": Entry(
        SINK, "CWE-330/weak-random",
        {"nextint": (ANY_ARG,), "nextlong": (ANY_ARG,), "nextdouble": (ANY_ARG,)}),
    "org.apache.commons.lang3.RandomStringUtils": Entry(
        SINK, "CWE-330/weak-random",
        {"random": (ANY_ARG,), "randomalphabetic": (ANY_ARG,),
         "randomalphanumeric": (ANY_ARG,), "randomascii": (ANY_ARG,),
         "randomnumeric": (ANY_ARG,)},
        note="backed by java.util.Random unless the *Secure variants are used",
    ),
    # INSECURE COOKIE. Same shape: the defect is a missing/false flag argument,
    # not tainted data arriving. 67 Benchmark cases.
    "javax.servlet.http.Cookie": Entry(
        SINK, "CWE-614/insecure-cookie",
        {"setsecure": (0,), "sethttponly": (0,)},
        note="flag when argument 0 is literal false, or when the setter is "
             "never called on a cookie carrying session state. NOTE Cookie is "
             "ALSO a mined response-splitting sink — one Entry carries one role, "
             "and the mined entry wins unless listed in _CURATED_WINS",
    ),
    # TYPE COERCION — the highest-yield sanitizer class, and absent from every
    # upstream rule set mined here.
    #
    # `Integer.parseInt(request.getParameter("id"))` is not "probably safe", it is
    # PROVABLY safe: the result is an int, and an int cannot carry a SQL payload,
    # a shell metacharacter or a script tag. The taint stops at the parse whether
    # or not anyone thought about security.
    #
    # FindSecBugs does not model this because in its type system a primitive is
    # never tainted to begin with, so there is nothing to mark. That reasoning is
    # sound for their engine and useless for a catalog consumed by an LLM or a
    # path walker, which sees `parseInt(taintedString)` and has no rule saying the
    # chain ends there. In a legacy app most numeric parameters go through these,
    # so this is a large false-positive class removed for a dozen entries.
    #
    # Position 0 in every case: the string being parsed.
    "java.lang.Integer": Entry(
        SANITIZER, "CWE-20/type-coercion",
        {"parseint": (0,), "valueof": (0,), "parseunsignedint": (0,),
         "decode": (0,)},
    ),
    "java.lang.Long": Entry(
        SANITIZER, "CWE-20/type-coercion",
        {"parselong": (0,), "valueof": (0,), "parseunsignedlong": (0,),
         "decode": (0,)},
    ),
    "java.lang.Short": Entry(
        SANITIZER, "CWE-20/type-coercion",
        {"parseshort": (0,), "valueof": (0,), "decode": (0,)}),
    "java.lang.Byte": Entry(
        SANITIZER, "CWE-20/type-coercion",
        {"parsebyte": (0,), "valueof": (0,), "decode": (0,)}),
    "java.lang.Double": Entry(
        SANITIZER, "CWE-20/type-coercion",
        {"parsedouble": (0,), "valueof": (0,)}),
    "java.lang.Float": Entry(
        SANITIZER, "CWE-20/type-coercion",
        {"parsefloat": (0,), "valueof": (0,)}),
    "java.lang.Boolean": Entry(
        SANITIZER, "CWE-20/type-coercion",
        {"parseboolean": (0,), "valueof": (0,)}),
    "java.util.UUID": Entry(
        SANITIZER, "CWE-20/type-coercion", {"fromstring": (0,)},
        note="throws on anything that is not a UUID, so the output alphabet is "
             "hex and dashes — a strong validator, not merely a parse",
    ),
    "java.math.BigDecimal": Entry(
        SANITIZER, "CWE-20/type-coercion", {"<init>": (0,), "valueof": (0,)}),
    "java.math.BigInteger": Entry(
        SANITIZER, "CWE-20/type-coercion", {"<init>": (0,), "valueof": (0,)}),
    # Encoders that are not injection-specific but end taint for their context.
    "java.net.URLEncoder": Entry(
        SANITIZER, "CWE-79/xss", {"encode": (0,)},
        note="percent-encoding; correct for a URL context and NOT sufficient "
             "for HTML body or JavaScript contexts",
    ),
    "org.owasp.encoder.Encode": Entry(
        SANITIZER, "CWE-79/xss",
        {"forhtml": (0,), "forhtmlcontent": (0,), "forhtmlattribute": (0,),
         "forjavascript": (0,), "forjavascriptblock": (0,),
         "forjavascriptattribute": (0,), "foruricomponent": (0,),
         "forcssstring": (0,), "forxml": (0,), "forxmlcontent": (0,),
         "forxmlattribute": (0,)},
        note="OWASP Java Encoder — the context-correct encoder set",
    ),
    "org.springframework.web.util.HtmlUtils": Entry(
        SANITIZER, "CWE-79/xss",
        {"htmlescape": (0,), "htmlescapedecimal": (0,), "htmlescapehex": (0,)}),
    # SECOND-ORDER SOURCES. Nothing upstream marks these, and for a legacy
    # enterprise app they are arguably the most important source class there is:
    # a value an attacker stored through one screen, read back out of the
    # database later, and concatenated into the next query or written to a page.
    # The classic stored-XSS / second-order-SQL chain, and completely invisible
    # to a catalog that only knows HTTP entry points.
    #
    # Deliberately NOT the whole ResultSet surface — the cursor and metadata
    # methods (next, close, getMetaData, wasNull, findColumn) carry no data and
    # would only add noise.
    #
    # Cost of being wrong here is precision, not recall: over-marking makes every
    # DB read a source, so the getters are listed explicitly rather than by
    # prefix. Note external_api gives this priority over its DB_OTHER fallback
    # but NOT over db_execute/db_acquire/db_release — see the note there.
    "java.sql.ResultSet": Entry(
        SOURCE, "CWE-20/untrusted-input",
        {"getstring": (RECEIVER,), "getobject": (RECEIVER,),
         "getnstring": (RECEIVER,), "getcharacterstream": (RECEIVER,),
         "getncharacterstream": (RECEIVER,), "getclob": (RECEIVER,),
         "getnclob": (RECEIVER,), "getbytes": (RECEIVER,),
         "getbinarystream": (RECEIVER,), "getasciistream": (RECEIVER,),
         "getblob": (RECEIVER,), "getarray": (RECEIVER,),
         "getsqlxml": (RECEIVER,), "getref": (RECEIVER,)},
        note="second-order source: stored data read back. Numeric/date/boolean "
             "getters are excluded on purpose — they cannot carry an injection "
             "payload, so marking them would be pure false-positive volume",
    ),
    # Deserialization. Not in the mined set (upstream handles it in a detector,
    # not the taint config).
    "java.io.ObjectInputStream": Entry(
        SINK, "CWE-502/unsafe-deserialization",
        {"readobject": (ANY_ARG,), "readunshared": (ANY_ARG,)},
        note="the taint is the STREAM, bound before this call — there is no "
             "argument to point at, which is what ANY_ARG is for",
    ),
}

# Owners where the curated entry replaces the mined one outright. Only
# PreparedStatement qualifies: it is genuinely both (executeQuery is a sink, the
# setters are sanitizers) and one Entry carries one role, so the sanitizer role
# is the one worth keeping — a PreparedStatement whose SQL was concatenated is
# already caught by the java.sql.Connection.prepareStatement sink, which is where
# the string is built.
_CURATED_WINS = frozenset({"java.sql.PreparedStatement"})

JAVA: dict[str, Entry] = {**_MINED_SOURCES, **_MINED_SINKS}
for _owner, _entry in _CURATED.items():
    if _owner in JAVA and _owner not in _CURATED_WINS:
        # Mined data wins by default; a curated duplicate is almost always a
        # hand-written second opinion, which is the thing that went wrong before.
        continue
    JAVA[_owner] = _entry

# javax.* / jakarta.* aliasing, applied AFTER the merge so mined and curated
# entries are both covered. Same object on both spellings so they cannot drift.
for _owner, _entry in list(JAVA.items()):
    if _owner.startswith("javax."):
        JAVA.setdefault(_owner.replace("javax.", "jakarta.", 1), _entry)



# Simple-name index, for the heuristic resolution path where only a simple type
# name is available (java.py stores receiver types via simple_type_name). Built
# once at import, not per lookup.
#
# A simple name that maps to MORE THAN ONE catalogued owner is EXCLUDED rather
# than resolved arbitrarily: `Statement` alone cannot distinguish
# java.sql.Statement from another library's, and guessing is how a catalog
# produces confident garbage. Ambiguous simple names simply do not match, and
# the fully-qualified path (bytecode, 99.98% of Java here) still does.
def _build_simple_index(table: dict[str, Entry]) -> dict[str, Entry]:
    by_simple: dict[str, list[str]] = {}
    for owner in table:
        by_simple.setdefault(owner.rsplit(".", 1)[-1], []).append(owner)
    out: dict[str, Entry] = {}
    for simple, owners in by_simple.items():
        # javax/jakarta pairs are the SAME entry object, so a simple name
        # resolving to both is not ambiguous. Compare identity, not owner count.
        entries = {id(table[o]) for o in owners}
        if len(entries) == 1:
            out[simple] = table[owners[0]]
    return out


JAVA_BY_SIMPLE_NAME: dict[str, Entry] = _build_simple_index(JAVA)


# Taint PROPAGATION rules: {owner: {method: (positions whose taint reaches the
# return value,)}}. Mined from the same upstream config as the roles above.
#
# Kept OUT of JAVA/Entry on purpose. A propagator has no role — it is neither a
# source, a sink, nor a sanitizer, and folding it into the role table would make
# classify_taint answer "this call is dangerous" for `StringBuilder.append`,
# which is exactly the confident-garbage failure the owner-first rule exists to
# prevent. Propagation is a separate question asked by a separate consumer.
#
# This is what a deterministic DFG needs and an LLM does not: the model reads
# `"..." + request.getParameter("id")` and understands it; a def-use walker has
# to be told that javac compiled it into StringBuilder.append and that append's
# result carries its argument's taint.
PROPAGATORS: dict[str, dict[str, tuple[int, ...]]] = dict(_fsb.PROPAGATORS)
PROPAGATORS_BY_SIMPLE_NAME: dict[str, dict[str, tuple[int, ...]]] = {}
for _simple, _owners in {
    s: [o for o in PROPAGATORS if o.rsplit(".", 1)[-1] == s]
    for s in {o.rsplit(".", 1)[-1] for o in PROPAGATORS}
}.items():
    if len(_owners) == 1:
        PROPAGATORS_BY_SIMPLE_NAME[_simple] = PROPAGATORS[_owners[0]]


def classify_propagator(owner: str, method: str) -> tuple[int, ...] | None:
    """Positions whose taint flows to this call's RETURN value, or None.

    RECEIVER in the result is not an oddity: `StringBuilder.append(String)`
    returns (0, RECEIVER), meaning the returned builder is tainted if either the
    appended argument or the builder already was. Chained appends accumulate
    taint through the receiver, which is precisely how a concatenated SQL string
    becomes tainted.
    """
    if not owner or not method:
        return None
    table = (PROPAGATORS.get(owner)
             or PROPAGATORS_BY_SIMPLE_NAME.get(owner.rsplit(".", 1)[-1]))
    if not table:
        return None
    return table.get(method.lower())


# Memoised because this is a HOT PATH and its key space is tiny relative to its
# call count: one measured run made 3.8M invocations across just 26,003 distinct
# owner#method pairs, a ~99.3% hit rate. Uncached it cost ~700s across the
# bytecode and resolve stages — `rsplit` and `.lower()` allocate on every call,
# and the common case (owner not catalogued) always takes the slow path.
#
# Safe to cache unbounded: the catalog is immutable after import, and the key
# space is bounded by the APIs a codebase actually calls, not by call volume.
# Deliberately NOT dependent on the enabled-category setting — that filter is
# applied by the caller, so this stays a pure function of the catalog.
@lru_cache(maxsize=None)
def classify_taint(owner: str, method: str) -> tuple[Entry, tuple[int, ...]] | None:
    """Return (entry, argument_positions) for a catalogued API, else None.

    ``owner`` may be fully qualified (the bytecode path, and the only spelling
    that is unambiguous) or a simple type name (the heuristic path). ``method``
    is matched case-insensitively; ``<init>`` matches a constructor.
    """
    if not owner or not method:
        return None
    entry = JAVA.get(owner) or JAVA_BY_SIMPLE_NAME.get(owner.rpartition(".")[2])
    if entry is None:
        return None
    args = entry.methods.get(method.lower())
    if args is None:
        return None
    return entry, args


def missing_edge_coverage() -> list[str]:
    """Catalogued owners whose calls external_api.py cannot classify yet.

    Those entries are INERT: with no CALLS_EXTERNAL edge in the graph there is
    nothing for a lookup to run against, so the role is known and unusable.

    COMPUTED, not declared. An earlier version carried a hand-set
    `needs_external_api` flag per entry, which is the same maintenance trap as
    hand-listing API methods — it would silently go stale the moment
    external_api's type sets changed. This asks the real classifier instead, so
    the answer is correct by construction.

    An owner counts as covered when classify_call recognises AT LEAST ONE of its
    catalogued methods. Partial coverage is reported as covered rather than
    missing: it means the type is known to external_api and the gap is a method
    list, which is a different and much smaller fix.
    """
    from ..external_api import classify_call

    missing = []
    for owner, entry in JAVA.items():
        if not any(classify_call(owner, m) for m in entry.methods):
            missing.append(owner)
    return sorted(missing)


# Owner types whose calls are high-volume and cannot be a source, sink or
# sanitizer under any entry — string building, collections, boxing, math. They
# dominate an external-call inventory by count (a legacy Java webapp calls
# StringBuilder.append and Vector.elementAt millions of times) and drown out the
# tail that catalog work actually needs to see.
#
# Used ONLY to suppress per-method detail in the inventory, never to drop an
# owner: the owner is always recorded, so removing a prefix from this set and
# re-running is all it takes to get its methods back. That is the difference
# between a deliberate filter and a truncation — nothing here can hide a type
# from view, only its method breakdown.
#
# Note what is NOT here and looks like it should be: java.io.* (path traversal
# and deserialization sinks live there) and java.lang.System (getenv/getProperty
# are sources). Both are high-volume AND security-relevant.
#
# java.lang.String IS here and is also a catalogued sink (String.format,
# CWE-134). That is deliberate and not a contradiction: the filter governs the
# UNRECOGNISED-API inventory, and once external_api classifies String.format it
# stops being unrecognised, so it would never have appeared there anyway.
INVENTORY_NOISE_PREFIXES: tuple[str, ...] = (
    "java.lang.String",         # covers String, StringBuilder, StringBuffer
    "java.lang.Integer", "java.lang.Long", "java.lang.Double",
    "java.lang.Float", "java.lang.Short", "java.lang.Byte",
    "java.lang.Boolean", "java.lang.Character", "java.lang.Number",
    "java.lang.Math", "java.lang.Object", "java.lang.Enum",
    "java.lang.Comparable", "java.lang.Iterable", "java.lang.CharSequence",
    "java.util.Vector", "java.util.Hashtable", "java.util.ArrayList",
    "java.util.HashMap", "java.util.LinkedList", "java.util.LinkedHashMap",
    "java.util.HashSet", "java.util.TreeMap", "java.util.TreeSet",
    "java.util.Iterator", "java.util.Enumeration", "java.util.Collection",
    "java.util.List", "java.util.Map", "java.util.Set", "java.util.Arrays",
    "java.util.Collections", "java.util.Objects", "java.util.Optional",
    "java.util.StringTokenizer", "java.util.stream",
    "java.math.BigDecimal", "java.math.BigInteger",
)


# Categories deliberately OFF by default when the catalog feeds external_api.
# Not because they are wrong — they are mined from the same upstream source as the
# rest — but because their call volume is enormous and their yield is low.
# String.format / PrintStream / Formatter / logging calls are among the most
# frequent operations in a legacy Java webapp, and turning them into
# CALLS_EXTERNAL edges would add hundreds of thousands of rows that no
# injection-path query would ever follow. Sources and injection sinks are what a
# taint analysis needs; these are what it would have to wade through.
HIGH_VOLUME_LOW_VALUE_CATEGORIES: frozenset[str] = frozenset({
    "CWE-134/format-string",
    "CWE-117/log-injection",
})


def all_categories() -> frozenset[str]:
    return frozenset(e.category for e in JAVA.values())


def recommended_categories() -> frozenset[str]:
    """Everything except the high-volume/low-value classes above.

    XSS is deliberately INCLUDED despite being the highest-volume of what
    remains (PrintWriter/JspWriter/ServletOutputStream writes are everywhere in a
    JSP app). It is a real sink class that a servlet codebase genuinely has, so
    excluding it would hide actual findings — unlike format-string, which mostly
    reports on log statements. Watch the edge count on the first run with it on;
    the category gate makes dropping it a config change, not a code change.
    """
    return all_categories() - HIGH_VOLUME_LOW_VALUE_CATEGORIES


def is_inventory_noise(owner: str) -> bool:
    """True when per-method inventory detail for this owner is not worth keeping.

    Never means "ignore this type" — see INVENTORY_NOISE_PREFIXES.
    """
    return owner.startswith(INVENTORY_NOISE_PREFIXES)


def stats() -> dict:
    """Coverage summary — what the catalog knows, and how much of it is live."""
    by_role: dict[str, int] = {}
    signatures = 0
    # Deduped by identity, not with a set: Entry is frozen (so dataclass gives it
    # a __hash__) but carries a dict field, which makes hashing it a TypeError at
    # runtime. Identity is also the right notion here — javax/jakarta aliases are
    # deliberately the same object.
    for entry in {id(e): e for e in JAVA.values()}.values():
        by_role[entry.role] = by_role.get(entry.role, 0) + 1
    for owner, entry in JAVA.items():
        signatures += len(entry.methods)
    return {
        "owners": len(JAVA),
        "distinct_entries": len({id(e) for e in JAVA.values()}),
        "signatures": signatures,
        "by_role": by_role,
        "owners_needing_external_api": len(missing_edge_coverage()),
    }

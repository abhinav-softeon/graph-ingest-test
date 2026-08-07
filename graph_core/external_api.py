"""Classify calls that leave the repo, so the fact survives instead of being lost.

WHY THIS EXISTS
`resolver.py`'s external-receiver step correctly refuses to invent an in-repo
target for `conn.close()` — no Function node can be the destination, so every
edge it produced was false. But it then discards the observation entirely, and
with it the only record that a function touches a database at all.

This module turns that discarded observation into a fact:

    Function -[:CALLS_EXTERNAL]-> (:External {key:'java.sql.Connection#close',
                                              kind:'db_release'})

CLASSIFY ON THE TYPE, NOT THE METHOD NAME
A table keyed on `close`/`execute` would tag `inputStream.close()` as a database
release — confident garbage, exactly the failure mode this work is supposed to
avoid. The owner type is checked first and the method name only within it.

ACQUIRE IS DETECTED BY RETURN TYPE, WHICH IS THE NON-OBVIOUS PART
Measured on the target repo: 171 in-repo methods return a `java.sql.Connection`,
named `getConnection`, `getDbConn`, `getCon`, `getDbConnection` — no consistent
convention, so no name-based rule finds them. Only 2,571 calls go to
`Connection.prepareStatement` while 11,319 go to `Connection.close`; connections
overwhelmingly come from the repo's own pool wrappers, not from JDBC directly.

So a call is an acquire when it RETURNS a connection, whoever owns it. That is
what makes the leak query work on a codebase like this one, and it is only
available because bytecode carries the descriptor.
"""
from __future__ import annotations

from functools import lru_cache

from .catalog import classify_taint as _classify_taint
from .ids import make_id

# Kinds. Deliberately coarse: the graph's job is to find candidates, the LLM's
# job is to read the code and decide (IMPLEMENTATION_PLAN.md Phase 4.4).
DB_ACQUIRE = "db_acquire"
DB_EXECUTE = "db_execute"
DB_RELEASE = "db_release"
DB_OTHER = "db_other"

# A reflective call. The target is chosen at runtime from a string, so NO
# resolver can bind it — `Class.forName(name).getMethod(m).invoke(o)` produces
# call edges to forName/getMethod/invoke and nothing to whatever actually runs.
# Recording the call site is what lets the analysis layer route a reader there:
# the class name is usually a literal sitting in the source, so an LLM resolves
# what the resolver must refuse to guess at. Mark-don't-extract, Phase 4 P2.
REFLECTION = "reflection"

# Out-of-repo, recognised as nothing in particular. Opt-in (see
# classify_external) because emitting it turns EVERY library call into an edge.
EXTERNAL_OTHER = "other"

# Types whose methods are database work. Checked before any method name.
_JDBC_TYPES = {
    "java.sql.Connection", "java.sql.Statement", "java.sql.PreparedStatement",
    "java.sql.CallableStatement", "java.sql.ResultSet", "java.sql.DataSource",
    "javax.sql.DataSource", "javax.sql.PooledConnection", "java.sql.DriverManager",
}
_JPA_TYPES = {
    "javax.persistence.EntityManager", "jakarta.persistence.EntityManager",
    "javax.persistence.EntityManagerFactory", "jakarta.persistence.EntityManagerFactory",
    "javax.persistence.Query", "jakarta.persistence.Query",
    "javax.persistence.TypedQuery", "jakarta.persistence.TypedQuery",
    "org.hibernate.Session", "org.hibernate.SessionFactory", "org.hibernate.Transaction",
}
_SPRING_TYPES = {
    "org.springframework.jdbc.core.JdbcTemplate",
    "org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate",
    "org.springframework.transaction.support.TransactionTemplate",
}
_MYBATIS_TYPES = {
    "org.apache.ibatis.session.SqlSession",
    "org.apache.ibatis.session.SqlSessionFactory",
}

DB_TYPES = _JDBC_TYPES | _JPA_TYPES | _SPRING_TYPES | _MYBATIS_TYPES

# Simple names, for the heuristic path where only recv_type is available and it
# carries a simple name (java.py stores types via simple_type_name).
DB_SIMPLE_NAMES = {t.rsplit(".", 1)[-1] for t in DB_TYPES}

# The connection type itself — a method returning this is an acquire.
CONNECTION_TYPES = {
    "java.sql.Connection", "javax.sql.PooledConnection",
    "org.hibernate.Session",
    "javax.persistence.EntityManager", "jakarta.persistence.EntityManager",
    "org.apache.ibatis.session.SqlSession",
}
# Precomputed, NOT built per call. classify_call runs once per invocation —
# 3.8M times on the measured repo — so a set comprehension inside it would be
# 3.8M set constructions for a six-element constant.
CONNECTION_SIMPLE_NAMES = {t.rsplit(".", 1)[-1] for t in CONNECTION_TYPES}

_ACQUIRE_METHODS = {
    "getconnection", "createstatement", "preparestatement", "preparecall",
    "getsession", "opensession", "createentitymanager", "opensqlsession",
    "getcurrentsession",
}
_EXECUTE_METHODS = {
    "executequery", "executeupdate", "execute", "executebatch",
    "executelargeupdate", "query", "queryforobject", "queryforlist", "update",
    "createquery", "createnativequery", "getresultlist", "getsingleresult",
    "selectone", "selectlist", "insert", "delete", "persist", "merge", "remove",
    "save", "find",
}
_RELEASE_METHODS = {"close", "commit", "rollback", "setautocommit", "releasesavepoint"}

# Types that exist ONLY for reflective access, so any method on them counts.
_REFLECT_TYPES = {
    "java.lang.reflect.Method",
    "java.lang.reflect.Constructor",
    "java.lang.reflect.Field",
}
# java.lang.Class and ClassLoader carry plenty of harmless methods
# (getName/getSimpleName/isInstance, and `getClass().getName()` is everywhere),
# so these are method-filtered rather than type-wide — only the calls that
# actually reach code or members.
_REFLECT_METHODS_BY_TYPE = {
    "java.lang.Class": {
        "forname", "newinstance",
        "getmethod", "getmethods", "getdeclaredmethod", "getdeclaredmethods",
        "getconstructor", "getconstructors",
        "getdeclaredconstructor", "getdeclaredconstructors",
        "getfield", "getfields", "getdeclaredfield", "getdeclaredfields",
    },
    "java.lang.ClassLoader": {"loadclass"},
}
# Reflection is matched on FULLY-QUALIFIED owners only — deliberately unlike the
# DB path, which also accepts simple names. `Field`, `Method` and `Class` are
# common class names in ordinary code, so a simple-name match would tag a repo's
# own `Field` as reflection. The bytecode path always has FQNs (from
# descriptors), which is the path this matters on.


def _classify_reflection(owner: str, method: str) -> str:
    if owner in _REFLECT_TYPES:
        return REFLECTION
    allowed = _REFLECT_METHODS_BY_TYPE.get(owner)
    if allowed and (method or "").lower() in allowed:
        return REFLECTION
    return ""


# Taint kinds, contributed by the vulnerability catalog rather than by the
# resource classifier above. Kept as distinct values so a consumer can tell "this
# call touches a database" from "this call is a known injection sink" — they are
# different claims and only the second one names a vulnerability class.
TAINT_SOURCE = "taint_source"
TAINT_SINK = "taint_sink"
TAINT_SANITIZER = "taint_sanitizer"

_ROLE_TO_KIND = {
    "source": TAINT_SOURCE,
    "sink": TAINT_SINK,
    "sanitizer": TAINT_SANITIZER,
}

# Catalog category -> the kind the ANALYSIS LAYER ALREADY ASKS FOR.
#
# analysis/reach.py seeds its reachability closure from
# DANGEROUS_KINDS = [db_execute, db_other, exec, file_write, deserialize,
# response, reflection] — but this module has only ever emitted db_*, reflection
# and other. So `exec`, `file_write`, `deserialize` and `response` were dead
# vocabulary: the analysis was written for sinks nothing ever produced.
#
# Those four are precisely the categories the catalog covers, so mapping onto the
# existing words rather than minting new ones makes the catalog usable by
# reach.py with no change on that side at all. A category with no existing word
# falls back to the generic TAINT_SINK.
_CATEGORY_TO_KIND = {
    "CWE-78/command-injection": "exec",
    "CWE-94/code-injection": "exec",          # ScriptEngine.eval executes code
    "CWE-22/path-traversal": "file_write",
    "CWE-502/unsafe-deserialization": "deserialize",
    "CWE-79/xss": "response",
    "CWE-113/response-splitting": "response",
    "CWE-89/sql-injection": DB_EXECUTE,       # non-JDBC SQL sinks (JPA/Hibernate)
}

# Resolved ONCE per process, not per call. classify_call runs ~3.8M times on the
# measured repo, and reading os.environ that often would cost more than the
# classification itself — the same reason CONNECTION_SIMPLE_NAMES above is
# precomputed instead of built inline.
_ENABLED_CATEGORIES: frozenset | None = None


def _enabled_categories() -> frozenset:
    global _ENABLED_CATEGORIES
    if _ENABLED_CATEGORIES is None:
        from .catalog import all_categories, recommended_categories
        from .config import catalog_external_setting
        raw = catalog_external_setting()
        low = raw.lower()
        if low in ("", "off", "0", "false", "no"):
            _ENABLED_CATEGORIES = frozenset()
        elif low == "all":
            _ENABLED_CATEGORIES = all_categories()
        elif low == "recommended":
            _ENABLED_CATEGORIES = recommended_categories()
        else:
            # Explicit list. Unknown names are kept rather than validated away —
            # silently ignoring a typo'd category would look identical to the
            # category simply having no calls, which is the harder bug to find.
            _ENABLED_CATEGORIES = frozenset(
                p.strip() for p in raw.split(",") if p.strip())
    return _ENABLED_CATEGORIES


def _reset_enabled_categories() -> None:
    """Drop the cached setting. For tests that change the environment."""
    global _ENABLED_CATEGORIES
    _ENABLED_CATEGORIES = None
    # The category filter is baked into this cache's values, so it MUST be
    # dropped with the setting or a test would read the previous run's answers.
    _classify_catalogued.cache_clear()


@lru_cache(maxsize=None)
def _classify_catalogued(owner: str, method: str) -> str:
    """Taint kind from the vulnerability catalog, or '' when not catalogued.

    Returns '' immediately when the feature is off, which is the default — so the
    hot path costs one frozenset truth test in that case.
    """
    enabled = _enabled_categories()
    if not enabled:
        return ""
    hit = _classify_taint(owner, method)
    if hit is None:
        return ""
    entry, _args = hit
    if entry.category not in enabled:
        return ""
    # Sinks speak reach.py's vocabulary where one exists. Sources and sanitizers
    # have no equivalent there (reach.py seeds entry points from annotations, not
    # from External nodes), so they keep the generic taint_* kinds.
    if entry.role == "sink":
        return _CATEGORY_TO_KIND.get(entry.category, TAINT_SINK)
    return _ROLE_TO_KIND.get(entry.role, "")


def classify_call(owner: str, method: str, return_type: str = "") -> str:
    """Kind for a call, or '' when it is not database work.

    ``owner``/``return_type`` may be dotted fully-qualified names or bare simple
    names; both forms are accepted so the bytecode path (which has descriptors)
    and the heuristic path (which has only ``recv_type``) share one classifier.

    Return type is checked FIRST and independently of the owner: a repo's own
    ``DbManager.getConnection()`` is an acquire even though ``DbManager`` is not
    a JDBC type. See the module docstring for why that dominates here.
    """
    if return_type and _is_connection_type(return_type):
        return DB_ACQUIRE
    reflect = _classify_reflection(owner, method)
    if reflect:
        return reflect
    if owner and _is_db_type(owner):
        name = (method or "").lower()
        if name in _ACQUIRE_METHODS:
            return DB_ACQUIRE
        if name in _RELEASE_METHODS:
            return DB_RELEASE
        if name in _EXECUTE_METHODS:
            return DB_EXECUTE
        # Before the generic fallback: a catalogued answer is MORE specific than
        # "unrecognised method on a database type", so it wins over DB_OTHER —
        # and only over DB_OTHER. db_acquire/db_execute/db_release above are
        # themselves specific and still take precedence.
        #
        # This exists for second-order taint. ResultSet.getString is database
        # work AND a source of untrusted data (a value an attacker stored
        # earlier, read back and concatenated into the next query). Classifying
        # it DB_OTHER is not wrong, it is just the less useful of two true
        # answers — and DB_OTHER sits in reach.py's DANGEROUS_KINDS, so it would
        # additionally mark every ResultSet read as reaching a SINK, which is
        # backwards for something that is a source.
        catalogued = _classify_catalogued(owner, method)
        if catalogued:
            return catalogued
        # An unrecognised method on a KNOWN database type is still database work.
        # "this function touches a Connection" is itself the signal; dropping it
        # would lose ResultSet.getString, PreparedStatement.setString and friends,
        # which are 32k calls in the measured repo.
        return DB_OTHER
    # Everything above is unchanged and still wins: the DB and reflection answers
    # carry a RESOURCE vocabulary (acquire/execute/release) that the catalog does
    # not express, and the acquire-by-return-type rule above is the only thing
    # that finds this repo's 171 differently-named connection factories.
    #
    # Only what would otherwise fall through to '' — and therefore produce no edge
    # at all — reaches the catalog.
    return _classify_catalogued(owner, method)


def classify_external(owner: str, method: str, return_type: str = "",
                      include_other: bool = False) -> str:
    """classify_call, plus an optional catch-all for everything else.

    ``include_other=False`` (the default) is exactly classify_call — an
    unrecognised out-of-repo call classifies as nothing and the caller drops it.
    That keeps `inputStream.close()` out of the graph, which is what
    test_external_api asserts and what the type-first design is protecting.

    ``include_other=True`` classifies EVERY out-of-repo call, unknown ones as
    EXTERNAL_OTHER. Turn it on when the analysis layer needs sinks the DB
    classifier does not know — Runtime.exec, FileOutputStream,
    ObjectInputStream.readObject, response.getWriter().write — none of which are
    database work and all of which are currently invisible.

    The cost is real and is why this is opt-in: it converts every library call
    into an edge (3.8M invocations on the measured repo, most of them out of
    repo). External NODES stay cheap regardless — they are keyed on owner#method
    and shared — but the edge count is not. Prefer adding the sink types you
    actually care about to the classifier over switching this on wholesale.
    """
    kind = classify_call(owner, method, return_type)
    if kind:
        return kind
    return EXTERNAL_OTHER if include_other else ""


def _is_db_type(type_name: str) -> bool:
    if type_name in DB_TYPES:
        return True
    return type_name.rsplit(".", 1)[-1] in DB_SIMPLE_NAMES


def _is_connection_type(type_name: str) -> bool:
    if type_name in CONNECTION_TYPES:
        return True
    return type_name.rsplit(".", 1)[-1] in CONNECTION_SIMPLE_NAMES


def external_key(owner: str, method: str) -> str:
    """Stable identity for an external target: ``owner#method``.

    Overloads collapse deliberately. The question this answers is "does this
    function close a connection", not "which close overload did it call"."""
    return f"{owner}#{method}"


def external_id(owner: str, method: str) -> str:
    """Node id, keyed on the repo 'external' rather than the indexed repo.

    External targets are shared: two repos that both call
    ``java.sql.Connection#close`` should reference one node, exactly as
    apispec.endpoint_id does for external endpoints."""
    return make_id("external", external_key(owner, method), "external")


def external_display(owner: str, method: str) -> str:
    return f"{owner.rsplit('.', 1)[-1]}.{method}"

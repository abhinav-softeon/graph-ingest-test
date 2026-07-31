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

from .ids import make_id

# Kinds. Deliberately coarse: the graph's job is to find candidates, the LLM's
# job is to read the code and decide (IMPLEMENTATION_PLAN.md Phase 4.4).
DB_ACQUIRE = "db_acquire"
DB_EXECUTE = "db_execute"
DB_RELEASE = "db_release"
DB_OTHER = "db_other"

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
    if not owner or not _is_db_type(owner):
        return ""
    name = (method or "").lower()
    if name in _ACQUIRE_METHODS:
        return DB_ACQUIRE
    if name in _RELEASE_METHODS:
        return DB_RELEASE
    if name in _EXECUTE_METHODS:
        return DB_EXECUTE
    # An unrecognised method on a KNOWN database type is still database work.
    # "this function touches a Connection" is itself the signal; dropping it
    # would lose ResultSet.getString, PreparedStatement.setString and friends,
    # which are 32k calls in the measured repo.
    return DB_OTHER


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

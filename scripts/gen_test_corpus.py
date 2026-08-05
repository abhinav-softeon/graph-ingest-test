"""Generate an interconnected Java corpus with GROUND TRUTH for the analysis layer.

WHY GENERATED AND NOT HAND-WRITTEN
100 hand-written files would be a demo. A generator emits the files *and* a
manifest saying which functions are genuinely vulnerable and which merely look it,
so precision and recall become measurable instead of eyeballed. It is also
regoldenable: change the shape, regenerate, re-measure.

Fully deterministic — every choice is driven by an index, never by random — so the
same invocation always produces the same corpus and the same expected findings.

WHAT IT DELIBERATELY EXERCISES
  * Pool-wrapper acquisition. Connections come from ~N in-repo factories with four
    different naming conventions and non-JDBC owning classes, mirroring the real
    repo. Only the return-type rule in external_api catches these; an owner-based
    rule finds none of them.
  * The leak distinction the graph CANNOT make. LEAK_NO_FINALLY closes on the happy
    path only, so CALLS_EXTERNAL sees a db_release and the graph calls it clean
    while it leaks on exception. This is the single most important case in the
    corpus — it is what db.released_in_finally exists for.
  * Sanitizer interposition. Some paths concatenate a parameter straight into SQL;
    others route it through a validator first. Both reach the same sink, so a
    detector that ignores sanitizers scores 100% recall and terrible precision.
  * Deep chains. endpoint -> service -> service -> dao -> pool, 5+ frames, so a
    3-hop bound provably misses end-to-end paths.
  * Utility hubs called from nearly every class, to make hub exclusion matter.
  * Overloads sharing an fqn, interfaces with multiple impls, lambdas and anonymous
    classes — the constructs that need bytecode synthesis and -g.

USAGE
    python scripts/gen_test_corpus.py --out test_corpora/java_interconnected
    python scripts/gen_test_corpus.py --out /tmp/corpus --daos 40 --services 30
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil

PKG = "com.testcorp"
_PKG_PATH = PKG.replace(".", os.sep)

# Four naming conventions, exactly as measured on the real repo — no consistent
# convention means no name-based rule can find them.
POOL_METHODS = ["getConnection", "getDbConn", "getCon", "getDbConnection"]

# Resource-handling shapes. `leaks` is the ground truth; `graph_sees_release`
# records whether a purely graph-based detector would be fooled.
SHAPES = [
    ("CLEAN_FINALLY", False, True),      # close() in finally -> genuinely clean
    ("LEAK_NO_FINALLY", True, True),     # close() on happy path only -> THE case
    ("CLEAN_TWR", False, True),          # try-with-resources -> clean
    ("LEAK_NO_CLOSE", True, False),      # never closed -> graph catches this one
]


def _w(root: str, rel: str, body: str) -> str:
    path = os.path.join(root, _PKG_PATH, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return f"{PKG}.{rel[:-5].replace('/', '.')}"


# --------------------------------------------------------------------------- utils
def gen_utils(root: str) -> list[dict]:
    """Utility hubs. Called from nearly everything, so they dominate caller counts
    and must be excluded from path enumeration or they appear on every chain."""
    _w(root, "util/StkGeneral.java", f"""\
package {PKG}.util;

public final class StkGeneral {{
    private StkGeneral() {{}}

    public static String nullCheck(String v) {{
        return v == null ? "" : v;
    }}

    public static String[] getStringArray(String v) {{
        return nullCheck(v).split(",");
    }}

    public static boolean isEmpty(String v) {{
        return nullCheck(v).length() == 0;
    }}
}}
""")
    # The sanitizer. Its presence on a path is what makes an otherwise-identical
    # flow non-exploitable, which is how precision gets tested.
    _w(root, "util/Validator.java", f"""\
package {PKG}.util;

public final class Validator {{
    private Validator() {{}}

    /** Allow-lists to digits only, so the result cannot alter SQL structure. */
    public static String sanitizeId(String raw) {{
        String s = StkGeneral.nullCheck(raw);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {{
            char c = s.charAt(i);
            if (c >= '0' && c <= '9') {{
                sb.append(c);
            }}
        }}
        return sb.toString();
    }}

    public static int toInt(String raw) {{
        try {{
            return Integer.parseInt(StkGeneral.nullCheck(raw));
        }} catch (NumberFormatException e) {{
            return -1;
        }}
    }}
}}
""")
    _w(root, "util/AuditLog.java", f"""\
package {PKG}.util;

public final class AuditLog {{
    private AuditLog() {{}}

    public static void record(String who, String what) {{
        String w = StkGeneral.nullCheck(who);
        System.out.println("[audit] " + w + " " + StkGeneral.nullCheck(what));
    }}
}}
""")
    return [{"file": "util/StkGeneral.java", "role": "hub"},
            {"file": "util/Validator.java", "role": "sanitizer"},
            {"file": "util/AuditLog.java", "role": "hub"}]


def gen_pools(root: str, count: int) -> list[str]:
    """Connection factories. Non-JDBC owner types, four naming conventions — only a
    return-type rule finds these."""
    names = []
    for i in range(count):
        cls = f"DbManager{i}"
        method = POOL_METHODS[i % len(POOL_METHODS)]
        names.append(f"{cls}#{method}")
        _w(root, f"db/{cls}.java", f"""\
package {PKG}.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper {i}. Not a JDBC type, but {method}() returns a Connection. */
public class {cls} {{
    private static final String URL = "jdbc:h2:mem:test{i}";

    public Connection {method}() throws SQLException {{
        return DriverManager.getConnection(URL);
    }}

    public boolean healthy() {{
        return URL.length() > 0;
    }}
}}
""")
    return names


# ---------------------------------------------------------------------------- dao
def _dao_method(name: str, shape: str, pool_cls: str, pool_method: str,
                inject: bool, sanitize: bool) -> str:
    """One DAO method in the given resource shape and injection posture."""
    if sanitize:
        prep = '        String safe = Validator.sanitizeId(id);\n'
        used = "safe"
    else:
        prep = ""
        used = "id"

    if inject:
        sql_stmt = (f'            java.sql.Statement st = c.createStatement();\n'
                    f'            java.sql.ResultSet rs = st.executeQuery('
                    f'"SELECT name FROM t WHERE id=\'" + {used} + "\'");\n'
                    f'            if (rs.next()) {{ out = rs.getString(1); }}\n')
    else:
        sql_stmt = (f'            java.sql.PreparedStatement ps = c.prepareStatement('
                    f'"SELECT name FROM t WHERE id=?");\n'
                    f'            ps.setString(1, {used});\n'
                    f'            java.sql.ResultSet rs = ps.executeQuery();\n'
                    f'            if (rs.next()) {{ out = rs.getString(1); }}\n')

    if shape == "CLEAN_FINALLY":
        body = (f'        Connection c = null;\n'
                f'        try {{\n'
                f'            c = pool.{pool_method}();\n'
                f'{sql_stmt}'
                f'        }} finally {{\n'
                f'            if (c != null) {{ c.close(); }}\n'
                f'        }}\n')
    elif shape == "LEAK_NO_FINALLY":
        # close() IS present, so CALLS_EXTERNAL sees a db_release and the graph
        # calls this clean. It leaks whenever anything above it throws.
        body = (f'        Connection c = pool.{pool_method}();\n'
                f'{sql_stmt}'
                f'        c.close();\n')
    elif shape == "CLEAN_TWR":
        body = (f'        try (Connection c = pool.{pool_method}()) {{\n'
                f'{sql_stmt}'
                f'        }}\n')
    else:  # LEAK_NO_CLOSE
        body = (f'        Connection c = pool.{pool_method}();\n'
                f'{sql_stmt}')

    return (f'    public String {name}(String id) throws Exception {{\n'
            f'        String out = "";\n'
            f'{prep}{body}'
            f'        return out;\n'
            f'    }}\n')


def gen_daos(root: str, count: int, pool_count: int) -> tuple[list[str], list[dict]]:
    """DAOs, cycling through every (shape x injection x sanitizer) combination."""
    fqns, expected = [], []
    for i in range(count):
        cls = f"Dao{i}"
        shape, leaks, graph_sees_release = SHAPES[i % len(SHAPES)]
        inject = (i % 3) != 0            # two thirds concatenate
        sanitize = inject and (i % 6) == 1   # some of those sanitize first
        pool_idx = i % pool_count
        pool_cls = f"DbManager{pool_idx}"
        pool_method = POOL_METHODS[pool_idx % len(POOL_METHODS)]

        # Two overloads sharing an fqn — only descriptor-level disambiguation tells
        # them apart, so this is where overload handling gets exercised.
        methods = _dao_method("load", shape, pool_cls, pool_method, inject, sanitize)
        methods += (f'    public String load(String id, boolean trace) throws Exception {{\n'
                    f'        if (trace) {{ AuditLog.record("dao", id); }}\n'
                    f'        return load(id);\n'
                    f'    }}\n')
        methods += (f'    public int count() throws Exception {{\n'
                    f'        return StkGeneral.getStringArray(load("1")).length;\n'
                    f'    }}\n')

        _w(root, f"dao/{cls}.java", f"""\
package {PKG}.dao;

import java.sql.Connection;
import {PKG}.db.{pool_cls};
import {PKG}.util.AuditLog;
import {PKG}.util.StkGeneral;
import {PKG}.util.Validator;

public class {cls} {{
    private final {pool_cls} pool = new {pool_cls}();

{methods}}}
""")
        fqns.append(f"{PKG}.dao.{cls}")
        expected.append({
            "fqn": f"{PKG}.dao.{cls}#load",
            "file": f"dao/{cls}.java",
            "shape": shape,
            "expect_leak": leaks,
            "graph_sees_a_release": graph_sees_release,
            # A concatenated query is only exploitable if nothing sanitizes first.
            "expect_injection": inject and not sanitize,
            "sanitized": sanitize,
        })
    return fqns, expected


# ------------------------------------------------------------------------ service
def gen_managers(root: str, count: int, service_count: int) -> list[str]:
    """Manager tier, between endpoints and services.

    Exists purely to make chains DEEP. Without it the corpus tops out at
    endpoint -> service -> dao (2 hops) and cannot demonstrate that a 3-hop bound
    loses end-to-end paths — which is one of the things it is supposed to prove.
    `deep()` routes through a service's peer call, giving
    endpoint -> manager -> service -> peer service -> dao = 4 hops.
    """
    fqns = []
    for i in range(count):
        cls = f"Manager{i}"
        svc = f"Service{i % service_count}"
        _w(root, f"manager/{cls}.java", f"""\
package {PKG}.manager;

import {PKG}.service.{svc};
import {PKG}.util.StkGeneral;

public class {cls} {{
    private final {svc} svc = new {svc}();

    public String process(String id) throws Exception {{
        return svc.handle(StkGeneral.nullCheck(id));
    }}

    /** Routes through the service's peer, adding two frames to the chain. */
    public String deep(String id) throws Exception {{
        return svc.viaPeer(id);
    }}

    /** Hands off to a PEER MANAGER, which then goes deep. This is what pushes the
      * longest chain past six frames: facade -> manager -> peer manager -> service
      * -> peer service -> dao. Anything with a small hop bound loses it entirely. */
    public String chain(String id) throws Exception {{
        return new Manager{(i + 1) % count}().deep(id);
    }}
}}
""")
        fqns.append(f"{PKG}.manager.{cls}")
    return fqns


def gen_facades(root: str, count: int, manager_count: int) -> list[str]:
    """Facade tier above managers — pure depth.

    The corpus exists partly to prove that a bounded-hop analysis loses real
    end-to-end paths, and that claim is only worth making if the corpus actually
    CONTAINS paths longer than the bound. It has been wrong about this before: the
    module docstring claimed 5+ frame chains while the generator emitted at most 2,
    because the deep route existed as code that nothing called.

    So this tier is wired from the endpoints and verified by the reachability check
    that reads the emitted .java, not by the comment you are reading.
    """
    fqns = []
    for i in range(count):
        cls = f"Facade{i}"
        mgr = f"Manager{i % manager_count}"
        _w(root, f"facade/{cls}.java", f"""\
package {PKG}.facade;

import {PKG}.manager.{mgr};
import {PKG}.util.StkGeneral;

public class {cls} {{
    private final {mgr} mgr = new {mgr}();

    public String orchestrate(String id) throws Exception {{
        if (StkGeneral.isEmpty(id)) {{
            return "";
        }}
        return mgr.chain(id);
    }}

    public String orchestrateDirect(String id) throws Exception {{
        return mgr.process(id);
    }}
}}
""")
        fqns.append(f"{PKG}.facade.{cls}")
    return fqns


def gen_services(root: str, count: int, dao_count: int) -> tuple[list[str], dict[int, list[int]]]:
    """Service tier. Cross-calls another service so chains exceed 3 hops and a
    3-hop bound demonstrably misses end-to-end paths.

    THE DAO MAPPING MUST BE SURJECTIVE, AND ONCE WAS NOT
    This was `Dao{i % dao_count}`, which with 25 services and 30 DAOs reached only
    Dao0-24 — the last five were never called by anything. Combined with the same
    bug one tier up, ten DAOs became unreachable dead code, and the analysis
    correctly refused to report vulnerabilities in code no request can execute. That
    read as 40% missing recall for two full runs.

    `range(i, dao_count, count)` strides instead, so every DAO belongs to exactly
    one service however the two counts relate. Returns the mapping so generate() can
    assert coverage rather than trust this comment.
    """
    fqns, mapping = [], {}
    for i in range(count):
        cls = f"Service{i}"
        dao_idx = list(range(i, dao_count, count)) or [i % dao_count]
        mapping[i] = dao_idx
        primary = f"Dao{dao_idx[0]}"
        extras = [f"Dao{d}" for d in dao_idx[1:]]
        peer = f"Service{(i + 1) % count}"
        peer_call = (f'    public String viaPeer(String id) throws Exception {{\n'
                     f'        return new {peer}().handle(id);\n'
                     f'    }}\n') if count > 1 else ""
        extra_imports = "".join(f"import {PKG}.dao.{d};\n" for d in extras)
        extra_fields = "".join(
            f"    private final {d} dao{n} = new {d}();\n"
            for n, d in enumerate(extras, start=1))
        extra_calls = "".join(
            f'    public String handleAlt{n}(String id) throws Exception {{\n'
            f'        return dao{n}.load(id);\n'
            f'    }}\n\n'
            for n, _d in enumerate(extras, start=1))
        # handle() must CALL these, not merely sit beside them. Defining handleAlt1
        # and leaving it uncalled left Dao25-29 unreachable exactly as before — the
        # methods existed, nothing invoked them, and the mapping-based self-check
        # happily confirmed the mapping it was handed. A realistic fallback chain
        # puts them on a real path from an endpoint.
        alt_fallback = "".join(
            f'        if (out.isEmpty()) {{ out = handleAlt{n}(id); }}\n'
            for n, _d in enumerate(extras, start=1))
        _w(root, f"service/{cls}.java", f"""\
package {PKG}.service;

import {PKG}.dao.{primary};
{extra_imports}import {PKG}.util.StkGeneral;

public class {cls} {{
    private final {primary} dao = new {primary}();
{extra_fields}
    public String handle(String id) throws Exception {{
        if (StkGeneral.isEmpty(id)) {{
            return "";
        }}
        String out = dao.load(id);
{alt_fallback}        return out;
    }}

    public String handleTraced(String id) throws Exception {{
        return dao.load(id, true);
    }}

{extra_calls}{peer_call}}}
""")
        fqns.append(f"{PKG}.service.{cls}")
    return fqns, mapping


# ----------------------------------------------------------------- interface/impl
def gen_handlers(root: str, count: int, service_count: int) -> list[str]:
    """One interface, many impls — the shape polymorphic dispatch and OVERRIDES
    exist for. INSTANTIATES on the concrete types is what can narrow the fan-out."""
    _w(root, "api/Handler.java", f"""\
package {PKG}.api;

public interface Handler {{
    String run(String input) throws Exception;
}}
""")
    fqns = [f"{PKG}.api.Handler"]
    for i in range(count):
        cls = f"HandlerImpl{i}"
        svc = f"Service{i % service_count}"
        _w(root, f"api/{cls}.java", f"""\
package {PKG}.api;

import {PKG}.service.{svc};

public class {cls} implements Handler {{
    private final {svc} svc = new {svc}();

    @Override
    public String run(String input) throws Exception {{
        return svc.handle(input);
    }}
}}
""")
        fqns.append(f"{PKG}.api.{cls}")
    return fqns


# ----------------------------------------------------------------------- endpoints
def gen_endpoints(root: str, count: int, service_count: int,
                  handler_count: int, manager_count: int = 0,
                  facade_count: int = 0) -> tuple[list[str], list[dict]]:
    """JAX-WS entry points — the taint SOURCES. Without these the forward
    reachability pass has nothing to walk from and the universe is empty.

    Some route through a lambda and an anonymous class, which have no source
    declaration: those need bytecode synthesis, which needs -g. If the corpus is
    compiled without -g their bodies vanish entirely, which is exactly the failure
    worth being able to reproduce.
    """
    fqns, entries, mapping = [], [], {}
    for i in range(count):
        cls = f"Endpoint{i}"
        # Strided, not modulo — see gen_services for what `i % service_count` cost.
        # With 20 endpoints and 25 services, modulo reached Service0-19 and orphaned
        # the rest, and every DAO behind them became unreachable dead code.
        svc_idx = list(range(i, service_count, count)) or [i % service_count]
        mapping[i] = svc_idx
        svc = f"Service{svc_idx[0]}"
        extra_svcs = [f"Service{n}" for n in svc_idx[1:]]
        handler = f"HandlerImpl{i % handler_count}"
        # Every third endpoint reaches the DAO through a lambda + anonymous class.
        indirect = (i % 3) == 0
        extra = (f'''
    @WebMethod
    public String viaLambda(String id) throws Exception {{
        final {svc} s = new {svc}();
        java.util.List<String> one = java.util.Collections.singletonList(id);
        final StringBuilder sb = new StringBuilder();
        one.forEach(v -> {{
            try {{
                sb.append(s.handle(v));
            }} catch (Exception e) {{
                sb.append("");
            }}
        }});
        return sb.toString();
    }}

    @WebMethod
    public String viaAnon(final String id) throws Exception {{
        Handler h = new Handler() {{
            @Override
            public String run(String input) throws Exception {{
                return new {svc}().handleTraced(input);
            }}
        }};
        return h.run(id);
    }}
''' if indirect else "")

        fac = f"Facade{i % facade_count}" if facade_count else ""
        fac_import = f"import {PKG}.facade.{fac};\n" if fac else ""
        # The longest chain in the corpus, and the reason the facade tier exists:
        # endpoint -> facade -> manager -> peer manager -> service -> peer service
        # -> dao. Six frames past the entry point.
        deepest = (f'''
    @WebMethod
    public String deepest(String id) throws Exception {{
        return new {fac}().orchestrate(id);
    }}

    @WebMethod
    public String viaFacade(String id) throws Exception {{
        return new {fac}().orchestrateDirect(id);
    }}
''' if fac else "")
        mgr = f"Manager{i % manager_count}" if manager_count else ""
        mgr_import = f"import {PKG}.manager.{mgr};\n" if mgr else ""
        # The deep route: endpoint -> manager -> service -> peer service -> dao.
        # This is what makes a 3-hop bound demonstrably lossy.
        deep_methods = (f'''
    @WebMethod
    public String viaManager(String id) throws Exception {{
        return new {mgr}().process(id);
    }}

    @WebMethod
    public String deepChain(String id) throws Exception {{
        return new {mgr}().deep(id);
    }}
''' if mgr else "")

        # Extra services this endpoint owns, so the endpoint->service mapping covers
        # every service even when there are fewer endpoints than services.
        alt_imports = "".join(f"import {PKG}.service.{v};\n" for v in extra_svcs)
        alt_methods = "".join(
            f'''
    @WebMethod
    public String lookupAlt{n}(String id) throws Exception {{
        return new {v}().handle(id);
    }}
''' for n, v in enumerate(extra_svcs, start=1))

        _w(root, f"sei/impl/{cls}.java", f"""\
package {PKG}.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import {PKG}.api.Handler;
import {PKG}.api.{handler};
{mgr_import}{fac_import}{alt_imports}import {PKG}.service.{svc};

@WebService
public class {cls} {{

    @WebMethod
    public String lookup(String id) throws Exception {{
        return new {svc}().handle(id);
    }}

    @WebMethod
    public String dispatch(String id) throws Exception {{
        Handler h = new {handler}();
        return h.run(id);
    }}
{deep_methods}{deepest}{alt_methods}{extra}}}
""")
        fqns.append(f"{PKG}.sei.impl.{cls}")
        entries.append({"fqn": f"{PKG}.sei.impl.{cls}", "file": f"sei/impl/{cls}.java",
                        "indirect": indirect})
    return fqns, entries, mapping


def _jws_stubs(root: str) -> None:
    """Minimal javax.jws annotations so the corpus compiles with a bare JDK.

    Depending on a real jaxws jar would make the corpus need a classpath, which
    would defeat the point of being able to compile it with plain `javac`."""
    for name, target in (("WebService", "TYPE"), ("WebMethod", "METHOD")):
        path = os.path.join(root, "javax", "jws", f"{name}.java")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"""\
package javax.jws;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.{target})
public @interface {name} {{
    String name() default "";
}}
""")


def _assert_all_reachable(out: str, dao_count: int, service_count: int) -> None:
    """Every DAO must be reachable from an endpoint — checked against the EMITTED CODE.

    WHY THIS READS FILES INSTEAD OF A MAPPING DICT
    The first version of this check took the generator's own service->DAO and
    endpoint->service maps and verified those covered everything. It passed, and the
    corpus was still broken: the fix had added `handleAlt1()` methods that call the
    extra DAOs, and NOTHING CALLED `handleAlt1`. The mapping described what the
    generator intended; the check confirmed the intention against itself and never
    looked at the Java.

    That is the same failure the whole corpus exists to catch — every file valid in
    isolation, the call graph between them broken. So this parses what was actually
    written: class -> methods it defines, and class -> (class, method) pairs it
    invokes, then walks forward from the @WebService endpoints. Crude (regex, not a
    parser) but it reads the artifact rather than the plan, which is the property
    that matters.
    """
    defines: dict[str, set[str]] = {}
    invokes: dict[str, set[tuple[str, str]]] = {}
    entries: list[str] = []

    for dirpath, _dn, filenames in os.walk(out):
        for name in filenames:
            if not name.endswith(".java"):
                continue
            cls = name[:-5]
            text = open(os.path.join(dirpath, name), encoding="utf-8").read()
            defines[cls] = set(re.findall(r"\b(?:public|private)\s+\w[\w.<>\[\]]*\s+(\w+)\s*\(", text))
            # `new Foo().bar(` and `field.bar(` where the field's type is known from
            # its declaration — enough for this corpus's shapes.
            calls: set[tuple[str, str]] = set()
            for tgt, meth in re.findall(r"new\s+(\w+)\s*\(\s*\)\s*\.\s*(\w+)\s*\(", text):
                calls.add((tgt, meth))
            # variable name -> declared type. The regex captures (type, name), so
            # this must be reversed; built the other way round it silently resolves
            # nothing and every field call vanishes.
            fields = {name: typ for typ, name in
                      re.findall(r"private\s+final\s+(\w+)\s+(\w+)\s*=", text)}
            for var, meth in re.findall(r"\b(\w+)\s*\.\s*(\w+)\s*\(", text):
                if var in fields:
                    calls.add((fields[var], meth))
            # A bare in-class call, e.g. handleAlt1(id) from handle().
            for meth in re.findall(r"(?<![\w.])(\w+)\s*\(", text):
                if meth in defines[cls]:
                    calls.add((cls, meth))
            invokes[cls] = calls
            if "@WebService" in text:
                entries.append(cls)

    seen: set[tuple[str, str]] = set()
    stack = [(c, m) for c in entries for m in defines.get(c, ())]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        cls, _m = node
        for tgt, meth in invokes.get(cls, ()):
            if (tgt, meth) not in seen:
                stack.append((tgt, meth))

    reached = {c for c, _m in seen}
    orphan_daos = sorted(i for i in range(dao_count) if f"Dao{i}" not in reached)
    orphan_svcs = sorted(i for i in range(service_count) if f"Service{i}" not in reached)
    # The bug is specifically that a DAO's load() is never invoked, so check the
    # method and not merely the class — a Dao reached only via count() would still
    # leave its planted finding unmeasurable.
    unloaded = sorted(i for i in range(dao_count) if (f"Dao{i}", "load") not in seen)

    if orphan_daos or orphan_svcs or unloaded:
        raise SystemExit(
            "corpus wiring is broken — ground truth would sit on dead code:\n"
            f"  services no endpoint reaches : {orphan_svcs}\n"
            f"  DAOs no endpoint reaches     : {orphan_daos}\n"
            f"  DAOs whose load() is uncalled: {unloaded}\n"
            "Expected findings on unreachable code are unmeasurable."
        )
    print(f"[gen] reachability check OK (read from emitted .java) — all {dao_count} "
          f"DAO load() methods and {service_count} services reachable from a @WebService")


def generate(out: str, daos: int = 30, services: int = 25, endpoints: int = 20,
             handlers: int = 12, pools: int = 8, managers: int = 10,
             facades: int = 8, clean: bool = True) -> dict:
    if clean and os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    _jws_stubs(out)
    gen_utils(out)
    gen_pools(out, pools)
    _dao_fqns, dao_expect = gen_daos(out, daos, pools)
    _svc_fqns, svc_to_daos = gen_services(out, services, daos)
    gen_managers(out, managers, services)
    gen_facades(out, facades, managers)
    gen_handlers(out, handlers, services)
    _ep_fqns, entries, ep_to_svcs = gen_endpoints(
        out, endpoints, services, handlers, managers, facades)

    _assert_all_reachable(out, daos, services)

    java_files = [os.path.join(dp, f) for dp, _dn, fn in os.walk(out)
                  for f in fn if f.endswith(".java")]
    manifest = {
        "package": PKG,
        "counts": {
            "files": len(java_files), "daos": daos, "services": services,
            "endpoints": endpoints, "handlers": handlers, "pools": pools,
            "managers": managers, "facades": facades,
        },
        "entry_points": entries,
        "expected_dao_findings": dao_expect,
        "expected": {
            "leaks": sum(1 for d in dao_expect if d["expect_leak"]),
            # The subset a graph-only detector must miss: it leaks, yet a release
            # IS present so CALLS_EXTERNAL is satisfied. Summary-only territory.
            "leaks_graph_cannot_see": sum(
                1 for d in dao_expect
                if d["expect_leak"] and d["graph_sees_a_release"]),
            "injections": sum(1 for d in dao_expect if d["expect_injection"]),
            "sanitized_not_vulnerable": sum(1 for d in dao_expect if d["sanitized"]),
        },
        "notes": [
            "Compile with -g or every lambda/anonymous body is lost to synthesis.",
            "expected.leaks_graph_cannot_see is the number only the summary layer "
            "can find — a graph-only detector scoring 0 there is behaving correctly, "
            "not failing.",
        ],
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="test_corpora/java_interconnected")
    ap.add_argument("--daos", type=int, default=30)
    ap.add_argument("--services", type=int, default=25)
    ap.add_argument("--endpoints", type=int, default=20)
    ap.add_argument("--handlers", type=int, default=12)
    ap.add_argument("--pools", type=int, default=8)
    ap.add_argument("--managers", type=int, default=10)
    ap.add_argument("--facades", type=int, default=8)
    ap.add_argument("--keep", action="store_true", help="do not wipe the output dir")
    args = ap.parse_args()

    m = generate(args.out, args.daos, args.services, args.endpoints,
                 args.handlers, args.pools, args.managers, args.facades,
                 clean=not args.keep)
    print(json.dumps(m["counts"], indent=2))
    print("expected findings:", json.dumps(m["expected"], indent=2))
    print(f"manifest: {os.path.join(args.out, 'manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

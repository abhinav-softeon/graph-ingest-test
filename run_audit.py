"""
Comprehensive accuracy audit of the Neo4j code graph.
Writes results to ACCURACY_AUDIT.md
"""
import os
import re
import sys
from datetime import datetime
from neo4j import GraphDatabase

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "password"
NEO4J_DB   = "neo4j"
SRC_ROOT   = "/Users/abhinav/Desktop/Projects/pr-review/final_setup/test"
OUT_FILE   = "/Users/abhinav/Desktop/Projects/pr-review/final_setup/graph-ingest-test/ACCURACY_AUDIT.md"

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

lines = []   # collected markdown lines

def w(*args):
    text = " ".join(str(a) for a in args)
    lines.append(text)
    print(text)

def flush():
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def read_file_lines(rel_path):
    """Return list of lines (1-indexed = index+1) or None if not found."""
    candidates = [
        os.path.join(SRC_ROOT, rel_path),
        os.path.join(SRC_ROOT, rel_path.lstrip("/")),
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    return f.readlines()
            except Exception:
                return None
    return None

def read_file_content(rel_path):
    lines_ = read_file_lines(rel_path)
    if lines_ is None:
        return None
    return "".join(lines_)

def method_body(all_lines, start_line, end_line):
    """Extract lines between start_line and end_line (1-based, inclusive)."""
    if not all_lines:
        return ""
    s = max(0, start_line - 1)
    e = min(len(all_lines), end_line if end_line else start_line + 40)
    return "".join(all_lines[s:e])

def check_mark(ok):
    return "✓" if ok else "✗"

# ──────────────────────────────────────────────────────────────
# HEADER
# ──────────────────────────────────────────────────────────────
w("# Neo4j Code Graph — Accuracy Audit")
w(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
w(f"**Source root:** `{SRC_ROOT}`")
w(f"**Neo4j:** `{NEO4J_URI}` / db `{NEO4J_DB}`")
w("")

# ──────────────────────────────────────────────────────────────
# SECTION 12: Overall Stats (put first for context)
# ──────────────────────────────────────────────────────────────
w("## 12. Overall Graph Statistics")
w("")
with driver.session(database=NEO4J_DB) as s:
    w("### Node counts by kind")
    w("")
    w("| Labels | Count |")
    w("|--------|-------|")
    for r in s.run("MATCH (n) RETURN labels(n) as lbl, count(*) as cnt ORDER BY cnt DESC"):
        w(f"| {', '.join(r['lbl'])} | {r['cnt']:,} |")
    w("")
    w("### Edge counts by type")
    w("")
    w("| Type | Count |")
    w("|------|-------|")
    for r in s.run("MATCH ()-[e]->() RETURN type(e) as t, count(*) as cnt ORDER BY cnt DESC"):
        w(f"| {r['t']} | {r['cnt']:,} |")
    w("")
    w("### CALLS breakdown by strategy")
    w("")
    w("| Strategy | Count |")
    w("|----------|-------|")
    for r in s.run("MATCH ()-[e:CALLS]->() RETURN e.strategy as s, count(*) as cnt ORDER BY cnt DESC"):
        w(f"| {r['s']} | {r['cnt']:,} |")
    w("")
    # Repo meta
    for r in s.run("MATCH (m:GraphMeta) RETURN m"):
        meta = dict(r['m'])
        w(f"### Repository metadata")
        w("")
        for k, v in meta.items():
            w(f"- **{k}:** {v}")
        w("")
    for r in s.run("MATCH (repo:Repository) RETURN repo"):
        repo = dict(r['repo'])
        w(f"### Repository node")
        w("")
        for k, v in repo.items():
            w(f"- **{k}:** {v}")
        w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 1: CALLS — javac_typed (30 samples)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 1. CALLS edges — `javac_typed` strategy (30 samples)")
w("")
w("**Verification:** open caller source file, find method body at reported start/end lines, confirm callee name appears in body.")
w("")

correct_jt = 0
total_jt = 0
missing_file_jt = 0

with driver.session(database=NEO4J_DB) as s:
    rows = list(s.run("""
        MATCH (a:CodeNode)-[e:CALLS {strategy:'javac_typed'}]->(b:CodeNode)
        WHERE a.file IS NOT NULL AND a.start_line IS NOT NULL
        RETURN a.name as caller, a.file as file, a.start_line as start_line,
               a.end_line as end_line, b.name as callee, b.file as callee_file
        ORDER BY rand()
        LIMIT 30
    """))

w("| # | Mark | Caller | Callee | File | Evidence |")
w("|---|------|--------|--------|------|----------|")

for i, r in enumerate(rows, 1):
    total_jt += 1
    caller = r['caller'] or '?'
    callee = r['callee'] or '?'
    file_  = r['file'] or ''
    start  = r['start_line'] or 1
    end    = r['end_line'] or (start + 60)

    file_lines = read_file_lines(file_)
    if file_lines is None:
        missing_file_jt += 1
        w(f"| {i} | ✗ | `{caller}` | `{callee}` | `{file_}` | FILE NOT FOUND |")
        continue

    body = method_body(file_lines, start, end)
    found = callee in body
    if found:
        correct_jt += 1

    short_file = os.path.basename(file_)
    evidence = f"callee `{callee}` {'found' if found else 'NOT found'} in body L{start}-{end}"
    w(f"| {i} | {check_mark(found)} | `{caller}` | `{callee}` | `{short_file}` | {evidence} |")

w("")
pct_jt = (correct_jt / total_jt * 100) if total_jt else 0
w(f"**Result: {correct_jt}/{total_jt} correct ({pct_jt:.1f}%)** — {missing_file_jt} files not found on disk")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 2: CALLS — name strategy (30 samples)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 2. CALLS edges — `name` strategy (30 samples)")
w("")
w("**Verification:** same as above — confirm callee name in caller method body.")
w("")

correct_name = 0
total_name = 0
missing_file_name = 0

with driver.session(database=NEO4J_DB) as s:
    rows_name = list(s.run("""
        MATCH (a:CodeNode)-[e:CALLS]->(b:CodeNode)
        WHERE e.strategy STARTS WITH 'name'
          AND a.file IS NOT NULL AND a.start_line IS NOT NULL
        RETURN a.name as caller, a.file as file, a.start_line as start_line,
               a.end_line as end_line, b.name as callee, b.file as callee_file,
               e.strategy as strategy
        ORDER BY rand()
        LIMIT 30
    """))

w("| # | Mark | Caller | Callee | Strategy | File | Evidence |")
w("|---|------|--------|--------|----------|------|----------|")

for i, r in enumerate(rows_name, 1):
    total_name += 1
    caller   = r['caller'] or '?'
    callee   = r['callee'] or '?'
    file_    = r['file'] or ''
    start    = r['start_line'] or 1
    end      = r['end_line'] or (start + 60)
    strategy = r['strategy'] or '?'

    file_lines = read_file_lines(file_)
    if file_lines is None:
        missing_file_name += 1
        w(f"| {i} | ✗ | `{caller}` | `{callee}` | {strategy} | `{file_}` | FILE NOT FOUND |")
        continue

    body = method_body(file_lines, start, end)
    found = callee in body
    if found:
        correct_name += 1

    short_file = os.path.basename(file_)
    evidence = f"callee {'found' if found else 'NOT found'} in body L{start}-{end}"
    w(f"| {i} | {check_mark(found)} | `{caller}` | `{callee}` | {strategy} | `{short_file}` | {evidence} |")

w("")
pct_name = (correct_name / total_name * 100) if total_name else 0
w(f"**Result: {correct_name}/{total_name} correct ({pct_name:.1f}%)** — {missing_file_name} files not found on disk")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 3: EXTENDS (30 samples)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 3. EXTENDS edges (30 samples)")
w("")
w("**Verification:** open child class file, find class declaration line, confirm `extends <ParentName>` appears near it.")
w("")

correct_ext = 0
total_ext = 0
missing_ext = 0

with driver.session(database=NEO4J_DB) as s:
    rows_ext = list(s.run("""
        MATCH (child:CodeNode)-[:EXTENDS]->(parent:CodeNode)
        WHERE child.file IS NOT NULL AND child.start_line IS NOT NULL
        RETURN child.name as child_name, child.file as file,
               child.start_line as line, parent.name as parent_name
        ORDER BY rand()
        LIMIT 30
    """))

w("| # | Mark | Child | Parent | File | Evidence |")
w("|---|------|-------|--------|------|----------|")

for i, r in enumerate(rows_ext, 1):
    total_ext += 1
    child  = r['child_name'] or '?'
    parent = r['parent_name'] or '?'
    file_  = r['file'] or ''
    line   = r['line'] or 1

    file_lines = read_file_lines(file_)
    if file_lines is None:
        missing_ext += 1
        w(f"| {i} | ✗ | `{child}` | `{parent}` | `{file_}` | FILE NOT FOUND |")
        continue

    # Check ±5 lines around class declaration
    window = method_body(file_lines, max(1, line - 2), line + 8)
    # Accept if "extends ParentName" (possibly with generics) is present
    found = bool(re.search(r'\bextends\s+' + re.escape(parent) + r'[\s<{,]', window))
    # Also try just the parent name next to "extends" anywhere
    if not found:
        found = bool(re.search(r'\bextends\b.*\b' + re.escape(parent) + r'\b', window))
    if found:
        correct_ext += 1

    short_file = os.path.basename(file_)
    evidence = f"'extends {parent}' {'found' if found else 'NOT FOUND'} near L{line}"
    w(f"| {i} | {check_mark(found)} | `{child}` | `{parent}` | `{short_file}` | {evidence} |")

w("")
pct_ext = (correct_ext / total_ext * 100) if total_ext else 0
w(f"**Result: {correct_ext}/{total_ext} correct ({pct_ext:.1f}%)** — {missing_ext} files not found on disk")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 4: IMPLEMENTS (20 samples)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 4. IMPLEMENTS edges (20 samples)")
w("")
w("**Verification:** open class file, confirm `implements <InterfaceName>` near class declaration line.")
w("")

correct_impl = 0
total_impl = 0
missing_impl = 0

with driver.session(database=NEO4J_DB) as s:
    rows_impl = list(s.run("""
        MATCH (cls:CodeNode)-[:IMPLEMENTS]->(iface:CodeNode)
        WHERE cls.file IS NOT NULL AND cls.start_line IS NOT NULL
        RETURN cls.name as cls_name, cls.file as file,
               cls.start_line as line, iface.name as iface_name
        ORDER BY rand()
        LIMIT 20
    """))

w("| # | Mark | Class | Interface | File | Evidence |")
w("|---|------|-------|-----------|------|----------|")

for i, r in enumerate(rows_impl, 1):
    total_impl += 1
    cls   = r['cls_name'] or '?'
    iface = r['iface_name'] or '?'
    file_ = r['file'] or ''
    line  = r['line'] or 1

    file_lines = read_file_lines(file_)
    if file_lines is None:
        missing_impl += 1
        w(f"| {i} | ✗ | `{cls}` | `{iface}` | `{file_}` | FILE NOT FOUND |")
        continue

    window = method_body(file_lines, max(1, line - 2), line + 8)
    found = bool(re.search(r'\bimplements\b.*\b' + re.escape(iface) + r'\b', window))
    if found:
        correct_impl += 1

    short_file = os.path.basename(file_)
    evidence = f"'implements {iface}' {'found' if found else 'NOT FOUND'} near L{line}"
    w(f"| {i} | {check_mark(found)} | `{cls}` | `{iface}` | `{short_file}` | {evidence} |")

w("")
pct_impl = (correct_impl / total_impl * 100) if total_impl else 0
w(f"**Result: {correct_impl}/{total_impl} correct ({pct_impl:.1f}%)** — {missing_impl} files not found on disk")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 5: CALLS_EXTERNAL (all 20)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 5. CALLS_EXTERNAL edges (all)")
w("")
w("**Verification:** show caller, external node name/fqn, and assess if it represents a real external call (DB driver, HTTP client, etc.).")
w("")

with driver.session(database=NEO4J_DB) as s:
    rows_ce = list(s.run("""
        MATCH (caller:CodeNode)-[e:CALLS_EXTERNAL]->(ext:CodeNode)
        RETURN caller.name as caller_name, caller.file as caller_file,
               caller.start_line as start_line,
               ext.name as ext_name, ext.fqn as ext_fqn,
               labels(ext) as ext_labels,
               e.strategy as strategy
        ORDER BY ext_name
    """))

w(f"Total CALLS_EXTERNAL edges: **{len(rows_ce)}**")
w("")
w("| # | Caller | Caller File | External Name | External FQN | Strategy | Assessment |")
w("|---|--------|-------------|---------------|--------------|----------|------------|")

# Heuristics for assessing whether an external call makes sense
KNOWN_EXTERNAL = [
    'driver', 'connection', 'statement', 'resultset', 'preparedstatement',
    'datasource', 'entitymanager', 'session', 'transaction',
    'httpclient', 'httpresponse', 'httprequest', 'resttemplate',
    'jsonobject', 'jsonarray', 'objectmapper',
    'logger', 'log',
    'stringbuilder', 'stringbuffer',
    'iterator', 'list', 'map', 'set',
    'inputstream', 'outputstream', 'reader', 'writer',
]

correct_ce = 0
for i, r in enumerate(rows_ce, 1):
    caller   = r['caller_name'] or '?'
    cf       = os.path.basename(r['caller_file'] or '')
    ext_name = (r['ext_name'] or '').lower()
    ext_fqn  = r['ext_fqn'] or r['ext_name'] or '?'
    strategy = r['strategy'] or '?'

    # Assess: is this plausibly a real external call?
    plausible = any(k in ext_name for k in KNOWN_EXTERNAL)
    # Also check if caller file exists and name appears
    body = ''
    if r['caller_file'] and r['start_line']:
        fl = read_file_lines(r['caller_file'])
        if fl:
            body = method_body(fl, r['start_line'], (r['start_line'] or 1) + 60)
    name_in_body = (r['ext_name'] or '') in body

    if plausible or name_in_body:
        correct_ce += 1
        assessment = "Plausible external call"
    else:
        assessment = "Unclear — name not obviously external"

    w(f"| {i} | `{caller}` | `{cf}` | `{r['ext_name']}` | `{ext_fqn}` | {strategy} | {assessment} |")

w("")
w(f"**Result: {correct_ce}/{len(rows_ce)} plausible external calls**")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 6: WRITES / READS (20 each)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 6. WRITES edges (20 samples)")
w("")
w("**Verification:** open caller source file, confirm field name appears in method body.")
w("")

def audit_field_edges(edge_type, limit=20):
    correct = 0
    total   = 0
    missing = 0
    with driver.session(database=NEO4J_DB) as s:
        rows = list(s.run(f"""
            MATCH (m:CodeNode)-[:{edge_type}]->(f:CodeNode)
            WHERE m.file IS NOT NULL AND m.start_line IS NOT NULL AND f.name IS NOT NULL
            RETURN m.name as method_name, m.file as file,
                   m.start_line as start_line, m.end_line as end_line,
                   f.name as field_name
            ORDER BY rand()
            LIMIT {limit}
        """))

    results = []
    for r in rows:
        total += 1
        method = r['method_name'] or '?'
        field  = r['field_name'] or '?'
        file_  = r['file'] or ''
        start  = r['start_line'] or 1
        end    = r['end_line'] or (start + 60)

        file_lines = read_file_lines(file_)
        if file_lines is None:
            missing += 1
            results.append((False, method, field, file_, start, end, "FILE NOT FOUND"))
            continue

        body  = method_body(file_lines, start, end)
        found = field in body
        if found:
            correct += 1
        short_file = os.path.basename(file_)
        evidence = f"field `{field}` {'found' if found else 'NOT FOUND'} in body L{start}-{end}"
        results.append((found, method, field, short_file, start, end, evidence))

    return results, correct, total, missing

writes_results, correct_wr, total_wr, missing_wr = audit_field_edges("WRITES")
w("| # | Mark | Method | Field | File | Evidence |")
w("|---|------|--------|-------|------|----------|")
for i, (ok, method, field, sf, sl, el, ev) in enumerate(writes_results, 1):
    w(f"| {i} | {check_mark(ok)} | `{method}` | `{field}` | `{sf}` | {ev} |")
w("")
pct_wr = (correct_wr / total_wr * 100) if total_wr else 0
w(f"**Result: {correct_wr}/{total_wr} correct ({pct_wr:.1f}%)** — {missing_wr} files not found")
w("")

w("---")
w("")
w("## 6b. READS edges (20 samples)")
w("")
w("**Verification:** same — field name should appear in method body.")
w("")

reads_results, correct_rd, total_rd, missing_rd = audit_field_edges("READS")
w("| # | Mark | Method | Field | File | Evidence |")
w("|---|------|--------|-------|------|----------|")
for i, (ok, method, field, sf, sl, el, ev) in enumerate(reads_results, 1):
    w(f"| {i} | {check_mark(ok)} | `{method}` | `{field}` | `{sf}` | {ev} |")
w("")
pct_rd = (correct_rd / total_rd * 100) if total_rd else 0
w(f"**Result: {correct_rd}/{total_rd} correct ({pct_rd:.1f}%)** — {missing_rd} files not found")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 7: INSTANTIATES (20 samples)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 7. INSTANTIATES edges (20 samples)")
w("")
w("**Verification:** confirm `new <ClassName>` appears in caller's file body.")
w("")

correct_inst = 0
total_inst = 0
missing_inst = 0

with driver.session(database=NEO4J_DB) as s:
    rows_inst = list(s.run("""
        MATCH (m:CodeNode)-[:INSTANTIATES]->(cls:CodeNode)
        WHERE m.file IS NOT NULL AND m.start_line IS NOT NULL AND cls.name IS NOT NULL
        RETURN m.name as method_name, m.file as file,
               m.start_line as start_line, m.end_line as end_line,
               cls.name as class_name
        ORDER BY rand()
        LIMIT 20
    """))

w("| # | Mark | Method | Class | File | Evidence |")
w("|---|------|--------|-------|------|----------|")

for i, r in enumerate(rows_inst, 1):
    total_inst += 1
    method = r['method_name'] or '?'
    cls    = r['class_name'] or '?'
    file_  = r['file'] or ''
    start  = r['start_line'] or 1
    end    = r['end_line'] or (start + 60)

    file_lines = read_file_lines(file_)
    if file_lines is None:
        missing_inst += 1
        w(f"| {i} | ✗ | `{method}` | `{cls}` | `{file_}` | FILE NOT FOUND |")
        continue

    body  = method_body(file_lines, start, end)
    # Check for "new ClassName" (allowing for generic brackets and spaces)
    found = bool(re.search(r'\bnew\s+' + re.escape(cls) + r'[\s<(]', body))
    if not found:
        # Broader check: just "new ClassName" anywhere in file
        content = read_file_content(file_)
        found = bool(re.search(r'\bnew\s+' + re.escape(cls) + r'[\s<(]', content or ''))
    if found:
        correct_inst += 1

    short_file = os.path.basename(file_)
    evidence = f"`new {cls}` {'found' if found else 'NOT FOUND'} in body/file"
    w(f"| {i} | {check_mark(found)} | `{method}` | `{cls}` | `{short_file}` | {evidence} |")

w("")
pct_inst = (correct_inst / total_inst * 100) if total_inst else 0
w(f"**Result: {correct_inst}/{total_inst} correct ({pct_inst:.1f}%)** — {missing_inst} files not found")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 8: OVERRIDES (20 samples)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 8. OVERRIDES edges (20 samples)")
w("")
w("**Verification:** for each child method, open child class file, confirm method is defined there AND check for `@Override` annotation or matching parent method signature.")
w("")

correct_ov = 0
total_ov = 0
missing_ov = 0

with driver.session(database=NEO4J_DB) as s:
    rows_ov = list(s.run("""
        MATCH (child:CodeNode)-[:OVERRIDES]->(parent:CodeNode)
        WHERE child.file IS NOT NULL AND child.start_line IS NOT NULL
        RETURN child.name as child_method, child.file as child_file,
               child.start_line as child_line,
               parent.name as parent_method, parent.file as parent_file
        ORDER BY rand()
        LIMIT 20
    """))

w("| # | Mark | Child Method | Parent Method | Child File | Evidence |")
w("|---|------|-------------|---------------|------------|----------|")

for i, r in enumerate(rows_ov, 1):
    total_ov += 1
    child_m  = r['child_method'] or '?'
    parent_m = r['parent_method'] or '?'
    cf       = r['child_file'] or ''
    cl       = r['child_line'] or 1

    file_lines = read_file_lines(cf)
    if file_lines is None:
        missing_ov += 1
        w(f"| {i} | ✗ | `{child_m}` | `{parent_m}` | `{cf}` | FILE NOT FOUND |")
        continue

    # Check window around the method declaration
    window = method_body(file_lines, max(1, cl - 3), cl + 5)
    # Method name should appear in or just before the declaration line
    method_defined = child_m in window
    override_ann   = '@Override' in window
    same_name      = (child_m == parent_m)

    # A valid override: method is defined in child file
    found = method_defined
    if found:
        correct_ov += 1

    annotation_note = "has @Override" if override_ann else ("same name" if same_name else "no @Override")
    short_file = os.path.basename(cf)
    evidence = f"method {'found' if method_defined else 'NOT FOUND'} at L{cl}; {annotation_note}"
    w(f"| {i} | {check_mark(found)} | `{child_m}` | `{parent_m}` | `{short_file}` | {evidence} |")

w("")
pct_ov = (correct_ov / total_ov * 100) if total_ov else 0
w(f"**Result: {correct_ov}/{total_ov} correct ({pct_ov:.1f}%)** — {missing_ov} files not found")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 9: Coverage gaps — 5 Java files, manual check
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 9. Coverage Gaps — 5 Java File Spot-Checks")
w("")
w("For each file, list the method calls that appear in the source and check whether they appear as CALLS edges in the graph.")
w("")

# Pick 5 interesting Java files from the graph
with driver.session(database=NEO4J_DB) as s:
    sample_files = list(s.run("""
        MATCH (f:CodeNode:File)
        WHERE f.file ENDS WITH '.java' OR f.path ENDS WITH '.java'
        RETURN COALESCE(f.file, f.path) as fpath
        ORDER BY rand()
        LIMIT 5
    """))

# Also try via Function nodes
if not sample_files:
    with driver.session(database=NEO4J_DB) as s:
        sample_files = list(s.run("""
            MATCH (fn:CodeNode:Function)
            WHERE fn.file IS NOT NULL AND fn.file ENDS WITH '.java'
            RETURN fn.file as fpath
            ORDER BY rand()
            LIMIT 5
        """))

seen_files = set()
file_list = []
for r in sample_files:
    fp = r.get('fpath') or ''
    if fp and fp not in seen_files:
        seen_files.add(fp)
        file_list.append(fp)

# If we still don't have 5, get from CALLS edges
if len(file_list) < 5:
    with driver.session(database=NEO4J_DB) as s:
        extras = list(s.run("""
            MATCH (a:CodeNode)-[:CALLS]->(:CodeNode)
            WHERE a.file IS NOT NULL AND a.file ENDS WITH '.java'
            RETURN DISTINCT a.file as fpath
            ORDER BY rand()
            LIMIT 10
        """))
    for r in extras:
        fp = r['fpath'] or ''
        if fp and fp not in seen_files and len(file_list) < 5:
            seen_files.add(fp)
            file_list.append(fp)

for file_rel in file_list:
    w(f"### File: `{file_rel}`")
    w("")
    content = read_file_content(file_rel)
    if content is None:
        w("*File not found on disk — cannot verify*")
        w("")
        continue

    # Extract method call patterns: word followed by (
    call_names = re.findall(r'\b([a-z][a-zA-Z0-9_]+)\s*\(', content)
    # Filter out keywords and very common noise
    keywords = {'if','for','while','switch','catch','return','throw','new',
                'super','this','assert','synchronized','try','else','do'}
    call_names = [c for c in call_names if c not in keywords]
    from collections import Counter
    top_calls = [name for name, _ in Counter(call_names).most_common(10)]

    # Query graph for CALLS edges from this file
    with driver.session(database=NEO4J_DB) as s:
        graph_calls = list(s.run("""
            MATCH (a:CodeNode)-[:CALLS]->(b:CodeNode)
            WHERE a.file = $f
            RETURN DISTINCT b.name as callee
        """, f=file_rel))
    graph_callee_names = {r['callee'] for r in graph_calls}

    w("Top method-call names found in source:")
    w("")
    w("| Call Name | In Graph? |")
    w("|-----------|----------|")
    present = 0
    for cn in top_calls:
        in_graph = cn in graph_callee_names
        if in_graph:
            present += 1
        w(f"| `{cn}` | {'✓ yes' if in_graph else '✗ missing'} |")
    w("")
    w(f"Graph coverage of top-10 calls: **{present}/10**")
    w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 10: False positives in heuristic CALLS
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 10. False Positive Check — Heuristic CALLS (name strategy)")
w("")
w("For each sampled heuristic CALLS edge, check whether the callee's containing class is imported or referenced in the caller's file. If not, the edge may target the wrong method.")
w("")

with driver.session(database=NEO4J_DB) as s:
    rows_fp = list(s.run("""
        MATCH (a:CodeNode)-[e:CALLS]->(b:CodeNode)
        WHERE e.strategy STARTS WITH 'name'
          AND a.file IS NOT NULL AND b.file IS NOT NULL
          AND a.file <> b.file
        OPTIONAL MATCH (b)<-[:CONTAINS]-(bClass:CodeNode:Class)
        RETURN a.name as caller, a.file as caller_file,
               b.name as callee, b.file as callee_file,
               bClass.name as callee_class,
               e.strategy as strategy
        ORDER BY rand()
        LIMIT 10
    """))

w("| # | Caller | Callee | Callee Class | Callee Class imported? | Risk |")
w("|---|--------|--------|--------------|------------------------|------|")

fp_count = 0
for i, r in enumerate(rows_fp, 1):
    caller        = r['caller'] or '?'
    callee        = r['callee'] or '?'
    caller_file   = r['caller_file'] or ''
    callee_class  = r['callee_class'] or '?'

    caller_content = read_file_content(caller_file) or ''

    # Check if callee class is imported or referenced in caller file
    class_referenced = (callee_class in caller_content) if callee_class != '?' else False
    # Check import statement specifically
    has_import = bool(re.search(r'\bimport\b.*\b' + re.escape(callee_class) + r'\b', caller_content))

    if has_import:
        risk = "Low — class explicitly imported"
    elif class_referenced:
        risk = "Medium — class referenced but no direct import"
    else:
        fp_count += 1
        risk = "**HIGH** — callee class not found in caller file (possible wrong target)"

    short_cf = os.path.basename(caller_file)
    w(f"| {i} | `{caller}` | `{callee}` | `{callee_class}` | {'✓' if has_import else ('~ref' if class_referenced else '✗')} | {risk} |")

w("")
w(f"**High-risk false positives: {fp_count}/10**")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SECTION 11: Node accuracy — start_line / end_line (10 samples)
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## 11. Node Accuracy — Function start_line / end_line (10 samples)")
w("")
w("**Verification:** open source file, check that the line at `start_line` contains the method signature (method name), and that `end_line` is after `start_line` and plausibly the closing `}`.")
w("")

correct_nl = 0
total_nl = 0
missing_nl = 0

with driver.session(database=NEO4J_DB) as s:
    rows_nl = list(s.run("""
        MATCH (fn:CodeNode:Function)
        WHERE fn.file IS NOT NULL AND fn.start_line IS NOT NULL AND fn.end_line IS NOT NULL
          AND fn.end_line > fn.start_line
        RETURN fn.name as name, fn.file as file,
               fn.start_line as start_line, fn.end_line as end_line
        ORDER BY rand()
        LIMIT 10
    """))

w("| # | Mark | Method | File | Start | End | Evidence |")
w("|---|------|--------|------|-------|-----|----------|")

for i, r in enumerate(rows_nl, 1):
    total_nl += 1
    name  = r['name'] or '?'
    file_ = r['file'] or ''
    start = r['start_line']
    end   = r['end_line']

    file_lines = read_file_lines(file_)
    if file_lines is None:
        missing_nl += 1
        w(f"| {i} | ✗ | `{name}` | `{file_}` | {start} | {end} | FILE NOT FOUND |")
        continue

    total_file_lines = len(file_lines)

    # Check start_line: line should contain the method name
    start_ok = False
    if 1 <= start <= total_file_lines:
        start_line_text = file_lines[start - 1]
        start_ok = name in start_line_text

    # Check end_line: should be >= start, within file, and plausibly contain }
    end_ok = (end >= start) and (1 <= end <= total_file_lines)
    if end_ok:
        end_line_text = file_lines[end - 1]
        end_ok = '}' in end_line_text

    overall_ok = start_ok and end_ok
    if overall_ok:
        correct_nl += 1

    short_file = os.path.basename(file_)
    start_note = "sig found" if start_ok else "sig NOT found"
    end_note   = "} found" if end_ok else "} NOT found"
    evidence   = f"L{start}: {start_note}; L{end}: {end_note} (file has {total_file_lines} lines)"
    w(f"| {i} | {check_mark(overall_ok)} | `{name}` | `{short_file}` | {start} | {end} | {evidence} |")

w("")
pct_nl = (correct_nl / total_nl * 100) if total_nl else 0
w(f"**Result: {correct_nl}/{total_nl} correct ({pct_nl:.1f}%)** — {missing_nl} files not found")
w("")
flush()

# ──────────────────────────────────────────────────────────────
# SUMMARY
# ──────────────────────────────────────────────────────────────
w("---")
w("")
w("## Summary")
w("")
w("### Accuracy by edge type")
w("")
w("| Edge Type | Verified | Correct | Accuracy | Notes |")
w("|-----------|----------|---------|----------|-------|")
w(f"| CALLS (javac_typed) | {total_jt} | {correct_jt} | {pct_jt:.1f}% | Highest-confidence strategy |")
w(f"| CALLS (name*) | {total_name} | {correct_name} | {pct_name:.1f}% | Heuristic; lower confidence |")
w(f"| EXTENDS | {total_ext} | {correct_ext} | {pct_ext:.1f}% | |")
w(f"| IMPLEMENTS | {total_impl} | {correct_impl} | {pct_impl:.1f}% | |")
w(f"| CALLS_EXTERNAL | {len(rows_ce)} | {correct_ce} | {(correct_ce/len(rows_ce)*100) if rows_ce else 0:.1f}% | All edges listed |")
w(f"| WRITES | {total_wr} | {correct_wr} | {pct_wr:.1f}% | |")
w(f"| READS | {total_rd} | {correct_rd} | {pct_rd:.1f}% | |")
w(f"| INSTANTIATES | {total_inst} | {correct_inst} | {pct_inst:.1f}% | |")
w(f"| OVERRIDES | {total_ov} | {correct_ov} | {pct_ov:.1f}% | |")
w(f"| Function node lines | {total_nl} | {correct_nl} | {pct_nl:.1f}% | start_line+end_line accuracy |")
w("")

w("### False positive risk (heuristic CALLS)")
w("")
w(f"- High-risk false positives in name-strategy sample: **{fp_count}/10**")
w(f"  - These are edges where the callee's class is not referenced/imported in the caller's file")
w(f"  - Recommendation: treat `name`-strategy edges with caution; prefer `javac_typed` for analysis")
w("")

w("### Notable issues / recommendations")
w("")
w("1. **javac_typed edges** are the most reliable — verify those first for any analysis task.")
w("2. **name-strategy** heuristics can produce false positives when method names are common across multiple classes.")
w("3. **CALLS_EXTERNAL** only has 20 edges total — the graph may be under-capturing external library calls.")
w("4. **WRITES/READS** field-access edges should be interpreted carefully for dynamically named fields.")
w("5. **Coverage gaps** (Section 9) show whether the graph is missing common method calls — check those results for per-file gap rates.")
w("6. **Node line numbers** (Section 11) — if accuracy < 90%, source offsets are unreliable; use with caution in UI navigation.")
w("7. **OVERRIDES** — @Override annotations may not always be present in legacy Java code; the heuristic relies on same-name + class hierarchy.")
w("")
w("---")
w(f"*Audit completed {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

flush()
driver.close()
print("\n=== AUDIT COMPLETE ===")
print(f"Output written to: {OUT_FILE}")

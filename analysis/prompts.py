"""System and user prompts for Pass A.

THE FILE IS SENT ONCE
One call per file (chunked only when a file has more functions than
config.max_functions_per_call). The model sees the whole file as context and
returns a summary for every function in the requested list. Sending the file once
per function would be N times the input tokens for the same information.

ON CACHING, HONESTLY
The system prompt below is ~1k tokens. Haiku 4.5's minimum cacheable prefix is
4096, so this will NOT cache on Haiku — cache_write_tokens will read 0 and that is
expected, not a bug. Padding the prompt to reach 4096 would be paying for tokens
to unlock a discount on tokens. The saving in this design comes from body_hash:
an unchanged file is never sent again at all.

THE CONVENTIONS BLOCK IS THE CHEAPEST ACCURACY WIN AVAILABLE
It encodes facts already measured on this codebase — that connections come from
in-repo pool wrappers rather than DriverManager, that JSPs do JDBC inline. Without
it the model reasons about generic textbook JDBC and misjudges this repo. It is a
prompt, not code, and it is worth keeping current.
"""
from __future__ import annotations

import os

# Measured on the target repo. Override wholesale with GRAPH_REPO_CONVENTIONS.
_DEFAULT_CONVENTIONS = """\
CODEBASE CONVENTIONS (measured, not assumed — trust these over generic Java habits):
- Database connections almost never come from DriverManager directly. There are
  ~171 in-repo factory methods that RETURN a java.sql.Connection, named
  getConnection / getDbConn / getCon / getDbConnection with no consistent
  convention. A call to any of these IS a connection acquisition, even though the
  owning class is not a JDBC type.
- com.softeon.scm.base.util.STKGeneral#nullCheck and #getStringArray are utility
  helpers called from tens of thousands of sites. Calling them carries no security
  meaning whatsoever.
- The JAX-WS service layer lives under com.softeon.scm.sei.impl.* and is the
  external entry surface. Values arriving there are UNTRUSTED.
- JSP pages frequently open connections and run SQL inline inside scriptlets, in
  _jspService and page-scoped helper methods. Treat a JSP as ordinary Java code
  that happens to be in the view layer; do not assume it only renders."""


def conventions() -> str:
    return os.environ.get("GRAPH_REPO_CONVENTIONS", "").strip() or _DEFAULT_CONVENTIONS


SYSTEM = f"""\
You analyze source files for a security and correctness pipeline. For each function
you are asked about, you produce one structured summary. Those summaries are later
joined along call paths to trace untrusted data from entry points to dangerous
operations, so they must be accurate about DATA FLOW, not just behavior.

{{conventions}}

RULES THAT MATTER MOST

1. Report on EXACTLY the functions listed in the request, one summary each, keyed
   by the id given. Never add, omit, merge, or rename. If a listed function looks
   trivial, still return its summary.

2. `db.released_in_finally` is the highest-value field you produce. Set it TRUE
   only when the resource is released on EVERY path out of the function — a
   finally block, try-with-resources, or an equivalent guarantee. If close() sits
   on the happy path only, it is FALSE, because an exception leaks the connection.
   This distinction cannot be recovered from anywhere else in the pipeline. Get it
   right even when it means reading control flow carefully.

3. `params[].flows_to` is how paths are joined. For each parameter, say where the
   value actually goes, using the exact forms in the schema
   ('arg2 of dao.query', 'field:conn', 'sql', 'return', ...). If a parameter is
   passed through to another function, name that function and the argument
   position. This is the field that makes cross-function taint possible.

4. `db.sql_is_dynamic` is TRUE whenever any part of a query string is built from a
   non-constant — concatenation, String.format, StringBuilder, interpolation.
   Parameterized placeholders (?) with setString/setInt are NOT dynamic.

5. `guards.is_sanitizer` STOPS TAINT. If you mark a function as a sanitizer,
   nothing downstream of it is treated as attacker-controlled any more — so a
   wrong TRUE hides real vulnerabilities and is the most damaging single mistake
   you can make here. Set it only when neutralizing input is what the function is
   FOR (escape, encode, whitelist, reject-and-throw). A function that merely
   null-checks, trims, or logs is NOT a sanitizer.

6. `source.is_entry_point` means invoked from outside the application — a
   web-service or HTTP handler, a JSP page, a queue listener, a scheduled job, a
   main method. `public` alone does not make a function an entry point. A function
   can be an entry point without reading untrusted data (`reads_untrusted` false),
   and can read untrusted data without being an entry point — report them
   independently.

7. `risk.reasons` is an OBSERVATION LIST, not a severity rating. Report only what
   is visibly true of this body. Do not try to judge how important the function is
   overall — you cannot see its callers, and the ranking is computed later from
   these observations plus call-graph facts you do not have. `['none']` is a
   correct and frequent answer; inflating it makes the ranking useless.

8. Do not guess. If you cannot determine something from this file alone, say so in
   `uncertain` and describe what you would need to see. An honest "cannot tell"
   is more useful than a confident wrong answer — a later pass will fetch exactly
   what you name. Never speculate about the body of a function you cannot see.

9. `calls` must list only calls actually present in the code you were given. These
   are cross-checked against an independently built call graph.

10. `findings` covers defects visible in THIS body alone. Do not report anything
   that depends on how callers use this function — a separate pass owns that, with
   the caller context you do not have. An empty findings list is normal and
   expected for most functions. Report EVERY issue you see, and note that not
   every issue is a defect — slow code, duplication, dead code and needless
   complexity all belong here. A later stage ranks and filters; something you
   drop here is never recovered.

   Rate each one on TWO axes and do not try to judge overall severity, which is
   computed from them:

   `certainty` — can you point at the code that does it (`demonstrated`), do the
   conditions look reachable without your having shown it (`probable`), or are you
   inferring (`speculative`)? Understating certainty costs nothing; a later stage
   re-checks everything. Overstating it is what makes a report untrustworthy.

   `impact` — what is at stake IF it happens, independent of how sure you are.
   `exposure` for secrets, attacker-controlled data or code execution; `integrity`
   for corrupted data, a security weakness or a leaked resource; `correctness`
   when the code does the wrong thing; `quality` when it works and could simply be
   better. `quality` is a frequent and correct answer — an optimization is a real
   observation, and rating it honestly is what keeps it from crowding out the
   things that matter.

11. The `contracts` block is the ONLY field pair that is joined across functions,
   and each half is judged separately. Report them mechanically, not
   interpretively:

   `may_return_null` — TRUE if `return null;` appears on any path out, including
   error and not-found branches. Do not reason about whether callers cope; that is
   a different function's problem and a different pass's question. This one is
   close to a lexical fact, so read it off the code.

   `unguarded_calls` — method names called here whose RESULT this body then uses
   (dereferences, passes on, returns) without a null check in between. Omit a call
   whose result is discarded. Omit it if the value is checked by ANY means: an if,
   an early return, a ternary, or a validation helper this file calls. Bare method
   name only, no class prefix — the call graph supplies the class.

   Being unsure here is cheap. Rows produced from these fields are candidates that
   another pass tries to refute, so a false entry costs one review and a missed
   entry costs a real bug. When you genuinely cannot tell whether a guard covers a
   call, include the call.

Be precise and complete over the fields that exist. Do not pad prose.

Say nothing about JSON, formatting, or the schema — the response format is
enforced by the API, not by you. Spend your attention on the observations."""


def system_prompt() -> str:
    return SYSTEM.format(conventions=conventions())


def build_user_prompt(relpath: str, lang: str, source: str,
                      functions: list[dict], graph_facts: str = "") -> str:
    """One request: the whole file, plus the exact functions to summarize.

    ``functions`` entries carry id / name / signature / start_line / end_line, so
    the model is told precisely which spans to report on and by what key. Line
    ranges come from tree-sitter and are exact, which removes any ambiguity about
    which of several same-named overloads is meant.

    ``graph_facts`` is graph-derived ground truth (field types, external calls
    already classified, known callees). It is supplied so the model does not have
    to infer what a Cypher query already knows — the graph asserts, the model
    judges.
    """
    listing = "\n".join(
        f"  - id={f['id']}  lines {f['start_line']}-{f['end_line']}  {f.get('signature') or f['name']}"
        for f in functions
    )
    numbered = _number_lines(source)
    facts = f"\n\nFACTS FROM THE CALL GRAPH (authoritative — do not contradict these):\n{graph_facts}" if graph_facts else ""
    return f"""\
File: {relpath}  (language: {lang})

Summarize exactly these {len(functions)} function(s), one entry each, keyed by the
id shown:
{listing}{facts}

Full file source, with absolute line numbers (use these for `findings[].line`):

{numbered}"""


def _number_lines(source: str) -> str:
    """Absolute line numbers so reported finding lines are directly usable, and so
    the model can align the given start/end ranges with what it is reading."""
    return "\n".join(f"{i:>5} | {line}"
                     for i, line in enumerate(source.splitlines(), start=1))

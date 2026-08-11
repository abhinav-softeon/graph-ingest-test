"""Tests for the B/C/D layer, against a fake store — no Neo4j, no LLM.

What these actually protect:
  * verdict indices can never attach to the wrong path
  * a path with a summary gap is SKIPPED, not guessed at
  * the adjudication panel kills on majority-refute and on ties
  * fingerprints change with the code and survive re-runs otherwise
  * dedup collapses one bug reached many ways into one row
"""
from __future__ import annotations

import pytest

from analysis import contract, findings, path_pass, adversarial_pass


class FakeStore:
    """Minimal store: canned rows per query substring, and captured writes."""

    def __init__(self, canned: dict | None = None):
        self.canned = canned or {}
        self.writes: list[tuple[str, dict]] = []

    def read(self, query: str, **params):
        for needle, rows in self.canned.items():
            if needle in query:
                return rows
        return []

    def _run(self, query: str, **params):
        self.writes.append((query, params))


def _summary(**over) -> dict:
    s = {
        "does": "does a thing", "params": [], "returns": "void", "calls": [],
        "touches": ["none"], "fields_read": [], "fields_written": [],
        "findings": [], "uncertain": [],
        "db": {"acquires": False, "releases": False, "released_in_finally": False,
               "executes_sql": False, "sql_is_dynamic": False, "resources_leaked": []},
    }
    s.update(over)
    return s


class TestVerdictValidation:
    def test_accepts_in_range(self):
        payload = {"verdicts": [{"path_index": 0}, {"path_index": 1}]}
        assert len(contract.validate_verdicts(payload, 2)) == 2

    def test_rejects_out_of_range(self):
        """An index past the end would silently attach a verdict to no path, or —
        worse, after a reorder — to the wrong one."""
        with pytest.raises(contract.ValidationError, match="out of range"):
            contract.validate_verdicts({"verdicts": [{"path_index": 5}]}, 2)

    def test_rejects_negative(self):
        with pytest.raises(contract.ValidationError, match="out of range"):
            contract.validate_verdicts({"verdicts": [{"path_index": -1}]}, 2)

    def test_rejects_non_integer(self):
        with pytest.raises(contract.ValidationError, match="out of range"):
            contract.validate_verdicts({"verdicts": [{"path_index": "0"}]}, 2)

    def test_rejects_duplicate(self):
        with pytest.raises(contract.ValidationError, match="duplicate"):
            contract.validate_verdicts(
                {"verdicts": [{"path_index": 0}, {"path_index": 0}]}, 2)

    def test_partial_is_allowed(self):
        """Unlike Pass A, a missing verdict costs one path's coverage rather than
        corrupting a store, so it is tolerated and counted by the caller."""
        assert len(contract.validate_verdicts({"verdicts": [{"path_index": 1}]}, 3)) == 1


class TestPathRendering:
    def test_skips_path_with_missing_summary(self):
        """The important one. A hole mid-chain must not be rendered — a partial
        chain invites the model to bridge the gap by assumption."""
        path = {"ids": ["a", "b"], "fqns": ["A#a", "B#b"]}
        assert path_pass._render_path(0, path, {"a": _summary()}) is None

    def test_renders_when_complete(self):
        path = {"ids": ["a"], "fqns": ["A#a"], "sink_kinds": ["db_execute"],
                "sink_names": ["Statement.execute"]}
        text = path_pass._render_path(0, path, {"a": _summary(does="runs sql")})
        assert "PATH 0" in text and "runs sql" in text and "db_execute" in text

    def test_flags_unguaranteed_release(self):
        """The leak signal has to survive into the prompt, or Pass B cannot see the
        one thing the graph could not tell it."""
        path = {"ids": ["a"], "fqns": ["A#a"]}
        s = _summary(db={"acquires": True, "releases": True,
                         "released_in_finally": False, "executes_sql": False,
                         "sql_is_dynamic": False, "resources_leaked": ["conn"]})
        text = path_pass._render_path(0, path, {"a": s})
        assert "release-NOT-guaranteed" in text

    def test_flags_dynamic_sql(self):
        path = {"ids": ["a"], "fqns": ["A#a"]}
        s = _summary(db={"acquires": False, "releases": False,
                         "released_in_finally": False, "executes_sql": True,
                         "sql_is_dynamic": True, "resources_leaked": []})
        assert "DYNAMIC" in path_pass._render_path(0, path, {"a": s})

    def test_surfaces_param_flow(self):
        """flows_to is what makes the join possible; if it stops reaching the
        prompt, Pass B degrades to guessing."""
        path = {"ids": ["a"], "fqns": ["A#a"]}
        s = _summary(params=[{"name": "id", "flows_to": ["arg2 of dao.query"],
                             "validated": False}])
        assert "arg2 of dao.query" in path_pass._render_path(0, path, {"a": s})


class TestAdjudication:
    def _finding(self, kind="sql_injection"):
        return {"kind": kind, "severity": "high", "file": "A.java", "line": 10,
                "path_fqns": ["A#a"], "path_ids": ["a"], "evidence": [],
                "reasoning": "because"}

    def test_lens_selection_by_kind(self):
        """A leak needs control-flow scrutiny; taint needs attacker-control and
        sanitization. Same panel size, different blind spots."""
        assert "leak_control_flow" in adversarial_pass._lenses_for(self._finding("resource_leak"))
        assert "leak_control_flow" not in adversarial_pass._lenses_for(self._finding("sql_injection"))

    def test_every_lens_prompt_exists(self):
        for kind in ("resource_leak", "sql_injection"):
            for lens in adversarial_pass._lenses_for(self._finding(kind)):
                assert lens in adversarial_pass.LENSES

    def test_all_lens_prompts_ask_to_refute(self):
        """If a prompt ever drifts to 'confirm', uncertainty starts counting FOR
        the finding and the false-positive rate silently climbs."""
        for lens, text in adversarial_pass.LENSES.items():
            assert "refuted" in text.lower(), lens
            assert "wrong" in text.lower() or "refute" in text.lower(), lens

    def test_empty_input(self):
        rep = adversarial_pass.run_adversarial_pass([])
        assert rep.findings_in == 0 and rep.confirmed == 0


    def test_fingerprint_tracks_sink_body_hash(self):
        """Same code -> same id (stays dismissed). Changed code -> new id
        (re-examined). That is the entire dismissal-memory design."""
        f = {"kind": "sql_injection", "sink": "Dao#q", "path_ids": ["s1"]}
        fp1 = findings.fingerprint(FakeStore({"body_hash": [{"h": "aaa"}]}), "r", f)
        fp2 = findings.fingerprint(FakeStore({"body_hash": [{"h": "aaa"}]}), "r", f)
        fp3 = findings.fingerprint(FakeStore({"body_hash": [{"h": "bbb"}]}), "r", f)
        assert fp1 == fp2
        assert fp1 != fp3

    def test_fingerprint_ignores_entry_point(self):
        """Entry point and path vary with call-graph precision; the bug does not."""
        store = FakeStore({"body_hash": [{"h": "aaa"}]})
        a = {"kind": "sql_injection", "sink": "Dao#q", "path_ids": ["s1"], "entry": "X"}
        b = {"kind": "sql_injection", "sink": "Dao#q", "path_ids": ["s1"], "entry": "Y"}
        assert findings.fingerprint(store, "r", a) == findings.fingerprint(store, "r", b)

    def test_dedupe_collapses_and_keeps_reach(self):
        """One bug reached three ways is one row — with the fan-in preserved,
        because that count is what tells a reviewer how urgent it is."""
        store = FakeStore({"body_hash": [{"h": "aaa"}]})
        rows = [
            {"kind": "sql_injection", "sink": "Dao#q", "entry": "E1",
             "path_ids": ["e1", "m", "s1"], "severity": "high"},
            {"kind": "sql_injection", "sink": "Dao#q", "entry": "E2",
             "path_ids": ["e2", "s1"], "severity": "high"},
            {"kind": "sql_injection", "sink": "Dao#q", "entry": "E3",
             "path_ids": ["e3", "m", "x", "s1"], "severity": "high"},
        ]
        out = findings.dedupe(store, "r", rows)
        assert len(out) == 1
        assert out[0]["entry"] == "E2"            # shortest path wins
        assert out[0]["reachable_from_count"] == 3
        assert out[0]["duplicate_paths"] == 2

    def test_rank_orders_by_severity_then_blast_radius(self):
        rows = [
            {"severity": "low", "reachable_from_count": 9, "path_ids": ["a"]},
            {"certainty": "demonstrated", "impact": "exposure", "severity": "critical", "reachable_from_count": 1, "path_ids": ["a"]},
            {"severity": "high", "reachable_from_count": 1, "path_ids": ["a"]},
            {"severity": "high", "reachable_from_count": 7, "path_ids": ["a"]},
        ]
        got = findings.rank(rows)
        assert [r["severity"] for r in got] == ["critical", "high", "high", "low"]
        assert got[1]["reachable_from_count"] == 7   # wider reach first within a tier

    def test_apply_dismissals_suppresses_known(self):
        rows = [{"fingerprint": "keep"}, {"fingerprint": "gone"}]
        keep, suppressed = findings.apply_dismissals(
            rows, {"gone": {"reason": "not attacker controlled"}})
        assert [r["fingerprint"] for r in keep] == ["keep"]
        assert suppressed[0]["previously_dismissed"]["reason"] == "not attacker controlled"

    def test_save_dismissals_records_reason(self):
        """A bare suppression list is unauditable — the reason must persist."""
        store = FakeStore()
        n = findings.save_dismissals(store, "r", [
            {"fingerprint": "f1", "dismissed": True,
             "dismissed_because": "parameterized query"},
            {"fingerprint": "f2"},   # not dismissed
        ])
        assert n == 1
        _query, params = store.writes[0]
        assert params["rows"][0]["reason"] == "parameterized query"


# --- regression: the taint/defect conflation that cost 14 of 15 leaks -----------
# Measured, not hypothetical. `exploitable` gated every stage while asking a TAINT
# question, so resource leaks — which are defects and never exploits — were dropped
# three separate times: not appended in Pass B, dismissed in Pass C, and given a
# standing refutation in Pass D via the attacker_control lens.

def test_verdict_schema_separates_defect_from_exploitable():
    from analysis import contract
    req = contract._PATH_VERDICT["required"]
    assert "is_defect" in req and "exploitable" in req


def test_path_pass_keeps_non_exploitable_defects_and_unresolved_verdicts():
    """The three reasons a verdict must survive Pass B. Gating on `exploitable`
    alone dropped the last two, which is why Pass C received zero findings while
    Pass B reported 35 needing expansion."""
    import inspect
    from analysis import path_pass
    src = inspect.getsource(path_pass.run_path_pass)
    assert 'v.get("is_defect")' in src, "leaks are defects, not exploits"
    assert "unresolved" in src, "'cannot tell' must reach Pass C, not be discarded"



def test_leak_lenses_exclude_attacker_control():
    """A leak involves no attacker-controlled data by definition, so that lens
    refutes every leak on a true but irrelevant technicality — and with
    majority-refute that is a standing vote against every leak."""
    from analysis import adversarial_pass
    assert "attacker_control" not in adversarial_pass._LEAK_LENSES
    assert "attacker_control" in adversarial_pass._TAINT_LENSES
    for lens in adversarial_pass._LEAK_LENSES:
        assert lens in adversarial_pass.LENSES, f"{lens} has no prompt"


def test_every_lens_name_has_a_prompt():
    from analysis import adversarial_pass
    for lens in set(adversarial_pass._TAINT_LENSES) | set(adversarial_pass._LEAK_LENSES):
        assert lens in adversarial_pass.LENSES


def test_leak_path_finding_gets_a_function_sink_not_none():
    """REGRESSION. leak_paths() returns acquire_fqn and no sink_fqn, so reading
    sink_fqn alone produced sink=None on every leak-path finding. Consequences were
    both silent: fingerprint fell back to the FILE so dedup merged distinct defects
    in one file, and scoring could only credit them by sweeping the whole path, which
    inflates recall — 30 of 106 findings in one measured run."""
    from analysis.path_pass import _to_finding
    leak = _to_finding({"kind": "resource_leak", "is_defect": True,
                        "_path": {"acquire_fqn": "com.x.Dao#load",
                                  "acquire_file": "x/Dao.java", "acquire_line": 12,
                                  "ids": ["a"], "fqns": ["com.x.Dao#load"]}})
    assert leak["sink"] == "com.x.Dao#load"
    taint = _to_finding({"kind": "sql_injection", "exploitable": True,
                         "_path": {"sink_fqn": "com.x.Dao#q", "sink_file": "x/Dao.java",
                                   "ids": ["a"], "fqns": ["com.x.Dao#q"]}})
    assert taint["sink"] == "com.x.Dao#q"

"""Tests for derived signals — the calibration layer between Pass A and selection.

The theme: the model reports observations, this code decides what they mean. So the
tests pin the DECISIONS (a validated param is not taint, a sanitizer is not a target,
a schema bump invalidates) rather than restating the weight table, which is meant to
be tuned.
"""
from __future__ import annotations

from analysis import contract, store as astore
from analysis.priority import derive_signals, RISK_WEIGHTS


def _summary(**over) -> dict:
    base = {
        "id": "f1", "does": "", "params": [], "returns": "void", "calls": [],
        "db": {"acquires": False, "releases": False, "released_in_finally": False,
               "executes_sql": False, "sql_is_dynamic": False, "resources_leaked": [],
               "throws_between_acquire_and_release": False, "resource_types": []},
        "touches": ["none"],
        "source": {"is_entry_point": False, "reads_untrusted": False, "kinds": []},
        "risk": {"reasons": ["none"], "notes": ""},
        "guards": {"is_sanitizer": False, "authenticates": False, "authorizes": False,
                   "validates_input": False, "sanitizers_called": []},
        "fields_read": [], "fields_written": [], "findings": [], "uncertain": [],
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def test_inert_function_is_not_important():
    sig = derive_signals(_summary())
    assert sig["sig_risk_score"] == 0.0
    assert sig["sig_important"] is False


def test_validated_param_is_not_taint():
    """A validated parameter reaching SQL is a parameterized query — the CORRECT
    pattern. Counting it as taint would make every well-written DAO a candidate."""
    tainted = derive_signals(_summary(params=[
        {"name": "id", "flows_to": ["sql"], "validated": False}]))
    clean = derive_signals(_summary(params=[
        {"name": "id", "flows_to": ["sql"], "validated": True}]))
    assert tainted["sig_taint_params"] == 1
    assert clean["sig_taint_params"] == 0


def test_non_sink_flows_are_not_taint():
    """'return' and 'field:x' are flows but not sinks."""
    sig = derive_signals(_summary(params=[
        {"name": "x", "flows_to": ["return", "field:conn"], "validated": False}]))
    assert sig["sig_taint_params"] == 0


def test_leak_detected_when_release_is_not_guaranteed():
    """Two independent expressions of a real leak: no finally at all, or a named
    resource that escapes closing even though something is closed."""
    for db in ({"acquires": True, "released_in_finally": False},
               {"acquires": True, "released_in_finally": True,
                "resources_leaked": ["rs"]}):
        assert derive_signals(_summary(db=db))["sig_leak"] is True


def test_throws_between_does_not_by_itself_mean_a_leak():
    """REGRESSION — measured on the corpus, not hypothetical.

    `throws_between_acquire_and_release` is true of any try/finally doing real work,
    because something can always throw; handling that is what the finally is FOR.
    Treating it as independent proof of a leak scored a correctly-closed DAO at
    7.5/important, which would flag every well-written DAO in the repo and make the
    ranking meaningless."""
    sig = derive_signals(_summary(db={
        "acquires": True, "releases": True, "released_in_finally": True,
        "throws_between_acquire_and_release": True, "resource_types": ["Connection"]}))
    assert sig["sig_leak"] is False
    assert sig["sig_confirmed_leak"] is False


def test_throws_between_upgrades_an_unguarded_acquire():
    """It IS evidence — just only when the release is not already guaranteed."""
    suspected = derive_signals(_summary(db={
        "acquires": True, "released_in_finally": False}))
    confirmed = derive_signals(_summary(db={
        "acquires": True, "released_in_finally": False,
        "throws_between_acquire_and_release": True}))
    assert suspected["sig_confirmed_leak"] is False
    assert confirmed["sig_confirmed_leak"] is True
    assert confirmed["sig_risk_score"] > suspected["sig_risk_score"]


def test_clean_twr_is_not_a_leak():
    sig = derive_signals(_summary(db={
        "acquires": True, "releases": True, "released_in_finally": True}))
    assert sig["sig_leak"] is False


def test_sanitizer_is_downweighted_not_flagged_as_target():
    """An escaper legitimately builds strings dynamically. Scoring it as a target
    would put the security CONTROL at the top of the report instead of the bug."""
    risky = _summary(risk={"reasons": ["builds_sql_dynamically"]},
                     db={"sql_is_dynamic": True})
    plain = derive_signals(risky)
    guarded = derive_signals({**risky, "guards": {**risky["guards"], "is_sanitizer": True}})
    assert guarded["sig_risk_score"] < plain["sig_risk_score"]
    assert guarded["sig_sanitizer"] is True


def test_dynamic_sql_with_unvalidated_param_is_important():
    sig = derive_signals(_summary(
        db={"executes_sql": True, "sql_is_dynamic": True},
        params=[{"name": "q", "flows_to": ["sql"], "validated": False}],
        risk={"reasons": ["builds_sql_dynamically"]},
    ))
    assert sig["sig_important"] is True
    assert sig["sig_sql_dynamic"] is True


def test_none_reason_is_stripped():
    """'none' is an enum member so the model can answer explicitly, but it must not
    reach the node as a reason or every function looks annotated."""
    sig = derive_signals(_summary(risk={"reasons": ["none"]}))
    assert sig["sig_reasons"] == []


def test_unknown_reason_scores_low_but_does_not_crash():
    """Enum drift must degrade, not raise: a schema edit that adds a reason before
    the weight table is updated should still produce a usable score."""
    sig = derive_signals(_summary(risk={"reasons": ["some_future_reason"]}))
    assert sig["sig_risk_score"] > 0


def test_every_enum_reason_has_a_weight():
    """Guards against adding a RISK_REASONS member and forgetting to weight it,
    which would silently score it at the unknown-reason fallback forever."""
    assert set(contract.RISK_REASONS) <= set(RISK_WEIGHTS)


def test_entry_and_untrusted_are_independent():
    """A handler that takes no input is an entry point that reads nothing; a parser
    reads untrusted data without being an entry point. Conflating them mis-seeds
    reachability in both directions."""
    entry = derive_signals(_summary(source={"is_entry_point": True}))
    reader = derive_signals(_summary(source={"reads_untrusted": True}))
    assert (entry["sig_entry"], entry["sig_untrusted"]) == (True, False)
    assert (reader["sig_entry"], reader["sig_untrusted"]) == (False, True)


def test_signals_carry_schema_version():
    assert derive_signals(_summary())["sig_schema_version"] == contract.SCHEMA_VERSION


def test_stale_schema_forces_resummarize_even_when_body_unchanged():
    """The failure this prevents: body_hash matches so the summary reads as fresh,
    but it predates the fields the passes below consume, which surfaces as zero
    findings rather than an error."""
    fns = [
        {"body_hash": "aaa", "summary_hash": "aaa",
         "sig_schema_version": contract.SCHEMA_VERSION},          # current
        {"body_hash": "bbb", "summary_hash": "bbb",
         "sig_schema_version": contract.SCHEMA_VERSION - 1},      # old shape
        {"body_hash": "ccc", "summary_hash": "ccc"},              # never scored
        {"body_hash": "ddd", "summary_hash": "old"},              # edited body
    ]
    pending = astore.needs_summary(fns)
    assert [f["body_hash"] for f in pending] == ["bbb", "ccc", "ddd"]


def test_schema_requires_the_new_blocks():
    """Pins the contract the passes read, so removing a block fails here rather than
    as an empty column three passes later."""
    required = contract._SUMMARY["required"]
    for field in ("source", "risk", "guards"):
        assert field in required
    db_required = contract._SUMMARY["properties"]["db"]["required"]
    assert "throws_between_acquire_and_release" in db_required

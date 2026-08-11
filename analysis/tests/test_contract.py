"""Pass A's validation layer — the thing that makes a small model trustworthy here.

Every test is a way a response could quietly corrupt the summary store. Rejecting
a chunk costs one retry; storing a bad summary poisons every path built on it and
is invisible downstream, so the bias is strongly toward rejection.
"""
from __future__ import annotations

import json

import pytest

from analysis import contract


def _summary(fid: str, **over) -> dict:
    row = {
        "id": fid, "does": "does a thing", "params": [], "returns": "void",
        "calls": [], "touches": ["none"], "fields_read": [], "fields_written": [],
        "findings": [], "uncertain": [],
        "contracts": {"may_return_null": False, "null_condition": "",
                      "returns_sentinel": "", "unguarded_calls": [],
                      "swallowed_exception_calls": []},
        "db": {"acquires": False, "releases": False, "released_in_finally": False,
               "executes_sql": False, "sql_is_dynamic": False, "resources_leaked": [],
               "throws_between_acquire_and_release": False, "resource_types": []},
        "source": {"is_entry_point": False, "reads_untrusted": False, "kinds": []},
        "risk": {"reasons": ["none"], "notes": ""},
        "guards": {"is_sanitizer": False, "authenticates": False, "authorizes": False,
                   "validates_input": False, "sanitizers_called": []},
    }
    row.update(over)
    return row


class TestValidate:
    def test_accepts_exact_match(self):
        payload = {"summaries": [_summary("a"), _summary("b")]}
        assert len(contract.validate(payload, ["a", "b"])) == 2

    def test_order_does_not_matter(self):
        """The model may return summaries in any order; only the SET must match."""
        payload = {"summaries": [_summary("b"), _summary("a")]}
        assert len(contract.validate(payload, ["a", "b"])) == 2

    def test_rejects_response_missing_every_signal_block(self):
        """Systematic omission is a schema failure, so it must retry rather than store.

        This only matters on the legacy Bedrock endpoint, where the schema is
        prompt-requested rather than API-enforced. Because every derived signal
        defaults to falsy, storing these would produce a repo in which nothing is
        important and no path is ever selected — indistinguishable from clean code."""
        bare = [{"id": "a", "does": "x"}, {"id": "b", "does": "y"}]
        with pytest.raises(contract.ValidationError, match="omits the 'source' block"):
            contract.validate({"summaries": bare}, ["a", "b"])

    def test_tolerates_one_summary_missing_a_block(self):
        """Sporadic is not systematic. One under-filled summary is not worth
        discarding a whole file's work — the defaults absorb it."""
        rows = [_summary("a"), _summary("b")]
        del rows[1]["guards"]
        assert len(contract.validate({"summaries": rows}, ["a", "b"])) == 2

    def test_rejects_invented_id(self):
        """A hallucinated id means it described a function that does not exist —
        the single most dangerous failure, because the summary looks well-formed."""
        payload = {"summaries": [_summary("a"), _summary("ghost")]}
        with pytest.raises(contract.ValidationError, match="unknown function id"):
            contract.validate(payload, ["a", "b"])

    def test_rejects_missing_function(self):
        """A silently dropped function is a coverage hole. Nothing downstream can
        tell the difference between 'no findings' and 'never looked'."""
        payload = {"summaries": [_summary("a")]}
        with pytest.raises(contract.ValidationError, match="missing from the response"):
            contract.validate(payload, ["a", "b"])

    def test_rejects_duplicate_id(self):
        """Two summaries for one id means one silently wins — non-deterministic
        storage, which is worse than an error."""
        payload = {"summaries": [_summary("a"), _summary("a")]}
        with pytest.raises(contract.ValidationError, match="duplicate"):
            contract.validate(payload, ["a"])

    def test_rejects_non_object_payload(self):
        with pytest.raises(contract.ValidationError):
            contract.validate([], ["a"])  # type: ignore[arg-type]

    def test_rejects_missing_summaries_key(self):
        with pytest.raises(contract.ValidationError, match="missing 'summaries'"):
            contract.validate({"results": []}, ["a"])

    def test_empty_request_accepts_empty_response(self):
        assert contract.validate({"summaries": []}, []) == []


class TestUnknownCallees:
    def test_all_known(self):
        rows = [_summary("a", calls=["findById", "save"])]
        assert contract.unknown_callee_rate(rows, {"a": {"findById", "save"}}) == (0, 2)

    def test_flags_unknown(self):
        rows = [_summary("a", calls=["findById", "totallyMadeUp"])]
        assert contract.unknown_callee_rate(rows, {"a": {"findById"}}) == (1, 2)

    def test_strips_qualifier_and_parens(self):
        """`dao.findById(x)` and `findById` are the same callee — the graph stores
        the bare method name, so the comparison normalizes to that."""
        rows = [_summary("a", calls=["dao.findById(id)", "this.save()"])]
        assert contract.unknown_callee_rate(rows, {"a": {"findById", "save"}}) == (0, 2)

    def test_no_facts_means_everything_unknown(self):
        """Diagnostic, not a gate: a function with no resolved callees in the graph
        reports 100% unknown, which is a statement about graph coverage rather
        than about the model."""
        rows = [_summary("a", calls=["x", "y"])]
        assert contract.unknown_callee_rate(rows, {}) == (2, 2)


class TestSchema:
    def test_closed_at_every_level(self):
        """Structured outputs REQUIRE additionalProperties:false on every object,
        and reject length/numeric bounds. A violation 400s the whole request, so
        this is enforced here rather than discovered at runtime."""
        def walk(node, path="root"):
            bad = []
            if isinstance(node, dict):
                if node.get("type") == "object" and node.get("additionalProperties") is not False:
                    bad.append(f"{path}: additionalProperties must be false")
                for k in ("minLength", "maxLength", "minimum", "maximum", "multipleOf"):
                    if k in node:
                        bad.append(f"{path}: unsupported constraint {k}")
                for k, v in node.items():
                    bad += walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    bad += walk(v, f"{path}[{i}]")
            return bad

        assert walk(contract.SUMMARY_SCHEMA) == []

    def test_leak_critical_fields_are_required(self):
        """released_in_finally is the field the graph cannot express. If it ever
        becomes optional, leak precision silently reverts to the graph's."""
        wire = (contract.SUMMARY_SCHEMA["properties"]["summaries"]["items"])
        assert "db_released_in_finally" in wire["required"]
        assert "db_sql_is_dynamic" in wire["required"]
        assert "db_resources_leaked" in wire["required"]
        # ...and that nest() restores the name the rest of the pipeline reads.
        assert contract.nest({"db_released_in_finally": True})["db"]["released_in_finally"]

    def test_is_json_serializable(self):
        json.dumps(contract.SUMMARY_SCHEMA)

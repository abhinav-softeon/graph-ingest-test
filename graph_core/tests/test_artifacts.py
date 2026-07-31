"""Phase 0 guardrails: what gets admitted at upload, and what gets parsed.

The invariant these protect is a single asymmetry that is easy to break by
accident and silent when broken:

    an artifact is admitted from build-output directories and NEVER parsed
    as source; a source file keeps the build-output exclusion.

Get the first half wrong and widening the extension allowlist admits nothing,
because `target/classes` is both where .class files live and a directory the
upload filter excludes. Get the second half wrong and generated .java under
target/ floods the graph with duplicate definitions.
"""
from __future__ import annotations

import os
import tempfile

from graph_core.artifacts import artifact_kind, is_artifact
from graph_core.discovery import discover, discover_artifacts, list_candidate_relpaths
from graph_core.schema import EDGE_TYPES, NODE_LABELS
from ingest.upload_utils import DEFAULT_EXCLUDED_DIRS, SUPPORTED_CODE_EXTS, _admits


def _tree(*relpaths: str) -> str:
    root = tempfile.mkdtemp()
    for rel in relpaths:
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("class A {}\n")
    return root


def _admitted(rel: str) -> bool:
    return _admits(rel, SUPPORTED_CODE_EXTS, set(DEFAULT_EXCLUDED_DIRS))


class TestArtifactClassification:
    def test_kinds(self):
        assert artifact_kind("Foo.class") == "bytecode"
        assert artifact_kind("WEB-INF/lib/spring.jar") == "archive"
        assert artifact_kind("c.tld") == "taglib"
        assert artifact_kind("pom.xml") == "buildconfig"
        assert artifact_kind("WEB-INF/web.xml") == "webdeploy"
        assert artifact_kind("tsconfig.json") == "jsconfig"

    def test_jsp_is_source_not_artifact(self):
        """extractors/jsp.py translates JSP to synthetic Java and parses it, so
        JSP is source. Claiming it here would make _source_lang refuse it and
        the pages would silently never be extracted."""
        for ext in (".jsp", ".jspf", ".tag"):
            assert not is_artifact("page" + ext)

    def test_config_matched_by_filename_not_extension(self):
        """A blanket .xml/.json open would swallow every fixture in the repo."""
        assert is_artifact("pom.xml")
        assert not is_artifact("src/test/resources/fixture.xml")
        assert is_artifact("package.json")
        assert not is_artifact("package-lock.json")

    def test_d_ts_beats_splitext(self):
        """splitext('foo.d.ts') is '.ts' — a source extension. Order matters."""
        assert artifact_kind("foo.d.ts") == "stub"
        assert not is_artifact("foo.ts")


class TestUploadAdmission:
    def test_artifacts_survive_build_output_dirs(self):
        assert _admitted("target/classes/com/acme/Foo.class")
        assert _admitted("build/classes/java/main/A$1.class")
        assert _admitted("WEB-INF/classes/org/apache/jsp/index_jsp.class")
        assert _admitted("WEB-INF/lib/spring-core.jar")

    def test_source_keeps_build_output_exclusion(self):
        """The asymmetry. Generated sources under target/ must stay out."""
        assert not _admitted("target/generated-sources/com/acme/Gen.java")
        assert _admitted("src/main/java/com/acme/Foo.java")

    def test_vendored_trees_excluded_for_both(self):
        assert not _admitted("node_modules/left-pad/index.js")
        assert not _admitted("node_modules/x/y.class")

    def test_unrelated_files_still_rejected(self):
        assert not _admitted("README.md")
        assert not _admitted("docs/notes.txt")


class TestDiscoverySeparation:
    def test_artifacts_never_parsed_as_source(self):
        root = _tree(
            "src/main/java/com/acme/Foo.java",
            "target/classes/com/acme/Foo.class",
            "target/generated-sources/com/acme/Gen.java",
            "WEB-INF/web.xml",
            "src/main/webapp/index.jsp",
            "pom.xml",
        )
        source = {f.relpath.replace(os.sep, "/") for f in discover(root)}
        # JSP is source (translated to synthetic Java); generated .java under
        # target/ is not.
        assert source == {
            "src/main/java/com/acme/Foo.java",
            "src/main/webapp/index.jsp",
        }

        arts = {a.relpath: a.kind for a in discover_artifacts(root)}
        assert arts == {
            "target/classes/com/acme/Foo.class": "bytecode",
            "WEB-INF/web.xml": "webdeploy",
            "pom.xml": "buildconfig",
        }

    def test_upload_path_drops_artifacts(self):
        """discover(candidate_relpaths=...) is a separate branch from the walk;
        the upload flow uses it, so it needs the same guarantee."""
        names = ["A.java", "A.class", "lib.jar", "web.xml", "index.jsp", "q.sql", "t.d.ts"]
        root = _tree(*names)
        got = sorted(f.relpath for f in discover(root, candidate_relpaths=names))
        assert got == ["A.java", "index.jsp", "q.sql"]

    def test_d_ts_not_parsed_as_typescript(self):
        """Declaration files have no bodies — every Function node from one is a
        phantom that then competes as a resolution candidate."""
        root = _tree("app.ts", "types/global.d.ts")
        assert {f.relpath.replace(os.sep, "/") for f in discover(root)} == {"app.ts"}
        assert list_candidate_relpaths(root) == ["app.ts"]


class TestSchema:
    def test_external_call_types_registered(self):
        """assert_label/assert_edge gate every label interpolated into Cypher."""
        assert "External" in NODE_LABELS
        assert "CALLS_EXTERNAL" in EDGE_TYPES

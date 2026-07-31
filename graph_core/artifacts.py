"""What counts as a non-source *artifact*, and what kind it is.

Source files are classified by ``discovery.EXT_LANG`` and handed to tree-sitter.
Artifacts are everything the precise resolvers need that is NOT parsed as
source: compiled bytecode, archives, templates that compile to code, type stubs,
and the build/deployment configuration that tells a compiler how to resolve
anything at all.

This module deliberately holds no logic beyond classification and has no imports
from the rest of the package, so both the ingest/upload layer and the graph
discovery layer can share one definition. Duplicating these tables was the
obvious alternative and would drift: the upload filter and the artifact walk
have to agree exactly, or a file is admitted at upload and then never found (or
found on disk but never uploaded).
"""
from __future__ import annotations

import os

# Extension -> artifact kind.
#
#   bytecode  compiled classes: exact call bindings, exact field access, and the
#             only source for lambda / anonymous-class / <clinit> methods
#   archive   jars — dependency classpath, and sometimes the app's own classes
#   taglib    .tld maps <mytag:foo> to its Java handler class
#   stub      type stubs; the input a type checker needs for third-party code
#
# .jsp/.jspf/.tag are deliberately NOT here: extractors/jsp.py translates them
# to synthetic Java and parses them, so they are source and belong in
# discovery.EXT_LANG. Listing them here would make _source_lang refuse them.
EXT_ARTIFACT = {
    ".class": "bytecode",
    ".jar": "archive",
    ".war": "archive",
    ".ear": "archive",
    ".tld": "taglib",
    ".pyi": "stub",
}

# Exact filenames, NOT extensions.
#
# Opening ``.xml``/``.json``/``.toml`` wholesale would pull in every test
# fixture, lockfile and IDE state file in a repo. These are the specific files
# that carry a classpath, a module layout, or a URL -> handler mapping — the
# things without which a compiler cannot resolve a single external symbol.
ARTIFACT_FILENAMES = {
    # JVM build: where the dependency classpath comes from
    "pom.xml": "buildconfig",
    "build.gradle": "buildconfig",
    "build.gradle.kts": "buildconfig",
    "settings.gradle": "buildconfig",
    "settings.gradle.kts": "buildconfig",
    "ivy.xml": "buildconfig",
    # A literal classpath, no build tool required to read it
    ".classpath": "buildconfig",
    "manifest.mf": "buildconfig",
    # Servlet mappings: URL -> servlet/JSP, i.e. the endpoint layer
    "web.xml": "webdeploy",
    # JS/TS module resolution: without tsconfig, path aliases resolve to nothing
    "tsconfig.json": "jsconfig",
    "jsconfig.json": "jsconfig",
    "package.json": "jsconfig",
    # Python import roots and checker configuration
    "pyproject.toml": "pyconfig",
    "pyrightconfig.json": "pyconfig",
    "setup.cfg": "pyconfig",
}

# Directories that a SOURCE walk excludes but an ARTIFACT walk must enter.
#
# This is the subtle half of admitting bytecode: `target/classes`,
# `build/classes` and `WEB-INF/classes` are exactly where .class files live, and
# they are precisely the directories excluded as build output. Widening the
# extension allowlist alone would therefore have admitted nothing at all.
#
# The exclusion stays correct for source — build output holds generated and
# duplicated code that would corrupt the graph — so the rule is per-file-kind:
# source keeps the exclusion, artifacts ignore it.
ARTIFACT_DIR_OVERRIDES = {"target", "build", "out", "bin", "dist", ".mvn"}


def artifact_kind(relpath: str) -> str | None:
    """Artifact kind for a path, or None if it is not an artifact.

    Matches on the basename only, so it works identically against a zip entry
    name and an on-disk relative path.
    """
    name = relpath.replace("\\", "/").rsplit("/", 1)[-1].lower()
    kind = ARTIFACT_FILENAMES.get(name)
    if kind is not None:
        return kind
    # `.d.ts` must be checked before splitext, which would only see ".ts" —
    # and ".ts" is a source extension, so the order matters.
    if name.endswith(".d.ts"):
        return "stub"
    return EXT_ARTIFACT.get(os.path.splitext(name)[1])


def is_artifact(relpath: str) -> bool:
    return artifact_kind(relpath) is not None

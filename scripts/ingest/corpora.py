"""Canonical definition of the Aztec DocsGPT knowledge-base corpora.

This module is the single source of truth for what gets ingested when
we cut a new Aztec release. Both the ``build`` and ``upload`` CLIs read
this list, and the ``README.md`` in this directory references it.

Adding a new corpus or pointing an existing one at a new version is a
matter of editing this file and re-running the build + upload steps.

Conventions
-----------

``rel_prefix``
    Every chunk's ``metadata.source`` is the file's path inside the
    uploaded zip, relative to the zip root. We deliberately namespace
    each corpus under a stable directory at the zip root (e.g.
    ``aztec-nr/...``, ``noir-stdlib/...``). This is what
    ``api/answer/routes/base.py:_aztec_source_url`` uses to translate
    chunk source paths back to public URLs, and what
    ``scripts/eval/eval_retrieval.py`` uses to bucket retrieval hits.
    Do **not** change a ``rel_prefix`` without updating both files.

``transform``
    ``passthrough``
        Files are zipped as-is. Used for native markdown corpora.
    ``rename_code_to_txt``
        Code files (``.nr`` / ``.ts`` / ``.sol``) get an extra ``.txt``
        suffix appended so the DocsGPT parser allowlist accepts them.
        ``Token.nr`` → ``aztec-nr/.../Token.nr.txt``. Used for body-
        bearing corpora (examples, tests, circuits, TS code, Solidity).
    ``noir_apiref``
        ``.nr`` files are run through ``noir_apiref.py``, which strips
        bodies and ``//`` comments and emits a Markdown view of the
        public surface. Output filenames are ``foo.nr.md``. Only
        applied to library-shaped Noir corpora (``aztec-nr``,
        ``noir-stdlib``) — examples and circuits keep their bodies.

``include_extensions``
    Extensions to copy under ``passthrough`` / ``rename_code_to_txt``.
    Ignored when ``transform == "noir_apiref"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SourceTree:
    """One source-tree slice within a corpus.

    A corpus is one ``sources`` row in the DB and one zip on the wire,
    but it may bundle multiple source trees that each get their own
    top-level prefix inside the zip. The CLI corpus, for instance,
    contains both ``yarn-project/cli/src`` (zipped under ``cli/``)
    AND ``yarn-project/cli-wallet/src`` (zipped under ``cli-wallet/``)
    — each tree's prefix has to match the corresponding entry in
    ``_SOURCE_TO_REPO_PREFIX`` (api/answer/routes/base.py) so source
    URLs link to the right GitHub blob.
    """
    # Path inside the source root (e.g. "yarn-project/cli/src").
    path: str
    # Top-level directory inside the zip that this tree lands under
    # (e.g. "cli"). Trailing slash is optional and stripped on use.
    zip_prefix: str
    # Optional fnmatch-style patterns for files to exclude — relative
    # to ``path``. Matches against the file's relative path INSIDE the
    # source tree, NOT against just the basename. Use this for
    # surgical removals like transitional release-note / migration
    # files that mention every renamed identifier in both spellings
    # and dominate identifier-shaped queries despite never being the
    # canonical answer. Example::
    #
    #     exclude_paths=("docs/resources/migration_notes.*",)
    #
    # Patterns that match a directory exclude every file beneath it::
    #
    #     exclude_paths=("operators/reference/changelog/*",)
    exclude_paths: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Corpus:
    # Human-readable name (this becomes the ``sources.name`` row in the
    # database — visible to admins via SQL).
    name: str
    # Short slug used for the zip filename and as a CLI argument.
    slug: str
    # Where the source files live, relative to one of the supported
    # source roots.
    source_root: str  # "aztec-packages" | "noir"
    # One or more source trees that contribute to this corpus.
    trees: Tuple[SourceTree, ...]
    # Extensions to copy into the zip. Use lower-case.
    include_extensions: Tuple[str, ...]
    # How to transform files on the way into the zip.
    transform: str
    # Optional notes — surfaced in ``aztec_corpora.py list``.
    notes: str = ""
    # Whether this corpus is a member of the production widget's
    # source list. The build CLI marks the manifest accordingly so
    # the swap-sources step knows which UUIDs to wire in.
    in_production_agent: bool = True

    @property
    def rel_prefix(self) -> str:
        """Convenience: the dominant zip prefix of this corpus, used
        for matching against ``_SOURCE_TO_REPO_PREFIX`` and for the
        eval harness's bucket detection. For multi-tree corpora this
        is the prefix of the FIRST tree — eval / source-URL mapping
        treats each tree's files separately by their own prefix."""
        return self.trees[0].zip_prefix.rstrip("/") + "/"

    @property
    def source_paths(self) -> Tuple[str, ...]:
        return tuple(t.path for t in self.trees)


# ── The 12 corpora ─────────────────────────────────────────────────────────


CORPORA: Tuple[Corpus, ...] = (
    # ---- Markdown / docs corpora (passthrough) -------------------------
    Corpus(
        name="Aztec Developer Docs v4.2.0",
        slug="aztec_developer_docs",
        source_root="aztec-packages",
        trees=(
            SourceTree(
                "docs/developer_versioned_docs/version-v4.2.0",
                "version-v4.2.0/docs",
                exclude_paths=(
                    # Migration notes accumulate every renamed identifier
                    # in both old and new spellings, so they dominate
                    # identifier-shaped queries despite never being the
                    # canonical answer. 285 chunks at v4.2.0 — biggest
                    # single source of off-target citations in the
                    # widget. If we ever need migration content again,
                    # carve it into a separate corpus that's only
                    # included for migration-shaped queries.
                    "docs/resources/migration_notes.*",
                ),
            ),
        ),
        include_extensions=(".md", ".mdx", ".json"),
        transform="passthrough",
    ),
    Corpus(
        name="Aztec Network Docs v4.2.0",
        slug="aztec_network_docs",
        source_root="aztec-packages",
        trees=(
            SourceTree(
                "docs/network_versioned_docs/version-v4.2.0",
                "version-v4.2.0/operators",
                # Same logic as migration_notes above — release notes
                # mention every renamed config / flag / RPC method
                # but aren't the canonical reference for any of them.
                exclude_paths=(
                    "operators/reference/changelog/*",
                    "reference/changelog/*",
                ),
            ),
        ),
        include_extensions=(".md",),
        transform="passthrough",
    ),
    Corpus(
        name="Aztec TypeScript API v4.2.0",
        slug="aztec_typescript_api",
        source_root="aztec-packages",
        trees=(SourceTree("docs/static/typescript-api/testnet", "typescript-api"),),
        include_extensions=(".md", ".txt"),
        transform="passthrough",
        notes="auto-generated from yarn-project/* tsdoc; the corpus is "
              "the rendered output, not the .ts source",
    ),
    Corpus(
        name="Noir Language Docs v4.2.0",
        slug="noir_language_docs",
        source_root="noir",
        trees=(SourceTree("docs/docs", "noir-docs"),),
        include_extensions=(".md", ".mdx"),
        transform="passthrough",
        notes="from noir-lang/noir at the commit pinned by aztec-packages "
              "v4.2.0 (see CLAUDE.md for the canonical commit hash)",
    ),
    # ---- Apiref corpora (noir_apiref transform) ------------------------
    Corpus(
        name="Aztec.nr Framework v4.2.0 (apiref)",
        slug="aztec_nr_apiref",
        source_root="aztec-packages",
        trees=(SourceTree("noir-projects/aztec-nr", "aztec-nr"),),
        include_extensions=(".nr",),
        transform="noir_apiref",
        notes="public-surface only: doc comments + signatures",
    ),
    Corpus(
        name="Noir stdlib v4.2.0 (apiref)",
        slug="noir_stdlib_apiref",
        source_root="noir",
        trees=(SourceTree("noir_stdlib/src", "noir-stdlib"),),
        include_extensions=(".nr",),
        transform="noir_apiref",
        notes="public-surface only: doc comments + signatures",
    ),
    # ---- Body-bearing code corpora (rename_code_to_txt) ----------------
    Corpus(
        name="Aztec Example Contracts v4.2.0",
        slug="aztec_example_contracts",
        source_root="aztec-packages",
        trees=(SourceTree("noir-projects/noir-contracts/contracts", "noir-contracts"),),
        include_extensions=(".nr",),
        transform="rename_code_to_txt",
        notes="kept body-bearing: examples are the implementation",
    ),
    Corpus(
        name="Aztec Protocol Circuits v4.2.0",
        slug="aztec_protocol_circuits",
        source_root="aztec-packages",
        trees=(SourceTree("noir-projects/noir-protocol-circuits", "noir-protocol-circuits"),),
        include_extensions=(".nr",),
        transform="rename_code_to_txt",
    ),
    Corpus(
        name="aztec.js SDK v4.2.0",
        slug="aztec_js_sdk",
        source_root="aztec-packages",
        trees=(SourceTree("yarn-project/aztec.js/src", "aztec.js"),),
        include_extensions=(".ts",),
        transform="rename_code_to_txt",
    ),
    Corpus(
        name="Aztec CLI v4.2.0",
        slug="aztec_cli",
        source_root="aztec-packages",
        # CLI ships as two yarn packages with separate zip prefixes
        # so the source-URL mapping in api/answer/routes/base.py
        # routes each to its correct yarn-project subdirectory.
        trees=(
            SourceTree("yarn-project/cli/src", "cli"),
            SourceTree("yarn-project/cli-wallet/src", "cli-wallet"),
        ),
        include_extensions=(".ts",),
        transform="rename_code_to_txt",
        notes="bundles both cli/ and cli-wallet/ packages with distinct prefixes",
    ),
    Corpus(
        name="Aztec E2E Tests v4.2.0",
        slug="aztec_e2e_tests",
        source_root="aztec-packages",
        trees=(SourceTree("yarn-project/end-to-end/src", "end-to-end"),),
        include_extensions=(".ts",),
        transform="rename_code_to_txt",
    ),
    Corpus(
        name="Aztec L1 Contracts v4.2.0",
        slug="aztec_l1_contracts",
        source_root="aztec-packages",
        trees=(SourceTree("l1-contracts", "l1-contracts"),),
        include_extensions=(".sol",),
        transform="rename_code_to_txt",
    ),
)


def get_corpus(slug: str) -> Corpus:
    for c in CORPORA:
        if c.slug == slug:
            return c
    raise KeyError(f"Unknown corpus slug: {slug}. Known: {[c.slug for c in CORPORA]}")


def get_corpora_for_root(source_root: str) -> Tuple[Corpus, ...]:
    return tuple(c for c in CORPORA if c.source_root == source_root)

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
    # Optional fnmatch-style allowlist. When non-empty, ONLY files
    # whose relative path matches one of these patterns are kept (in
    # addition to the ``include_extensions`` check). Empty (default)
    # means "include every file that passes the other filters". Use
    # this to pull a single file or a short list out of a tree that
    # otherwise contains far more than we want to index — e.g. just
    # ``networks.md`` out of ``docs/docs/`` rather than the whole
    # unversioned-docs folder.
    include_paths: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Corpus:
    # Human-readable name (this becomes the ``sources.name`` row in the
    # database — visible to admins via SQL).
    name: str
    # Short slug used for the zip filename and as a CLI argument.
    slug: str
    # Where the source files live, relative to one of the supported
    # source roots. Three roots exist because the corpora are pinned at
    # *different* upstream commits per release:
    #   - ``aztec-packages``       → the aztec-packages release tag
    #     (e.g. ``v4.3.0``). Used for code corpora (aztec.js, CLI, e2e,
    #     L1, examples, circuits, aztec-nr apiref) and the
    #     auto-generated TypeScript API reference.
    #   - ``aztec-packages-docs``  → a snapshot commit on aztec-packages'
    #     ``next`` branch where the new ``version-vX.Y.Z/`` Docusaurus
    #     folder lives. The docs version snapshot is taken from a
    #     moving branch, so the literal release tag does NOT contain
    #     the matching ``version-vX.Y.Z/`` folder — see comment block
    #     in ``application/api/answer/routes/base.py`` for the long
    #     story. Used for the three rendered-docs corpora (developer
    #     docs / network docs / site-root networks page).
    #   - ``noir``                 → the noir-lang/noir commit pinned
    #     by aztec-packages' ``noir/noir-repo`` submodule at the
    #     release tag. Used for noir docs + noir stdlib apiref.
    source_root: str  # "aztec-packages" | "aztec-packages-docs" | "noir"
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


# ── The 14 corpora ─────────────────────────────────────────────────────────


CORPORA: Tuple[Corpus, ...] = (
    # ---- Markdown / docs corpora (passthrough) -------------------------
    Corpus(
        name="Aztec Developer Docs v4.3.0",
        slug="aztec_developer_docs",
        # NB: source_root="aztec-packages-docs", not "aztec-packages".
        # version-v4.3.0/ only exists on the ``next`` branch — see the
        # docstring on ``source_root`` for the full story.
        source_root="aztec-packages-docs",
        trees=(
            SourceTree(
                "docs/developer_versioned_docs/version-v4.3.0",
                # ``zip_prefix`` is the top-level dir each file lands
                # under in the zip. The source tree already has
                # ``docs/`` as a subfolder alongside top-level files
                # (``overview.md``, ``ai_tooling.md``, …); prefix with
                # ``version-v4.3.0`` alone so files preserve their
                # relative path and metadata.source ends up like
                # ``version-v4.3.0/docs/aztec-js/foo.md`` (single
                # ``docs/``, what the URL rewriter expects), not
                # ``version-v4.3.0/docs/docs/aztec-js/foo.md``.
                "version-v4.3.0",
                exclude_paths=(
                    # Migration notes accumulate every renamed identifier
                    # in both old and new spellings, so they dominate
                    # identifier-shaped queries despite never being the
                    # canonical answer. Biggest single source of
                    # off-target citations in the widget at v4.2.0
                    # (~285 chunks). If we ever need migration content
                    # again, carve it into a separate corpus that's
                    # only included for migration-shaped queries.
                    "docs/resources/migration_notes.*",
                ),
            ),
        ),
        include_extensions=(".md", ".mdx", ".json"),
        transform="passthrough",
    ),
    Corpus(
        name="Aztec Network Docs v4.3.0",
        slug="aztec_network_docs",
        source_root="aztec-packages-docs",
        trees=(
            SourceTree(
                "docs/network_versioned_docs/version-v4.3.0",
                # Same single-prefix structure as the developer tree:
                # the source has ``operators/`` and ``reference/`` as
                # siblings, so a prefix of ``version-v4.3.0/operators``
                # would both duplicate (``…/operators/operators/…``)
                # and misroute ``reference/*`` under ``operators/``.
                "version-v4.3.0",
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
        name="Aztec TypeScript API v4.3.0",
        slug="aztec_typescript_api",
        # TS API stays on the release tag: the v4.3.0 tag still ships
        # the auto-generated reference under ``testnet/``; the
        # ``mainnet/`` rename only landed on ``next`` after the tag was
        # cut. Re-source-rooting this to ``aztec-packages-docs`` (and
        # switching to ``mainnet/``) would mean the GitHub blob links
        # in source citations 404, since the tag has no ``mainnet/``.
        source_root="aztec-packages",
        trees=(SourceTree("docs/static/typescript-api/testnet", "typescript-api"),),
        include_extensions=(".md", ".txt"),
        transform="passthrough",
        notes="auto-generated from yarn-project/* tsdoc; the corpus is "
              "the rendered output, not the .ts source",
    ),
    Corpus(
        name="Noir Language Docs v4.3.0",
        slug="noir_language_docs",
        source_root="noir",
        trees=(SourceTree("docs/docs", "noir-docs"),),
        include_extensions=(".md", ".mdx"),
        transform="passthrough",
        notes="from noir-lang/noir at the commit pinned by aztec-packages "
              "v4.3.0 (see CLAUDE.md for the canonical commit hash)",
    ),
    Corpus(
        # Site-root unversioned pages in aztec-packages that the
        # versioned developer/network docs explicitly defer to. The
        # only entry today is ``networks.md`` — the canonical L1
        # contract address table comparing mainnet vs. Sepolia
        # (Governance Staking Escrow, Rollup, Registry, etc.). The
        # versioned network docs hardcode mainnet addresses with a
        # "for mainnet" qualifier and point operators here for the
        # testnet column; without this corpus the bot has no way to
        # produce the testnet GSE address and tends to serve the
        # mainnet one for testnet questions.
        name="Aztec Site Networks Page v4.3.0",
        slug="aztec_site_networks",
        # Site-root pages live alongside the versioned docs in
        # ``docs/docs/`` — and ``networks.md`` was updated in the same
        # PR (#23375) that cut version-v4.3.0/. Sourcing from
        # aztec-packages-docs ensures we pick up the updated address
        # table; the v4.3.0 tag still has the older copy.
        source_root="aztec-packages-docs",
        trees=(
            SourceTree(
                "docs/docs",
                "aztec-site",
                include_paths=("networks.md",),
            ),
        ),
        include_extensions=(".md", ".mdx"),
        transform="passthrough",
        notes="single-file corpus: docs/docs/networks.md, rendered at "
              "docs.aztec.network/networks. Add more site-root pages "
              "here by extending include_paths.",
    ),
    Corpus(
        # Unversioned "Participate" docs (``docs-participate/``) — the
        # educational governance/staking content. NOT in the versioned
        # developer/network trees, so it was never ingested; the widget
        # had no crisp source for "can a delegator vote?" or the
        # unstaking/withdrawal flow and the model filled the gap with
        # hallucinated method names (``completeUnstake``) and a wrong
        # "1-week cooldown". See honk-report 2026-05-27.
        #
        # CURATED SUBSET: only ``token/`` + ``governance/`` — the slice
        # that closes that gap. ``basics/`` is deliberately excluded: it
        # overlaps the versioned developer concept docs and would risk
        # the off-target-citation duplication that ``migration_notes`` /
        # changelogs already cause. Widen via ``include_paths`` after
        # measuring citation overlap.
        #
        # Unversioned, same as ``networks.md``: sourced from the
        # ``next`` snapshot (``aztec-packages-docs``); rendered at
        # ``docs.aztec.network/participate/<rest>`` (Docusaurus instance
        # ``routeBasePath: "participate"``). The ``aztec-participate/``
        # zip prefix is wired into ``_aztec_source_url`` (routes/base.py)
        # and ``SOURCE_PREFIXES`` (eval_retrieval.py).
        name="Aztec Participate Docs v4.3.0",
        slug="aztec_participate_docs",
        source_root="aztec-packages-docs",
        trees=(
            SourceTree(
                "docs/docs-participate",
                "aztec-participate",
                include_paths=("token/*", "governance/*"),
            ),
        ),
        include_extensions=(".md", ".mdx"),
        transform="passthrough",
        notes="curated subset (token/ + governance/) of the unversioned "
              "docs-participate tree, rendered at "
              "docs.aztec.network/participate/<rest>. Widen include_paths "
              "after measuring citation overlap with the versioned docs.",
    ),
    # ---- Apiref corpora (noir_apiref transform) ------------------------
    Corpus(
        name="Aztec.nr Framework v4.3.0 (apiref)",
        slug="aztec_nr_apiref",
        source_root="aztec-packages",
        trees=(SourceTree("noir-projects/aztec-nr", "aztec-nr"),),
        include_extensions=(".nr",),
        transform="noir_apiref",
        notes="public-surface only: doc comments + signatures",
    ),
    Corpus(
        name="Noir stdlib v4.3.0 (apiref)",
        slug="noir_stdlib_apiref",
        source_root="noir",
        trees=(SourceTree("noir_stdlib/src", "noir-stdlib"),),
        include_extensions=(".nr",),
        transform="noir_apiref",
        notes="public-surface only: doc comments + signatures",
    ),
    # ---- Body-bearing code corpora (rename_code_to_txt) ----------------
    Corpus(
        name="Aztec Example Contracts v4.3.0",
        slug="aztec_example_contracts",
        source_root="aztec-packages",
        trees=(SourceTree("noir-projects/noir-contracts/contracts", "noir-contracts"),),
        include_extensions=(".nr",),
        transform="rename_code_to_txt",
        notes="kept body-bearing: examples are the implementation",
    ),
    Corpus(
        name="Aztec Protocol Circuits v4.3.0",
        slug="aztec_protocol_circuits",
        source_root="aztec-packages",
        trees=(SourceTree("noir-projects/noir-protocol-circuits", "noir-protocol-circuits"),),
        include_extensions=(".nr",),
        transform="rename_code_to_txt",
    ),
    Corpus(
        name="aztec.js SDK v4.3.0",
        slug="aztec_js_sdk",
        source_root="aztec-packages",
        trees=(SourceTree("yarn-project/aztec.js/src", "aztec.js"),),
        include_extensions=(".ts",),
        transform="rename_code_to_txt",
    ),
    Corpus(
        name="Aztec CLI v4.3.0",
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
        name="Aztec E2E Tests v4.3.0",
        slug="aztec_e2e_tests",
        source_root="aztec-packages",
        trees=(SourceTree("yarn-project/end-to-end/src", "end-to-end"),),
        include_extensions=(".ts",),
        transform="rename_code_to_txt",
    ),
    Corpus(
        name="Aztec L1 Contracts v4.3.0",
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

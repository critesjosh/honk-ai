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
    ``inline_external_links``
        Markdown files are copied like ``passthrough``, but each inline
        ``[label](url)`` is rewritten before zipping: external URLs
        (``http(s)://`` / ``//`` / ``mailto:``) become ``label (url)``
        so the bare URL survives the shared markdown parser (which
        otherwise strips every link to its label), while internal /
        relative links are stripped to ``label``. Used for link-list
        corpora (``awesome_aztec``) whose value is the external URLs.
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
    # source roots. Four roots exist because the corpora are pinned at
    # *different* upstream commits per release:
    #   - ``aztec-packages``       → the aztec-packages release tag
    #     (e.g. ``v5.0.0-rc.1``). Used for the code corpora (aztec.js,
    #     CLI, e2e, L1, examples, circuits, aztec-nr apiref) — real
    #     source code at the tag.
    #   - ``aztec-packages-docs``  → a snapshot commit on aztec-packages'
    #     ``next`` branch where the new ``version-vX.Y.Z/`` Docusaurus
    #     folder lives. The docs version snapshot is taken from a
    #     moving branch, so the literal release tag does NOT contain
    #     the matching ``version-vX.Y.Z/`` folder — see comment block
    #     in ``application/api/answer/routes/base.py`` for the long
    #     story. Used for the rendered-docs corpora (developer docs /
    #     network docs / site-root networks page / participate) AND the
    #     auto-generated TypeScript API reference — its current copy
    #     lives on ``next`` under ``testnet/``, not the tag (see that
    #     corpus's comment).
    #   - ``noir``                 → the noir-lang/noir commit pinned
    #     by aztec-packages' ``noir/noir-repo`` submodule at the
    #     release tag. Used for noir docs + noir stdlib apiref.
    #   - ``awesome-aztec``        → the AztecProtocol/awesome-aztec
    #     community repo. NOT release-pinned (moving target); whatever
    #     checkout is passed at build time. Used only for the
    #     ``awesome_aztec`` resource-list corpus.
    source_root: str  # "aztec-packages" | "aztec-packages-docs" | "noir" | "awesome-aztec"
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
    # The Aztec release this corpus's content is pinned to ("v4.3.1" |
    # "v5.0.0-rc.1"), or "" for genuinely unversioned/shared corpora. Stamped
    # into ``sources.metadata`` by ``swap_sources.py`` at swap time (NOT at
    # upload); consumed by the retrieval version-scoping resolver.
    version: str = ""
    # Which live network this corpus serves: "mainnet" | "testnet" | "shared".
    # "shared" corpora are always retrieved regardless of the active version;
    # versioned corpora are only retrieved when their version is active.
    network: str = "shared"

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


# ── Corpora ──────────────────────────────────────────────────────────────
#
# The KB serves TWO live networks at once: mainnet (v4.3.1) and testnet
# (v5.0.0-rc.1). 12 of the 15 corpora are version-specific (their content
# differs per release) and are generated once per active version by
# ``_versioned_corpora`` below; the other 3 are genuinely unversioned/shared
# (networks page, participate docs, awesome-aztec) and ingested once.
#
# Per-version corpora carry ``version`` + ``network``. These are NOT set by
# ``/api/upload`` — ``swap_sources.py`` stamps them into ``sources.metadata``
# (the UPDATE SQL it emits, keyed by slug→source_id from the upload manifest)
# at swap time; the retrieval version-scoping resolver
# (``application/retriever/version_scope.py``) then reads them. Until that stamp
# runs, every source looks unversioned and narrowing is a no-op. Each
# per-version ``slug`` is suffixed with the version so v4.3.1 and v5 sources
# never collide. Shared corpora keep their bare slug and ``network="shared"`` so
# they are retrieved regardless of the active version.
#
# Build/ingest note: each version's bundle is built from THAT version's source
# roots — ``--aztec-pkg`` at the matching release tag, ``--noir`` at that tag's
# pinned commit, ``--aztec-pkg-docs`` at the shared ``next`` snapshot (which
# carries every ``version-vX.Y.Z/`` folder + ``typescript-api/{mainnet,testnet}``).
# Use ``build.py --version <v>`` to build just one version's corpora.


def _vslug(version: str) -> str:
    """``v5.0.0-rc.1`` -> ``v5_0_0_rc_1`` for use as a slug suffix."""
    return version.replace(".", "_").replace("-", "_")


def _versioned_corpora(version: str, network: str, ts_api_folder: str) -> Tuple[Corpus, ...]:
    """The 12 version-specific corpora for one Aztec release.

    ``ts_api_folder`` is the rendered TypeScript-API network folder on the
    ``next`` snapshot: ``mainnet`` (=v4.3.1) or ``testnet`` (=v5.0.0-rc.1) —
    the release tag does NOT keep that artifact current, so it always comes
    from ``next`` (see the TS-API corpus comment).
    """
    s = _vslug(version)
    docs_folder = f"version-{version}"
    return (
        # ---- Markdown / docs corpora (passthrough) ---------------------
        Corpus(
            name=f"Aztec Developer Docs {version}",
            slug=f"aztec_developer_docs_{s}",
            # source_root="aztec-packages-docs": version-<version>/ lives on
            # the ``next`` branch, NOT the release tag — see the source_root
            # docstring. zip_prefix is just ``version-<version>`` so files keep
            # their relative path (single ``docs/``) and metadata.source ends up
            # like ``version-<version>/docs/aztec-js/foo.md`` — what the URL
            # rewriter expects.
            source_root="aztec-packages-docs",
            trees=(
                SourceTree(
                    f"docs/developer_versioned_docs/{docs_folder}",
                    docs_folder,
                    # Migration notes accumulate every renamed identifier in
                    # both spellings, so they dominate identifier-shaped queries
                    # despite never being the canonical answer (~285 off-target
                    # chunks at v4.2.0). Carve into a separate migration-only
                    # corpus if ever needed.
                    exclude_paths=("docs/resources/migration_notes.*",),
                ),
            ),
            include_extensions=(".md", ".mdx", ".json"),
            transform="passthrough",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Aztec Network Docs {version}",
            slug=f"aztec_network_docs_{s}",
            source_root="aztec-packages-docs",
            trees=(
                SourceTree(
                    f"docs/network_versioned_docs/{docs_folder}",
                    # Single-prefix (``version-<version>``) for the same reason
                    # as the developer tree: ``operators/`` and ``reference/``
                    # are siblings, so a deeper prefix would duplicate/misroute.
                    docs_folder,
                    # Release-note changelogs mention every renamed config /
                    # flag / RPC method but aren't the canonical reference.
                    exclude_paths=(
                        "operators/reference/changelog/*",
                        "reference/changelog/*",
                    ),
                ),
            ),
            include_extensions=(".md",),
            transform="passthrough",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Aztec TypeScript API {version}",
            slug=f"aztec_typescript_api_{s}",
            # TS API sources from the ``next`` snapshot, NOT the release tag.
            # The rendered reference is an auto-generated artifact under
            # ``docs/static/typescript-api/`` that the tag does NOT keep current
            # (at the v5.0.0-rc.1 tag the only folder, ``mainnet/``, still
            # declares Version: v4.3.0). On ``next`` it is split by LIVE NETWORK:
            # ``mainnet/`` = v4.3.1, ``testnet/`` = v5.0.0-rc.1. Because the
            # source is a moving branch, the GitHub blob links in
            # ``_aztec_source_url`` for the ``typescript-api/`` prefix resolve
            # against the pinned ``next`` snapshot (per active version).
            source_root="aztec-packages-docs",
            trees=(SourceTree(f"docs/static/typescript-api/{ts_api_folder}", "typescript-api"),),
            include_extensions=(".md", ".txt"),
            transform="passthrough",
            notes="auto-generated from yarn-project/* tsdoc; the corpus is "
                  "the rendered output, not the .ts source",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Noir Language Docs {version}",
            slug=f"noir_language_docs_{s}",
            source_root="noir",
            trees=(SourceTree("docs/docs", "noir-docs"),),
            include_extensions=(".md", ".mdx"),
            transform="passthrough",
            notes=f"from noir-lang/noir at the commit pinned by aztec-packages "
                  f"{version} (see CLAUDE.md for the canonical commit hash)",
            version=version,
            network=network,
        ),
        # ---- Apiref corpora (noir_apiref transform) --------------------
        Corpus(
            name=f"Aztec.nr Framework {version} (apiref)",
            slug=f"aztec_nr_apiref_{s}",
            source_root="aztec-packages",
            trees=(SourceTree("noir-projects/aztec-nr", "aztec-nr"),),
            include_extensions=(".nr",),
            transform="noir_apiref",
            notes="public-surface only: doc comments + signatures",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Noir stdlib {version} (apiref)",
            slug=f"noir_stdlib_apiref_{s}",
            source_root="noir",
            trees=(SourceTree("noir_stdlib/src", "noir-stdlib"),),
            include_extensions=(".nr",),
            transform="noir_apiref",
            notes="public-surface only: doc comments + signatures",
            version=version,
            network=network,
        ),
        # ---- Body-bearing code corpora (rename_code_to_txt) ------------
        Corpus(
            name=f"Aztec Example Contracts {version}",
            slug=f"aztec_example_contracts_{s}",
            source_root="aztec-packages",
            trees=(SourceTree("noir-projects/noir-contracts/contracts", "noir-contracts"),),
            include_extensions=(".nr",),
            transform="rename_code_to_txt",
            notes="kept body-bearing: examples are the implementation",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Aztec Protocol Circuits {version}",
            slug=f"aztec_protocol_circuits_{s}",
            source_root="aztec-packages",
            trees=(SourceTree("noir-projects/noir-protocol-circuits", "noir-protocol-circuits"),),
            include_extensions=(".nr",),
            transform="rename_code_to_txt",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"aztec.js SDK {version}",
            slug=f"aztec_js_sdk_{s}",
            source_root="aztec-packages",
            trees=(SourceTree("yarn-project/aztec.js/src", "aztec.js"),),
            include_extensions=(".ts",),
            transform="rename_code_to_txt",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Aztec CLI {version}",
            slug=f"aztec_cli_{s}",
            source_root="aztec-packages",
            # CLI ships as two yarn packages with separate zip prefixes so the
            # source-URL mapping routes each to its yarn-project subdirectory.
            trees=(
                SourceTree("yarn-project/cli/src", "cli"),
                SourceTree("yarn-project/cli-wallet/src", "cli-wallet"),
            ),
            include_extensions=(".ts",),
            transform="rename_code_to_txt",
            notes="bundles both cli/ and cli-wallet/ packages with distinct prefixes",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Aztec E2E Tests {version}",
            slug=f"aztec_e2e_tests_{s}",
            source_root="aztec-packages",
            trees=(SourceTree("yarn-project/end-to-end/src", "end-to-end"),),
            include_extensions=(".ts",),
            transform="rename_code_to_txt",
            version=version,
            network=network,
        ),
        Corpus(
            name=f"Aztec L1 Contracts {version}",
            slug=f"aztec_l1_contracts_{s}",
            source_root="aztec-packages",
            trees=(SourceTree("l1-contracts", "l1-contracts"),),
            include_extensions=(".sol",),
            transform="rename_code_to_txt",
            version=version,
            network=network,
        ),
    )


# ---- Shared (unversioned) corpora — ingested once, retrieved for any version
_SHARED_CORPORA: Tuple[Corpus, ...] = (
    Corpus(
        # Site-root unversioned ``networks.md`` — the canonical L1 contract
        # address table comparing mainnet vs. testnet (GSE, Rollup, Registry).
        # The versioned network docs hardcode mainnet addresses and defer here
        # for the testnet column. Network-specific CONTENT but unversioned on
        # ``next`` (one file, covers both networks) → shared.
        name="Aztec Site Networks Page",
        slug="aztec_site_networks",
        source_root="aztec-packages-docs",
        trees=(SourceTree("docs/docs", "aztec-site", include_paths=("networks.md",)),),
        include_extensions=(".md", ".mdx"),
        transform="passthrough",
        network="shared",
        notes="single-file corpus: docs/docs/networks.md, rendered at "
              "docs.aztec.network/networks. Unversioned (covers both networks).",
    ),
    Corpus(
        # Unversioned "Participate" docs — educational governance/staking
        # content (curated to token/ + governance/). Rendered at
        # docs.aztec.network/participate/<rest>; unversioned → shared.
        name="Aztec Participate Docs",
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
        network="shared",
        notes="curated subset (token/ + governance/) of the unversioned "
              "docs-participate tree, rendered at docs.aztec.network/participate/.",
    ),
    Corpus(
        # Community resource list — the single awesome-aztec README (faucets,
        # explorers, tooling). NOT release-pinned (moving community repo); source
        # URLs map to the GitHub blob on ``main``. Re-pins to current main each
        # ingest (``--awesome-aztec <checkout>``). Live since 2026-06-02.
        name="Awesome Aztec (community resources)",
        slug="awesome_aztec",
        source_root="awesome-aztec",
        trees=(SourceTree(".", "awesome-aztec", include_paths=("README.md",)),),
        include_extensions=(".md",),
        # NOT passthrough: ``inline_external_links`` pre-inlines external URLs
        # (and strips internal links) so the URLs — this corpus's whole value —
        # survive the shared markdown parser's link stripping.
        transform="inline_external_links",
        in_production_agent=True,
        network="shared",
        notes="single-file corpus: the awesome-aztec README link list. Moving "
              "community repo; external links inlined so URLs survive ingest.",
    ),
)


# Active versions in the KB: mainnet (v4.3.1) + testnet (v5.0.0-rc.1).
# The rendered TS-API folder on ``next`` is per network (mainnet/testnet).
CORPORA: Tuple[Corpus, ...] = (
    *_versioned_corpora("v4.3.1", "mainnet", ts_api_folder="mainnet"),
    *_versioned_corpora("v5.0.0-rc.1", "testnet", ts_api_folder="testnet"),
    *_SHARED_CORPORA,
)


def get_corpus(slug: str) -> Corpus:
    for c in CORPORA:
        if c.slug == slug:
            return c
    raise KeyError(f"Unknown corpus slug: {slug}. Known: {[c.slug for c in CORPORA]}")


def get_corpora_for_root(source_root: str) -> Tuple[Corpus, ...]:
    return tuple(c for c in CORPORA if c.source_root == source_root)

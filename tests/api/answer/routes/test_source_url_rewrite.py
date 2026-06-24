"""Tests for ``application.api.answer.routes.base._aztec_source_url``.

The rewriter maps corpus-internal `metadata.source` paths into clickable
public URLs that we emit in `{type: "source"}` SSE frames. Several
classes of source paths each need different routing; this file pins the
behaviour so we don't silently regress on any of them.

Key bugs the current cases guard against:

* Top-level developer docs (``version-v4.3.0/<file>.md``) used to fall
  through to a GitHub URL under ``docs/developer_versioned_docs/...``
  that does not exist at the v4.3.0 git tag (the docs version snapshot
  is taken from a moving branch, so the tag has the *previous* version
  folder instead). Now they route to docs.aztec.network/developers/<file>.

* Network / operator docs (``version-v4.3.0/operators/<rest>``) used
  to route to a non-existent GitHub path; now they route to
  docs.aztec.network/operate/operators/<rest>.

* Files like ``aztec-js/index.md`` produced ``/developers/docs/aztec-js
  /index`` which 404s — Docusaurus serves index pages at the bare
  folder URL. The rewriter now strips trailing ``/index``.
"""

from __future__ import annotations

import pytest

from application.api.answer.routes.base import _aztec_source_url


class TestRenderedDeveloperDocs:
    """Files under `version-v4.3.0/docs/...` → docs.aztec.network/developers/docs/<rest>."""

    def test_strips_md_extension(self):
        assert _aztec_source_url("version-v4.3.0/docs/aztec-nr/api.mdx") == (
            "https://docs.aztec.network/developers/docs/aztec-nr/api"
        )

    def test_strips_md_for_plain_md(self):
        assert _aztec_source_url("version-v4.3.0/docs/aztec-js/how_to_create_account.md") == (
            "https://docs.aztec.network/developers/docs/aztec-js/how_to_create_account"
        )

    def test_strips_trailing_index_segment(self):
        # Docusaurus serves `aztec-js/index.md` as `/developers/docs/aztec-js`.
        assert _aztec_source_url("version-v4.3.0/docs/aztec-js/index.md") == (
            "https://docs.aztec.network/developers/docs/aztec-js"
        )

    def test_nested_subfolders(self):
        assert _aztec_source_url(
            "version-v4.3.0/docs/foundational-topics/advanced/circuits/public_execution.md"
        ) == (
            "https://docs.aztec.network/developers/docs/foundational-topics/advanced/circuits/public_execution"
        )


class TestTopLevelDeveloperDocs:
    """Files at `version-v4.3.0/<file>.md` (NOT under `docs/`) — rendered
    at /developers/<file> on the site, not under /developers/docs/."""

    def test_top_level_overview(self):
        assert _aztec_source_url("version-v4.3.0/overview.md") == (
            "https://docs.aztec.network/developers/overview"
        )

    def test_top_level_with_underscores(self):
        assert _aztec_source_url("version-v4.3.0/getting_started_on_local_network.md") == (
            "https://docs.aztec.network/developers/getting_started_on_local_network"
        )

    def test_top_level_mdx(self):
        assert _aztec_source_url("version-v4.3.0/ai_tooling.mdx") == (
            "https://docs.aztec.network/developers/ai_tooling"
        )


class TestNetworkOperatorDocs:
    """Files at `version-v4.3.0/operators/...` — rendered under /operate/."""

    def test_operator_setup_doc(self):
        assert _aztec_source_url("version-v4.3.0/operators/setup/quickstart.md") == (
            "https://docs.aztec.network/operate/operators/setup/quickstart"
        )

    def test_operator_index_strips_to_folder(self):
        assert _aztec_source_url("version-v4.3.0/operators/index.md") == (
            "https://docs.aztec.network/operate/operators"
        )

    def test_operator_top_level(self):
        assert _aztec_source_url("version-v4.3.0/operators/sequencer_management.md") == (
            "https://docs.aztec.network/operate/operators/sequencer_management"
        )


class TestDocusaurusIdOverrides:
    """Files whose Docusaurus ``id:`` frontmatter differs from the
    filename get a different URL slug — keeping the filename slug 404s.

    The slug map (``application.api.answer.routes.aztec_doc_slugs``) is a UNION
    over both KB versions (28 entries = 14 per version). The override applies
    regardless of version; the version segment (bare for v4.3.1 mainnet,
    ``/testnet/`` for v5) is layered on by the path-derived routing. These
    spot-check a handful per version; the full set is covered by the data file.
    """

    # (corpus tail, expected slug) — same id: override exists for both versions.
    _OVERRIDE_CASES = [
        ("operators/setup/registering-sequencer.md", "operators/setup/registering_sequencer"),
        ("operators/setup/staking-provider.md", "operators/setup/become_a_staking_provider"),
        ("operators/setup/sequencer-setup.md", "operators/setup/sequencer_management"),
        (
            "operators/sequencer-management/governance-participation.md",
            "operators/sequencer-management/creating_and_voting_on_proposals",
        ),
    ]

    @pytest.mark.parametrize("tail,expected", _OVERRIDE_CASES)
    def test_override_applied_v4_3_1_mainnet_bare(self, tail, expected):
        # v4.3.1 (mainnet) → BARE /operate path + the id: slug override.
        assert _aztec_source_url(f"version-v4.3.1/{tail}") == (
            f"https://docs.aztec.network/operate/{expected}"
        )

    @pytest.mark.parametrize("tail,expected", _OVERRIDE_CASES)
    def test_override_applied_v5_testnet_infix(self, tail, expected):
        # v5.0.0-rc.1 (testnet) → /operate/testnet path + the SAME slug override.
        assert _aztec_source_url(f"version-v5.0.0-rc.1/{tail}") == (
            f"https://docs.aztec.network/operate/testnet/{expected}"
        )

    @pytest.mark.parametrize("tail,expected", _OVERRIDE_CASES)
    def test_override_retained_for_legacy_v4_3_0(self, tail, expected):
        # Pre-cutover safety net: the still-served v4.3.0 corpus keeps its slug
        # overrides (retained in the generator), routed BARE like mainnet via
        # the unknown-version→bare fallback. Without this, deploying the
        # two-version image against the current v4.3.0 corpus would 404 these.
        assert _aztec_source_url(f"version-v4.3.0/{tail}") == (
            f"https://docs.aztec.network/operate/{expected}"
        )

    def test_unmapped_operator_doc_falls_back_to_filename(self):
        # claiming-rewards.md has no `id:` frontmatter — must not be
        # remapped. Confirmed 200 on the live site at the filename slug.
        assert _aztec_source_url(
            "version-v4.3.1/operators/sequencer-management/claiming-rewards.md"
        ) == (
            "https://docs.aztec.network/operate/operators/sequencer-management/claiming-rewards"
        )

    def test_index_md_with_id_still_uses_parent_path(self):
        # operators/keystore/index.md declares id: advanced_keystore_guide,
        # but Docusaurus serves index files at the parent path regardless
        # of the declared id. We must NOT inject the id into the URL here.
        assert _aztec_source_url(
            "version-v5.0.0-rc.1/operators/keystore/index.md"
        ) == (
            "https://docs.aztec.network/operate/testnet/operators/keystore"
        )

    def test_apply_slug_override_defensive_index_guard(self):
        # If the override map ever accidentally contains a key ending in
        # ``/index``, ``apply_slug_override`` must NOT mangle the
        # already-stripped parent-folder URL. ``base.py`` strips
        # ``/index`` before calling the helper, so the helper sees a
        # path like ``operators/keystore`` paired with the unstripped
        # source ``...operators/keystore/index`` — returning ``rest``
        # unchanged is the safe behaviour.
        from application.api.answer.routes.aztec_doc_slugs import apply_slug_override, AZTEC_DOC_SLUG_OVERRIDES

        # Inject a hypothetical bad entry to exercise the guard.
        AZTEC_DOC_SLUG_OVERRIDES["version-v4.3.0/foo/bar/index"] = "should_be_ignored"
        try:
            assert apply_slug_override("foo/bar", "version-v4.3.0/foo/bar/index") == "foo/bar"
        finally:
            del AZTEC_DOC_SLUG_OVERRIDES["version-v4.3.0/foo/bar/index"]


class TestSiteRootPages:
    """Files at `aztec-site/...` come from the unversioned
    ``docs/docs/`` folder in aztec-packages and render at the
    site root on docs.aztec.network (e.g. /networks)."""

    def test_networks_page(self):
        # docs/docs/networks.md → docs.aztec.network/networks. This is
        # the canonical L1 contract address table (mainnet vs. Sepolia
        # GSE etc.) the operator widget needs to cite directly.
        assert _aztec_source_url("aztec-site/networks.md") == (
            "https://docs.aztec.network/networks"
        )

    def test_strips_mdx_extension(self):
        # `index.mdx` is excluded by the corpus today but the rewriter
        # should still handle it correctly if we ever add it.
        assert _aztec_source_url("aztec-site/index.mdx") == (
            "https://docs.aztec.network"
        )

    def test_does_not_route_to_github(self):
        url = _aztec_source_url("aztec-site/networks.md")
        assert "github.com" not in url
        assert url == "https://docs.aztec.network/networks"


class TestParticipateDocs:
    """Files at `aztec-participate/...` come from the unversioned
    ``docs-participate/`` tree and render at /participate/<rest> on
    docs.aztec.network (Docusaurus ``routeBasePath: "participate"``)."""

    def test_token_staking_page(self):
        # The page that answers the unstaking/withdrawal questions the
        # widget previously hallucinated (`completeUnstake`, "1-week cooldown").
        assert _aztec_source_url("aztec-participate/token/staking.md") == (
            "https://docs.aztec.network/participate/token/staking"
        )

    def test_governance_voting_page(self):
        # The crisp delegator-voting source.
        assert _aztec_source_url("aztec-participate/governance/voting.md") == (
            "https://docs.aztec.network/participate/governance/voting"
        )

    def test_strips_trailing_index_segment(self):
        assert _aztec_source_url("aztec-participate/governance/index.md") == (
            "https://docs.aztec.network/participate/governance"
        )

    def test_handles_mdx(self):
        assert _aztec_source_url("aztec-participate/token/voting.mdx") == (
            "https://docs.aztec.network/participate/token/voting"
        )

    def test_does_not_route_to_github(self):
        url = _aztec_source_url("aztec-participate/governance/gse.md")
        assert "github.com" not in url
        assert url == "https://docs.aztec.network/participate/governance/gse"


class TestAwesomeAztec:
    """Files at `awesome-aztec/...` come from the AztecProtocol/awesome-aztec
    community repo (a moving, non-release-pinned resource list) and link
    back to the GitHub blob on ``main`` — NOT docs.aztec.network."""

    def test_readme_routes_to_github_main(self):
        assert _aztec_source_url("awesome-aztec/README.md") == (
            "https://github.com/AztecProtocol/awesome-aztec/blob/main/README.md"
        )

    def test_does_not_route_to_docs_site(self):
        url = _aztec_source_url("awesome-aztec/README.md")
        assert "docs.aztec.network" not in url


class TestCuratedTroubleshooting:
    """Files at `curated-troubleshooting/...` are hand-authored in-repo docs
    (scripts/ingest/curated/) with no upstream blob. They route to the
    version-aware operator-FAQ page — the canonical operator-trouble landing
    page — so the citation has a working, on-topic href instead of a dead
    raw-path string."""

    def test_routes_to_operator_faq_default_version(self):
        # Default active_version is v5.0.0-rc.1 (testnet), so the default
        # carries the /testnet infix.
        assert _aztec_source_url(
            "curated-troubleshooting/genesis-archive-root-mismatch.md"
        ) == "https://docs.aztec.network/operate/testnet/operators/operator-faq"

    def test_mainnet_active_version_drops_infix(self):
        assert _aztec_source_url(
            "curated-troubleshooting/genesis-archive-root-mismatch.md", "v4.3.1"
        ) == "https://docs.aztec.network/operate/operators/operator-faq"

    def test_testnet_active_version_keeps_infix(self):
        assert _aztec_source_url(
            "curated-troubleshooting/genesis-archive-root-mismatch.md", "v5.0.0-rc.1"
        ) == "https://docs.aztec.network/operate/testnet/operators/operator-faq"

    def test_unknown_version_falls_back_to_bare(self):
        # Unknown version → bare (current) operate base, same as _docs_site_bases.
        assert _aztec_source_url(
            "curated-troubleshooting/genesis-archive-root-mismatch.md", "v9.9.9"
        ) == "https://docs.aztec.network/operate/operators/operator-faq"


class TestNoirRepoMappings:
    """noir-docs/ → rendered noir-lang.org/docs (NOT GitHub).
    noir-stdlib/ stays on GitHub since those are real `.nr` source files."""

    def test_noir_docs_routes_to_rendered_site(self):
        # Rendered docs are on noir-lang.org/docs, not the GitHub source.
        assert _aztec_source_url("noir-docs/getting_started/quick_start.md") == (
            "https://noir-lang.org/docs/getting_started/quick_start"
        )

    def test_noir_docs_strips_index_suffix(self):
        assert _aztec_source_url("noir-docs/noir/concepts/index.md") == (
            "https://noir-lang.org/docs/noir/concepts"
        )

    def test_noir_docs_handles_mdx(self):
        assert _aztec_source_url("noir-docs/tutorials/noirjs_app.mdx") == (
            "https://noir-lang.org/docs/tutorials/noirjs_app"
        )

    def test_noir_docs_does_not_route_to_github(self):
        url = _aztec_source_url("noir-docs/getting_started/quick_start.md")
        assert "github.com" not in url

    def test_noir_stdlib_still_routes_to_github(self):
        # apiref ingest emits `hash/mod.nr.md` → strip to `hash/mod.nr`.
        # Stdlib stays on GitHub because those are source files, not docs.
        # Default active_version is v5 → the v5 noir pin.
        url = _aztec_source_url("noir-stdlib/hash/mod.nr.md")
        assert url == (
            "https://github.com/noir-lang/noir/blob/c57152f91260ecdb9faad4efc20abb14b6d2ece7/"
            "noir_stdlib/src/hash/mod.nr"
        )

    def test_noir_stdlib_uses_mainnet_pin_for_v4_3_1(self):
        # noir-stdlib is version-LESS in the path, so it follows active_version:
        # a mainnet (v4.3.1) answer must link the v4.3.1 noir pin, NOT the v5 one.
        url = _aztec_source_url("noir-stdlib/hash/mod.nr.md", active_version="v4.3.1")
        assert url == (
            "https://github.com/noir-lang/noir/blob/1d9727a6e0a9df75a71bb9c87daacbe30659ba09/"
            "noir_stdlib/src/hash/mod.nr"
        )


class TestCodeRepoMappings:
    """Body-bearing code corpora → AztecProtocol/aztec-packages at the release tag."""

    @pytest.mark.parametrize(
        "corpus_path,expected_repo_path",
        [
            (
                "noir-contracts/app/private_token_contract/src/main.nr.txt",
                "noir-projects/noir-contracts/contracts/app/private_token_contract/src/main.nr",
            ),
            (
                "aztec-nr/aztec/src/lib.nr.md",
                "noir-projects/aztec-nr/aztec/src/lib.nr",
            ),
            (
                "aztec.js/account/account.ts.txt",
                "yarn-project/aztec.js/src/account/account.ts",
            ),
            (
                "l1-contracts/src/core/Rollup.sol.txt",
                "l1-contracts/src/core/Rollup.sol",
            ),
            (
                "end-to-end/e2e_token_contract.ts.txt",
                "yarn-project/end-to-end/src/e2e_token_contract.ts",
            ),
            (
                "cli/index.ts.txt",
                "yarn-project/cli/src/index.ts",
            ),
        ],
    )
    def test_strips_extension_hack(self, corpus_path, expected_repo_path):
        # Default active_version is v5 → the v5.0.0-rc.1 release tag.
        assert _aztec_source_url(corpus_path) == (
            f"https://github.com/AztecProtocol/aztec-packages/blob/v5.0.0-rc.1/{expected_repo_path}"
        )

    def test_code_corpus_uses_mainnet_tag_for_v4_3_1(self):
        # Code corpora are version-LESS in the path, so they follow
        # active_version: a mainnet (v4.3.1) answer links the v4.3.1 tag.
        assert _aztec_source_url(
            "aztec.js/account/account.ts.txt", active_version="v4.3.1"
        ) == (
            "https://github.com/AztecProtocol/aztec-packages/blob/v4.3.1/"
            "yarn-project/aztec.js/src/account/account.ts"
        )

    def test_typescript_api_routes_to_next_snapshot(self):
        # The TS API reference's v5 content lives ONLY on the ``next`` snapshot
        # under ``testnet/`` (the release tag's copy is stale v4.3.0), so it
        # links to the pinned next snapshot, NOT the release tag. It's a
        # passthrough corpus, so the real ``.md`` / ``.txt`` extension is KEPT
        # (the GitHub blob needs the real filename). Default active_version v5.
        base = (
            "https://github.com/AztecProtocol/aztec-packages/blob/"
            "d69ab88adc2bef952696ff4b6ab8b109ae4b75ac/docs/static/typescript-api/testnet/"
        )
        assert _aztec_source_url("typescript-api/aztec.js.md") == base + "aztec.js.md"
        assert _aztec_source_url("typescript-api/llm-summary.txt") == base + "llm-summary.txt"

    def test_typescript_api_uses_mainnet_folder_for_v4_3_1(self):
        # Same pinned next snapshot SHA, but the network folder follows
        # active_version: mainnet (v4.3.1) → ``mainnet/`` (testnet/v5 → ``testnet/``).
        assert _aztec_source_url(
            "typescript-api/aztec.js.md", active_version="v4.3.1"
        ) == (
            "https://github.com/AztecProtocol/aztec-packages/blob/"
            "d69ab88adc2bef952696ff4b6ab8b109ae4b75ac/docs/static/typescript-api/mainnet/"
            "aztec.js.md"
        )


class TestEdgeCases:
    def test_unknown_path_passes_through(self):
        # We deliberately preserve the original string when nothing matches
        # so the message can still render the title — just without an href.
        assert _aztec_source_url("totally/unknown/path.txt") == "totally/unknown/path.txt"

    def test_empty_input(self):
        assert _aztec_source_url("") == ""

    def test_non_string_passes_through(self):
        # `metadata.source` is supposed to be a str but be defensive.
        assert _aztec_source_url(None) is None  # type: ignore[arg-type]

    def test_does_not_route_dev_docs_to_github(self):
        # Regression: previously the GitHub fallback caught all
        # `version-v4.3.0/...` paths under
        # `docs/developer_versioned_docs/version-v4.3.0/...`, which
        # produced 404 URLs because the docs version folder at the
        # actual v4.3.0 tag is `version-v4.2.0-aztecnr-rc.2`, not v4.3.0.
        url = _aztec_source_url("version-v4.3.0/overview.md")
        assert "github.com" not in url
        assert url.startswith("https://docs.aztec.network/developers/")

    def test_does_not_route_operator_docs_to_github(self):
        url = _aztec_source_url("version-v4.3.0/operators/setup/quickstart.md")
        assert "github.com" not in url
        assert url.startswith("https://docs.aztec.network/operate/operators/")

    def test_v4_3_1_docs_route_to_bare_mainnet(self):
        # v4.3.1 is the mainnet/current Docusaurus version → BARE site paths
        # (no version segment), matching the live docs.aztec.network scheme.
        assert _aztec_source_url("version-v4.3.1/overview.md") == (
            "https://docs.aztec.network/developers/overview"
        )
        assert _aztec_source_url("version-v4.3.1/docs/aztec-js/index.md") == (
            "https://docs.aztec.network/developers/docs/aztec-js"
        )
        assert _aztec_source_url("version-v4.3.1/operators/setup/quickstart.md") == (
            "https://docs.aztec.network/operate/operators/setup/quickstart"
        )

    def test_v5_docs_route_to_testnet_infix(self):
        # v5.0.0-rc.1 is the testnet version → the docs URL carries the
        # ``/testnet/`` path segment (derived from the source path's version
        # prefix, NOT from the active_version arg). Paths with no slug override.
        assert _aztec_source_url("version-v5.0.0-rc.1/overview.md") == (
            "https://docs.aztec.network/developers/testnet/overview"
        )
        assert _aztec_source_url("version-v5.0.0-rc.1/docs/aztec-js/index.md") == (
            "https://docs.aztec.network/developers/testnet/docs/aztec-js"
        )
        assert _aztec_source_url("version-v5.0.0-rc.1/operators/setup/quickstart.md") == (
            "https://docs.aztec.network/operate/testnet/operators/setup/quickstart"
        )

    def test_same_doc_routes_per_version(self):
        # The version is IN the source path, so the SAME doc tail routes to a
        # different site URL per version: mainnet (v4.3.1) bare vs testnet (v5)
        # under /testnet/. Guards against a regression back to version-agnostic
        # stripping (where both produced the same URL).
        for tail in ("docs/aztec-js/index.md", "operators/setup/quickstart.md", "overview.md"):
            v4 = _aztec_source_url(f"version-v4.3.1/{tail}")
            v5 = _aztec_source_url(f"version-v5.0.0-rc.1/{tail}")
            assert "/testnet/" not in v4
            assert "/testnet/" in v5
            assert v4 != v5

    def test_v5_slug_override_now_applied(self):
        # Regression guard for the closed gap: the slug map is now a UNION over
        # both versions, so a v5 path WITH an ``id:`` override uses the override
        # (underscore), not the filename — alongside the /testnet/ version
        # segment. (Was a documented fallback gap before the map regen.)
        assert _aztec_source_url(
            "version-v5.0.0-rc.1/operators/setup/registering-sequencer.md"
        ) == (
            "https://docs.aztec.network/operate/testnet/operators/setup/registering_sequencer"
        )

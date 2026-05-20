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

    Spot-checks a handful of the 14 known overrides recorded in
    ``application.api.answer.routes.aztec_doc_slugs``; the full set is
    covered by the data file itself.
    """

    def test_registering_sequencer_uses_id_slug(self):
        # File: docs/network_versioned_docs/version-v4.3.0/operators/setup/registering-sequencer.md
        # Frontmatter: id: registering_sequencer
        # Live URL: .../operate/operators/setup/registering_sequencer (200)
        # Filename-slug URL (.../registering-sequencer) returns 404.
        assert _aztec_source_url(
            "version-v4.3.0/operators/setup/registering-sequencer.md"
        ) == (
            "https://docs.aztec.network/operate/operators/setup/registering_sequencer"
        )

    def test_staking_provider_uses_id_slug(self):
        # id: become_a_staking_provider (drastically different from filename)
        assert _aztec_source_url(
            "version-v4.3.0/operators/setup/staking-provider.md"
        ) == (
            "https://docs.aztec.network/operate/operators/setup/become_a_staking_provider"
        )

    def test_sequencer_setup_uses_id_slug(self):
        # id: sequencer_management (file lives under setup/, but id moves it to /setup/sequencer_management)
        assert _aztec_source_url(
            "version-v4.3.0/operators/setup/sequencer-setup.md"
        ) == (
            "https://docs.aztec.network/operate/operators/setup/sequencer_management"
        )

    def test_governance_participation_uses_id_slug(self):
        # id: creating_and_voting_on_proposals (under sequencer-management/)
        assert _aztec_source_url(
            "version-v4.3.0/operators/sequencer-management/governance-participation.md"
        ) == (
            "https://docs.aztec.network/operate/operators/sequencer-management/creating_and_voting_on_proposals"
        )

    def test_unmapped_operator_doc_falls_back_to_filename(self):
        # claiming-rewards.md has no `id:` frontmatter — must not be
        # remapped. Confirmed 200 on the live site at the filename slug.
        assert _aztec_source_url(
            "version-v4.3.0/operators/sequencer-management/claiming-rewards.md"
        ) == (
            "https://docs.aztec.network/operate/operators/sequencer-management/claiming-rewards"
        )

    def test_index_md_with_id_still_uses_parent_path(self):
        # operators/keystore/index.md declares id: advanced_keystore_guide,
        # but Docusaurus serves index files at the parent path regardless
        # of the declared id. We must NOT inject the id into the URL here.
        assert _aztec_source_url(
            "version-v4.3.0/operators/keystore/index.md"
        ) == (
            "https://docs.aztec.network/operate/operators/keystore"
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
        url = _aztec_source_url("noir-stdlib/hash/mod.nr.md")
        assert url == (
            "https://github.com/noir-lang/noir/blob/1d9727a6e0a9df75a71bb9c87daacbe30659ba09/"
            "noir_stdlib/src/hash/mod.nr"
        )


class TestCodeRepoMappings:
    """Body-bearing code corpora → AztecProtocol/aztec-packages at v4.3.0."""

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
            (
                "typescript-api/aztec.js.md",
                "docs/static/typescript-api/testnet/aztec.js",
            ),
        ],
    )
    def test_strips_extension_hack(self, corpus_path, expected_repo_path):
        assert _aztec_source_url(corpus_path) == (
            f"https://github.com/AztecProtocol/aztec-packages/blob/v4.3.0/{expected_repo_path}"
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

"""Tests for ``application.api.answer.routes.base._aztec_source_url``.

The rewriter maps corpus-internal `metadata.source` paths into clickable
public URLs that we emit in `{type: "source"}` SSE frames. Several
classes of source paths each need different routing; this file pins the
behaviour so we don't silently regress on any of them.

Key bugs the current cases guard against:

* Top-level developer docs (``version-v4.2.0/<file>.md``) used to fall
  through to a GitHub URL under ``docs/developer_versioned_docs/...``
  that does not exist at the v4.2.0 git tag (the docs version snapshot
  is taken from a moving branch, so v4.2.0 has the *previous* version
  folder instead). Now they route to docs.aztec.network/developers/<file>.

* Network / operator docs (``version-v4.2.0/operators/<rest>``) used
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
    """Files under `version-v4.2.0/docs/...` → docs.aztec.network/developers/docs/<rest>."""

    def test_strips_md_extension(self):
        assert _aztec_source_url("version-v4.2.0/docs/aztec-nr/api.mdx") == (
            "https://docs.aztec.network/developers/docs/aztec-nr/api"
        )

    def test_strips_md_for_plain_md(self):
        assert _aztec_source_url("version-v4.2.0/docs/aztec-js/how_to_create_account.md") == (
            "https://docs.aztec.network/developers/docs/aztec-js/how_to_create_account"
        )

    def test_strips_trailing_index_segment(self):
        # Docusaurus serves `aztec-js/index.md` as `/developers/docs/aztec-js`.
        assert _aztec_source_url("version-v4.2.0/docs/aztec-js/index.md") == (
            "https://docs.aztec.network/developers/docs/aztec-js"
        )

    def test_nested_subfolders(self):
        assert _aztec_source_url(
            "version-v4.2.0/docs/foundational-topics/advanced/circuits/public_execution.md"
        ) == (
            "https://docs.aztec.network/developers/docs/foundational-topics/advanced/circuits/public_execution"
        )


class TestTopLevelDeveloperDocs:
    """Files at `version-v4.2.0/<file>.md` (NOT under `docs/`) — rendered
    at /developers/<file> on the site, not under /developers/docs/."""

    def test_top_level_overview(self):
        assert _aztec_source_url("version-v4.2.0/overview.md") == (
            "https://docs.aztec.network/developers/overview"
        )

    def test_top_level_with_underscores(self):
        assert _aztec_source_url("version-v4.2.0/getting_started_on_local_network.md") == (
            "https://docs.aztec.network/developers/getting_started_on_local_network"
        )

    def test_top_level_mdx(self):
        assert _aztec_source_url("version-v4.2.0/ai_tooling.mdx") == (
            "https://docs.aztec.network/developers/ai_tooling"
        )


class TestNetworkOperatorDocs:
    """Files at `version-v4.2.0/operators/...` — rendered under /operate/."""

    def test_operator_setup_doc(self):
        assert _aztec_source_url("version-v4.2.0/operators/setup/quickstart.md") == (
            "https://docs.aztec.network/operate/operators/setup/quickstart"
        )

    def test_operator_index_strips_to_folder(self):
        assert _aztec_source_url("version-v4.2.0/operators/index.md") == (
            "https://docs.aztec.network/operate/operators"
        )

    def test_operator_top_level(self):
        assert _aztec_source_url("version-v4.2.0/operators/sequencer_management.md") == (
            "https://docs.aztec.network/operate/operators/sequencer_management"
        )


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
            "https://github.com/noir-lang/noir/blob/842974fcf034b0a652631e69fc24f92f9ddd1d37/"
            "noir_stdlib/src/hash/mod.nr"
        )


class TestCodeRepoMappings:
    """Body-bearing code corpora → AztecProtocol/aztec-packages at v4.2.0."""

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
            f"https://github.com/AztecProtocol/aztec-packages/blob/v4.2.0/{expected_repo_path}"
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
        # `version-v4.2.0/...` paths under
        # `docs/developer_versioned_docs/version-v4.2.0/...`, which
        # produced 404 URLs because the docs version folder at the
        # actual v4.2.0 tag is `version-v4.1.0-rc.2`, not v4.2.0.
        url = _aztec_source_url("version-v4.2.0/overview.md")
        assert "github.com" not in url
        assert url.startswith("https://docs.aztec.network/developers/")

    def test_does_not_route_operator_docs_to_github(self):
        url = _aztec_source_url("version-v4.2.0/operators/setup/quickstart.md")
        assert "github.com" not in url
        assert url.startswith("https://docs.aztec.network/operate/operators/")

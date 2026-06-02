"""Tests for the ingest-side corpora config + file walker.

Lightweight integration around ``scripts.ingest.corpora`` and
``scripts.ingest.build._walk_files``. The corpora module is the
single source of truth for what gets pulled in on every re-ingest,
so adding a new corpus or a new filter knob warrants a guard test.

The two things pinned here:

  * ``aztec_site_networks`` is in the canonical CORPORA list and is
    wired to pull only ``networks.md`` from ``docs/docs/``. A
    typo in the slug or the include_paths allowlist would silently
    re-include the rest of ``docs/docs/`` (index.mdx, sunset page)
    or, worse, drop ``networks.md`` entirely.
  * ``_walk_files`` honors ``include_paths`` as an allowlist with
    fnmatch semantics. ``exclude_paths`` still applies on top, so
    a file appearing in both is still excluded.
"""

from __future__ import annotations

from pathlib import Path

from scripts.ingest.build import (
    _inline_external_links,
    _skip_for_missing_root,
    _walk_files,
)
from scripts.ingest.corpora import CORPORA, get_corpus


def test_networks_corpus_in_canonical_list():
    slugs = [c.slug for c in CORPORA]
    assert "aztec_site_networks" in slugs


def test_networks_corpus_targets_only_networks_md():
    c = get_corpus("aztec_site_networks")
    assert c.transform == "passthrough"
    assert c.source_root == "aztec-packages-docs"
    assert len(c.trees) == 1
    tree = c.trees[0]
    assert tree.path == "docs/docs"
    assert tree.zip_prefix == "aztec-site"
    assert tree.include_paths == ("networks.md",)


def test_participate_corpus_in_canonical_list():
    slugs = [c.slug for c in CORPORA]
    assert "aztec_participate_docs" in slugs


def test_participate_corpus_curated_subset():
    # Curated to token/ + governance/ only — basics/ is intentionally
    # excluded to avoid duplicating the versioned developer concept docs.
    c = get_corpus("aztec_participate_docs")
    assert c.transform == "passthrough"
    assert c.source_root == "aztec-packages-docs"
    assert c.in_production_agent is True
    assert len(c.trees) == 1
    tree = c.trees[0]
    assert tree.path == "docs/docs-participate"
    assert tree.zip_prefix == "aztec-participate"
    assert tree.include_paths == ("token/*", "governance/*")


def test_participate_corpus_wired_into_swap_order():
    # swap_sources._CANONICAL_ORDER is a hand-maintained mirror of CORPORA.
    # If a production corpus is missing from it, `_ordered()` silently drops
    # the upload from the swap SQL + AZTEC_SOURCE_IDS block — the corpus
    # would build + embed fine but never reach the prod agents.
    from scripts.ingest.swap_sources import _CANONICAL_ORDER

    assert "aztec_participate_docs" in _CANONICAL_ORDER


def test_every_production_corpus_in_swap_order():
    # Stronger invariant guarding the whole class of "added a corpus but
    # forgot the swap order" bugs: every in_production_agent corpus must be
    # wired into _CANONICAL_ORDER.
    from scripts.ingest.swap_sources import _CANONICAL_ORDER

    prod_slugs = {c.slug for c in CORPORA if c.in_production_agent}
    missing = prod_slugs - set(_CANONICAL_ORDER)
    assert not missing, f"production corpora missing from swap order: {missing}"


def test_awesome_aztec_corpus_in_canonical_list():
    slugs = [c.slug for c in CORPORA]
    assert "awesome_aztec" in slugs


def test_awesome_aztec_corpus_shape():
    c = get_corpus("awesome_aztec")
    # Must NOT be passthrough: the shared markdown parser would strip the
    # external URLs that are this corpus's whole value (see build.py
    # _inline_external_links).
    assert c.transform == "inline_external_links"
    assert c.source_root == "awesome-aztec"
    assert len(c.trees) == 1
    tree = c.trees[0]
    assert tree.path == "."
    assert tree.zip_prefix == "awesome-aztec"
    assert tree.include_paths == ("README.md",)


def test_awesome_aztec_held_out_of_prod_agent():
    # Deliberately in_production_agent=False until citation-overlap is
    # measured (see the corpus comment + the source_url rewriter). When
    # flipping to True, add "awesome_aztec" to _CANONICAL_ORDER too — the
    # test_every_production_corpus_in_swap_order invariant enforces that.
    from scripts.ingest.swap_sources import _CANONICAL_ORDER

    c = get_corpus("awesome_aztec")
    assert c.in_production_agent is False
    assert "awesome_aztec" not in _CANONICAL_ORDER


def test_inline_external_links_keeps_external_urls():
    # External links: the bare URL must survive (as plain text) so the
    # downstream markdown parser can't strip it.
    md = "- [Aztec Faucet](https://aztec-faucet.nethermind.io/) - testnet"
    out, kept, stripped = _inline_external_links(md)
    assert "https://aztec-faucet.nethermind.io/" in out
    assert "Aztec Faucet (https://aztec-faucet.nethermind.io/)" in out
    assert "](" not in out  # no markdown link syntax left for the parser
    assert (kept, stripped) == (1, 0)


def test_inline_external_links_strips_internal_links():
    # Relative / anchor links are stripped to their label, matching the
    # parser's default behaviour for internal doc cross-links.
    md = "See [the guide](./getting_started.md) and [top](#intro)."
    out, kept, stripped = _inline_external_links(md)
    assert out == "See the guide and top."
    assert (kept, stripped) == (0, 2)


def test_inline_external_links_keeps_outer_href_of_linked_badge():
    # awesome-* READMEs open with rows of linked shields.io badges:
    # [![alt](badge-img)](destination). The real destination is the OUTER
    # href; the inner badge image src must NOT win.
    md = "[![Twitter](https://img.shields.io/x.svg)](https://twitter.com/aztecnetwork)"
    out, kept, stripped = _inline_external_links(md)
    assert out == "Twitter (https://twitter.com/aztecnetwork)"
    assert "img.shields.io" not in out
    assert (kept, stripped) == (1, 0)


def test_inline_external_links_drops_standalone_image_src():
    md = "![logo](https://example.com/logo.png) Aztec"
    out, _, _ = _inline_external_links(md)
    assert out == "logo Aztec"


def test_inline_external_links_handles_mailto_and_protocol_relative():
    md = "[mail](mailto:x@y.z) [cdn](//cdn.example.com/a.js)"
    out, kept, _ = _inline_external_links(md)
    assert "mailto:x@y.z" in out
    assert "//cdn.example.com/a.js" in out
    assert kept == 2


def test_inline_external_links_mixed_list_roundtrips_via_build(tmp_path: Path):
    # Integration: the transform's build helper rewrites README content
    # in place under the zip prefix.
    from scripts.ingest.build import _build_inline_external_links

    c = get_corpus("awesome_aztec")
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text(
        "- [Faucet](https://faucet.example) desc\n- [Docs](./local.md)\n",
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    stats = _build_inline_external_links(c, [src], staging)
    out = (staging / "awesome-aztec" / "README.md").read_text(encoding="utf-8")
    assert "https://faucet.example" in out
    assert "./local.md" not in out
    assert stats["external_links_kept"] == 1
    assert stats["internal_links_stripped"] == 1


def test_skip_for_missing_root_holds_out_awesome_aztec():
    c = get_corpus("awesome_aztec")
    prod_roots = {"aztec-packages": Path("/x"), "aztec-packages-docs": Path("/y"),
                  "noir": Path("/z")}
    # Default build, awesome-aztec root absent → skipped.
    assert _skip_for_missing_root(c, prod_roots, explicitly_selected=False) is True
    # Root supplied → not skipped.
    assert _skip_for_missing_root(
        c, {**prod_roots, "awesome-aztec": Path("/a")}, explicitly_selected=False
    ) is False
    # Explicit --corpus selection → never skipped (let it hard-error if missing).
    assert _skip_for_missing_root(c, prod_roots, explicitly_selected=True) is False


def test_skip_for_missing_root_never_skips_production_corpus():
    # A production corpus with a missing root must NOT be silently skipped
    # (it should hard-error downstream instead).
    c = get_corpus("aztec_developer_docs")
    assert _skip_for_missing_root(c, {}, explicitly_selected=False) is False


def test_participate_walk_files_excludes_basics(tmp_path: Path):
    # `basics/` must NOT survive the include_paths allowlist.
    (tmp_path / "token").mkdir()
    (tmp_path / "governance").mkdir()
    (tmp_path / "basics").mkdir()
    (tmp_path / "token" / "staking.md").write_text("x")
    (tmp_path / "governance" / "voting.md").write_text("x")
    (tmp_path / "basics" / "intro.md").write_text("x")
    kept = _walk_files(
        tmp_path,
        extensions=(".md", ".mdx"),
        include_paths=("token/*", "governance/*"),
    )
    names = sorted(p.name for p in kept)
    assert names == ["staking.md", "voting.md"]


def test_walk_files_include_paths_allowlist(tmp_path: Path):
    # Layout that mirrors aztec-packages docs/docs/ at v4.3.0:
    # networks.md, index.mdx, aztec_connect_sunset.mdx
    (tmp_path / "networks.md").write_text("# Networks\n")
    (tmp_path / "index.mdx").write_text("# Index\n")
    (tmp_path / "aztec_connect_sunset.mdx").write_text("# Sunset\n")

    kept = _walk_files(
        tmp_path,
        extensions=(".md", ".mdx"),
        include_paths=("networks.md",),
    )

    assert [p.name for p in kept] == ["networks.md"]


def test_walk_files_empty_include_paths_is_no_op(tmp_path: Path):
    # Empty allowlist must mean "include everything that passes the
    # other filters" — the default for existing corpora.
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")

    kept = _walk_files(tmp_path, extensions=(".md",), include_paths=())
    assert sorted(p.name for p in kept) == ["a.md", "b.md"]


def test_walk_files_exclude_still_wins_over_include(tmp_path: Path):
    # If a file is named by both an include and an exclude pattern,
    # the exclude should win — it's the more specific intent.
    (tmp_path / "networks.md").write_text("# Networks\n")
    (tmp_path / "secret.md").write_text("# Secret\n")

    kept = _walk_files(
        tmp_path,
        extensions=(".md",),
        exclude_paths=("secret.md",),
        include_paths=("networks.md", "secret.md"),
    )
    assert [p.name for p in kept] == ["networks.md"]

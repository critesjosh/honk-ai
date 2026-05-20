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

from scripts.ingest.build import _walk_files
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

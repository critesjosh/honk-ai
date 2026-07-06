#!/usr/bin/env python3
"""Regenerate ``application/api/answer/routes/aztec_doc_slugs.py`` by
walking an aztec-packages checkout.

Docusaurus uses the ``id:`` field in a Markdown file's YAML frontmatter
as the URL slug whenever it's present, falling back to the filename
otherwise. Our corpus paths preserve the filename, so for any rendered
doc whose ``id:`` differs from its basename, a pure path-based URL
rewriter 404s on the live site. ``aztec_doc_slugs.py`` records every
such override; this script regenerates that file after a corpus bump.

Two-version KB: the slug map is a UNION over EVERY ``version-vX.Y.Z/`` folder
present in the checkout (e.g. both ``version-v4.3.1`` mainnet and
``version-v5.0.0-rc.1`` testnet on the ``next`` snapshot). Keys are
version-prefixed, so the two versions' overrides never collide and one
checkout regenerates both. Auto-discovery means a version bump can't silently
drop a version from the map — whatever folders the checkout has are emitted.

Walks, under each of the two versioned-docs trees in the supplied checkout,
EVERY ``version-v*`` folder it finds:
    docs/network_versioned_docs/version-*/
    docs/developer_versioned_docs/version-*/

For each ``.md`` / ``.mdx`` file:
  1. Skip if filename is ``index`` (Docusaurus serves index files at
     the parent path regardless of the declared ``id:``, and
     ``_strip_index_suffix`` in ``base.py`` handles that).
  2. Parse the first ``---``-delimited YAML block for a top-level
     ``id:`` value.
  3. Emit an entry only when ``id`` is present and differs from the
     filename basename.

Usage:
    python3 scripts/build_aztec_doc_slug_map.py \\
        --aztec-pkg /path/to/aztec-packages-next-snapshot \\
        > application/api/answer/routes/aztec_doc_slugs.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The two versioned-docs trees; each holds one ``version-v*`` folder per
# Docusaurus version. We glob ``version-v*`` under both and union the result.
_VERSIONED_TREES = ("network_versioned_docs", "developer_versioned_docs")
# Only real version folders (``version-v4.3.1`` etc.), digit-guarded so a
# stray ``version-vault`` can't be picked up (mirrors base.py's _VERSIONED_DOCS_RE).
_VERSION_DIR_RE = re.compile(r"^version-v\d")

# Retained-legacy overrides: versions STILL SERVED by a deployed image but no
# longer present in the ``next`` checkout (so auto-discovery can't find them).
# Until the cutover swaps the corpus to v4.3.1+v5, prod still serves the frozen
# v4.3.0 docs; if this image is deployed against that corpus, dropping these
# keys would 404 the v4.3.0 operator-doc citations. v4.3.0's override set is
# frozen (the version is retired from ``next``) and identical to v4.3.1's, so
# we keep it verbatim. **Delete this block once v4.3.0 leaves the served corpus
# (post-cutover, after the rollback window).** Merged AFTER the auto-discovered
# union; a collision with a discovered key is a hard error (see scan_tree).
_RETAINED_LEGACY_OVERRIDES: dict[str, str] = {
    "version-v4.3.0/operators/keystore/creating-keystores": "creating_keystores",
    "version-v4.3.0/operators/reference/ethereum-rpc-reference": "ethereum_rpc_reference",
    "version-v4.3.0/operators/reference/node-api-reference": "node_api_reference",
    "version-v4.3.0/operators/sequencer-management/governance-participation": "creating_and_voting_on_proposals",
    "version-v4.3.0/operators/sequencer-management/slashing-configuration": "slashing_and_offenses",
    "version-v4.3.0/operators/setup/bootnode-operation": "bootnode_operation",
    "version-v4.3.0/operators/setup/building-from-source": "building_from_source",
    "version-v4.3.0/operators/setup/high-availability": "high_availability_sequencers",
    "version-v4.3.0/operators/setup/registering-sequencer": "registering_sequencer",
    "version-v4.3.0/operators/setup/running-a-node": "running_a_node",
    "version-v4.3.0/operators/setup/running-a-prover": "running_a_prover",
    "version-v4.3.0/operators/setup/sequencer-setup": "sequencer_management",
    "version-v4.3.0/operators/setup/staking-provider": "become_a_staking_provider",
    "version-v4.3.0/operators/setup/syncing-best-practices": "syncing_best_practices",
    # Retained v5.0.0-rc.1 keys through the rc.2 cutover: identical slugs to rc.2,
    # but rc.1 sources stay live until the post-swap sweep, so dropping these would
    # 404 rc.1 id-slug operator-doc citations in the swap window. **Delete this
    # block once the rc.1 sources are swept** (same lifecycle as the v4.3.0 block).
    "version-v5.0.0-rc.1/operators/keystore/creating-keystores": "creating_keystores",
    "version-v5.0.0-rc.1/operators/reference/ethereum-rpc-reference": "ethereum_rpc_reference",
    "version-v5.0.0-rc.1/operators/reference/node-api-reference": "node_api_reference",
    "version-v5.0.0-rc.1/operators/sequencer-management/governance-participation": "creating_and_voting_on_proposals",
    "version-v5.0.0-rc.1/operators/sequencer-management/slashing-configuration": "slashing_and_offenses",
    "version-v5.0.0-rc.1/operators/setup/bootnode-operation": "bootnode_operation",
    "version-v5.0.0-rc.1/operators/setup/building-from-source": "building_from_source",
    "version-v5.0.0-rc.1/operators/setup/high-availability": "high_availability_sequencers",
    "version-v5.0.0-rc.1/operators/setup/registering-sequencer": "registering_sequencer",
    "version-v5.0.0-rc.1/operators/setup/running-a-node": "running_a_node",
    "version-v5.0.0-rc.1/operators/setup/running-a-prover": "running_a_prover",
    "version-v5.0.0-rc.1/operators/setup/sequencer-setup": "sequencer_management",
    "version-v5.0.0-rc.1/operators/setup/staking-provider": "become_a_staking_provider",
    "version-v5.0.0-rc.1/operators/setup/syncing-best-practices": "syncing_best_practices",
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# Accept any non-space / non-quote / non-`#` characters in the id value so
# we don't silently miss shapes like ``id: foo # comment`` or ids with
# `.` or `/`. Quotes (single or double) around the value are optional.
_ID_LINE_RE = re.compile(
    r"^id:\s*['\"]?([^'\"\s#]+)['\"]?\s*(?:#.*)?$",
    re.MULTILINE,
)


def parse_id(text: str) -> str | None:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm = m.group(1)
    id_match = _ID_LINE_RE.search(fm)
    return id_match.group(1) if id_match else None


def _discover_version_roots(docs_root: Path) -> list[tuple[Path, str]]:
    """Find every ``version-v*`` folder under the two versioned-docs trees.

    Returns ``(tree_path, corpus_prefix)`` pairs where ``corpus_prefix`` is the
    folder name (``version-v4.3.1``) — the same prefix the corpus path carries
    and the key namespace in the slug map.
    """
    roots: list[tuple[Path, str]] = []
    for tree_name in _VERSIONED_TREES:
        tree = docs_root / tree_name
        if not tree.is_dir():
            print(f"warning: {tree} missing — skipping", file=sys.stderr)
            continue
        for version_dir in sorted(tree.iterdir()):
            if version_dir.is_dir() and _VERSION_DIR_RE.match(version_dir.name):
                roots.append((version_dir, version_dir.name))
    return roots


def scan_tree(aztec_pkg: Path) -> dict[str, str]:
    overrides: dict[str, str] = {}
    docs_root = aztec_pkg / "docs"
    if not docs_root.is_dir():
        raise SystemExit(f"docs/ not found under {aztec_pkg}")

    version_roots = _discover_version_roots(docs_root)
    if not version_roots:
        raise SystemExit(
            f"no version-v* folders under {docs_root}/"
            "{network,developer}_versioned_docs/"
        )
    versions = sorted({prefix for _, prefix in version_roots})
    print(f"scanning versions: {', '.join(versions)}", file=sys.stderr)

    for tree, corpus_prefix in version_roots:
        for path in sorted(tree.rglob("*")):
            if not path.is_file() or path.suffix not in (".md", ".mdx"):
                continue
            basename = path.stem
            if basename == "index":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            id_value = parse_id(text)
            if id_value is None or id_value == basename:
                continue
            rel = path.relative_to(tree)
            key = f"{corpus_prefix}/{rel.with_suffix('').as_posix()}"
            # Keys are version-prefixed so the two versions never collide, but
            # the SAME version's network_ and developer_ trees could in
            # principle hold the same relative path with different ids — that
            # would be a silent overwrite. Fail loud instead.
            if key in overrides and overrides[key] != id_value:
                raise SystemExit(
                    f"slug-override collision for {key!r}: "
                    f"{overrides[key]!r} vs {id_value!r} (two trees disagree)"
                )
            overrides[key] = id_value

    # Merge retained-legacy keys (a still-served version absent from the
    # checkout). A collision with a freshly-discovered key means the version is
    # back in the checkout and the retained block is stale — fail loud.
    for key, slug in _RETAINED_LEGACY_OVERRIDES.items():
        if key in overrides and overrides[key] != slug:
            raise SystemExit(
                f"retained-legacy key {key!r} collides with a discovered "
                f"override ({overrides[key]!r} vs {slug!r}); drop it from "
                "_RETAINED_LEGACY_OVERRIDES — that version is in the checkout now."
            )
        overrides[key] = slug
    return overrides


_HEADER = '''"""URL-slug overrides for Docusaurus-rendered Aztec docs.

Docusaurus uses the ``id:`` field in a Markdown file's YAML frontmatter
as the URL slug, falling back to the filename when ``id:`` is absent.
The corpus path keeps the original filename (e.g.
``registering-sequencer.md``), so a purely path-based URL rewriter
emits ``.../registering-sequencer`` which 404s — the live site serves
the page at ``.../registering_sequencer`` because the file declares
``id: registering_sequencer``.

This module is the authoritative map of those overrides, UNIONED over every
``version-vX.Y.Z/`` corpus the KB serves (two-version KB: v4.3.1 mainnet +
v5.0.0-rc.1 testnet). Keys are version-prefixed corpus-relative source paths
*without* the ``.md`` / ``.mdx`` extension, so the two versions' overrides are
namespaced and never collide. Values are the URL slug Docusaurus serves.

Index files (``foo/index.md``) are NOT listed here even when they
declare an ``id:`` — Docusaurus serves them at the parent path
regardless of the id, and the existing ``_strip_index_suffix`` logic
in ``base.py`` already handles that correctly.

To regenerate after a corpus bump: ``python3 scripts/build_aztec_doc_slug_map.py
--aztec-pkg /path/to/aztec-packages-next-snapshot > application/api/answer/routes/aztec_doc_slugs.py``
(auto-discovers every ``version-v*`` folder in the checkout).
"""

from __future__ import annotations

# Generated by scripts/build_aztec_doc_slug_map.py — UNION over every
# version-v* folder in the next snapshot (network_ + developer_versioned_docs),
# PLUS retained-legacy keys for a still-served version absent from the checkout
# (v4.3.0, until the cutover retires it). Only non-index files where ``id:``
# differs from the filename basename.
AZTEC_DOC_SLUG_OVERRIDES: dict[str, str] = {
'''

_FOOTER = '''}


def apply_slug_override(rest: str, source_path_no_ext: str) -> str:
    """If ``source_path_no_ext`` has a slug override, replace the last
    segment of ``rest`` with it; otherwise return ``rest`` unchanged.

    ``rest`` is the portion of the URL path after the corpus-prefix
    has been stripped (e.g. ``operators/setup/registering-sequencer``
    when emitting under ``/operate/``). ``source_path_no_ext`` is the
    full corpus-relative path with the extension stripped (e.g.
    ``version-vX.Y.Z/operators/setup/registering-sequencer``) — that
    is the key into ``AZTEC_DOC_SLUG_OVERRIDES``.

    Defensive: index files are served at the parent path regardless of
    their declared ``id:`` (and ``base.py`` strips the ``/index``
    suffix before calling this helper). If a future regen accidentally
    emits a key ending in ``/index``, returning ``rest`` unchanged
    avoids mangling the parent-folder URL.
    """
    if source_path_no_ext.endswith("/index"):
        return rest
    override = AZTEC_DOC_SLUG_OVERRIDES.get(source_path_no_ext)
    if override is None:
        return rest
    parent, sep, _basename = rest.rpartition("/")
    return f"{parent}{sep}{override}" if sep else override
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--aztec-pkg",
        required=True,
        type=Path,
        help="Path to an aztec-packages next-branch snapshot worktree that "
             "contains the version-v*/ Docusaurus folders (NOT the release "
             "tag — 'Option B' four-root layout per PR #150). EVERY version-v* "
             "folder found is unioned into the map.",
    )
    args = parser.parse_args()
    overrides = scan_tree(args.aztec_pkg)
    out = sys.stdout
    out.write(_HEADER)
    for key in sorted(overrides):
        # Double-quoted strings to match the rest of the codebase's style
        # (avoids a churning diff against tools that normalize on save).
        out.write(f'    "{key}": "{overrides[key]}",\n')
    out.write(_FOOTER)
    print(f"emitted {len(overrides)} overrides", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Regenerate ``application/api/answer/routes/aztec_doc_slugs.py`` by
walking an aztec-packages checkout.

Docusaurus uses the ``id:`` field in a Markdown file's YAML frontmatter
as the URL slug whenever it's present, falling back to the filename
otherwise. Our corpus paths preserve the filename, so for any rendered
doc whose ``id:`` differs from its basename, a pure path-based URL
rewriter 404s on the live site. ``aztec_doc_slugs.py`` records every
such override; this script regenerates that file after a corpus bump.

Walks two trees under the supplied ``aztec-packages`` checkout:
    docs/network_versioned_docs/version-v4.3.0/
    docs/developer_versioned_docs/version-v4.3.0/

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
        --aztec-pkg /path/to/aztec-packages \\
        > application/api/answer/routes/aztec_doc_slugs.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_DOC_ROOTS = (
    ("network_versioned_docs/version-v4.3.0", "version-v4.3.0"),
    ("developer_versioned_docs/version-v4.3.0", "version-v4.3.0"),
)

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


def scan_tree(aztec_pkg: Path) -> dict[str, str]:
    overrides: dict[str, str] = {}
    docs_root = aztec_pkg / "docs"
    if not docs_root.is_dir():
        raise SystemExit(f"docs/ not found under {aztec_pkg}")

    for tree_suffix, corpus_prefix in _DOC_ROOTS:
        tree = docs_root / tree_suffix
        if not tree.is_dir():
            print(f"warning: {tree} missing — skipping", file=sys.stderr)
            continue
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
            overrides[key] = id_value
    return overrides


_HEADER = '''"""URL-slug overrides for Docusaurus-rendered Aztec docs.

Docusaurus uses the ``id:`` field in a Markdown file's YAML frontmatter
as the URL slug, falling back to the filename when ``id:`` is absent.
The corpus path keeps the original filename (e.g.
``registering-sequencer.md``), so a purely path-based URL rewriter
emits ``.../registering-sequencer`` which 404s — the live site serves
the page at ``.../registering_sequencer`` because the file declares
``id: registering_sequencer``.

This module is the authoritative map of those overrides for the v4.3.0
corpus. Keys are corpus-relative source paths *without* the ``.md`` /
``.mdx`` extension. Values are the URL slug Docusaurus actually serves.

Index files (``foo/index.md``) are NOT listed here even when they
declare an ``id:`` — Docusaurus serves them at the parent path
regardless of the id, and the existing ``_strip_index_suffix`` logic
in ``base.py`` already handles that correctly.

To regenerate after a corpus bump: ``python3 scripts/build_aztec_doc_slug_map.py
--aztec-pkg /path/to/aztec-packages > application/api/answer/routes/aztec_doc_slugs.py``
"""

from __future__ import annotations

# Generated from aztec-packages @ v4.3.0 (network_versioned_docs +
# developer_versioned_docs). Only non-index files where ``id:`` differs
# from the filename basename are listed.
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
    ``version-v4.3.0/operators/setup/registering-sequencer``) — that
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
        help="Path to an aztec-packages checkout (next-branch worktree "
             "that contains version-v4.3.0/, NOT the release tag — "
             "'Option B' four-root layout per PR #150)",
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

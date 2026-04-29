#!/usr/bin/env python3
"""Transform Noir source (`.nr`) into a Markdown API-reference view.

Why
---
The default DocsGPT ingest pipeline embeds full ``.nr`` files (renamed to
``.txt`` so the parser allowlist accepts them). The implementation
bodies, ``//`` line comments, helper assertions and the like dilute the
embedding signal — so a vague concept-doc paragraph routinely outranks
the canonical API-reference for identifier-shaped questions like
"what's the signature of ``compute_secret_hash``?".

This module strips ``.nr`` files down to:

  * file-level ``//!`` module doc as prose
  * each public item's ``///`` doc comments as prose
  * its attributes (``#[oracle(...)]``, ``#[contract]``, …)
  * its signature (struct fields, enum variants, trait/impl headers, fn
    signatures up to the return type and ``where`` clause)

Bodies, ``//`` line comments, ``#[test]`` items, and private items are
dropped. The output is a Markdown file with one ``##``-level section per
item, so downstream chunking lands on item-sized units.

Strategy
--------
A two-pass approach:

  1. Build a "code skeleton" — a copy of the source with the same
     length, where line/block comments and string/char literals have
     been replaced by spaces. This lets us walk a single contiguous
     string with safe bracket-depth tracking. Doc comments (``///`` /
     ``//!``) remain readable in the *original* source; we record their
     spans separately.
  2. Walk the skeleton at depth zero to identify top-level items, then
     for each item read the ORIGINAL source text for its header and
     children. Doc comments and attributes are collected by looking at
     the original source between the previous item's end and the
     current item's start.

This is simpler and more robust than a token-stream walker: items that
contain ``//`` comments mid-body can't accidentally split the parse.

Coverage manifest
-----------------
Every run writes a JSON manifest reporting:
  * files seen, files with parse errors, files with zero public items
  * per-file public-item counts by kind

Silent parser drift is the single biggest risk for this transform.
The release ritual is: review the manifest, hand-audit a sample of 20
files spanning aztec-nr/noir-stdlib before re-ingesting.

CLI
---
::

    python scripts/ingest/noir_apiref.py \
        --input  /tmp/aztec-v4.2.0/noir-projects/aztec-nr \
        --output /tmp/apiref-out/aztec-nr \
        --manifest /tmp/apiref-out/aztec-nr.manifest.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Skeleton builder ────────────────────────────────────────────────────────


@dataclass
class DocSpan:
    """A `///` or `//!` doc comment span."""
    kind: str  # "doc" or "module_doc"
    start: int
    end: int  # exclusive
    text: str  # original text including the `///` markers


@dataclass
class AttrSpan:
    """A `#[...]` attribute span at depth zero."""
    start: int
    end: int  # exclusive (one past the closing `]`)
    text: str


def build_skeleton(src: str) -> Tuple[str, List[DocSpan], List[AttrSpan]]:
    """Return (skeleton, doc_spans, attr_spans).

    ``skeleton`` is a string of the same length as ``src`` where:
      - ``//`` line comments (NOT ``///``/``//!``) and ``/* ... */`` block
        comments are replaced character-by-character with spaces.
      - ``///`` and ``//!`` are preserved AS spaces in the skeleton (so
        bracket-depth tracking is safe), but their original text is
        stashed in ``doc_spans`` for later prose extraction.
      - String literals and char literals have their *interiors* replaced
        with spaces, but the surrounding ``"`` / ``'`` quotes are
        preserved (so we can still see "this position is inside a
        string" if we wanted; bracket counters skip strings entirely).
      - All other characters (code) are unchanged.

    ``attr_spans`` collects every ``#[...]`` / ``#![...]`` attribute span
    encountered at brace/paren depth zero — these are item attributes
    we'll re-attach to the next item start.
    """
    n = len(src)
    skel: List[str] = list(src)
    docs: List[DocSpan] = []
    attrs: List[AttrSpan] = []

    # Outer pass: walk source, decide for each character whether to
    # blank it. We track bracket depth lazily — only used to tag
    # attribute spans as "depth-zero".
    i = 0
    brace = paren = bracket = 0
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if c == "/" and nxt == "/":
            third = src[i + 2] if i + 2 < n else ""
            fourth = src[i + 3] if i + 3 < n else ""
            j = src.find("\n", i)
            j = n if j == -1 else j
            text = src[i:j]
            # `///x` (third == "/") with NO further `/` → doc comment.
            # `////` and beyond → plain comment (rustdoc convention).
            if third == "/" and fourth != "/":
                docs.append(DocSpan(kind="doc", start=i, end=j, text=text))
                for k in range(i, j):
                    skel[k] = " "
            elif third == "!":
                docs.append(DocSpan(kind="module_doc", start=i, end=j, text=text))
                for k in range(i, j):
                    skel[k] = " "
            else:
                for k in range(i, j):
                    skel[k] = " "
            i = j
            continue

        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                skel[k] = " "
            i = j
            continue

        if c == '"':
            # Walk to closing quote. Replace interior with spaces.
            j = i + 1
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    skel[j] = " "
                    skel[j + 1] = " "
                    j += 2
                    continue
                if src[j] == '"':
                    j += 1
                    break
                skel[j] = " "
                j += 1
            i = j
            continue

        if c == "'":
            # Char literal — only blank the interior if a closing `'`
            # appears within ~4 chars on the same line. Otherwise leave
            # alone (could be a Noir lifetime in generic args, though
            # Noir doesn't have lifetimes today).
            j = i + 1
            saw_close = False
            while j < n and j - i <= 4 and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if src[j] == "'":
                    saw_close = True
                    j += 1
                    break
                j += 1
            if saw_close:
                for k in range(i + 1, j - 1):
                    skel[k] = " "
                i = j
                continue
            # Not a char literal — leave intact.
            i += 1
            continue

        if c == "#":
            # Possible attribute: `#[...]` or `#![...]`
            ai = i
            aj = i + 1
            if aj < n and src[aj] == "!":
                aj += 1
            if aj < n and src[aj] == "[":
                # Scan for matching `]`. Strings inside attributes
                # follow the same rules; we'll consume them via the
                # outer loop on a follow-up pass — but we already
                # blank-out strings at the outer-loop level. For the
                # "find matching `]`" we need to be safe against `]`
                # in strings. The string blanking will already replace
                # `]` inside strings with spaces (well — `]` is not in
                # a string literal escape, but the `"` blanking did
                # NOT change `]` inside strings; let's be safe and
                # walk strings ourselves here too.)
                depth = 1
                k = aj + 1
                while k < n:
                    cc = src[k]
                    if cc == '"':
                        k += 1
                        while k < n and src[k] != '"':
                            if src[k] == "\\":
                                k += 2
                                continue
                            k += 1
                        k += 1
                        continue
                    if cc == "[":
                        depth += 1
                    elif cc == "]":
                        depth -= 1
                        if depth == 0:
                            k += 1
                            break
                    k += 1
                if depth == 0:
                    if brace == 0 and paren == 0 and bracket == 0:
                        attrs.append(AttrSpan(start=ai, end=k, text=src[ai:k]))
                    i = k
                    continue
            # `#` not part of an attribute — fall through.
            i += 1
            continue

        if c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket -= 1
        i += 1

    return "".join(skel), docs, attrs


# ── Item walker (works on the skeleton + original source) ──────────────────


@dataclass
class ApiItem:
    """A public item extracted from a Noir source file."""
    kind: str  # fn | struct | enum | trait | impl | pub_use | pub_const | pub_mod | type_alias
    name: str
    doc: str  # already-stripped of /// markers
    attrs: List[str] = field(default_factory=list)
    signature: str = ""
    children: List["ApiItem"] = field(default_factory=list)
    src_start: int = 0  # for error reporting / debugging
    src_end: int = 0


# Item-introducing keyword patterns. We compile one big regex matching
# BOTH public and private item leads. Capturing private items too
# matters for correctness: their positions are used to advance the
# "where doc-comments end up attached" cursor in ``parse_file_items``.
# A private ``#[test] fn helper_test() { ... }`` followed by a public
# ``pub fn real() { ... }`` would otherwise leak the ``#[test]``
# attribute onto ``real`` and silently drop it (codex review caught
# this).
_ITEM_RE = re.compile(
    r"\b("
    # Visibility prefix (optional). The pub/private distinction is
    # made downstream in ``_classify_item`` by inspecting whether the
    # match starts with ``pub`` (or for impl: always public).
    r"(?:pub\s*(?:\(\s*crate\s*\))?\s+)?"
    r"(?:"
    # use / mod / const / global / type / struct / enum / trait
    r"use\s|mod\s|const\s|global\s|type\s|struct\s|enum\s|trait\s|"
    # fn (with optional unconstrained / comptime modifiers)
    r"(?:unconstrained\s+|comptime\s+)*fn\s"
    r")"
    r"|impl\b"
    r")"
)


def _find_matching_brace(skel: str, open_idx: int) -> int:
    """Return index of `}` matching the `{` at ``open_idx``, or -1."""
    n = len(skel)
    if open_idx >= n or skel[open_idx] != "{":
        return -1
    depth = 1
    i = open_idx + 1
    while i < n:
        c = skel[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _scan_top_level_terminator(skel: str, start: int, terminators: str) -> int:
    """Walk ``skel`` from ``start`` until we hit a terminator at depth
    zero. Returns the index of the terminator, or len(skel) if none."""
    n = len(skel)
    i = start
    brace = paren = bracket = 0
    while i < n:
        c = skel[i]
        if brace == 0 and paren == 0 and bracket == 0 and c in terminators:
            return i
        if c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket -= 1
        i += 1
    return n


def _depth_at(skel: str, idx: int) -> Tuple[int, int, int]:
    """Compute (brace, paren, bracket) depth at position ``idx`` in
    the skeleton. Used when classifying a regex match position — we
    only treat the match as a top-level item if it sits at depth zero."""
    brace = paren = bracket = 0
    for i in range(idx):
        c = skel[i]
        if c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket -= 1
    return brace, paren, bracket


def _all_top_level_item_positions(skel: str) -> List[int]:
    """Find all candidate item-start positions in the skeleton at depth
    zero, in source order. Uses a single linear scan that maintains
    depth as it goes (avoiding the O(N²) of re-scanning depth at each
    match)."""
    out: List[int] = []
    n = len(skel)
    i = 0
    brace = paren = bracket = 0
    while i < n:
        c = skel[i]
        # At depth zero, try to match an item keyword starting here.
        if brace == 0 and paren == 0 and bracket == 0:
            # Quick alpha-or-`p`/`i` check before regex.
            if c.isalpha() or c == "_":
                m = _ITEM_RE.match(skel, i)
                if m:
                    # Make sure this is a word-boundary start (the
                    # regex uses \b, but with \b at i=0 it's fine).
                    out.append(i)
                    # Skip past the matched lead so we don't re-match
                    # nested keywords inside the same item header.
                    # The actual item end is computed downstream; for
                    # candidate-position purposes we only care that we
                    # don't double-record this start. A simple bump
                    # past the match is enough because items at depth
                    # zero are non-overlapping in source.
                    i = m.end()
                    continue
        if c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket -= 1
        i += 1
    return out


def _classify_item(head: str) -> str:
    """Classify an item-head string (e.g. "pub use ..." or "fn foo()")
    into one of the known kinds. Returns "?" if the head doesn't look
    like an item start. Private items return a "private_<kind>" tag —
    the caller uses these to advance ``prev_end`` past them without
    emitting an ApiItem."""
    s = head.lstrip()
    pub_re = r"pub(?:\s*\(\s*crate\s*\))?\s+"

    # Public variants (return the canonical kind name).
    if re.match(pub_re + r"use\s", s):
        return "pub_use"
    if re.match(pub_re + r"mod\s", s):
        return "pub_mod"
    if re.match(pub_re + r"(?:const|global)\s", s):
        return "pub_const"
    if re.match(pub_re + r"type\s", s):
        return "type_alias"
    if re.match(pub_re + r"struct\s", s):
        return "struct"
    if re.match(pub_re + r"enum\s", s):
        return "enum"
    if re.match(pub_re + r"trait\s", s):
        return "trait"
    if re.match(pub_re + r"(?:unconstrained\s+|comptime\s+)*fn\s", s):
        return "fn"

    # impl is always emitted (visibility doesn't apply to impl blocks).
    if re.match(r"impl\b", s):
        return "impl"

    # Private variants — we track their END so doc/attr regions don't
    # leak across them, but we don't emit ApiItems.
    if re.match(r"use\s", s):
        return "private_use"
    if re.match(r"mod\s", s):
        return "private_mod"
    if re.match(r"(?:const|global)\s", s):
        return "private_const"
    if re.match(r"type\s", s):
        return "private_type"
    if re.match(r"struct\s", s):
        return "private_struct"
    if re.match(r"enum\s", s):
        return "private_enum"
    if re.match(r"trait\s", s):
        return "private_trait"
    if re.match(r"(?:unconstrained\s+|comptime\s+)*fn\s", s):
        return "private_fn"
    return "?"


_PUBLIC_KINDS = frozenset({
    "pub_use", "pub_mod", "pub_const", "type_alias",
    "struct", "enum", "trait", "fn", "impl",
})


def _strip_angle_generics(header: str, start: int) -> int:
    """Given ``header`` and an index ``start`` that points at ``<``,
    return the index just past the matching ``>``. Handles nested
    generics. If no `<` at start, returns start unchanged."""
    if start >= len(header) or header[start] != "<":
        return start
    depth = 0
    i = start
    while i < len(header):
        c = header[i]
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return start


def _extract_name(kind: str, header: str) -> str:
    if kind == "pub_use":
        m = re.search(r"use\s+([^;]+);", header)
        if not m:
            return "?"
        path = m.group(1).strip()
        if " as " in path:
            return path.split(" as ", 1)[1].strip().rstrip("};,")
        return path.split("::")[-1].strip().rstrip("};,")
    if kind == "impl":
        # Skip the optional generic-params block right after `impl`:
        #   impl<K, V, Context> Map<K, V, Context> { ... }
        #   impl<...> Trait<...> for Type<...> { ... }
        s = header
        i = 0
        m = re.match(r"\s*impl\s*", s)
        if not m:
            return "impl"
        i = m.end()
        # Optional generic parameters
        if i < len(s) and s[i] == "<":
            i = _strip_angle_generics(s, i)
        # Optional whitespace, then either `Trait<...> for Type<...>`
        # or `Type<...>`. We need the name after `for`, otherwise the
        # type immediately after the generic-params.
        rest = s[i:].lstrip()
        # Walk forward looking for either ` for ` (with anything before it)
        # or just take the first identifier in ``rest``.
        for_idx = re.search(r"\bfor\b", rest)
        if for_idx:
            after_for = rest[for_idx.end():].lstrip()
            m = re.match(r"([A-Za-z_]\w*)", after_for)
            if m:
                return m.group(1)
        m = re.match(r"([A-Za-z_]\w*)", rest)
        if m:
            return m.group(1)
        return "impl"
    keywords = {"fn", "struct", "enum", "trait", "const", "global", "type", "mod"}
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", header)
    for idx, t in enumerate(toks):
        if t in keywords and idx + 1 < len(toks):
            return toks[idx + 1]
    return "?"


def _strip_doc_marker(text: str) -> str:
    r"""Strip leading `///`/`//!`/`//` markers from each line and return
    the prose. ATX-style headings (``# Foo`` … ``###### Foo``) at the
    line start are escaped to ``\#`` so the downstream markdown parser
    doesn't treat them as new sections — they belong to the item we're
    documenting, not as siblings of it."""
    out_lines = []
    for line in text.split("\n"):
        s = line.lstrip()
        if s.startswith("///"):
            s = s[3:]
        elif s.startswith("//!"):
            s = s[3:]
        elif s.startswith("//"):
            s = s[2:]
        if s.startswith(" "):
            s = s[1:]
        # Escape ATX headings inside doc comments so they don't split
        # the rendered apiref file into spurious sections.
        if re.match(r"^#+\s", s):
            s = "\\" + s
        out_lines.append(s)
    return "\n".join(out_lines).rstrip()


def _collect_pending_docs(
    docs: List[DocSpan],
    attrs: List[AttrSpan],
    item_start: int,
    prev_item_end: int,
) -> Tuple[str, List[str], bool]:
    """Walk doc spans + attribute spans that sit between
    ``prev_item_end`` and ``item_start``. Concatenate doc-comment text
    (in source order), collect attributes, and detect ``#[test]``."""
    doc_lines: List[str] = []
    attr_texts: List[str] = []
    has_test = False
    for d in docs:
        if prev_item_end <= d.start < item_start and d.kind == "doc":
            doc_lines.append(_strip_doc_marker(d.text))
    for a in attrs:
        if prev_item_end <= a.start < item_start:
            attr_texts.append(a.text)
            if re.search(r"#!?\[\s*test\b", a.text):
                has_test = True
    return "\n".join(doc_lines).strip(), attr_texts, has_test


def _file_module_doc(docs: List[DocSpan]) -> str:
    """Concatenate any leading `//!` spans (file-level module docs)
    until the first non-module-doc span. ``///`` spans attach to
    individual items downstream; the file header only renders the
    module-doc prefix."""
    out_lines: List[str] = []
    for d in docs:
        if d.kind == "module_doc":
            out_lines.append(_strip_doc_marker(d.text))
        else:
            break  # stop at first non-module-doc
    return "\n".join(out_lines).strip()


def parse_file_items(src: str) -> Tuple[List[ApiItem], str, List[str]]:
    """Parse a single Noir source file.

    Returns (items, file_doc, errors).
    """
    skel, docs, attrs = build_skeleton(src)
    errors: List[str] = []
    items: List[ApiItem] = []

    file_doc = _file_module_doc(docs)
    positions = _all_top_level_item_positions(skel)

    prev_end = 0
    for pos in positions:
        # Read a generous lookahead from the original source for
        # classification (200 chars is plenty for the keyword lead).
        head_lookahead = src[pos:pos + 200]
        kind = _classify_item(head_lookahead)
        if kind == "?":
            continue

        try:
            item_end = _consume_item(skel, src, pos, kind)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"item at offset {pos} ({kind}): {exc!r}")
            continue
        if item_end < 0:
            errors.append(f"item at offset {pos} ({kind}): unterminated")
            continue

        # Collect doc/attr spans that sit between the previous item's
        # end and this item's start. We do this BEFORE skipping
        # private items so any ``///`` or ``#[...]`` attached to a
        # private item is consumed (and discarded) by the private
        # item, not bled into the next public item.
        doc, attr_texts, has_test = _collect_pending_docs(docs, attrs, pos, prev_end)
        prev_end = item_end

        if kind not in _PUBLIC_KINDS:
            # Private item — we already advanced prev_end past it, so
            # any doc/attr that preceded it is correctly scoped to it
            # (and dropped along with it).
            continue
        if has_test:
            # Public item with a #[test] attribute (rare but legal:
            # `pub fn` with #[test] — still treat as test-only).
            continue

        item = _build_item(kind, src, pos, item_end, doc, attr_texts)
        if item is not None:
            item.src_start = pos
            item.src_end = item_end
            items.append(item)

    return items, file_doc, errors


def _consume_item(skel: str, src: str, pos: int, kind: str) -> int:
    """Determine where an item ends. Returns one-past-the-last byte of
    the item, or -1 if unterminated.

    For items terminated by ``;`` (pub use, pub const, pub type, simple
    pub mod, fn declarations without a body): return the index just
    past the ``;``.

    For items with a ``{...}`` body (struct/enum/trait/impl, fn with a
    body, inline pub mod): return the index just past the matching
    ``}``.
    """
    n = len(skel)
    end = _scan_top_level_terminator(skel, pos, ";{")
    if end >= n:
        return -1
    if skel[end] == ";":
        return end + 1
    body_end = _find_matching_brace(skel, end)
    if body_end < 0:
        return -1
    return body_end + 1


def _build_item(
    kind: str,
    src: str,
    start: int,
    end: int,
    doc: str,
    attr_texts: List[str],
) -> Optional[ApiItem]:
    """Construct an ApiItem from a parsed item span."""
    raw = src[start:end]
    if kind in ("pub_use", "pub_const", "type_alias"):
        # Single-line declarations end with `;`.
        signature = raw.strip()
        return ApiItem(
            kind=kind,
            name=_extract_name(kind, signature),
            doc=doc,
            attrs=attr_texts,
            signature=signature,
        )
    if kind == "pub_mod":
        # Either `pub mod foo;` or `pub mod foo { ... }`. We keep just
        # the leading `pub mod foo;` form for retrieval — the inner
        # items would be picked up if/when we recurse into them, but
        # in practice aztec-nr/noir-stdlib use file-per-module.
        sig = raw.split("{", 1)[0].rstrip()
        if not sig.endswith(";"):
            sig = sig + ";"
        return ApiItem(
            kind=kind,
            name=_extract_name(kind, sig),
            doc=doc,
            attrs=attr_texts,
            signature=sig,
        )
    if kind in ("struct", "enum"):
        # Keep the entire item text — the body IS the signature
        # (fields / variants).
        signature = raw.strip()
        return ApiItem(
            kind=kind,
            name=_extract_name(kind, signature),
            doc=doc,
            attrs=attr_texts,
            signature=signature,
        )
    if kind == "trait":
        # Header + method-signature children.
        return _build_trait_or_impl(kind, src, start, end, doc, attr_texts)
    if kind == "impl":
        return _build_trait_or_impl(kind, src, start, end, doc, attr_texts)
    if kind == "fn":
        # Drop the body, keep header + `;`.
        if "{" in raw:
            head_end = raw.index("{")
            sig = raw[:head_end].rstrip().rstrip(",") + ";"
        else:
            sig = raw.strip()
        return ApiItem(
            kind=kind,
            name=_extract_name(kind, sig),
            doc=doc,
            attrs=attr_texts,
            signature=sig,
        )
    return None


def _build_trait_or_impl(
    kind: str,
    src: str,
    start: int,
    end: int,
    doc: str,
    attr_texts: List[str],
) -> ApiItem:
    raw = src[start:end]
    brace_idx = raw.index("{")
    header = raw[:brace_idx].rstrip()
    body = raw[brace_idx + 1:-1]  # drop opening `{` and closing `}`
    body_offset = start + brace_idx + 1

    # Recursively parse the body for nested method signatures + items.
    children: List[ApiItem] = []
    sub_items, _sub_doc, _sub_errs = parse_file_items(body)
    for s in sub_items:
        # Adjust source offsets back into the parent file
        s.src_start += body_offset
        s.src_end += body_offset
        children.append(s)
    # Also pick up bare `fn` (no `pub`) inside trait bodies AND inside
    # `impl Trait for Type` blocks — both are part of the public
    # surface (trait methods are public by virtue of the trait).
    # Inherent impl blocks (`impl Type { ... }`) only surface `pub fn`
    # via the recursive parser above.
    is_trait_impl = kind == "impl" and re.search(r"\bfor\b", header) is not None
    if kind == "trait" or is_trait_impl:
        existing_starts = {c.src_start - body_offset for c in children}
        for bare in _extract_bare_fn_methods(body, body_offset):
            if (bare.src_start - body_offset) not in existing_starts:
                children.append(bare)
        # Sort by source order so rendering matches file order.
        children.sort(key=lambda c: c.src_start)

    return ApiItem(
        kind=kind,
        name=_extract_name(kind, header),
        doc=doc,
        attrs=attr_texts,
        signature=header,
        children=children,
    )


_BARE_FN_RE = re.compile(
    r"\b(?:unconstrained\s+|comptime\s+)*fn\s+[A-Za-z_]\w*"
)


def _extract_bare_fn_methods(body: str, offset: int) -> List[ApiItem]:
    """Inside a trait body (or `impl Trait for Type` body), extract
    method signatures that are NOT prefixed with ``pub`` — trait
    methods don't need `pub` to be part of the public surface.

    Tracks the previous method's end so doc/attr spans don't leak
    across consecutive bare fns (codex review caught this — the
    earlier version always passed ``prev_item_end=0``)."""
    skel, docs, attrs = build_skeleton(body)
    out: List[ApiItem] = []
    n = len(skel)
    i = 0
    brace = paren = bracket = 0
    seen_starts: set = set()
    prev_method_end = 0
    while i < n:
        c = skel[i]
        if brace == 0 and paren == 0 and bracket == 0:
            m = _BARE_FN_RE.match(skel, i)
            if m and i not in seen_starts:
                # Skip if the lead is preceded by `pub` (already
                # handled by the recursive parser).
                back = skel[max(0, i - 12):i]
                if "pub" in back:
                    i = m.end()
                    continue
                seen_starts.add(i)
                end = _scan_top_level_terminator(skel, i, ";{")
                if end >= n:
                    break
                if skel[end] == ";":
                    sig = body[i:end + 1].strip()
                    item_end = end + 1
                else:
                    body_end = _find_matching_brace(skel, end)
                    if body_end < 0:
                        break
                    sig = body[i:end].rstrip().rstrip(",") + ";"
                    item_end = body_end + 1
                doc, attr_texts, has_test = _collect_pending_docs(
                    docs, attrs, i, prev_method_end
                )
                prev_method_end = item_end
                if has_test:
                    i = item_end
                    continue
                out.append(
                    ApiItem(
                        kind="fn",
                        name=_extract_name("fn", sig),
                        doc=doc,
                        attrs=attr_texts,
                        signature=sig,
                        src_start=offset + i,
                        src_end=offset + item_end,
                    )
                )
                i = item_end
                continue
        if c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket -= 1
        i += 1
    return out


# ── Markdown rendering ─────────────────────────────────────────────────────


def render_item_md(item: ApiItem, level: int = 2) -> str:
    hashes = "#" * level
    out: List[str] = []
    out.append(f"{hashes} {item.kind} {item.name}".rstrip())
    out.append("")
    if item.doc:
        out.append(item.doc)
        out.append("")
    code_lines: List[str] = []
    code_lines.extend(item.attrs)
    if item.signature:
        code_lines.append(item.signature)
    if code_lines:
        out.append("```noir")
        for line in code_lines:
            out.append(line)
        out.append("```")
        out.append("")
    if item.children:
        for child in item.children:
            out.append(render_item_md(child, level + 1))
    return "\n".join(out).rstrip() + "\n"


def render_file_md(rel_path: str, file_doc: str, items: List[ApiItem]) -> str:
    out: List[str] = []
    out.append(f"# {rel_path}")
    out.append("")
    if file_doc:
        out.append(file_doc)
        out.append("")
    for it in items:
        out.append(render_item_md(it, level=2))
    return "\n".join(out).rstrip() + "\n"


# ── Driver ─────────────────────────────────────────────────────────────────


@dataclass
class FileResult:
    rel_path: str
    items: List[ApiItem]
    file_doc: str
    errors: List[str]


def transform_file(path: Path, root: Path) -> FileResult:
    src = path.read_text(encoding="utf-8", errors="ignore")
    rel = str(path.relative_to(root))
    try:
        items, file_doc, errors = parse_file_items(src)
    except Exception as exc:  # noqa: BLE001
        return FileResult(rel_path=rel, items=[], file_doc="", errors=[f"parse: {exc!r}"])
    return FileResult(rel_path=rel, items=items, file_doc=file_doc, errors=errors)


def transform_tree(
    input_root: Path,
    output_root: Path,
    rel_prefix: str = "",
    exclude_paths: tuple = (),
) -> dict:
    """Walk ``input_root`` recursively, emit one `.nr.md` per `.nr`
    under ``output_root``. Returns a manifest dict.

    ``exclude_paths`` is a tuple of fnmatch patterns evaluated against
    each ``.nr`` file's path relative to ``input_root`` (forward-slash
    form). Same semantics as ``SourceTree.exclude_paths`` over in
    ``scripts/ingest/corpora.py`` — the build CLI threads the value
    through, so an apiref corpus can declare exclusions just like a
    passthrough corpus.
    """
    import fnmatch as _fn
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "input_root": str(input_root),
        "output_root": str(output_root),
        "rel_prefix": rel_prefix,
        "files_seen": 0,
        "files_emitted": 0,
        "files_with_parse_errors": 0,
        "files_with_zero_items": 0,
        "totals_by_kind": {
            "fn": 0, "struct": 0, "enum": 0, "trait": 0, "impl": 0,
            "pub_use": 0, "pub_const": 0, "pub_mod": 0, "type_alias": 0,
        },
        "files": [],
    }

    nr_files = sorted(input_root.rglob("*.nr"))
    if exclude_paths:
        nr_files = [
            f for f in nr_files
            if not any(
                _fn.fnmatch(f.relative_to(input_root).as_posix(), pat)
                for pat in exclude_paths
            )
        ]
    for nr in nr_files:
        manifest["files_seen"] += 1
        result = transform_file(nr, input_root)

        kind_counts = {k: 0 for k in manifest["totals_by_kind"]}
        for item in result.items:
            if item.kind in kind_counts:
                kind_counts[item.kind] += 1
                manifest["totals_by_kind"][item.kind] += 1
            for child in item.children:
                if child.kind in kind_counts:
                    kind_counts[child.kind] += 1
                    manifest["totals_by_kind"][child.kind] += 1

        if result.errors:
            manifest["files_with_parse_errors"] += 1
        if not result.items:
            manifest["files_with_zero_items"] += 1

        # Skip writing an empty header-only file. With apiref chunks
        # exempt from the <50 token discard, an empty file would
        # otherwise embed a useless heading-only chunk for every
        # `*/test*.nr` and macro-fixture `.nr` (codex review caught
        # this — apiref exemption + emit-empty = ingest noise).
        emit = bool(result.items) or bool(result.file_doc.strip())

        manifest["files"].append({
            "path": result.rel_path,
            "items": kind_counts,
            "errors": result.errors,
            "emitted": emit,
        })

        if not emit:
            continue

        rel_with_prefix = (
            f"{rel_prefix.rstrip('/')}/{result.rel_path}"
            if rel_prefix else result.rel_path
        )
        out_path = output_root / (result.rel_path + ".md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            render_file_md(rel_with_prefix, result.file_doc, result.items),
            encoding="utf-8",
        )
        manifest["files_emitted"] += 1

    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Transform a tree of Noir `.nr` files into a Markdown API-reference."
    )
    parser.add_argument("--input", required=True, help="Root directory of .nr files")
    parser.add_argument("--output", required=True, help="Output directory for .nr.md files")
    parser.add_argument(
        "--rel-prefix",
        default="",
        help="Prefix prepended to file-header path (e.g. 'aztec-nr')",
    )
    parser.add_argument(
        "--manifest",
        help="Path to write the JSON coverage manifest (default: <output>/_manifest.json)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    inp = Path(args.input).resolve()
    out = Path(args.output).resolve()
    if not inp.is_dir():
        print(f"ERROR: input is not a directory: {inp}", file=sys.stderr)
        return 2

    manifest = transform_tree(inp, out, rel_prefix=args.rel_prefix)

    manifest_path = (
        Path(args.manifest).resolve()
        if args.manifest
        else out / "_manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(
        f"apiref transform: {manifest['files_emitted']}/{manifest['files_seen']} "
        f"files emitted, {manifest['files_with_parse_errors']} parse errors, "
        f"{manifest['files_with_zero_items']} files with zero public items"
    )
    print(
        "  by kind: " + ", ".join(
            f"{k}={v}" for k, v in manifest["totals_by_kind"].items() if v
        )
    )
    print(f"manifest: {manifest_path}")

    if manifest["files_seen"] > 0 and manifest["files_with_zero_items"] == manifest["files_seen"]:
        print("ERROR: zero items extracted from every file — parser is broken", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

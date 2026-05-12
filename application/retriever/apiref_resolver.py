"""Scoped reference resolver for identifier queries.

This module is the implementation of the design Codex recommended after
the first apiref-promotion prototype landed at 16/25 on the eval (vs
20/25 baseline). The first prototype optimized "some apiref first"; this
one optimizes "the RIGHT reference first" by requiring a scoped exact
match before pinning anything.

Pipeline (called once per ``/stream`` or ``/api/search`` request):

  1. Extract candidate identifiers from the question text.
  2. Infer which corpus family the user is asking about
     (typescript-api/, aztec-nr/, noir-stdlib/, …).
  3. Run a SINGLE SQL query that:
       - filters apiref-shaped chunks (``metadata.chunk_type='apiref'``)
       - intersects with the inferred source family
       - requires a *definition-shape* substring match (``pub fn X(``,
         ``struct X``, ``trait X``, ``impl X``) — NOT a mere mention,
         which is what plagued the first prototype where usage examples
         outranked the actual definition file
       - re-ranks the surviving rows by cosine distance to the query
  4. Return the best ``(Document, distance)`` pair, or ``None`` if no
     scoped exact match was found.

The caller pins this chunk at position 0 of its result list. If the
resolver returns ``None`` the caller falls back to standard retrieval
WITHOUT any promotion — that's the key difference from the previous
"always promote some apiref" prototype.

Why families: a TypeScript question like "How do I create a PXE client
with aztec.js?" must NOT pin a Noir apiref chunk just because some
sibling file has "create" in it. Family scoping makes the SQL return
[] for that question and the resolver returns ``None``.

Why definition-shape: the eval failures we wanted to fix
(``apiref-create-note``, ``apiref-private-context``, etc.) all have
the canonical definition file indexed but losing to verbose USAGE
sites on raw cosine. Definition-shape patterns are syntactic markers
that only appear in the file that defines the symbol.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, List, Sequence, Tuple

logger = logging.getLogger(__name__)


# ── identifier extraction ────────────────────────────────────────────────────

# ``module::sub::name`` patterns. Captures the full path so we can also
# match ``std::hash::poseidon2`` as a definition target.
_PATH_IDENT_RE = re.compile(r"\b([a-zA-Z_][\w]*(?:::[a-zA-Z_][\w]*){1,})\b")

# ``snake_case`` identifiers — at least two ``_``-separated lowercase
# segments, leading char alphabetic. Excludes pure numbers and one-word
# names (which are too generic to be useful pinning signals).
_SNAKE_IDENT_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,})\b")

# ``PascalCase`` type names — at least two capital-led segments. Filters
# out common English ``Title-Case`` words like "How", "What" by
# requiring a lowercase letter followed by another uppercase letter
# (i.e. "InternalCaps"), which is how Aztec/Noir types are written.
_PASCAL_IDENT_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]*[a-z][A-Z][a-zA-Z0-9]*)\b")

# Words that look PascalCase to the eye but are ALL-CAPS acronyms
# common in this domain (PXE, JWT, SQL, MCP, RPC). We promote single
# ALL-CAPS tokens of length 3-5 as candidates too, since users
# frequently capitalize a single acronym they want apiref about.
_ACRONYM_IDENT_RE = re.compile(r"\b([A-Z]{3,5})\b")

# Common English words that we never want as identifier signals even
# if a regex would otherwise match. Most of the snake-case false
# positives in concept questions ("note_delivery_modes" etc.) come
# from query phrasing, not real identifier mentions — but those ARE
# legit identifiers in the corpus, so the filter list is short.
_IDENT_DENYLIST = frozenset(
    {
        # bare English (rare; the regexes filter most of these by shape)
        "API",
        "TLS",
    }
)

# Minimum length for leaf-stripped identifiers (e.g. ``at`` from
# ``Map::at``). 2- and 3-char leaves are overwhelmingly generic and
# generate false-positive defn matches in unrelated apiref files. We
# still try the full path (``Map::at``) as an identifier.
_MIN_LEAF_LEN = 4


def _extract_identifiers(question: str) -> List[str]:
    """Return identifier candidates from the question, longest first.

    Longest-first matters because ``std::hash::poseidon2`` is a stronger
    pinning signal than ``poseidon2`` alone; the resolver tries the full
    path before the leaf segment.
    """
    if not isinstance(question, str) or not question:
        return []

    found = []
    seen = set()
    for regex in (_PATH_IDENT_RE, _PASCAL_IDENT_RE, _SNAKE_IDENT_RE, _ACRONYM_IDENT_RE):
        for m in regex.finditer(question):
            ident = m.group(1)
            if ident in _IDENT_DENYLIST or ident in seen:
                continue
            seen.add(ident)
            found.append(ident)

    # Also add leaf segments of ``::``-paths so a question like
    # "How do I use std::hash::poseidon2?" produces both the full path
    # AND ``poseidon2``. Leaves get matched against ``fn <leaf>(`` etc.
    #
    # Skip leaves shorter than ``_MIN_LEAF_LEN``: very short names like
    # ``at`` (from ``Map::at``) match ``pub fn at(`` in many unrelated
    # apiref files (BoundedVec::at, Vec::at, FixedVec::at, …) and the
    # cosine re-rank only sometimes prefers the right one. The full
    # path identifier (``Map::at``) is still tried; we simply don't
    # blast the SQL with the over-broad leaf.
    for ident in list(found):
        if "::" in ident:
            leaf = ident.split("::")[-1]
            if (
                len(leaf) >= _MIN_LEAF_LEN
                and leaf not in seen
                and leaf not in _IDENT_DENYLIST
            ):
                seen.add(leaf)
                found.append(leaf)

    # Sort by length desc — longer identifiers carry more signal so the
    # resolver tries them first (definition-shape patterns are clearer
    # for ``poseidon2_hash_with_separator`` than for ``poseidon2``).
    found.sort(key=lambda s: (-len(s), s))
    return found


# ── source family inference ──────────────────────────────────────────────────

# Maps explicit term → list of corpus path prefixes to scope the
# apiref search to. Term match is whole-word, case-insensitive. We
# union the prefix lists across all matching terms.
_FAMILY_TERMS: Sequence[Tuple[re.Pattern, Tuple[str, ...]]] = (
    # TypeScript / aztec.js surface.
    (re.compile(r"\baztec\.?js\b", re.IGNORECASE), ("typescript-api/", "aztec.js/")),
    (re.compile(r"\bpxe(?:\s+client)?\b", re.IGNORECASE), ("typescript-api/", "aztec.js/")),
    (re.compile(r"\bwallet\s+api\b", re.IGNORECASE), ("typescript-api/", "aztec.js/")),
    (re.compile(r"\btypescript\b", re.IGNORECASE), ("typescript-api/", "aztec.js/")),
    (re.compile(r"\.ts\b"), ("typescript-api/", "aztec.js/")),
    # Aztec.nr (the framework, Noir-side).
    (re.compile(r"\baztec[- ]nr\b", re.IGNORECASE), ("aztec-nr/",)),
    (re.compile(r"\baztec\s+contract\b", re.IGNORECASE), ("aztec-nr/",)),
    (re.compile(r"\.nr\b"), ("aztec-nr/", "noir-stdlib/")),
    # Noir stdlib.
    (re.compile(r"\bnoir\s+stdlib\b", re.IGNORECASE), ("noir-stdlib/",)),
    (re.compile(r"\bstd::"), ("noir-stdlib/",)),
    (re.compile(r"\bstdlib\b", re.IGNORECASE), ("noir-stdlib/",)),
    # L1 / Solidity.
    (re.compile(r"\bl1\s+contract\b", re.IGNORECASE), ("l1-contracts/",)),
    (re.compile(r"\.sol\b"), ("l1-contracts/",)),
    (re.compile(r"\bsolidity\b", re.IGNORECASE), ("l1-contracts/",)),
)


def _infer_source_families(question: str) -> List[str]:
    """Return the corpus prefixes to scope the apiref search to.

    When the question carries an explicit family vocabulary term
    (``aztec.js``, ``.nr``, ``Solidity``, …) we scope to that family.
    Without an explicit term we default to the Noir families
    (``aztec-nr/`` + ``noir-stdlib/``) — see DEFAULT comment below.
    """
    if not isinstance(question, str):
        return []
    families: List[str] = []
    for regex, prefixes in _FAMILY_TERMS:
        if regex.search(question):
            for p in prefixes:
                if p not in families:
                    families.append(p)
    if not families:
        # DEFAULT: Noir families. Identifier queries that DON'T mention
        # an explicit non-Noir surface (``aztec.js``, ``.ts``, ``PXE``,
        # ``Solidity``, ``.sol``, …) are overwhelmingly Noir-flavored
        # against this codebase. Two eval queries that drove this
        # default in (``How do I call poseidon2_hash_with_separator?``
        # and ``What methods does PrivateContext expose for note
        # management?``) carry zero family vocabulary but clearly want
        # an aztec-nr / noir-stdlib pin. The cost of a false-positive
        # Noir scope on a true TS query is small because the SQL just
        # returns [] (no defn-shape match in aztec-nr/* for a TS-only
        # identifier), the resolver returns None, and we fall back to
        # standard retrieval — same as if no default were applied.
        return ["aztec-nr/", "noir-stdlib/"]
    return families


# ── definition-shape SQL ─────────────────────────────────────────────────────

# Definition-shape templates. For each candidate identifier ``X`` we
# build ``pub fn X(``, ``fn X(``, ``struct X``, etc. and search apiref
# chunks via ``text ILIKE ANY(...)`` (one row per template).
#
# The ``(`` after ``fn X`` and the absence of a trailing token after
# ``struct X`` / ``trait X`` / ``impl X`` are deliberate — they
# discriminate definition sites from usage sites. ``fn create_note(``
# only appears in lifecycle.nr.md; ``create_note(`` alone would also
# match every usage example.
_DEFN_TEMPLATES = (
    "pub fn {ident}(",
    "fn {ident}(",
    "pub struct {ident}",
    "struct {ident}",
    "pub trait {ident}",
    "trait {ident}",
    "pub impl {ident}",
    "impl {ident}",
    # ``::``-path definitions appear in stdlib-side apiref where the
    # module path is part of the documented signature.
    "pub fn {ident}",
)


def _escape_ilike(s: str) -> str:
    """Escape SQL ``ILIKE`` metacharacters so an identifier matches
    literally rather than as a wildcard.

    Postgres ``ILIKE`` treats ``%`` as multi-char and ``_`` as one-char
    wildcards, with backslash as the default escape character.
    Identifiers like ``poseidon2_hash_with_separator`` contain
    underscores; without escaping, the resolver would accept
    ``poseidon2Xhash_with_separator`` as a defn match, loosening the
    "scoped EXACT" guarantee. We rely on the Postgres default escape
    character so no ``ESCAPE`` clause is needed at the SQL site.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _defn_like_patterns(identifiers: Iterable[str]) -> List[str]:
    """Expand identifiers × templates into ``ILIKE`` substring patterns.

    Returned strings are SQL parameter values for ``ANY(%s)``, not raw
    SQL — so caller passes the list as a bound array, no injection
    surface. Each ``{ident}`` is also wrapped in ``%`` on each side so
    the substring can appear anywhere in the chunk text. The identifier
    body is escaped via :func:`_escape_ilike` so ``_`` and ``%`` inside
    snake_case identifiers don't act as wildcards; the SQL pairs each
    ``ILIKE`` with ``ESCAPE '\\\\'``.

    Short leaves (``len < _MIN_LEAF_LEN``) are skipped for the same
    reason ``_extract_identifiers`` skips them: ``pub fn at(`` matches
    too many unrelated apiref files. The full ``::``-path identifier is
    still used as-is so we don't kill stdlib queries like
    ``std::hash::poseidon2`` whose leaf (``poseidon2``) is fine.
    """
    out: List[str] = []
    seen = set()
    for ident in identifiers:
        # Try the full ``::``-path verbatim first. Apiref content rarely
        # uses ``pub fn Module::name`` syntax in aztec-nr (the apiref
        # transform splits ``impl Module`` and ``fn name`` into separate
        # blocks), but stdlib summaries do include the full path in some
        # signatures, so it's worth trying.
        if "::" in ident:
            for template in _DEFN_TEMPLATES:
                pat = "%" + _escape_ilike(template.format(ident=ident)) + "%"
                if pat in seen:
                    continue
                seen.add(pat)
                out.append(pat)
        # Then templates against the bare leaf, but only if it's long
        # enough to discriminate. For ``Map::at`` the leaf ``at`` is
        # too generic and matches ``pub fn at(`` in many state-var
        # apiref files; that exact problem caused a regression on the
        # ``apiref-aztec-storage-map`` eval query.
        leaf = ident.split("::")[-1] if "::" in ident else ident
        if len(leaf) < _MIN_LEAF_LEN:
            continue
        for template in _DEFN_TEMPLATES:
            pat = "%" + _escape_ilike(template.format(ident=leaf)) + "%"
            if pat in seen:
                continue
            seen.add(pat)
            out.append(pat)
    return out


def _family_like_patterns(families: Iterable[str]) -> List[str]:
    """Expand corpus prefixes to ``ILIKE`` patterns on ``metadata.source``."""
    return [p + "%" for p in families]


# ── public entry point ───────────────────────────────────────────────────────


def resolve_canonical_apiref(
    docsearch,
    question: str,
    query_vector: List[float],
    source_ids: Sequence[str],
):
    """Return the best apiref chunk that DEFINES an identifier from the
    question, scoped to the inferred source family. ``None`` if no
    confident match.

    The match is intentionally narrow:

    * The chunk must be ``chunk_type='apiref'``.
    * The chunk's source path must start with a family prefix matched
      against the question vocabulary.
    * The chunk text must contain a *definition-shape* substring for at
      least one extracted identifier (``pub fn create_note(`` etc.) —
      mere usage doesn't count.

    Among rows that pass those filters, we re-rank by cosine distance to
    the question vector and return the closest one. If nothing passes,
    we return ``None`` — the caller MUST fall back to standard
    retrieval rather than substituting anything.

    Returns
    -------
    Tuple[Document, float] | None
        ``(doc, cosine_distance)`` for the pinned chunk, or ``None``.
        ``doc`` is the same shape returned by
        ``search_by_vector_with_score`` so callers can mix it into the
        normal pipeline transparently.
    """
    if not question or not query_vector or not source_ids:
        return None

    identifiers = _extract_identifiers(question)
    if not identifiers:
        logger.debug("apiref_resolver: no identifiers in question")
        return None

    families = _infer_source_families(question)
    if not families:
        logger.debug(
            "apiref_resolver: identifiers=%s but no source family signal",
            identifiers[:5],
        )
        return None

    defn_patterns = _defn_like_patterns(identifiers)
    if not defn_patterns:
        return None
    family_patterns = _family_like_patterns(families)

    # We piggyback on PGVectorStore's connection + register_vector by
    # going through its private accessors. The alternative (a new public
    # method on PGVectorStore) is doable but pollutes the vectorstore
    # API for one caller — keeping the SQL local to the resolver makes
    # it easy to delete this whole module if the experiment doesn't pan
    # out, without touching the vectorstore interface.
    try:
        conn = docsearch._get_connection()
    except Exception:
        logger.exception("apiref_resolver: could not open DB connection")
        return None

    cursor = conn.cursor()
    cleaned_sources = [str(s).strip() for s in source_ids if s and str(s).strip()]
    if not cleaned_sources:
        cursor.close()
        return None

    try:
        # Hardcoded column/metadata-key names so callers don't have to
        # forward the vectorstore's templated names; if those ever
        # diverge from defaults this resolver will skip silently rather
        # than corrupt a query.
        sql = """
        SELECT text, metadata, embedding <=> %s::vector AS distance, source_id
        FROM documents
        WHERE source_id = ANY(%s)
          AND (metadata ->> 'chunk_type') = 'apiref'
          AND (metadata ->> 'source') ILIKE ANY(%s)
          AND text ILIKE ANY(%s)
        ORDER BY embedding <=> %s::vector, source_id
        LIMIT 1;
        """
        cursor.execute(
            sql,
            (
                query_vector,
                cleaned_sources,
                family_patterns,
                defn_patterns,
                query_vector,
            ),
        )
        row = cursor.fetchone()
    except Exception:
        # Resolver failures must NOT poison the request — fall back to
        # standard retrieval. Log at warning level (not error) since
        # this is a feature-gating path, not the critical retrieval
        # call. The caller still has the global pass to work with.
        logger.warning(
            "apiref_resolver: SQL failed, falling back to standard retrieval",
            exc_info=True,
        )
        cursor.close()
        return None
    finally:
        if not cursor.closed:
            cursor.close()

    if row is None:
        logger.debug(
            "apiref_resolver: no defn-shape match for identifiers=%s families=%s",
            identifiers[:5],
            families,
        )
        return None

    text, metadata, distance, source_id = row
    md = dict(metadata or {})
    md["_source_id"] = source_id
    # Import lazily — avoids a top-level import cycle if anything
    # along the way pulls in retriever modules.
    from application.vectorstore.document_class import Document

    doc = Document(page_content=text, metadata=md)
    logger.info(
        "apiref_resolver: pinned %s (distance=%.4f) for identifiers=%s family=%s",
        md.get("source", "?"),
        float(distance),
        identifiers[:3],
        families,
    )
    return doc, float(distance)

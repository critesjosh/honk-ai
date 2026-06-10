import datetime
import hashlib
import json
import logging
import queue
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

from flask import jsonify, make_response, Response
from flask_restx import Namespace

from application.api.answer.routes.aztec_doc_slugs import apply_slug_override
from application.api.answer.services.continuation_service import ContinuationService
from application.api.answer.services.conversation_service import ConversationService
from application.core.model_utils import (
    get_api_key_for_provider,
    get_default_model_id,
    get_provider_from_model_id,
)

from application.core.settings import settings
from application.error import sanitize_api_error
from application.llm.llm_creator import LLMCreator
from application.storage.db.repositories.agents import AgentsRepository
from application.storage.db.repositories.token_usage import TokenUsageRepository
from application.storage.db.repositories.user_logs import UserLogsRepository
from application.storage.db.session import db_readonly, db_session
from application.utils import check_required_fields

logger = logging.getLogger(__name__)


answer_ns = Namespace("answer", description="Answer related operations", path="/")


_HEARTBEAT_INTERVAL_SECONDS = 15.0

# Cap sources emitted to the client. The React widget already shows 3 with
# a "+ N more" toggle, and long source lists push the answer off-screen.
_MAX_SOURCES_EMITTED = 10

# Tighter cap for the ``fail_open`` strategy. fail_open means we have NO
# grounding signal from the model — no trailing marker AND no inline filename
# the fallback could match — so the source list is "whatever was retrieved",
# not "what the answer used". Showing the full top-10 there overstates
# confidence on the ~7% of answers that hit this path, so we cap to the top
# few. The pre-cap retrieval count is preserved in the audit as
# ``available_count``.
_FAIL_OPEN_MAX_SOURCES = 3

# ---- Source URL mapping (Aztec fork) ------------------------------------
# Corpus paths stored in `metadata.source` are relative to the ingest zip.
# Map them to public URLs:
#   - Developer Docs (rendered) → docs.aztec.network
#   - Everything else (code, non-developer-docs markdown) → GitHub at v4.3.0
# Widget renders `source.source` as the <a href>, so we rewrite that field
# in-place before emitting.
_AZTEC_DOCS_BASE = "https://docs.aztec.network/developers/docs"
# Top-level developer docs (overview, ai_tooling, getting_started_*) live
# directly under /developers/ on the rendered site, NOT /developers/docs/.
_AZTEC_DEV_TOP_BASE = "https://docs.aztec.network/developers"
# Network docs (sequencer/prover/operator content) are rendered under
# /operate/ on the site even though the corpus prefix is `operators/`.
_AZTEC_OPERATE_BASE = "https://docs.aztec.network/operate"
# Unversioned site-root pages from aztec-packages' ``docs/docs/`` —
# e.g. ``networks.md`` rendered at /networks. Lives in the
# ``aztec_site_networks`` corpus (zip prefix ``aztec-site/``).
_AZTEC_SITE_BASE = "https://docs.aztec.network"
# Unversioned "Participate" docs (``docs-participate/``) — educational
# governance/staking content rendered at /participate/<rest> on the site
# (Docusaurus instance ``routeBasePath: "participate"``). Lives in the
# ``aztec_participate_docs`` corpus (zip prefix ``aztec-participate/``).
_AZTEC_PARTICIPATE_BASE = "https://docs.aztec.network/participate"
_AZTEC_GITHUB_BASE = (
    "https://github.com/AztecProtocol/aztec-packages/blob/v4.3.0"
)
# Rendered Noir language docs — point at the canonical site rather than
# the GitHub source. Stripping the markdown extension matches the
# Docusaurus URL scheme; trailing /index segments are also stripped.
_NOIR_DOCS_BASE = "https://noir-lang.org/docs"
# Noir is a separate repo; aztec-packages v4.3.0 pins it at this commit via
# the noir/noir-repo submodule. Used for noir-stdlib apiref source files
# (those are real .nr source code, not rendered docs). Update this commit
# when bumping Aztec versions.
_NOIR_GITHUB_BASE = (
    "https://github.com/noir-lang/noir/blob/1d9727a6e0a9df75a71bb9c87daacbe30659ba09"
)
# awesome-aztec is a moving community repo (not release-pinned), so its
# source URLs point at the GitHub blob on ``main`` rather than a tag.
# Lives in the ``awesome_aztec`` corpus (zip prefix ``awesome-aztec/``).
_AWESOME_AZTEC_GITHUB_BASE = (
    "https://github.com/AztecProtocol/awesome-aztec/blob/main"
)

# Corpus prefix → GitHub repo prefix. First match wins, so put the more
# specific prefixes before their catch-alls.
#
# Note on what is NOT here: `version-v4.3.0/` and `version-v4.3.0/operators/`
# used to fall through to GitHub at `docs/developer_versioned_docs/version-v4.3.0/`
# and `docs/network_versioned_docs/version-v4.3.0/operators/`, but those
# folders don't exist at the literal v4.3.0 tag — the tag was cut from a
# release branch before PR #23375 merged into ``next``, so at the tag
# the latest snapshot is ``version-v4.2.0-aztecnr-rc.2`` (dev) /
# ``version-v4.1.2`` (network). The docs version snapshot is taken from
# a moving branch; we ingest from ``next``-branch commits (the four-root
# "Option B" layout from PR #150). All such markdown content is rendered on
# docs.aztec.network anyway, so we route it there directly via the
# dedicated rules in `_aztec_source_url` — never via GitHub.
_SOURCE_TO_REPO_PREFIX: List[Tuple[str, str]] = [
    ("end-to-end/",               "yarn-project/end-to-end/src/"),
    ("cli/",                      "yarn-project/cli/src/"),
    ("cli-wallet/",               "yarn-project/cli-wallet/src/"),
    ("aztec.js/",                 "yarn-project/aztec.js/src/"),
    ("aztec-nr/",                 "noir-projects/aztec-nr/"),
    ("noir-contracts/",           "noir-projects/noir-contracts/contracts/"),
    ("noir-protocol-circuits/",   "noir-projects/noir-protocol-circuits/"),
    ("l1-contracts/",             "l1-contracts/"),
    # Auto-generated TypeScript API reference. The v4.3.0 tag still has
    # this under ``docs/static/typescript-api/testnet/`` — the rename to
    # ``mainnet/`` only landed on ``next`` after the tag was cut, so
    # sticking with ``testnet/`` keeps the URLs reachable at the tag.
    ("typescript-api/",           "docs/static/typescript-api/testnet/"),
]


def _strip_doc_ext(path: str) -> str:
    """Strip a markdown extension if present. Used for rendered-docs URLs."""
    for ext in (".mdx", ".md"):
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def _strip_index_suffix(path: str) -> str:
    """Docusaurus serves ``foo/index`` as ``foo`` on the rendered site —
    drop the trailing ``/index`` segment so the URL doesn't 404."""
    if path == "index":
        return ""
    if path.endswith("/index"):
        return path[: -len("/index")]
    return path


def _aztec_source_url(source_path: str) -> str:
    """Translate a corpus `metadata.source` path to a clickable public URL.

    Unknown patterns fall back to the original string; the widget will
    still render the title — just without a working href.

    Routing summary:
      * Rendered Aztec developer docs under `docs/`  → docs.aztec.network/developers/docs/<rest>
      * Top-level developer docs (overview, etc.)     → docs.aztec.network/developers/<rest>
      * Network / operator docs                       → docs.aztec.network/operate/operators/<rest>
      * Unversioned site-root pages (networks.md)    → docs.aztec.network/<rest>
      * Noir language docs + stdlib                   → github.com/noir-lang/noir at pinned commit
      * Aztec source code (TS / Sol / Noir)           → github.com/AztecProtocol/aztec-packages at v4.3.0
    """
    if not source_path or not isinstance(source_path, str):
        return source_path

    # Network / operator docs — rendered at /operate/operators/<rest> on
    # the site. The corpus path is `version-v4.3.0/operators/<rest>` per
    # the way the network docs are ingested.
    #
    # ``apply_slug_override`` swaps the final path segment when the
    # source file declares ``id:`` in its Docusaurus frontmatter (e.g.
    # ``registering-sequencer.md`` declares ``id: registering_sequencer``
    # so the site serves it at ``.../registering_sequencer``, not the
    # filename slug).
    if source_path.startswith("version-v4.3.0/operators/"):
        source_no_ext = _strip_doc_ext(source_path)
        rest = source_path[len("version-v4.3.0/"):]  # keep "operators/<rest>"
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        rest = apply_slug_override(rest, source_no_ext)
        return f"{_AZTEC_OPERATE_BASE}/{rest}".rstrip("/")

    # Rendered Aztec developer docs — files under `docs/` subfolder.
    if source_path.startswith("version-v4.3.0/docs/"):
        source_no_ext = _strip_doc_ext(source_path)
        rest = source_path[len("version-v4.3.0/docs/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        rest = apply_slug_override(rest, source_no_ext)
        return f"{_AZTEC_DOCS_BASE}/{rest}".rstrip("/")

    # Top-level developer docs (overview, ai_tooling, getting_started_*)
    # — these live directly under `version-v4.3.0/<file>.md` in the
    # corpus and are rendered at /developers/<filename> on the site, NOT
    # under /developers/docs/.
    #
    # Gap: the *network* corpus also has a top-level ``reference/``
    # sibling to ``operators/`` (at v4.3.0 it contains only
    # ``changelog/*`` which we filter out via ``exclude_paths``), so no
    # chunks reach the rewriter from there in the current corpus shape.
    # If a future network release puts non-changelog content under
    # ``reference/``, this catch-all would misroute it to
    # ``/developers/reference/<rest>``. Add a dedicated
    # ``version-vX.Y.Z/reference/`` → ``/operate/reference/`` case
    # before this block if/when that happens.
    if source_path.startswith("version-v4.3.0/"):
        source_no_ext = _strip_doc_ext(source_path)
        rest = source_path[len("version-v4.3.0/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        rest = apply_slug_override(rest, source_no_ext)
        return f"{_AZTEC_DEV_TOP_BASE}/{rest}".rstrip("/")

    # Unversioned aztec-packages ``docs/docs/`` pages — rendered at
    # the site root (e.g. ``aztec-site/networks.md`` →
    # ``docs.aztec.network/networks``). Same Docusaurus URL scheme as
    # the developer docs above.
    if source_path.startswith("aztec-site/"):
        rest = source_path[len("aztec-site/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        return f"{_AZTEC_SITE_BASE}/{rest}".rstrip("/")

    # Unversioned "Participate" docs (``aztec-participate/...``) — rendered
    # at /participate/<rest> on the site. Same Docusaurus URL scheme as the
    # site-root pages above (strip extension + trailing /index), with
    # ``apply_slug_override`` for files that declare an ``id:`` in their
    # frontmatter.
    if source_path.startswith("aztec-participate/"):
        source_no_ext = _strip_doc_ext(source_path)
        rest = source_path[len("aztec-participate/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        rest = apply_slug_override(rest, source_no_ext)
        return f"{_AZTEC_PARTICIPATE_BASE}/{rest}".rstrip("/")

    # awesome-aztec community resource list — a single README from the
    # AztecProtocol/awesome-aztec repo. Links back to the GitHub blob on
    # ``main`` (moving community repo, not a release tag).
    if source_path.startswith("awesome-aztec/"):
        rest = source_path[len("awesome-aztec/"):]
        return f"{_AWESOME_AZTEC_GITHUB_BASE}/{rest}".rstrip("/")

    # Noir language docs are rendered at noir-lang.org/docs. Strip the
    # markdown extension and any `/index` suffix to match the
    # Docusaurus URL scheme (same convention as docs.aztec.network).
    if source_path.startswith("noir-docs/"):
        rest = source_path[len("noir-docs/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        return f"{_NOIR_DOCS_BASE}/{rest}".rstrip("/")

    # Noir-stdlib stays on GitHub: those are real `.nr` source files
    # (apiref output of `noir_stdlib/src/`), not rendered docs pages.
    if source_path.startswith("noir-stdlib/"):
        rest = source_path[len("noir-stdlib/"):]
        for suffix in (".txt", ".md"):
            if rest.endswith(suffix):
                rest = rest[: -len(suffix)]
                break
        return f"{_NOIR_GITHUB_BASE}/noir_stdlib/src/{rest}"

    # Code / non-developer-docs → GitHub blob at v4.3.0
    for corpus_prefix, repo_prefix in _SOURCE_TO_REPO_PREFIX:
        if source_path.startswith(corpus_prefix):
            rest = source_path[len(corpus_prefix):]
            # Ingest pipelines append a parser-friendly extension to code
            # files so the DocsGPT parser allowlist accepts them:
            #   - body-bearing corpora (e.g. noir-contracts) append ``.txt``:
            #     ``Token.nr`` → ``Token.nr.txt``
            #   - apiref corpora (aztec-nr, noir-stdlib) append ``.md``:
            #     ``hash.nr`` → ``hash.nr.md`` (markdown rendered)
            # In both cases the canonical file the user wants linked is
            # the original ``.nr`` (or ``.ts`` / ``.sol``).
            for suffix in (".txt", ".md"):
                if rest.endswith(suffix):
                    rest = rest[: -len(suffix)]
                    break
            return f"{_AZTEC_GITHUB_BASE}/{repo_prefix}{rest}"

    return source_path


# ---- Citation marker (Aztec fork) ---------------------------------------
# The Aztec grounded prompts (aztec_4_3_0_grounded.txt and the Discord
# variant) instruct the LLM to end every answer with a machine-only
# ``[[cited: i, j, k]]`` (or ``[[cited: none]]``) marker referencing the
# 1-indexed chunk numbers in the ``{summaries}`` block. This filter
# parses that marker, strips it from both the streamed answer and the
# persisted ``response_full``, and emits a ``{type: "source"}`` SSE
# frame containing only the cited subset of retrieved docs (or omits
# the frame entirely on ``[[cited: none]]``). Fail-open: if the marker
# is missing or malformed, all retrieved docs are emitted as before, so
# a stale-prompt deploy keeps working.

# Regex anchored to end-of-string. ``\Z`` rejects mid-prose markers so
# a model that accidentally writes ``[[cited: ...]]`` in a code block
# won't be misparsed as the real marker.
_CITATION_MARKER_RE = re.compile(
    r"\s*\[\[\s*cited\s*:\s*([^\]\n]*)\]\]\s*\Z",
    re.IGNORECASE,
)

# Unanchored scrubber for INLINE markers — defense in depth against
# models that emit ``[[cited: N]]`` per paragraph / sentence instead of
# (or in addition to) the single trailing marker. The trailing-marker
# parser above is authoritative for source filtering; this regex only
# removes literal marker spans from the bytes the user sees so a
# degenerate emission loop (observed 2026-05-22: one Discord answer
# with 283 inline marker spans) can't leak through. The marker is
# machine-only by prompt contract, so unconditionally stripping every
# occurrence is information-preserving.
_INLINE_CITATION_RE = re.compile(
    r"\[\[\s*cited\s*:[^\]\n]*\]\]",
    re.IGNORECASE,
)

# Tail buffer size for streaming-answer-aware strip. Theoretical worst
# case at 12 sources, 3-char indices, max whitespace ≈ 60 chars; 128
# gives generous slack without delaying long answers materially. The
# trade-off is that very short answers (< 128 chars) are fully buffered
# and arrive after end-of-stream — acceptable for chitchat, which is
# the case this whole feature targets.
_CITATION_MARKER_MAX_LEN = 128

# Holdback window for the delta-boundary partial-marker check in
# ``_emit_answer_delta``. A marker is at most ~50 chars (12 sources,
# 3-char indices). 64 chars gives generous slack while preventing an
# unclosed ``[[`` in earlier prose from pinning the split forever.
_PARTIAL_MARKER_HOLDBACK_LEN = 64


def _scrub_inline_citation_markers(text: str) -> str:
    """Remove every ``[[cited: ...]]`` span from ``text`` regardless of
    position. Companion to :data:`_CITATION_MARKER_RE` — that regex
    parses the *authoritative* trailing marker and is intentionally
    anchored to end-of-string; this helper handles the leak case where
    the model emitted markers inline. Safe to call on any answer-side
    bytes (streamed deltas, ``pending_tail``, ``response_full``).
    """
    if not text or "[[" not in text:
        return text
    return _INLINE_CITATION_RE.sub("", text)


def _count_inline_markers(text: str) -> int:
    """Count ``[[cited: ...]]`` spans in ``text``. Used for the
    ``citation_filter.inline_markers_scrubbed`` audit field — a small
    structural signal that the scrubber actually removed something.
    Cheap (regex scan); call once at end-of-stream, never per-delta.
    """
    if not text or "[[" not in text:
        return 0
    return sum(1 for _ in _INLINE_CITATION_RE.finditer(text))


@dataclass
class CitationMarkerParse:
    """Outcome of scanning a streamed answer tail for the trailing
    ``[[cited: ...]]`` marker.

    * ``matched``: regex matched a structurally-correct marker at the
      end of ``tail``. ``False`` means no marker present → caller
      fail-opens (emit all sources, don't strip anything).
    * ``stripped_tail``: ``tail`` with the matched marker and
      surrounding whitespace removed. Equal to ``tail`` when
      ``matched=False``.
    * ``cited_indices``: 1-indexed source positions the LLM cited.
      ``None`` when matched but malformed (non-empty payload that
      parsed to zero usable integers, e.g. ``[[cited: banana]]``) —
      caller still strips the leaked marker text but fails open on
      source filtering. ``[]`` only for the explicit
      ``[[cited: none]]`` form (or the literal empty
      ``[[cited: ]]``), meaning the LLM deliberately attributed
      nothing. Otherwise a deduplicated, order-preserving list.
    * ``invalid_indices``: integers the LLM cited that fall outside
      ``[1, source_count]``. Logged as a warning.
    """

    matched: bool
    stripped_tail: str
    cited_indices: Optional[List[int]] = None
    invalid_indices: List[int] = field(default_factory=list)


def _parse_citation_marker(
    tail: str, source_count: int
) -> CitationMarkerParse:
    """Try to strip the trailing ``[[cited: ...]]`` marker from
    ``tail`` and return a parse result the caller can act on.

    See :class:`CitationMarkerParse` for the full state matrix. The
    strip is conservative: only the matched marker span (and its
    leading whitespace) is removed; everything before that span is
    returned verbatim.
    """
    match = _CITATION_MARKER_RE.search(tail)
    if not match:
        return CitationMarkerParse(matched=False, stripped_tail=tail)

    # The regex is anchored to ``\Z`` so the matched span is
    # definitively the terminal marker. Safe to strip regardless of
    # whether the payload parses cleanly — that keeps the
    # "machine-only marker never leaks to the user" contract intact
    # even on a garbled emit from the model.
    stripped = tail[: match.start()].rstrip("\n")
    raw = match.group(1).strip()
    if not raw or raw.lower() == "none":
        return CitationMarkerParse(
            matched=True, stripped_tail=stripped, cited_indices=[]
        )

    parsed: List[int] = []
    invalid: List[int] = []
    malformed_tokens = False
    seen: set = set()
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        try:
            n = int(t)
        except ValueError:
            # Non-integer token (e.g. ``banana``, ``1 2``). Flag the
            # marker as malformed; the strip still happens, but the
            # caller fails open on source filtering rather than
            # silently dropping every source.
            malformed_tokens = True
            continue
        if n in seen:
            continue
        seen.add(n)
        if 1 <= n <= source_count:
            parsed.append(n)
        else:
            invalid.append(n)
    if malformed_tokens and not parsed and not invalid:
        return CitationMarkerParse(
            matched=True, stripped_tail=stripped, cited_indices=None
        )
    return CitationMarkerParse(
        matched=True,
        stripped_tail=stripped,
        cited_indices=parsed,
        invalid_indices=invalid,
    )


def _filter_sources_by_indices(
    source_log_docs: List[Dict[str, Any]],
    cited_indices: List[int],
) -> List[Dict[str, Any]]:
    """Pick docs from the pre-dedup, pre-cap retrieval list by
    1-indexed position, preserving the LLM's citation order. The 1-index
    matches the ``# N. <filename>`` headers built in
    ``stream_processor.pre_fetch_docs`` and
    ``workflow_engine._get_source_template_data``.
    """
    if not source_log_docs:
        return []
    out: List[Dict[str, Any]] = []
    n = len(source_log_docs)
    for idx in cited_indices:
        if 1 <= idx <= n:
            out.append(source_log_docs[idx - 1])
    return out


# ---- Filename fallback (Aztec fork) -------------------------------------
# When the LLM doesn't emit the trailing ``[[cited: ...]]`` marker — common
# in long multi-turn deep-technical conversations with qwen3.6-flash, where
# the model substitutes ``Source: foo.md`` / ``[#10](foo.md)`` / inline
# `` `foo.md` `` patterns from its training priors — recover the citation
# signal by extracting filename tokens from the response and matching them
# against the retrieved chunks. Strict-improvement over the
# previously-unconditional fail-open: the marker-present path is unchanged.

# Restricted to extensions present in the Aztec corpus (see
# ``scripts/ingest/corpora.py``). Dotted package-like names that
# happen to end in an indexed extension (e.g. ``aztec.js``) WILL match
# the regex; the alias-intersection in ``_filename_fallback`` is what
# neutralises them when no retrieved doc has that filename. This
# matches the existing marker behaviour where an out-of-range cited
# index is silently dropped.
_FILENAME_RX = re.compile(
    r"\b[\w][\w./\-]*\.(?:md|mdx|nr|ts|tsx|sol|json|txt|yml|yaml|toml|sh|js|rs)\b",
    re.IGNORECASE,
)

# A basename match with this many or more candidate docs is considered
# "ambiguous" — surfaced in the audit row so operators can spot recurring
# patterns and decide whether to tighten matching. ``index.md`` is the
# canonical offender (every Docusaurus section has one).
_AMBIGUOUS_BASENAME_THRESHOLD = 3


@dataclass
class FilenameFallbackResult:
    """Outcome of the filename-extraction fallback.

    * ``kept``: docs from ``source_log_docs`` whose aliases matched the
      filenames the LLM named, in retrieval (global-rerank) order.
      ``None`` when no match could be made — caller fails open.
    * ``matched_aliases``: canonical aliases (lowercased) that produced a
      match. Audit/log signal.
    * ``ambiguous_basename_count``: count of basenames in
      ``matched_aliases`` that matched ``_AMBIGUOUS_BASENAME_THRESHOLD``
      or more docs.
    """

    kept: Optional[List[Dict[str, Any]]]
    matched_aliases: List[str] = field(default_factory=list)
    ambiguous_basename_count: int = 0


def _extract_cited_filenames(response: str) -> set:
    """Return the set of filename-like tokens the LLM named in its
    response, lowercased. Path-form (``docs/foo.md``) and bare-basename
    (``foo.md``) tokens are both returned verbatim. Callers that need
    the distinction (the fallback matcher does, to avoid pulling in
    unrelated basename-collision docs) should partition the set on
    presence of ``/``.

    Returning a single flat set keeps the public surface simple for
    tests/observability while letting downstream consumers decide how
    to use the tokens.
    """
    if not response:
        return set()
    return {token.lower() for token in _FILENAME_RX.findall(response)}


def _doc_aliases(doc: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (path_aliases, basename_aliases) for a retrieved doc,
    lowercased. ``path_aliases`` are full path-like forms (e.g.
    ``docs/aztec-nr/index.md``); ``basename_aliases`` are leaf names
    (e.g. ``index.md``). The prompt's chunk header is
    ``filename OR title OR source`` (see
    ``stream_processor.pre_fetch_docs``), so all three fields are
    candidate citation surfaces.
    """
    paths: List[str] = []
    bases: List[str] = []
    for key in ("filename", "title", "source"):
        raw = doc.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        lo = raw.lower()
        # ``source`` is often a corpus path (``version-v4.3.0/.../foo.md``)
        # before URL rewriting; treat it like a path alias.
        if "/" in lo:
            paths.append(lo)
            base = lo.rsplit("/", 1)[-1]
            if base:
                bases.append(base)
        else:
            bases.append(lo)
    return paths, bases


def _filename_fallback(
    response_full: str,
    source_log_docs: List[Dict[str, Any]],
) -> FilenameFallbackResult:
    """Try to recover a cited subset of ``source_log_docs`` from the
    filenames the LLM named inline in ``response_full``. Returns
    ``kept=None`` when no match could be made — callers fall open in
    that case (today's behaviour on a marker-less reply).
    """
    if not response_full or not source_log_docs:
        return FilenameFallbackResult(kept=None)
    named = _extract_cited_filenames(response_full)
    if not named:
        return FilenameFallbackResult(kept=None)

    path_tokens = {t for t in named if "/" in t}
    basename_tokens = {t for t in named if "/" not in t}

    # Build a basename → set-of-doc-indices map for ambiguity detection
    # and for the basename-fallback pass. Uses a set per basename so a
    # single doc that exposes the same basename via multiple aliases
    # (e.g. ``filename="index.md"`` AND ``title="index.md"``) only
    # contributes once toward the ``_AMBIGUOUS_BASENAME_THRESHOLD``.
    basename_to_indices: Dict[str, set] = {}
    doc_aliases: List[Tuple[List[str], List[str]]] = []
    for idx, doc in enumerate(source_log_docs):
        paths, bases = _doc_aliases(doc)
        doc_aliases.append((paths, bases))
        for b in bases:
            basename_to_indices.setdefault(b, set()).add(idx)

    matched_doc_indices: set = set()
    matched_aliases: set = set()
    ambiguous: set = set()

    # Pass 1: path-suffix match. Prefer specificity — if the LLM wrote
    # ``aztec-nr/index.md`` and we have a doc with that path suffix,
    # match it directly. No shadowing of bare-basename mentions: the
    # extractor returns the literal tokens the model emitted, so a
    # bare ``index.md`` in ``basename_tokens`` is something the model
    # actually wrote (not a derived form) and must still resolve.
    for token in path_tokens:
        for idx, (paths, _bases) in enumerate(doc_aliases):
            for p in paths:
                if p == token or p.endswith("/" + token):
                    matched_doc_indices.add(idx)
                    matched_aliases.add(token)
                    break

    # Pass 2: basename match for bare-basename mentions. Counts as
    # ambiguous when the basename appears on
    # ``_AMBIGUOUS_BASENAME_THRESHOLD`` distinct docs or more — still
    # keeps all candidates but flags it for operator awareness.
    for token in basename_tokens:
        candidates = basename_to_indices.get(token)
        if not candidates:
            continue
        matched_aliases.add(token)
        if len(candidates) >= _AMBIGUOUS_BASENAME_THRESHOLD:
            ambiguous.add(token)
        for idx in candidates:
            matched_doc_indices.add(idx)

    if not matched_doc_indices:
        return FilenameFallbackResult(kept=None)

    kept = [
        source_log_docs[i]
        for i in sorted(matched_doc_indices)  # retrieval order
    ]
    return FilenameFallbackResult(
        kept=kept,
        matched_aliases=sorted(matched_aliases),
        ambiguous_basename_count=len(ambiguous),
    )


def _fail_open_sources(
    source_log_docs: List[Dict[str, Any]],
    *,
    marker_present: bool,
    marker_malformed: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Cap the fail-open source set and build its audit metadata.

    fail_open has no model grounding signal, so we keep only the top
    ``_FAIL_OPEN_MAX_SOURCES`` retrieved docs (rather than the full list)
    and record the pre-cap count as ``available_count``. Returns the
    capped doc list and the ``citation_filter`` meta dict.
    """
    available = len(source_log_docs)
    kept = source_log_docs[:_FAIL_OPEN_MAX_SOURCES]
    meta: Dict[str, Any] = {
        "marker_present": marker_present,
        "strategy": "fail_open",
        "cited_indices": [],
        "invalid_indices": [],
        "filtered_count": len(kept),
        "available_count": available,
    }
    if marker_malformed:
        meta["marker_malformed"] = True
    return kept, meta


def _build_source_frame(
    source_log_docs: List[Dict[str, Any]],
) -> Optional[str]:
    """Render the truncated/rewritten/deduped/capped source list as a
    ready-to-yield SSE frame, or ``None`` when no sources survive
    transformation.

    This is the same shape that used to live inline in
    ``complete_stream`` — extracted so the citation filter can apply
    the rewrite *after* index-based filtering against the raw retrieval
    list (avoiding the index-vs-truncated mismatch flagged by Codex).
    """
    if not source_log_docs:
        return None
    truncated_sources: List[Dict[str, Any]] = []
    seen_urls: set = set()
    for source in source_log_docs:
        if len(truncated_sources) >= _MAX_SOURCES_EMITTED:
            break
        truncated_source = source.copy()
        if "text" in truncated_source:
            truncated_source["text"] = (
                truncated_source["text"][:100].strip() + "..."
            )
        raw_path = truncated_source.get("source")
        if raw_path:
            public_url = _aztec_source_url(raw_path)
            if public_url in seen_urls:
                continue
            seen_urls.add(public_url)
            truncated_source["source"] = public_url
        truncated_sources.append(truncated_source)
    if not truncated_sources:
        return None
    return json.dumps({"type": "source", "source": truncated_sources})


def _iter_with_heartbeat(
    source_iter: Iterable[Any],
    interval: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> Generator[Tuple[str, Any], None, None]:
    """Wrap a blocking iterator, inserting a heartbeat sentinel on silence.

    A single producer thread consumes ``source_iter`` and pushes items into
    a queue. The consumer pulls with a timeout; if the source has been
    silent for ``interval`` seconds it yields ``("heartbeat", None)``.
    Real items are yielded as ``("item", value)``; errors from the
    producer are re-raised on the consumer.

    Cancellation: when the consumer goes away (client disconnect closes
    this generator, raising GeneratorExit here), the ``finally`` block
    sets ``stop`` so the producer never blocks forever on a full queue,
    and closes ``source_iter`` so the agent generator's own ``finally``
    blocks run instead of silently draining the LLM to completion.

    The producer thread must not touch Flask request context.
    """
    q: "queue.Queue[Tuple[str, Any]]" = queue.Queue(maxsize=64)
    stop = threading.Event()

    def _close_source() -> None:
        close = getattr(source_iter, "close", None)
        if callable(close):
            try:
                close()
            except ValueError:
                # Generator is mid-``next()`` on the other thread; whichever
                # side observes ``stop`` after it resolves will close it.
                pass
            except Exception:
                logger.warning("Error closing SSE source iterator", exc_info=True)

    def _put(msg: Tuple[str, Any]) -> bool:
        """Blocking put that gives up once the consumer has gone away."""
        while not stop.is_set():
            try:
                q.put(msg, timeout=0.25)
                return True
            except queue.Full:
                continue
        return False

    def _producer() -> None:
        it = iter(source_iter)
        try:
            # Re-check ``stop`` before every advance so a disconnect
            # observed mid-stream stops draining the LLM.
            while not stop.is_set():
                try:
                    item = next(it)
                except StopIteration:
                    _put(("done", None))
                    return
                if not _put(("item", item)):
                    return
        except BaseException as exc:  # noqa: BLE001 — re-raised on consumer
            if not _put(("error", exc)):
                logger.warning(
                    "SSE producer raised after consumer disconnect", exc_info=exc
                )
        finally:
            if stop.is_set():
                _close_source()

    t = threading.Thread(target=_producer, daemon=True, name="sse-producer")
    t.start()

    try:
        while True:
            try:
                kind, payload = q.get(timeout=interval)
            except queue.Empty:
                yield ("heartbeat", None)
                continue
            if kind == "item":
                yield ("item", payload)
            elif kind == "done":
                return
            elif kind == "error":
                raise payload  # type: ignore[misc]
    finally:
        # Runs on GeneratorExit (client disconnect) as well as normal
        # completion: unblock the producer, then close the source so its
        # finally blocks run (no-op if already exhausted).
        stop.set()
        _close_source()


def _build_usage_frame(agent: Any, model_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Build the SSE ``usage`` payload from the agent's LLM tally.

    The ``gen_token_usage`` / ``stream_token_usage`` decorators in
    ``application/usage.py`` accumulate tiktoken-estimated prompt and
    generated tokens on ``llm.token_usage`` over the lifetime of the
    request. By the time we call this (after the streaming generator
    has fully drained — see the ordering at the ``id`` emit sites),
    the tally is final for the request.

    Returns ``None`` when no usable LLM tally exists (e.g. a test
    double agent without a real ``llm.token_usage``). Caller skips the
    emit in that case so unknown-frame defensiveness in clients isn't
    exercised on every test path.

    Token counts here are estimates (``cl100k_base``), not provider-
    reported usage. Bot-side billing layered on top of this frame
    should over-estimate $/token to compensate for tokenizer drift
    against non-OpenAI models like Qwen.
    """
    llm = getattr(agent, "llm", None)
    token_usage = getattr(llm, "token_usage", None) if llm is not None else None
    if not isinstance(token_usage, dict):
        return None
    prompt_tokens = token_usage.get("prompt_tokens")
    generated_tokens = token_usage.get("generated_tokens")
    if not isinstance(prompt_tokens, int) or not isinstance(generated_tokens, int):
        return None
    return {
        "type": "usage",
        "prompt_tokens": prompt_tokens,
        "generated_tokens": generated_tokens,
        "model_id": model_id,
    }


class BaseAnswerResource:
    """Shared base class for answer endpoints"""

    def __init__(self):
        self.default_model_id = get_default_model_id()
        self.conversation_service = ConversationService()

    def validate_request(
        self, data: Dict[str, Any], require_conversation_id: bool = False
    ) -> Optional[Response]:
        """Common request validation.

        Continuation requests (``tool_actions`` present) require
        ``conversation_id`` but not ``question``.
        """
        if data.get("tool_actions"):
            # Continuation mode — question is not required
            if missing := check_required_fields(data, ["conversation_id"]):
                return missing
            return None
        required_fields = ["question"]
        if require_conversation_id:
            required_fields.append("conversation_id")
        if missing_fields := check_required_fields(data, required_fields):
            return missing_fields
        return None

    @staticmethod
    def _prepare_tool_calls_for_logging(
        tool_calls: Optional[List[Dict[str, Any]]], max_chars: int = 10000
    ) -> List[Dict[str, Any]]:
        if not tool_calls:
            return []

        prepared = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                prepared.append({"result": str(tool_call)[:max_chars]})
                continue

            item = dict(tool_call)
            for key in ("result", "result_full"):
                value = item.get(key)
                if isinstance(value, str) and len(value) > max_chars:
                    item[key] = value[:max_chars]
            prepared.append(item)
        return prepared

    def check_usage(self, agent_config: Dict) -> Optional[Response]:
        """Check if there is a usage limit and if it is exceeded

        Args:
            agent_config: The config dict of agent instance

        Returns:
            None or Response if either of limits exceeded.

        """
        api_key = agent_config.get("user_api_key")
        if not api_key:
            return None
        # One read-only connection serves both the agent lookup and the
        # usage queries — opening a second connection per request was
        # pure churn on the hot /stream path.
        with db_readonly() as conn:
            agent = AgentsRepository(conn).find_by_key(api_key)
            if not agent:
                return make_response(
                    jsonify({"success": False, "message": "Invalid API key."}), 401
                )
            limited_token_mode_raw = agent.get("limited_token_mode", False)
            limited_request_mode_raw = agent.get("limited_request_mode", False)

            limited_token_mode = (
                limited_token_mode_raw
                if isinstance(limited_token_mode_raw, bool)
                else limited_token_mode_raw == "True"
            )
            limited_request_mode = (
                limited_request_mode_raw
                if isinstance(limited_request_mode_raw, bool)
                else limited_request_mode_raw == "True"
            )

            # No limits configured — skip the usage queries entirely.
            if not limited_token_mode and not limited_request_mode:
                return None

            token_limit = int(
                agent.get("token_limit") or settings.DEFAULT_AGENT_LIMITS["token_limit"]
            )
            request_limit = int(
                agent.get("request_limit")
                or settings.DEFAULT_AGENT_LIMITS["request_limit"]
            )

            end_date = datetime.datetime.now(datetime.timezone.utc)
            start_date = end_date - datetime.timedelta(hours=24)

            token_repo = TokenUsageRepository(conn)
            if limited_token_mode:
                daily_token_usage = token_repo.sum_tokens_in_range(
                    start=start_date, end=end_date, api_key=api_key,
                )
            else:
                daily_token_usage = 0
            if limited_request_mode:
                daily_request_usage = token_repo.count_in_range(
                    start=start_date, end=end_date, api_key=api_key,
                )
            else:
                daily_request_usage = 0
        token_exceeded = (
            limited_token_mode and token_limit > 0 and daily_token_usage >= token_limit
        )
        request_exceeded = (
            limited_request_mode
            and request_limit > 0
            and daily_request_usage >= request_limit
        )

        if token_exceeded or request_exceeded:
            return make_response(
                jsonify(
                    {
                        "success": False,
                        "message": "Exceeding usage limit, please try again later.",
                    }
                ),
                429,
            )
        return None

    def complete_stream(
        self,
        question: str,
        agent: Any,
        conversation_id: Optional[str],
        user_api_key: Optional[str],
        decoded_token: Dict[str, Any],
        isNoneDoc: bool = False,
        index: Optional[int] = None,
        should_save_conversation: bool = True,
        attachment_ids: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
        is_shared_usage: bool = False,
        shared_token: Optional[str] = None,
        model_id: Optional[str] = None,
        _continuation: Optional[Dict] = None,
    ) -> Generator[str, None, None]:
        """
        Generator function that streams the complete conversation response.

        Args:
            question: The user's question
            agent: The agent instance
            retriever: The retriever instance
            conversation_id: Existing conversation ID
            user_api_key: User's API key if any
            decoded_token: Decoded JWT token
            isNoneDoc: Flag for document-less responses
            index: Index of message to update
            should_save_conversation: Whether to persist the conversation
            attachment_ids: List of attachment IDs
            agent_id: ID of agent used
            is_shared_usage: Flag for shared agent usage
            shared_token: Token for shared agent
            model_id: Model ID used for the request
            retrieved_docs: Pre-fetched documents for sources (optional)

        Yields:
            Server-sent event strings
        """
        request_started_at = datetime.datetime.now(datetime.timezone.utc)
        user_id_for_log = decoded_token.get("sub") if decoded_token else None
        # Hash the question prompt so we can correlate start/end log lines
        # for one request without writing the end-user's text to INFO logs.
        # Widget/Discord users paste addresses, ops context, and occasionally
        # bearer tokens into prompts — keep that out of stdout/journald.
        question_hash = (
            hashlib.sha256((question or "").encode("utf-8")).hexdigest()[:12]
            if question
            else None
        )
        api_key_hash = (
            hashlib.sha256(user_api_key.encode("utf-8")).hexdigest()[:16]
            if user_api_key
            else None
        )
        logger.info(
            "stream.start user=%s agent_id=%s conversation_id=%s "
            "model_id=%s save=%s question_len=%d question_hash=%s",
            user_id_for_log,
            agent_id,
            conversation_id,
            model_id or self.default_model_id,
            should_save_conversation,
            len(question or ""),
            question_hash,
        )
        response_full, thought, source_log_docs, tool_calls = "", "", [], []
        inband_error_payload: Optional[str] = None
        # Aztec citation-marker plumbing — see _parse_citation_marker.
        # ``pending_tail`` holds the trailing portion of the streamed
        # answer so we can scan for ``[[cited: ...]]`` at end-of-stream
        # without it leaking to the client.
        pending_tail: str = ""
        # Set to True once we've found-and-stripped a marker. ClassicAgent
        # yields terminal ``sources`` then ``tool_calls`` frames AFTER the
        # answer streams, so the first call to ``_flush_pending_tail`` (on
        # the tool_calls branch) is what actually parses the marker.
        # Without this flag we'd either double-strip on the post-loop
        # parse OR (worse) emit the marker verbatim ahead of the
        # tool_calls frame.
        marker_parsed: bool = False
        citation_filter_meta: Optional[Dict[str, Any]] = None
        # Count of INLINE ``[[cited: ...]]`` spans the model emitted
        # (excluding the legitimate trailing marker). Snapshotted just
        # before each scrub site so the audit reflects the model's
        # actual output, not the scrubbed view. Surfaces as
        # ``citation_filter.inline_markers_scrubbed`` when > 0, and
        # triggers a ``llm.marker_leak_scrubbed`` WARN log so the
        # observable signal exists for monitoring / honk-report.
        inline_markers_scrubbed: int = 0

        def _try_parse_marker_from_tail(force: bool = False) -> None:
            """Look for the trailing citation marker in ``pending_tail``.
            On match: strip it from ``pending_tail`` and from
            ``response_full``, filter ``source_log_docs`` to the cited
            subset, populate the audit metadata. No-op for structured
            agents, paused-continuation flows, or when the marker has
            already been parsed.

            Called before every flush of ``pending_tail`` (so a
            terminal ``tool_calls`` / ``thought`` frame from
            ClassicAgent — yielded AFTER the marker-bearing answer —
            doesn't leak the marker to the client) AND once at
            end-of-stream with ``force=True``.

            Defers parsing until the source frame has arrived: if a
            non-answer frame fires BEFORE ``sources`` (some workflow
            agents emit ``thought`` mid-stream), parsing against
            ``source_count=0`` would mark every valid citation as
            invalid. Skipping until sources arrive lets the
            end-of-stream pass do the right thing. ``force=True``
            overrides this at end-of-stream (last-chance parse even
            if no sources were yielded — e.g. an empty-corpus run).
            """
            nonlocal pending_tail, response_full, source_log_docs
            nonlocal citation_filter_meta, marker_parsed
            nonlocal inline_markers_scrubbed
            if marker_parsed or is_structured or paused:
                return
            if not force and not source_log_docs:
                # Wait for the source frame to arrive — otherwise
                # we'd compute ``source_count=0`` and rule every
                # citation invalid. End-of-stream flush passes
                # ``force=True`` to handle the empty-corpus case.
                return
            parse = _parse_citation_marker(pending_tail, len(source_log_docs))
            if not parse.matched:
                return
            marker_parsed = True
            pending_tail = parse.stripped_tail
            full_marker = _CITATION_MARKER_RE.search(response_full)
            if full_marker:
                response_full = response_full[: full_marker.start()].rstrip(
                    "\n"
                )
            # Belt-and-braces: scrub any INLINE markers the model
            # emitted before the trailing one. The terminal-marker
            # strip above only touches the ``\Z``-anchored match;
            # leaked inline copies (the 283-marker Discord case) are
            # caught here. Count BEFORE the scrub so the audit
            # reflects the model's actual output.
            inline_markers_scrubbed += _count_inline_markers(response_full)
            pending_tail = _scrub_inline_citation_markers(pending_tail)
            response_full = _scrub_inline_citation_markers(response_full)
            if parse.invalid_indices:
                logger.warning(
                    "llm.cited_invalid_index agent_id=%s indices=%s "
                    "available=%d",
                    agent_id,
                    parse.invalid_indices,
                    len(source_log_docs),
                )
            if parse.cited_indices is None:
                # Marker matched but payload was garbage (e.g.
                # ``[[cited: banana]]``). The marker has been stripped
                # from response_full above, so it doesn't leak to the
                # user. Try the filename fallback against the stripped
                # response before falling open.
                logger.warning(
                    "llm.cited_malformed agent_id=%s available=%d",
                    agent_id,
                    len(source_log_docs),
                )
                fallback = _filename_fallback(response_full, source_log_docs)
                if fallback.kept is not None:
                    source_log_docs = fallback.kept
                    citation_filter_meta = {
                        "marker_present": True,
                        "marker_malformed": True,
                        "strategy": "filename_fallback",
                        "matched_aliases": fallback.matched_aliases,
                        "ambiguous_basename_count": fallback.ambiguous_basename_count,
                        "cited_indices": [],
                        "invalid_indices": [],
                        "filtered_count": len(source_log_docs),
                    }
                    logger.info(
                        "llm.cited_via_filenames agent_id=%s matched=%d "
                        "filtered=%d (post-malformed)",
                        agent_id,
                        len(fallback.matched_aliases),
                        len(source_log_docs),
                    )
                else:
                    source_log_docs, citation_filter_meta = _fail_open_sources(
                        source_log_docs,
                        marker_present=True,
                        marker_malformed=True,
                    )
                return
            if parse.cited_indices:
                source_log_docs = _filter_sources_by_indices(
                    source_log_docs, parse.cited_indices
                )
                strategy = "marker"
            else:
                # Empty payload: ``[[cited: none]]`` or ``[[cited: ]]``.
                # Model deliberately attributed nothing — do NOT fall
                # through to filename extraction.
                source_log_docs = []
                strategy = "marker_none"
            citation_filter_meta = {
                "marker_present": True,
                "strategy": strategy,
                "cited_indices": list(parse.cited_indices),
                "invalid_indices": parse.invalid_indices,
                "filtered_count": len(source_log_docs),
            }

        def _emit_answer_delta(delta: str) -> Optional[str]:
            """Append ``delta`` to ``pending_tail`` and return any
            leading portion that's safely past the marker window so the
            caller can yield it as an answer SSE frame. Returns ``None``
            when the entire delta fits inside the marker window.

            Inline ``[[cited: ...]]`` markers in the released bytes are
            scrubbed before return (defense in depth — see
            :func:`_scrub_inline_citation_markers`). A partial marker
            prefix (e.g. trailing ``[[cite``) at the split point is
            held back inside ``pending_tail`` so the next delta can
            complete it; otherwise a marker straddling the split would
            leak its prefix to the client.
            """
            nonlocal pending_tail
            pending_tail += delta
            if len(pending_tail) <= _CITATION_MARKER_MAX_LEN:
                return None
            split_at = len(pending_tail) - _CITATION_MARKER_MAX_LEN
            to_emit = pending_tail[:split_at]
            # If ``to_emit`` ends with an unfinished ``[[...`` (no
            # closing ``]]`` after the last ``[[``), the marker may
            # span the boundary. Back off to before the ``[[`` so the
            # marker reassembles inside ``pending_tail`` on the next
            # delta and gets scrubbed there. ONLY when the unclosed
            # ``[[`` is within ``_PARTIAL_MARKER_HOLDBACK_LEN`` chars
            # of the split point — otherwise an unmatched ``[[`` in
            # legitimate prose / code earlier in the answer (e.g. the
            # docstring describing the marker format, or a code-block
            # example using double-bracket syntax) would pin
            # ``split_at`` at that position forever and stall the
            # stream until end-of-stream flush.
            last_open = to_emit.rfind("[[")
            if (
                last_open != -1
                and "]]" not in to_emit[last_open:]
                and split_at - last_open <= _PARTIAL_MARKER_HOLDBACK_LEN
            ):
                split_at = last_open
                to_emit = pending_tail[:split_at]
            pending_tail = pending_tail[split_at:]
            if not to_emit:
                return None
            return _scrub_inline_citation_markers(to_emit)

        def _flush_pending_tail(force_parse: bool = False) -> Optional[str]:
            """Yield-side helper — try to parse-and-strip the citation
            marker (if any), then empty ``pending_tail`` and return the
            full SSE frame for the buffered text, or ``None`` if empty.
            Used before any non-``answer`` SSE frame so the relative
            event order is preserved AND any trailing marker is
            consumed before it would leak to the client.
            ``force_parse=True`` is used at end-of-stream so the marker
            is parsed even if the source frame never arrived (e.g.
            empty-corpus run, isNoneDoc).

            ``pending_tail`` is scrubbed of inline markers before
            emission — defense in depth for the no-trailing-marker
            path (the model emitted markers inline but never a
            terminal one, so ``_try_parse_marker_from_tail`` didn't
            match and didn't scrub).
            """
            nonlocal pending_tail
            _try_parse_marker_from_tail(force=force_parse)
            if not pending_tail:
                return None
            scrubbed = _scrub_inline_citation_markers(pending_tail)
            pending_tail = ""
            if not scrubbed:
                return None
            frame_data = json.dumps({"type": "answer", "answer": scrubbed})
            return f"data: {frame_data}\n\n"

        try:
            is_structured = False
            schema_info = None
            structured_chunks = []
            query_metadata = {}
            paused = False

            if _continuation:
                gen_iter = agent.gen_continuation(
                    messages=_continuation["messages"],
                    tools_dict=_continuation["tools_dict"],
                    pending_tool_calls=_continuation["pending_tool_calls"],
                    tool_actions=_continuation["tool_actions"],
                )
            else:
                gen_iter = agent.gen(query=question)

            for kind, line in _iter_with_heartbeat(gen_iter):
                if kind == "heartbeat":
                    # SSE comment line — clients discard it. Keeps the
                    # connection active across long silent gaps (tool
                    # calls, retrieval) so Cloudflare/Caddy don't close
                    # the stream and the frontend stays responsive.
                    yield ": ping\n\n"
                    continue
                if "metadata" in line:
                    query_metadata.update(line["metadata"])
                elif "answer" in line:
                    response_full += str(line["answer"])
                    if line.get("structured"):
                        # Structured agents don't use the citation
                        # marker (the answer is a JSON object, not
                        # prose). Bypass the tail buffer entirely.
                        is_structured = True
                        schema_info = line.get("schema")
                        structured_chunks.append(line["answer"])
                    else:
                        emit = _emit_answer_delta(str(line["answer"]))
                        if emit:
                            data = json.dumps({"type": "answer", "answer": emit})
                            yield f"data: {data}\n\n"
                elif "sources" in line:
                    # Stash the raw pre-dedup/pre-cap retrieval list;
                    # filtering + transform happens at end-of-stream so
                    # the LLM's 1-indexed citation marker resolves
                    # against the SAME list the prompt was built from.
                    source_log_docs = line["sources"]
                elif "tool_calls" in line:
                    flush = _flush_pending_tail()
                    if flush:
                        yield flush
                    tool_calls = line["tool_calls"]
                    data = json.dumps({"type": "tool_calls", "tool_calls": tool_calls})
                    yield f"data: {data}\n\n"
                elif "thought" in line:
                    flush = _flush_pending_tail()
                    if flush:
                        yield flush
                    thought += line["thought"]
                    data = json.dumps({"type": "thought", "thought": line["thought"]})
                    yield f"data: {data}\n\n"
                elif "type" in line:
                    if line.get("type") == "tool_calls_pending":
                        # Save continuation state and end the stream
                        flush = _flush_pending_tail()
                        if flush:
                            yield flush
                        paused = True
                        data = json.dumps(line)
                        yield f"data: {data}\n\n"
                    elif line.get("type") == "error":
                        # An in-band error from agent.gen (workflow node,
                        # LLM call, tool exec) is a request failure even
                        # though the outer generator did not raise. Flush
                        # any buffered answer text BEFORE the error frame
                        # so the user gets every answer byte we received
                        # (the marker hadn't arrived yet, so the buffered
                        # tail is plain answer text).
                        flush = _flush_pending_tail()
                        if flush:
                            yield flush
                        sanitized_inband = sanitize_api_error(
                            line.get("error", "An error occurred")
                        )
                        inband_error_payload = sanitized_inband
                        data = json.dumps(
                            {"type": "error", "error": sanitized_inband}
                        )
                        yield f"data: {data}\n\n"
                        break
                    else:
                        flush = _flush_pending_tail()
                        if flush:
                            yield flush
                        data = json.dumps(line)
                        yield f"data: {data}\n\n"
            if inband_error_payload is not None:
                # The agent yielded a structured error event (see
                # workflow_engine.py and LLM provider error paths). Treat
                # this as a request failure: write an error-shaped
                # user_logs row, log stream.end status=error, and return
                # without emitting the success-tail id/end frames or the
                # info-level user_logs row that would otherwise mark this
                # request as successful in activity reports.
                duration_ms = int(
                    (
                        datetime.datetime.now(datetime.timezone.utc)
                        - request_started_at
                    ).total_seconds()
                    * 1000
                )
                logger.warning(
                    "stream.end status=error_inband user=%s agent_id=%s "
                    "conversation_id=%s sanitized=%r duration_ms=%d "
                    "question_hash=%s",
                    user_id_for_log,
                    agent_id,
                    conversation_id,
                    inband_error_payload,
                    duration_ms,
                    question_hash,
                )
                try:
                    with db_session() as conn:
                        UserLogsRepository(conn).insert(
                            user_id=user_id_for_log,
                            endpoint="stream_answer",
                            data={
                                "action": "stream_answer",
                                "level": "error",
                                "user": user_id_for_log,
                                "api_key_hash": api_key_hash,
                                "agent_id": agent_id,
                                "question_len": len(question or ""),
                                "question_hash": question_hash,
                                "response_len": len(response_full or ""),
                                "error_class": "InbandError",
                                "error": inband_error_payload,
                                "duration_ms": duration_ms,
                                "timestamp": datetime.datetime.now(
                                    datetime.timezone.utc
                                ),
                            },
                        )
                except Exception as log_err:
                    logger.error(
                        f"Failed to persist stream_answer in-band error log: {log_err}",
                        exc_info=True,
                    )
                return
            # ---- Aztec citation-marker tail flush + source emit -----
            # ``_flush_pending_tail`` internally calls
            # ``_try_parse_marker_from_tail`` which parses + strips the
            # ``[[cited: ...]]`` marker from ``pending_tail`` and
            # ``response_full`` and filters ``source_log_docs`` to the
            # cited subset. If a terminal ``tool_calls`` / ``thought``
            # frame from ClassicAgent already triggered the parse
            # mid-loop, ``marker_parsed`` is True and this is a no-op
            # second pass — just flushes whatever's left (usually
            # nothing) and emits the source frame from the filtered
            # list. Structured / paused paths bypass marker logic.
            # ``force_parse=True`` handles the empty-corpus case where
            # no source frame was yielded but a short answer + marker
            # is in the buffer.
            tail_frame = _flush_pending_tail(force_parse=True)
            if tail_frame:
                yield tail_frame

            if not is_structured and not paused and not marker_parsed:
                # No trailing marker. Scrub any inline markers from
                # response_full before filename fallback + persistence —
                # the prompt's "machine-only marker" contract still
                # holds even when the model didn't put one at the end.
                inline_markers_scrubbed += _count_inline_markers(response_full)
                response_full = _scrub_inline_citation_markers(response_full)
                # Try to recover the citation signal from inline
                # filename references in the answer (qwen3.6-flash
                # often uses ``Source: foo.md`` / ``[#10](foo.md)`` /
                # ``(`foo.md`)`` instead of emitting the marker).
                # Operate on response_full so filenames mentioned
                # earlier in long answers — past the 128-char
                # pending_tail window — are still visible.
                fallback = _filename_fallback(response_full, source_log_docs)
                if fallback.kept is not None:
                    source_log_docs = fallback.kept
                    citation_filter_meta = {
                        "marker_present": False,
                        "strategy": "filename_fallback",
                        "matched_aliases": fallback.matched_aliases,
                        "ambiguous_basename_count": fallback.ambiguous_basename_count,
                        "cited_indices": [],
                        "invalid_indices": [],
                        "filtered_count": len(source_log_docs),
                    }
                    logger.info(
                        "llm.cited_via_filenames agent_id=%s matched=%d "
                        "filtered=%d",
                        agent_id,
                        len(fallback.matched_aliases),
                        len(source_log_docs),
                    )
                else:
                    if source_log_docs:
                        logger.info(
                            "llm.cited_missing agent_id=%s available=%d",
                            agent_id,
                            len(source_log_docs),
                        )
                    source_log_docs, citation_filter_meta = _fail_open_sources(
                        source_log_docs,
                        marker_present=False,
                    )

            # Emit the (possibly filtered) source frame BEFORE any
            # structured_answer / id / usage / end frame so v1
            # translator's [DONE] sentinel and the widget/Discord
            # ordering assumptions still hold.
            deferred_source_frame = _build_source_frame(source_log_docs)
            if deferred_source_frame:
                yield f"data: {deferred_source_frame}\n\n"

            if is_structured and structured_chunks:
                structured_data = {
                    "type": "structured_answer",
                    "answer": response_full,
                    "structured": True,
                    "schema": schema_info,
                }
                data = json.dumps(structured_data)
                yield f"data: {data}\n\n"

            if citation_filter_meta is not None:
                # Attach the inline-marker leak count when non-zero.
                # This is the structural signal an alert / dashboard
                # can pivot on without having to grep response bodies
                # for ``[[cited:`` (which would also hit legitimate
                # markers in archived rows from before this scrub
                # landed). When > 0 we also WARN-log a single line —
                # honk-report and operators can grep for
                # ``llm.marker_leak_scrubbed`` to surface regressions.
                if inline_markers_scrubbed > 0:
                    citation_filter_meta["inline_markers_scrubbed"] = (
                        inline_markers_scrubbed
                    )
                    logger.warning(
                        "llm.marker_leak_scrubbed agent_id=%s "
                        "count=%d strategy=%s response_len=%d",
                        agent_id,
                        inline_markers_scrubbed,
                        citation_filter_meta.get("strategy"),
                        len(response_full or ""),
                    )
                # Assign rather than ``setdefault`` so an agent that
                # also yields its own ``{"metadata": {...}}`` event
                # can't preempt the audit key. The marker-driven
                # filter is authoritative for this row.
                query_metadata["citation_filter"] = citation_filter_meta

            # ---- Paused: save continuation state and end stream early ----
            if paused:
                continuation = getattr(agent, "_pending_continuation", None)
                if continuation:
                    # Ensure we have a conversation_id — create a partial
                    # conversation if this is the first turn.
                    if not conversation_id and should_save_conversation:
                        try:
                            provider = (
                                get_provider_from_model_id(model_id)
                                if model_id
                                else settings.LLM_PROVIDER
                            )
                            sys_api_key = get_api_key_for_provider(
                                provider or settings.LLM_PROVIDER
                            )
                            llm = LLMCreator.create_llm(
                                provider or settings.LLM_PROVIDER,
                                api_key=sys_api_key,
                                user_api_key=user_api_key,
                                decoded_token=decoded_token,
                                model_id=model_id,
                                agent_id=agent_id,
                            )
                            conversation_id = (
                                self.conversation_service.save_conversation(
                                    None,
                                    question,
                                    response_full,
                                    thought,
                                    source_log_docs,
                                    tool_calls,
                                    llm,
                                    model_id or self.default_model_id,
                                    decoded_token,
                                    api_key=user_api_key,
                                    agent_id=agent_id,
                                    is_shared_usage=is_shared_usage,
                                    shared_token=shared_token,
                                )
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to create conversation for continuation: {e}",
                                exc_info=True,
                            )

                    if conversation_id:
                        try:
                            cont_service = ContinuationService()
                            cont_service.save_state(
                                conversation_id=str(conversation_id),
                                user=decoded_token.get("sub", "local"),
                                messages=continuation["messages"],
                                pending_tool_calls=continuation["pending_tool_calls"],
                                tools_dict=continuation["tools_dict"],
                                tool_schemas=getattr(agent, "tools", []),
                                agent_config={
                                    "model_id": model_id or self.default_model_id,
                                    "llm_name": getattr(agent, "llm_name", settings.LLM_PROVIDER),
                                    "api_key": getattr(agent, "api_key", None),
                                    "user_api_key": user_api_key,
                                    "agent_id": agent_id,
                                    "agent_type": agent.__class__.__name__,
                                    "prompt": getattr(agent, "prompt", ""),
                                    "json_schema": getattr(agent, "json_schema", None),
                                    "retriever_config": getattr(agent, "retriever_config", None),
                                },
                                client_tools=getattr(
                                    agent.tool_executor, "client_tools", None
                                ),
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to save continuation state: {str(e)}",
                                exc_info=True,
                            )

                id_data = {"type": "id", "id": str(conversation_id)}
                data = json.dumps(id_data)
                yield f"data: {data}\n\n"

                usage_frame = _build_usage_frame(agent, model_id or self.default_model_id)
                if usage_frame is not None:
                    yield f"data: {json.dumps(usage_frame)}\n\n"

                data = json.dumps({"type": "end"})
                yield f"data: {data}\n\n"
                return

            if isNoneDoc:
                for doc in source_log_docs:
                    doc["source"] = "None"
            provider = (
                get_provider_from_model_id(model_id)
                if model_id
                else settings.LLM_PROVIDER
            )
            system_api_key = get_api_key_for_provider(provider or settings.LLM_PROVIDER)

            llm = LLMCreator.create_llm(
                provider or settings.LLM_PROVIDER,
                api_key=system_api_key,
                user_api_key=user_api_key,
                decoded_token=decoded_token,
                model_id=model_id,
                agent_id=agent_id,
            )

            if should_save_conversation:
                conversation_id = self.conversation_service.save_conversation(
                    conversation_id,
                    question,
                    response_full,
                    thought,
                    source_log_docs,
                    tool_calls,
                    llm,
                    model_id or self.default_model_id,
                    decoded_token,
                    index=index,
                    api_key=user_api_key,
                    agent_id=agent_id,
                    is_shared_usage=is_shared_usage,
                    shared_token=shared_token,
                    attachment_ids=attachment_ids,
                    metadata=query_metadata if query_metadata else None,
                )
                # Persist compression metadata/summary if it exists and wasn't saved mid-execution
                compression_meta = getattr(agent, "compression_metadata", None)
                compression_saved = getattr(agent, "compression_saved", False)
                if conversation_id and compression_meta and not compression_saved:
                    try:
                        self.conversation_service.update_compression_metadata(
                            conversation_id, compression_meta
                        )
                        self.conversation_service.append_compression_message(
                            conversation_id, compression_meta
                        )
                        agent.compression_saved = True
                        logger.info(
                            f"Persisted compression metadata for conversation {conversation_id}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to persist compression metadata: {str(e)}",
                            exc_info=True,
                        )
            else:
                conversation_id = None
            id_data = {"type": "id", "id": str(conversation_id)}
            data = json.dumps(id_data)
            yield f"data: {data}\n\n"

            usage_frame = _build_usage_frame(agent, model_id or self.default_model_id)
            if usage_frame is not None:
                yield f"data: {json.dumps(usage_frame)}\n\n"

            tool_calls_for_logging = self._prepare_tool_calls_for_logging(
                getattr(agent, "tool_calls", tool_calls) or tool_calls
            )

            log_data = {
                "action": "stream_answer",
                "level": "info",
                "user": decoded_token.get("sub"),
                "api_key": user_api_key,
                "agent_id": agent_id,
                "question": question,
                "response": response_full,
                "sources": source_log_docs,
                "tool_calls": tool_calls_for_logging,
                "attachments": attachment_ids,
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
            }
            if is_structured:
                log_data["structured_output"] = True
                if schema_info:
                    log_data["schema"] = schema_info
            # Clean up text fields to be no longer than 10000 characters

            for key, value in log_data.items():
                if isinstance(value, str) and len(value) > 10000:
                    log_data[key] = value[:10000]
            try:
                with db_session() as conn:
                    UserLogsRepository(conn).insert(
                        user_id=log_data.get("user"),
                        endpoint="stream_answer",
                        data=log_data,
                    )
            except Exception as log_err:
                logger.error(
                    f"Failed to persist stream_answer user log: {log_err}",
                    exc_info=True,
                )

            duration_ms = int(
                (
                    datetime.datetime.now(datetime.timezone.utc)
                    - request_started_at
                ).total_seconds()
                * 1000
            )
            logger.info(
                "stream.end status=ok user=%s agent_id=%s "
                "conversation_id=%s response_len=%d sources=%d "
                "tool_calls=%d duration_ms=%d question_hash=%s",
                user_id_for_log,
                agent_id,
                conversation_id,
                len(response_full),
                len(source_log_docs or []),
                len(tool_calls or []),
                duration_ms,
                question_hash,
            )

            data = json.dumps({"type": "end"})
            yield f"data: {data}\n\n"
        except GeneratorExit:
            duration_ms = int(
                (
                    datetime.datetime.now(datetime.timezone.utc)
                    - request_started_at
                ).total_seconds()
                * 1000
            )
            logger.info(
                "stream.end status=aborted user=%s agent_id=%s "
                "conversation_id=%s response_len=%d duration_ms=%d "
                "question_hash=%s",
                user_id_for_log,
                agent_id,
                conversation_id,
                len(response_full or ""),
                duration_ms,
                question_hash,
            )
            # Save partial response. Defensively strip the citation
            # marker from response_full — most aborts hit mid-stream
            # before the marker arrives so the regex won't match, but
            # if the LLM finished AND the client disconnected during
            # the final SSE flush, the marker is in ``response_full``
            # and we don't want it persisted to conversation_messages.
            partial_marker = _CITATION_MARKER_RE.search(response_full or "")
            if partial_marker:
                response_full = response_full[: partial_marker.start()].rstrip(
                    "\n"
                )
            # Same defense-in-depth scrub as the success path: a model
            # that emitted markers inline before disconnecting would
            # otherwise persist those literal spans into the DB row.
            # Count + log to match the success-path observability so
            # disconnected-client incidents show up in the same audit
            # signal (``llm.marker_leak_scrubbed`` grep, dashboards).
            abort_inline_count = _count_inline_markers(response_full or "")
            response_full = _scrub_inline_citation_markers(response_full or "")
            if abort_inline_count > 0:
                logger.warning(
                    "llm.marker_leak_scrubbed agent_id=%s count=%d "
                    "strategy=aborted response_len=%d",
                    agent_id,
                    abort_inline_count,
                    len(response_full or ""),
                )
            if should_save_conversation and response_full:
                try:
                    if isNoneDoc:
                        for doc in source_log_docs:
                            doc["source"] = "None"
                    # Resolve the provider key the same way the success
                    # path does — ``settings.API_KEY`` is NOT a provider
                    # credential in prod (it's an agent UUID), and using
                    # it here made the title-generation call inside
                    # ``save_conversation`` 401 on first-turn aborts,
                    # silently dropping the partial response.
                    provider = (
                        get_provider_from_model_id(model_id)
                        if model_id
                        else settings.LLM_PROVIDER
                    )
                    system_api_key = get_api_key_for_provider(
                        provider or settings.LLM_PROVIDER
                    )
                    llm = LLMCreator.create_llm(
                        provider or settings.LLM_PROVIDER,
                        api_key=system_api_key,
                        user_api_key=user_api_key,
                        decoded_token=decoded_token,
                        model_id=model_id,
                        agent_id=agent_id,
                    )
                    self.conversation_service.save_conversation(
                        conversation_id,
                        question,
                        response_full,
                        thought,
                        source_log_docs,
                        tool_calls,
                        llm,
                        model_id or self.default_model_id,
                        decoded_token,
                        index=index,
                        api_key=user_api_key,
                        agent_id=agent_id,
                        is_shared_usage=is_shared_usage,
                        shared_token=shared_token,
                        attachment_ids=attachment_ids,
                        metadata=query_metadata if query_metadata else None,
                    )
                    compression_meta = getattr(agent, "compression_metadata", None)
                    compression_saved = getattr(agent, "compression_saved", False)
                    if conversation_id and compression_meta and not compression_saved:
                        try:
                            self.conversation_service.update_compression_metadata(
                                conversation_id, compression_meta
                            )
                            self.conversation_service.append_compression_message(
                                conversation_id, compression_meta
                            )
                            agent.compression_saved = True
                            logger.info(
                                f"Persisted compression metadata for conversation {conversation_id} (partial stream)"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to persist compression metadata (partial stream): {str(e)}",
                                exc_info=True,
                            )
                except Exception as e:
                    logger.error(
                        f"Error saving partial response: {str(e)}", exc_info=True
                    )
            raise
        except Exception as e:
            logger.error(f"Error in stream: {str(e)}", exc_info=True)
            sanitized = sanitize_api_error(e)
            duration_ms = int(
                (
                    datetime.datetime.now(datetime.timezone.utc)
                    - request_started_at
                ).total_seconds()
                * 1000
            )
            logger.warning(
                "stream.end status=error user=%s agent_id=%s "
                "conversation_id=%s error_class=%s sanitized=%r "
                "duration_ms=%d question_hash=%s",
                user_id_for_log,
                agent_id,
                conversation_id,
                type(e).__name__,
                sanitized,
                duration_ms,
                question_hash,
            )
            try:
                with db_session() as conn:
                    UserLogsRepository(conn).insert(
                        user_id=user_id_for_log,
                        endpoint="stream_answer",
                        data={
                            "action": "stream_answer",
                            "level": "error",
                            "user": user_id_for_log,
                            "api_key_hash": api_key_hash,
                            "agent_id": agent_id,
                            "question_len": len(question or ""),
                            "question_hash": question_hash,
                            "response_len": len(response_full or ""),
                            "error_class": type(e).__name__,
                            "error": sanitized,
                            "duration_ms": duration_ms,
                            "timestamp": datetime.datetime.now(
                                datetime.timezone.utc
                            ),
                        },
                    )
            except Exception as log_err:
                logger.error(
                    f"Failed to persist stream_answer error log: {log_err}",
                    exc_info=True,
                )
            data = json.dumps(
                {
                    "type": "error",
                    "error": sanitized,
                }
            )
            yield f"data: {data}\n\n"
            return

    def process_response_stream(self, stream) -> Dict[str, Any]:
        """Process the stream response for non-streaming endpoint.

        Returns:
            Dict with keys: conversation_id, answer, sources, tool_calls,
            thought, error, and optional extra.
        """
        conversation_id = ""
        response_full = ""
        source_log_docs = []
        tool_calls = []
        thought = ""
        stream_ended = False
        is_structured = False
        schema_info = None
        pending_tool_calls = None

        for line in stream:
            event_data = line.strip()
            # SSE comment lines (": ping" heartbeats) are not events.
            if not event_data or event_data.startswith(":"):
                continue
            try:
                # removeprefix, NOT replace — a literal "data: " inside the
                # JSON payload must survive.
                event = json.loads(event_data.removeprefix("data: "))

                if event["type"] == "id":
                    conversation_id = event["id"]
                elif event["type"] == "answer":
                    response_full += event["answer"]
                elif event["type"] == "structured_answer":
                    response_full = event["answer"]
                    is_structured = True
                    schema_info = event.get("schema")
                elif event["type"] == "source":
                    source_log_docs = event["source"]
                elif event["type"] == "tool_calls":
                    tool_calls = event["tool_calls"]
                elif event["type"] == "tool_calls_pending":
                    pending_tool_calls = event.get("data", {}).get(
                        "pending_tool_calls", []
                    )
                elif event["type"] == "thought":
                    thought += event["thought"]
                elif event["type"] == "error":
                    logger.error(f"Error from stream: {event['error']}")
                    return {
                        "conversation_id": None,
                        "answer": None,
                        "sources": None,
                        "tool_calls": None,
                        "thought": None,
                        "error": event["error"],
                    }
                elif event["type"] == "end":
                    stream_ended = True
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Error parsing stream event: {e}, line: {line}")
                continue
        if not stream_ended:
            logger.error("Stream ended unexpectedly without an 'end' event.")
            return {
                "conversation_id": None,
                "answer": None,
                "sources": None,
                "tool_calls": None,
                "thought": None,
                "error": "Stream ended unexpectedly",
            }

        result: Dict[str, Any] = {
            "conversation_id": conversation_id,
            "answer": response_full,
            "sources": source_log_docs,
            "tool_calls": tool_calls,
            "thought": thought,
            "error": None,
        }

        if pending_tool_calls is not None:
            result["extra"] = {"pending_tool_calls": pending_tool_calls}

        if is_structured:
            result["extra"] = {"structured": True, "schema": schema_info}

        return result

    def error_stream_generate(self, err_response):
        data = json.dumps({"type": "error", "error": err_response})
        yield f"data: {data}\n\n"

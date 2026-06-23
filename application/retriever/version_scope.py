"""Pre-retrieval version selection + source-set narrowing for the two-version KB.

The Aztec knowledge base carries TWO doc versions at once — **v4.3.1 (mainnet)**
and **v5.0.0-rc.1 (testnet)** — inside one agent/surface (see
``PLAN-two-version-kb.md``). To keep a single answer from mixing versions we:

  1. pick an ``active_version`` per request from the query / conversation
     (:func:`select_active_version`), and
  2. narrow the agent's source set to the sources matching that version PLUS
     the version-agnostic (``network == 'shared'``) sources
     (:func:`filter_sources_by_version`) BEFORE retrieval.

Retrieval (``ClassicRAG._get_data`` / ``_search_global``) then runs over the
reduced ``source_id = ANY(...)`` set, so every cited chunk in one answer is the
same version; the apiref resolver inherits the same narrowed set for free.

**No-op until the cutover.** Today's prod sources carry no ``metadata.version``,
so :func:`filter_sources_by_version` keeps every source (an unversioned source
is treated as shared/always-included) and the live single-version behaviour is
preserved EXACTLY. The narrowing only takes effect once the re-ingest stamps
``sources.metadata`` with ``version`` / ``network``. This means the code can
ship (and get real prod exercise on the hot path) ahead of the irreversible
cutover without changing any behaviour.

Version literals live here for the retrieval layer; the URL rewriter keeps its
own copy in ``application/api/answer/routes/base.py`` (different concern — URL
structure). Bumping the corpus version touches both, plus prompts / settings /
tests — see CLAUDE.md.

Scope boundaries (all no-op until cutover; none affect the Aztec prod agents,
which are all ``classic`` / ClassicRAG):

  * **classic agents only.** Narrowing + selection run in ``pre_fetch_docs``,
    which ``agentic`` / ``research`` agents skip — their on-demand search tool
    builds a retriever from the full source set and emits sources via its own
    path, so it is NOT version-scoped. Threading the active version into the
    internal-search tool is a follow-up if a chat surface ever moves off
    classic retrieval.
  * **continuation/tool-action resume** re-streams without re-running selection
    (``active_version`` defaults to v5 for the source-frame rewrite). Tied to
    the agentic flow above; classic answers don't resume.
  * **ambiguous comparison queries** ("mainnet vs testnet…") name both networks,
    so the selector finds no decisive signal and falls through to v5. This is
    consistent with "one answer, one version" but is a weak mode for explicit
    cross-version questions — the grounded prompt's Version-coverage section is
    expected to caveat version-pinned specifics.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Canonical doc versions served by the two-version KB.
MAINNET_VERSION = "v4.3.1"
TESTNET_VERSION = "v5.0.0-rc.1"
KNOWN_VERSIONS = (MAINNET_VERSION, TESTNET_VERSION)

# Default when the query/conversation carries no version or network signal.
# Testnet (v5) is the primary audience; for version-agnostic concept questions
# the two versions are ~equivalent, so the default is harmless there — it only
# matters for version-pinned specifics, where an explicit signal usually exists.
DEFAULT_VERSION = TESTNET_VERSION

# ``network`` sentinel for version-agnostic sources (e.g. the awesome-aztec
# community list) — always included regardless of the active version.
SHARED_NETWORK = "shared"

# Network → version map for the version-scoping layer.
_NETWORK_TO_VERSION = {
    "mainnet": MAINNET_VERSION,
    "testnet": TESTNET_VERSION,
}

# Explicit network / version signals in free text. Word-boundaried so we don't
# fire on substrings ("mainnet" inside a URL is fine; "v5" inside "rev54" is
# not). Kept deliberately tight — a false version pin is worse than the
# (harmless-for-concepts) default. ``sandbox`` is intentionally NOT a testnet
# signal: the local sandbox tracks whatever release the user installed.
_MAINNET_RE = re.compile(
    r"\bmainnet\b|\bv?4\.3(?:\.\d+)?\b|\bv4\b",
    re.IGNORECASE,
)
_TESTNET_RE = re.compile(
    r"\btestnet\b|\bv?5\.0(?:\.\d+)?(?:-?rc\.?\d+)?\b|\bv5\b",
    re.IGNORECASE,
)


def _signal_from_text(text: str) -> Optional[str]:
    """Return the version a single piece of text decisively points at.

    ``MAINNET_VERSION`` / ``TESTNET_VERSION`` when exactly one network is
    named, else ``None`` (no signal OR an ambiguous both-named mention — let
    the caller fall through to history / default rather than guess).
    """
    if not text or not isinstance(text, str):
        return None
    mainnet = bool(_MAINNET_RE.search(text))
    testnet = bool(_TESTNET_RE.search(text))
    if mainnet and not testnet:
        return MAINNET_VERSION
    if testnet and not mainnet:
        return TESTNET_VERSION
    return None


def _history_texts(history: Optional[Sequence[Any]]) -> List[str]:
    """Flatten conversation history into user/prompt text strings, newest last.

    Accepts the shapes the stream path threads around: a list of
    ``{"prompt": ..., "response": ...}`` query dicts, or chat-message dicts
    with ``content``/``text``. Unknown shapes contribute nothing.
    """
    out: List[str] = []
    for item in history or []:
        if isinstance(item, dict):
            for key in ("prompt", "content", "text", "question"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    out.append(val)
        elif isinstance(item, str) and item.strip():
            out.append(item)
    return out


def select_active_version(
    question: Optional[str],
    history: Optional[Sequence[Any]] = None,
) -> str:
    """Pick the doc version this answer should be scoped to (pre-retrieval).

    The version can't come from the model — retrieval happens before the LLM
    reasons — so a cheap heuristic decides it:

      1. an explicit network/version signal in the *current* question wins;
      2. otherwise the most recent decisive signal in the conversation history
         carries forward (a follow-up inherits the established network);
      3. otherwise :data:`DEFAULT_VERSION` (testnet / v5).

    Returns one of :data:`KNOWN_VERSIONS`.
    """
    signal = _signal_from_text(question or "")
    if signal:
        return signal

    # Newest-first: a later turn's explicit network overrides an earlier one.
    for text in reversed(_history_texts(history)):
        signal = _signal_from_text(text)
        if signal:
            return signal

    return DEFAULT_VERSION


def filter_sources_by_version(
    source_ids: Sequence[str],
    active_version: str,
    version_map: Dict[str, Dict[str, Optional[str]]],
) -> List[str]:
    """Narrow ``source_ids`` to the active version + shared sources. PURE.

    ``version_map``: ``{source_id_str: {"version": str|None, "network": str|None}}``
    as returned by ``SourcesRepository.version_metadata_for_ids`` — passed in so
    this function stays pure and unit-testable without a DB.

    A source is KEPT when ANY of:

      * it has no ``version`` metadata (unversioned/legacy → treated as shared);
      * its ``network`` is ``"shared"``;
      * its ``version`` equals ``active_version``.

    It is DROPPED only when it declares a *different* version. Order is
    preserved (the canonical retrieval order matters downstream). With today's
    unstamped sources every id has ``version is None`` → nothing is dropped
    (no-op). If narrowing would drop EVERYTHING (a misconfigured set with no
    active-version and no shared source), we log and fall back to the full
    input rather than retrieve over an empty set.
    """
    if not source_ids:
        return []

    kept: List[str] = []
    for sid in source_ids:
        meta = version_map.get(str(sid)) or {}
        version = meta.get("version")
        network = meta.get("network")
        if not version or network == SHARED_NETWORK or version == active_version:
            kept.append(sid)

    if not kept:
        logger.warning(
            "version narrowing dropped ALL %d sources for active_version=%s; "
            "falling back to the full set",
            len(source_ids),
            active_version,
        )
        return list(source_ids)
    return kept


def narrow_sources(
    source_ids: Sequence[str],
    active_version: str,
    *,
    repo_factory=None,
) -> List[str]:
    """Convenience wrapper: load the version map and apply the narrowing.

    Opens its own short read-only connection (mirrors the search/stream call
    sites) unless ``repo_factory`` is supplied (tests inject a fake). Any DB
    failure degrades to the full input — version narrowing must never take a
    request down.
    """
    ids = [str(s) for s in source_ids if s and str(s).strip()]
    if not ids:
        return ids

    try:
        version_map = _load_version_map(ids, repo_factory=repo_factory)
    except Exception:  # pragma: no cover - defensive; never fail the request
        logger.warning("version map load failed; skipping narrowing", exc_info=True)
        return ids

    return filter_sources_by_version(ids, active_version, version_map)


def _load_version_map(
    ids: Iterable[str],
    *,
    repo_factory=None,
) -> Dict[str, Dict[str, Optional[str]]]:
    """Fetch ``{id: {version, network}}`` from the ``sources`` table.

    Not cached: the lookup is a single PK ``= ANY`` query over the ~30-row
    ``sources`` table (sub-millisecond next to the embed + vector search
    already on the path). Add a TTL cache here if profiling ever shows it hot.
    """
    if repo_factory is not None:
        repo = repo_factory()
        return repo.version_metadata_for_ids(list(ids))

    # Local imports: keep this module importable by pure unit tests (and the
    # eval harness) without dragging in the DB session machinery.
    from application.storage.db.repositories.sources import SourcesRepository
    from application.storage.db.session import db_readonly

    with db_readonly() as conn:
        return SourcesRepository(conn).version_metadata_for_ids(list(ids))

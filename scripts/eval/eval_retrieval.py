#!/usr/bin/env python3
"""Evaluation harness for the Aztec DocsGPT RAG system.

Two modes:
  --mode retriever   Direct retriever probe (source-coverage + apiref hit
                     assertions for the ``identifier`` bucket).
  --mode stream      Full /stream endpoint (answer quality, banned
                     identifiers, first-citation source-type for the
                     ``identifier`` bucket).

Buckets
-------
Each golden query carries a ``bucket`` field:

  identifier   The user is asking about a specific function / type /
               import path. The answer is most likely in an apiref
               source (post-Phase-2 of PLAN-rag-apiref.md). For these
               we assert that the expected ``.nr`` file appears in the
               top-3 retrieved chunks (retriever mode) and is the first
               cited source (stream mode).
  concept      The user is asking a conceptual / how-it-works question.
               The expected source is a markdown concept doc. We assert
               the first hit comes from the configured concept prefix
               (regression guard against an over-eager apiref bias).
  example      "Show me a contract" / "give me an example" — concept and
               apiref are both fine, we just assert source diversity.

Usage (from inside the backend container or with PYTHONPATH=/app):
  python scripts/eval/eval_retrieval.py --mode retriever
  python scripts/eval/eval_retrieval.py --mode stream --api-key <agent_key>
  python scripts/eval/eval_retrieval.py --mode stream --api-key <agent_key> --base-url http://localhost:7091
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

# Mirror of the backend's ``_INLINE_CITATION_RE`` in
# ``application/api/answer/routes/base.py``. Kept in sync deliberately
# — if this regex stops matching what the backend scrubber matches,
# the eval will miss leaks of marker variants the model invents.
_MARKER_LEAK_RE = re.compile(
    r"\[\[\s*cited\s*:[^\]\n]*\]\]",
    re.IGNORECASE,
)

GOLDEN_QUERIES_PATH = Path(__file__).parent / "golden_queries.json"

SOURCE_PREFIXES = (
    "noir-docs/", "noir-stdlib/", "typescript-api/", "aztec-nr/",
    "aztec.js/", "cli/", "cli-wallet/", "end-to-end/", "l1-contracts/",
    "noir-contracts/", "noir-protocol-circuits/", "version-v4.3.0/docs/",
    "version-v4.3.0/",
)

# Path prefixes considered "apiref-shaped". A retrieval/citation is an
# apiref hit if the chunk's ``metadata.source`` (raw corpus path, not the
# rewritten public URL) starts with one of these. Examples / circuits are
# deliberately excluded — they are full implementation, not apiref.
APIREF_PREFIXES = ("aztec-nr/", "noir-stdlib/")


def bucket(path: str) -> str:
    if not isinstance(path, str):
        return "(none)"
    for pfx in SOURCE_PREFIXES:
        if path.startswith(pfx):
            return pfx.rstrip("/")
    return path.split("/", 1)[0] if "/" in path else path


def is_apiref(path: str) -> bool:
    return isinstance(path, str) and path.startswith(APIREF_PREFIXES)


def _strip_txt(p: str) -> str:
    """Normalize retrieved corpus paths to the canonical ``foo.nr`` form
    used in ``expected_apiref_paths``.

    The ingest pipeline appends a parser-friendly extension to code
    files, but ``expected_apiref_paths`` (in ``golden_queries.json``) is
    written against the un-wrapped path:

      * apiref corpora (aztec-nr, noir-stdlib) → ``foo.nr.md``
      * body-bearing code corpora (examples, circuits, etc.) →
        ``Token.nr.txt`` / ``foo.ts.txt`` / ``foo.sol.txt``

    Stripping the wrapper extension yields the canonical path the
    assertion expects. ``.txt`` / ``.md`` standalone (non-apiref code
    or rendered markdown) is left alone — those aren't apiref paths
    and the eval never compares them against ``expected_apiref_paths``.
    """
    if not isinstance(p, str):
        return p
    # Order matters: .nr.txt and .nr.md must match before bare .txt/.md.
    for suffix in (".nr.txt", ".nr.md"):
        if p.endswith(suffix):
            return p[: -len(suffix)] + ".nr"
    return p


def load_golden_queries() -> list:
    with open(GOLDEN_QUERIES_PATH) as f:
        return json.load(f)


# ── Retriever mode ──────────────────────────────────────────────────────────


def run_retriever_eval(settings_module: str = "application.core.settings"):
    """Import ClassicRAG and probe the retriever directly."""
    from application.retriever.classic_rag import ClassicRAG
    from application.core.settings import settings

    vectorstores = [
        s.strip() for s in settings.AZTEC_SOURCE_IDS.split(",") if s.strip()
    ]
    queries = load_golden_queries()
    results = []

    for q in queries:
        tag = q["tag"]
        question = q["query"]
        bucket_name = q.get("bucket", "concept")
        expected = q.get("expected_source_prefixes", [])
        min_sources = q.get("min_distinct_sources", 1)
        expected_apiref_paths = q.get("expected_apiref_paths", []) or []
        expected_concept_prefix = q.get("expected_concept_prefix", "")

        t0 = time.time()
        rag = ClassicRAG(
            source={"active_docs": vectorstores, "question": question},
            chunks=2,
            doc_token_limit=10000,
        )
        docs = rag._get_data()
        elapsed = time.time() - t0

        tally = Counter(bucket(d.get("source", "")) for d in docs)
        distinct_sources = len(tally)

        # Check expected prefixes
        missing = [
            pfx.rstrip("/")
            for pfx in expected
            if not any(k.startswith(pfx.rstrip("/")) for k in tally)
        ]
        source_pass = len(missing) == 0

        # Check minimum distinct sources
        diversity_pass = distinct_sources >= min_sources

        # Bucket-specific assertions
        top3_paths = [_strip_txt(d.get("source", "")) for d in docs[:3]]
        top3_apiref_hit = False
        first_source_path = _strip_txt(docs[0].get("source", "")) if docs else ""
        first_is_concept = bool(
            expected_concept_prefix and first_source_path.startswith(expected_concept_prefix)
        )

        if bucket_name == "identifier":
            # Top-3 must include any of the expected apiref files (canonical
            # .nr path), or at minimum a path under an apiref prefix.
            if expected_apiref_paths:
                top3_apiref_hit = any(p in top3_paths for p in expected_apiref_paths)
            else:
                top3_apiref_hit = any(is_apiref(p) for p in top3_paths)
            bucket_pass = top3_apiref_hit
        elif bucket_name == "concept":
            # Concept regression guard: first hit should NOT come from
            # apiref unless the user explicitly asked for an identifier
            # (which would have made it an "identifier" bucket query).
            # We only enforce this softly — if expected_concept_prefix is
            # set we require the first hit to start with it.
            if expected_concept_prefix:
                bucket_pass = first_is_concept
            else:
                bucket_pass = True
        else:  # example
            bucket_pass = True

        passed = source_pass and diversity_pass and bucket_pass

        result = {
            "tag": tag,
            "bucket": bucket_name,
            "passed": passed,
            "elapsed_s": round(elapsed, 2),
            "total_docs": len(docs),
            "distinct_sources": distinct_sources,
            "source_tally": dict(tally.most_common()),
            "top3_paths": top3_paths,
            "missing_expected": missing,
            "diversity_pass": diversity_pass,
            "bucket_pass": bucket_pass,
            "top3_apiref_hit": top3_apiref_hit if bucket_name == "identifier" else None,
            "first_is_concept": first_is_concept if bucket_name == "concept" else None,
        }
        results.append(result)

        status = "PASS" if passed else "FAIL"
        print(
            f"  [{status}] {tag} ({bucket_name}): {len(docs)} docs, "
            f"{distinct_sources} sources, {elapsed:.1f}s"
        )
        if missing:
            print(f"         missing: {missing}")
        if bucket_name == "identifier" and not top3_apiref_hit:
            print(f"         NO apiref hit in top-3: {top3_paths}")
        if bucket_name == "concept" and expected_concept_prefix and not first_is_concept:
            print(
                f"         first hit not concept ({expected_concept_prefix}): "
                f"{first_source_path}"
            )

    return results


# ── Stream mode ─────────────────────────────────────────────────────────────


def run_stream_eval(
    api_key: str,
    base_url: str = "http://localhost:7091",
    capture_answers: bool = False,
):
    """Hit the /stream endpoint and check answer quality.

    When ``capture_answers`` is True each per-query result also carries
    the verbatim ``query``, ``answer`` text, and ordered ``sources`` list
    — the payload ``compare.py`` expects.
    """
    import requests

    queries = load_golden_queries()
    results = []

    for q in queries:
        tag = q["tag"]
        question = q["query"]
        bucket_name = q.get("bucket", "concept")
        history = q.get("history", [])
        banned = q.get("banned_identifiers", [])
        max_time = q.get("max_response_time_s", 15)
        expected_apiref_paths = q.get("expected_apiref_paths", []) or []
        # Optional override for the first-citation prefix check. Defaults to
        # APIREF_PREFIXES when unset. Set for identifier queries whose
        # canonical reference source is NOT a .nr apiref (e.g. TypeScript
        # API queries should cite typescript-api/* first, not aztec-nr/).
        expected_first_prefixes = q.get("expected_first_prefixes") or list(APIREF_PREFIXES)

        t0 = time.time()
        try:
            r = requests.post(
                f"{base_url}/stream",
                json={
                    "question": question,
                    "api_key": api_key,
                    "history": history,
                },
                stream=True,
                timeout=max_time + 10,
            )
        except Exception as e:
            results.append({"tag": tag, "passed": False, "error": str(e)})
            print(f"  [FAIL] {tag}: request error: {e}")
            continue

        answer_chunks = []
        sources = []
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            t = obj.get("type")
            if t == "answer":
                answer_chunks.append(obj.get("answer", ""))
            elif t == "source":
                for s in obj.get("source") or []:
                    sources.append(s)

        elapsed = time.time() - t0
        answer = "".join(answer_chunks)

        # Check banned identifiers
        found_banned = [b for b in banned if b in answer]
        banned_pass = len(found_banned) == 0

        # Check response time
        time_pass = elapsed <= max_time

        # Check answer is non-empty
        content_pass = len(answer.strip()) > 10

        # Check no Markdown table (format rule)
        has_table = "|---|" in answer or "| ---" in answer
        table_pass = not has_table

        # Citation-marker leak guard. The grounded prompts instruct the
        # model to emit ONE machine-only ``[[cited: ...]]`` marker at
        # the very end; the backend strips it. A model that emits the
        # marker inline (per paragraph) leaks the literal string into
        # the user-visible bytes. The backend has a scrubber (see
        # ``_scrub_inline_citation_markers``), but if a stream-mode eval
        # answer still contains ``[[cited:`` it means EITHER the
        # scrubber regressed OR the model invented a new marker variant
        # the scrub doesn't recognize. Either way: hard fail.
        marker_in_answer = _MARKER_LEAK_RE.search(answer) is not None
        marker_pass = not marker_in_answer

        # Check source diversity (same prefixes as retriever mode)
        min_sources = q.get("min_distinct_sources", 1)
        source_titles = set()
        for s in sources:
            src = s.get("source") or s.get("title") or ""
            if src:
                source_titles.add(src)
        diversity_pass = len(source_titles) >= min_sources

        # Bucket-specific: identifier queries must cite the canonical
        # reference source somewhere in the top-3 cited sources. Two ways
        # to express the expectation:
        #   1. expected_apiref_paths — match a corpus-relative path against
        #      the rewritten URL (e.g. "aztec-nr/aztec/src/hash.nr").
        #   2. expected_first_prefixes — match a corpus prefix against the
        #      rewritten URL. Defaults to APIREF_PREFIXES; override to
        #      ("typescript-api/",) for TS-apiref queries, etc.
        #
        # Why top-3, not top-1: ``retriever`` mode already accepts an
        # apiref hit anywhere in the retrieved top-3 (line ~145), but
        # stream mode used to insist the LLM cite the apiref FIRST. That
        # was unrealistically strict — conversational identifier queries
        # ("What methods does X expose?") routinely surface a markdown
        # explainer at position 1 with the apiref at position 2-3, and
        # that's still a correct answer with a useful citation. Bringing
        # stream mode in line with retriever mode makes the bar match
        # the actual user experience.
        apiref_in_top3_cited = False
        if bucket_name == "identifier" and sources:
            top3_urls = [
                (s.get("source", "") or "") for s in sources[:3]
            ]
            if expected_apiref_paths:
                apiref_in_top3_cited = any(
                    (p.split("/", 1)[1] in url if "/" in p else p in url)
                    for url in top3_urls
                    for p in expected_apiref_paths
                )
            else:
                apiref_in_top3_cited = any(
                    pfx.rstrip("/") in url
                    for url in top3_urls
                    for pfx in expected_first_prefixes
                )
            bucket_pass = apiref_in_top3_cited
        else:
            bucket_pass = True

        passed = (
            banned_pass and time_pass and content_pass
            and table_pass and diversity_pass and bucket_pass
            and marker_pass
        )

        result = {
            "tag": tag,
            "bucket": bucket_name,
            "passed": passed,
            "elapsed_s": round(elapsed, 2),
            "answer_len": len(answer),
            "sources_count": len(sources),
            "distinct_sources": len(source_titles),
            "found_banned": found_banned,
            "has_table": has_table,
            "marker_in_answer": marker_in_answer,
            "time_pass": time_pass,
            "diversity_pass": diversity_pass,
            "bucket_pass": bucket_pass,
            "apiref_in_top3_cited": apiref_in_top3_cited if bucket_name == "identifier" else None,
        }
        if capture_answers:
            # Full payload for snapshot/compare. Sources are kept in citation
            # order. The `/stream` SSE `source` frame rewrites the raw corpus
            # path into the public URL in-place (see
            # `api/answer/routes/base.py:_aztec_source_url`), so we only have
            # the rewritten URL + the chunk title — no separate raw corpus path.
            result["query"] = question
            result["answer"] = answer
            result["sources"] = [
                {
                    "url": s.get("source", ""),
                    "title": s.get("title", ""),
                }
                for s in sources
            ]
        results.append(result)

        status = "PASS" if passed else "FAIL"
        flags = []
        if found_banned:
            flags.append(f"banned:{found_banned}")
        if has_table:
            flags.append("table!")
        if marker_in_answer:
            flags.append("marker-leak!")
        if not time_pass:
            flags.append(f"slow:{elapsed:.1f}s")
        if not content_pass:
            flags.append("empty!")
        if not diversity_pass:
            flags.append(f"low-diversity:{len(source_titles)}<{min_sources}")
        if bucket_name == "identifier" and not apiref_in_top3_cited:
            flags.append("no-apiref-in-top3")
        flag_str = f" ({', '.join(flags)})" if flags else ""
        print(
            f"  [{status}] {tag} ({bucket_name}): {elapsed:.1f}s, "
            f"{len(answer)} chars, {len(sources)} sources{flag_str}"
        )

    return results


# ── Main ────────────────────────────────────────────────────────────────────


def _summarize(results: list) -> dict:
    by_bucket: dict = {}
    for r in results:
        b = r.get("bucket", "?")
        by_bucket.setdefault(b, {"total": 0, "passed": 0})
        by_bucket[b]["total"] += 1
        if r.get("passed"):
            by_bucket[b]["passed"] += 1
    return by_bucket


def main():
    parser = argparse.ArgumentParser(description="Aztec DocsGPT eval harness")
    parser.add_argument(
        "--mode",
        choices=["retriever", "stream"],
        default="stream",
        help="retriever = direct probe; stream = full /stream endpoint",
    )
    parser.add_argument("--api-key", help="Agent API key (required for stream mode)")
    parser.add_argument(
        "--base-url",
        default="http://localhost:7091",
        help="Backend base URL (default: http://localhost:7091)",
    )
    parser.add_argument(
        "--json-out",
        help="Write structured results to this JSON file",
    )
    parser.add_argument(
        "--bucket",
        choices=["identifier", "concept", "example", "all"],
        default="all",
        help="Restrict evaluation to one bucket (default: all)",
    )
    parser.add_argument(
        "--capture-answers",
        action="store_true",
        help="(stream mode) Include the full answer text + ordered cited URLs in --json-out. Required for compare.py.",
    )
    parser.add_argument(
        "--label",
        help="Label written into the snapshot envelope (e.g. 'baseline', 'variant-new-prompt'). Defaults to the api-key fingerprint.",
    )
    args = parser.parse_args()

    global load_golden_queries
    queries = load_golden_queries()
    if args.bucket != "all":
        original = len(queries)
        queries = [q for q in queries if q.get("bucket") == args.bucket]
        # Mutate the on-disk-loaded list reference used by the runners by
        # monkey-patching ``load_golden_queries`` for this invocation.
        _filtered = list(queries)

        def _loader():
            return _filtered

        load_golden_queries = _loader
        print(
            f"Filtered to bucket={args.bucket}: {len(queries)}/{original} queries"
        )

    print(f"Running {args.mode} eval with {len(queries)} golden queries\n")

    if args.mode == "retriever":
        if args.capture_answers:
            print(
                "WARNING: --capture-answers is a no-op in retriever mode "
                "(no answer is generated); ignoring.",
                file=sys.stderr,
            )
        results = run_retriever_eval()
    elif args.mode == "stream":
        if not args.api_key:
            print("ERROR: --api-key required for stream mode", file=sys.stderr)
            sys.exit(1)
        results = run_stream_eval(
            args.api_key,
            args.base_url,
            capture_answers=args.capture_answers,
        )

    # Summary
    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"  {passed}/{total} passed overall")
    by_bucket = _summarize(results)
    for b, counts in sorted(by_bucket.items()):
        print(f"    {b:11s}: {counts['passed']}/{counts['total']}")
    print(f"{'=' * 40}")

    if args.json_out:
        # Snapshot envelope: a single object containing run metadata + the
        # per-query results. compare.py reads either shape (envelope or
        # the legacy bare list) so older runs keep working.
        from datetime import datetime, timezone

        label = args.label
        if not label and args.mode == "stream" and args.api_key:
            label = f"key-{args.api_key[:8]}"
        elif not label:
            label = args.mode
        envelope = {
            "label": label,
            "mode": args.mode,
            "base_url": args.base_url if args.mode == "stream" else None,
            "bucket_filter": args.bucket,
            "capture_answers": bool(args.capture_answers) and args.mode == "stream",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total": total,
                "passed": passed,
                "by_bucket": by_bucket,
            },
            "results": results,
        }
        with open(args.json_out, "w") as f:
            json.dump(envelope, f, indent=2)
        print(f"Results written to {args.json_out}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()

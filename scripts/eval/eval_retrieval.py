#!/usr/bin/env python3
"""Evaluation harness for the Aztec DocsGPT RAG system.

Two modes:
  --mode retriever   Direct retriever probe (source-coverage assertions).
  --mode stream      Full /stream endpoint (answer quality + banned identifiers).

Usage (from inside the backend container or with PYTHONPATH=/app):
  python scripts/eval/eval_retrieval.py --mode retriever
  python scripts/eval/eval_retrieval.py --mode stream --api-key <agent_key>
  python scripts/eval/eval_retrieval.py --mode stream --api-key <agent_key> --base-url http://localhost:7091
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

GOLDEN_QUERIES_PATH = Path(__file__).parent / "golden_queries.json"

SOURCE_PREFIXES = (
    "noir-docs/", "noir-stdlib/", "typescript-api/", "aztec-nr/",
    "aztec.js/", "cli/", "cli-wallet/", "end-to-end/", "l1-contracts/",
    "noir-contracts/", "noir-protocol-circuits/", "version-v4.2.0/docs/",
    "version-v4.2.0/",
)


def bucket(path: str) -> str:
    if not isinstance(path, str):
        return "(none)"
    for pfx in SOURCE_PREFIXES:
        if path.startswith(pfx):
            return pfx.rstrip("/")
    return path.split("/", 1)[0] if "/" in path else path


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
        expected = q.get("expected_source_prefixes", [])
        min_sources = q.get("min_distinct_sources", 1)

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

        passed = source_pass and diversity_pass

        result = {
            "tag": tag,
            "passed": passed,
            "elapsed_s": round(elapsed, 2),
            "total_docs": len(docs),
            "distinct_sources": distinct_sources,
            "source_tally": dict(tally.most_common()),
            "missing_expected": missing,
            "diversity_pass": diversity_pass,
        }
        results.append(result)

        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {tag}: {len(docs)} docs, {distinct_sources} sources, {elapsed:.1f}s")
        if missing:
            print(f"         missing: {missing}")

    return results


# ── Stream mode ─────────────────────────────────────────────────────────────


def run_stream_eval(api_key: str, base_url: str = "http://localhost:7091"):
    """Hit the /stream endpoint and check answer quality."""
    import requests

    queries = load_golden_queries()
    results = []

    for q in queries:
        tag = q["tag"]
        question = q["query"]
        history = q.get("history", [])
        banned = q.get("banned_identifiers", [])
        max_time = q.get("max_response_time_s", 15)

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

        passed = banned_pass and time_pass and content_pass and table_pass

        result = {
            "tag": tag,
            "passed": passed,
            "elapsed_s": round(elapsed, 2),
            "answer_len": len(answer),
            "sources_count": len(sources),
            "found_banned": found_banned,
            "has_table": has_table,
            "time_pass": time_pass,
        }
        results.append(result)

        status = "PASS" if passed else "FAIL"
        flags = []
        if found_banned:
            flags.append(f"banned:{found_banned}")
        if has_table:
            flags.append("table!")
        if not time_pass:
            flags.append(f"slow:{elapsed:.1f}s")
        if not content_pass:
            flags.append("empty!")
        flag_str = f" ({', '.join(flags)})" if flags else ""
        print(f"  [{status}] {tag}: {elapsed:.1f}s, {len(answer)} chars, {len(sources)} sources{flag_str}")

    return results


# ── Main ────────────────────────────────────────────────────────────────────


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
    args = parser.parse_args()

    print(f"Running {args.mode} eval with {len(load_golden_queries())} golden queries\n")

    if args.mode == "retriever":
        results = run_retriever_eval()
    elif args.mode == "stream":
        if not args.api_key:
            print("ERROR: --api-key required for stream mode", file=sys.stderr)
            sys.exit(1)
        results = run_stream_eval(args.api_key, args.base_url)

    # Summary
    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"  {passed}/{total} passed")
    print(f"{'=' * 40}")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Results written to {args.json_out}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()

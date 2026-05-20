"""Diff two ``eval_retrieval.py --capture-answers`` snapshots.

Reads two JSON snapshots written by ``eval_retrieval.py --json-out`` and
emits a Markdown report covering:

* Per-bucket pass/fail counts in both runs.
* Per-query status transitions (regress / fix / steady-fail).
* Per-query latency, answer-length, citation deltas.
* A unified diff of the answer text for every query whose answer
  changed (or just the ones that flipped pass/fail, with ``--only=flips``).

Pure stdlib — runs on the host, no backend image needed::

    python scripts/eval/compare.py baseline.json candidate.json \\
        --out /tmp/report.md

Snapshots can be either the envelope form (``{label, summary, results}``)
or the legacy bare list of result dicts; the loader accepts both.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path


def _load_snapshot(path: str) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {"label": Path(path).stem, "results": raw, "summary": None, "mode": None}
    if not isinstance(raw, dict) or "results" not in raw:
        raise SystemExit(f"unrecognized snapshot shape in {path}")
    raw.setdefault("label", Path(path).stem)
    return raw


def _index_by_tag(results: list[dict]) -> dict[str, dict]:
    return {r["tag"]: r for r in results if "tag" in r}


def _bucket_counts(results: list[dict]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in results:
        b = r.get("bucket", "?")
        slot = out.setdefault(b, {"total": 0, "passed": 0})
        slot["total"] += 1
        if r.get("passed"):
            slot["passed"] += 1
    return out


def _url_list(r: dict) -> list[str]:
    return [s.get("url", "") for s in r.get("sources") or [] if s.get("url")]


def _classify(base: dict | None, cand: dict | None) -> str:
    bp = bool(base and base.get("passed"))
    cp = bool(cand and cand.get("passed"))
    if base is None:
        return "added"
    if cand is None:
        return "removed"
    if bp and not cp:
        return "regressed"
    if cp and not bp:
        return "fixed"
    if bp and cp:
        return "still-passing"
    return "still-failing"


def _diff_block(base_text: str, cand_text: str, base_label: str, cand_label: str) -> str:
    base_lines = base_text.splitlines(keepends=False)
    cand_lines = cand_text.splitlines(keepends=False)
    diff = list(
        difflib.unified_diff(
            base_lines,
            cand_lines,
            fromfile=base_label,
            tofile=cand_label,
            lineterm="",
            n=2,
        )
    )
    if not diff:
        return ""
    return "```diff\n" + "\n".join(diff) + "\n```"


def _fmt_delta(base_v: float | int | None, cand_v: float | int | None, unit: str = "") -> str:
    if base_v is None and cand_v is None:
        return "—"
    if base_v is None:
        return f"({cand_v}{unit})"
    if cand_v is None:
        return f"({base_v}{unit} → ∅)"
    delta = cand_v - base_v
    sign = "+" if delta >= 0 else ""
    return f"{base_v}{unit} → {cand_v}{unit} ({sign}{round(delta, 2)}{unit})"


def _render_report(
    base: dict,
    cand: dict,
    only: str,
    max_answer_diff_chars: int,
) -> str:
    base_label = base.get("label", "baseline")
    cand_label = cand.get("label", "candidate")
    base_results = base.get("results") or []
    cand_results = cand.get("results") or []
    base_by_tag = _index_by_tag(base_results)
    cand_by_tag = _index_by_tag(cand_results)
    all_tags = sorted(set(base_by_tag) | set(cand_by_tag))

    lines: list[str] = []
    lines.append(f"# Eval comparison: `{base_label}` → `{cand_label}`")
    lines.append("")
    base_meta = {
        k: base.get(k)
        for k in ("mode", "base_url", "bucket_filter", "ran_at", "capture_answers")
        if base.get(k) is not None
    }
    cand_meta = {
        k: cand.get(k)
        for k in ("mode", "base_url", "bucket_filter", "ran_at", "capture_answers")
        if cand.get(k) is not None
    }
    if base_meta or cand_meta:
        lines.append("| | baseline | candidate |")
        lines.append("|---|---|---|")
        for k in sorted(set(base_meta) | set(cand_meta)):
            lines.append(f"| {k} | {base_meta.get(k, '—')} | {cand_meta.get(k, '—')} |")
        lines.append("")

    base_buckets = _bucket_counts(base_results)
    cand_buckets = _bucket_counts(cand_results)
    all_buckets = sorted(set(base_buckets) | set(cand_buckets))

    lines.append("## Summary")
    lines.append("")
    lines.append("| bucket | baseline pass/total | candidate pass/total | Δ |")
    lines.append("|---|---|---|---|")
    base_total = sum(b["total"] for b in base_buckets.values())
    base_pass = sum(b["passed"] for b in base_buckets.values())
    cand_total = sum(b["total"] for b in cand_buckets.values())
    cand_pass = sum(b["passed"] for b in cand_buckets.values())
    for b in all_buckets:
        bp = base_buckets.get(b, {"passed": 0, "total": 0})
        cp = cand_buckets.get(b, {"passed": 0, "total": 0})
        delta = cp["passed"] - bp["passed"]
        delta_str = "0" if delta == 0 else ("+" + str(delta) if delta > 0 else str(delta))
        lines.append(
            f"| {b} | {bp['passed']}/{bp['total']} | {cp['passed']}/{cp['total']} | {delta_str} |"
        )
    overall_delta = cand_pass - base_pass
    overall_delta_str = (
        "0" if overall_delta == 0 else ("+" + str(overall_delta) if overall_delta > 0 else str(overall_delta))
    )
    lines.append(
        f"| **TOTAL** | **{base_pass}/{base_total}** | **{cand_pass}/{cand_total}** | **{overall_delta_str}** |"
    )
    lines.append("")

    regressed: list[str] = []
    fixed: list[str] = []
    still_failing: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    changed_answers: list[str] = []
    for tag in all_tags:
        b = base_by_tag.get(tag)
        c = cand_by_tag.get(tag)
        cls = _classify(b, c)
        if cls == "regressed":
            regressed.append(tag)
        elif cls == "fixed":
            fixed.append(tag)
        elif cls == "still-failing":
            still_failing.append(tag)
        elif cls == "added":
            added.append(tag)
        elif cls == "removed":
            removed.append(tag)
        if b and c and (b.get("answer") or "") != (c.get("answer") or ""):
            changed_answers.append(tag)

    lines.append("## Status changes")
    lines.append("")
    lines.append(f"- **Regressed** (passing → failing): {len(regressed)}")
    for tag in regressed:
        lines.append(f"  - `{tag}`")
    lines.append(f"- **Fixed** (failing → passing): {len(fixed)}")
    for tag in fixed:
        lines.append(f"  - `{tag}`")
    lines.append(f"- **Still failing**: {len(still_failing)}")
    for tag in still_failing:
        lines.append(f"  - `{tag}`")
    if added:
        lines.append(f"- **Added** (only in candidate): {len(added)}")
        for tag in added:
            lines.append(f"  - `{tag}`")
    if removed:
        lines.append(f"- **Removed** (only in baseline): {len(removed)}")
        for tag in removed:
            lines.append(f"  - `{tag}`")
    lines.append("")

    lines.append("## Per-query detail")
    lines.append("")
    lines.append("| tag | bucket | status | latency | answer chars | cited URLs |")
    lines.append("|---|---|---|---|---|---|")
    for tag in all_tags:
        b = base_by_tag.get(tag)
        c = cand_by_tag.get(tag)
        bucket = (c or b or {}).get("bucket", "?")
        cls = _classify(b, c)
        emoji = {
            "regressed": "🔻",
            "fixed": "✅",
            "still-passing": "·",
            "still-failing": "✗",
            "added": "🆕",
            "removed": "🗑",
        }.get(cls, "?")
        lat = _fmt_delta(
            (b or {}).get("elapsed_s") if b else None,
            (c or {}).get("elapsed_s") if c else None,
            "s",
        )
        chars = _fmt_delta(
            (b or {}).get("answer_len") if b else None,
            (c or {}).get("answer_len") if c else None,
        )
        srcs = _fmt_delta(
            (b or {}).get("sources_count") if b else None,
            (c or {}).get("sources_count") if c else None,
        )
        lines.append(f"| `{tag}` | {bucket} | {emoji} {cls} | {lat} | {chars} | {srcs} |")
    lines.append("")

    if only == "flips":
        targets = [
            t for t in all_tags
            if _classify(base_by_tag.get(t), cand_by_tag.get(t)) in {"regressed", "fixed"}
        ]
        details_header = "## Detail: regressions + fixes only"
    elif only == "changed":
        targets = changed_answers
        details_header = "## Detail: queries whose answer text changed"
    elif only == "all":
        targets = all_tags
        details_header = "## Detail: every query"
    else:
        targets = []
        details_header = ""

    if targets:
        lines.append(details_header)
        lines.append("")
        for tag in targets:
            b = base_by_tag.get(tag)
            c = cand_by_tag.get(tag)
            lines.append(f"### `{tag}`")
            lines.append("")
            query = (c or b or {}).get("query")
            if query:
                lines.append(f"**Query**: {query}")
                lines.append("")
            cls = _classify(b, c)
            lines.append(f"**Status**: {cls}")
            base_flags = []
            cand_flags = []
            for src, flags in ((b, base_flags), (c, cand_flags)):
                if not src:
                    continue
                if src.get("found_banned"):
                    flags.append(f"banned:{src['found_banned']}")
                if src.get("has_table"):
                    flags.append("table!")
                if src.get("time_pass") is False:
                    flags.append(f"slow:{src.get('elapsed_s')}s")
                if src.get("diversity_pass") is False:
                    flags.append(
                        f"low-diversity:{src.get('distinct_sources')}"
                    )
                # Field renamed first_cited_apiref → apiref_in_top3_cited
                # when the stream-mode check went from top-1 to top-3.
                # Accept either spelling so we can still diff snapshots
                # captured by older harness versions.
                apiref_hit = src.get(
                    "apiref_in_top3_cited",
                    src.get("first_cited_apiref"),
                )
                if src.get("bucket") == "identifier" and apiref_hit is False:
                    flags.append("no-apiref-in-top3")
            if base_flags or cand_flags:
                lines.append(
                    f"- baseline flags: {', '.join(f'`{f}`' for f in base_flags) or '—'}"
                )
                lines.append(
                    f"- candidate flags: {', '.join(f'`{f}`' for f in cand_flags) or '—'}"
                )
            lines.append("")

            base_urls = _url_list(b or {})
            cand_urls = _url_list(c or {})
            if base_urls or cand_urls:
                lines.append("**Cited URLs** (in citation order):")
                lines.append("")
                base_set = set(base_urls)
                cand_set = set(cand_urls)
                only_base = [u for u in base_urls if u not in cand_set]
                only_cand = [u for u in cand_urls if u not in base_set]
                shared = [u for u in cand_urls if u in base_set]
                if shared:
                    lines.append("- shared:")
                    for u in shared:
                        lines.append(f"  - {u}")
                if only_base:
                    lines.append("- only in baseline:")
                    for u in only_base:
                        lines.append(f"  - {u}")
                if only_cand:
                    lines.append("- only in candidate:")
                    for u in only_cand:
                        lines.append(f"  - {u}")
                lines.append("")

            base_ans = (b or {}).get("answer") or ""
            cand_ans = (c or {}).get("answer") or ""
            if base_ans or cand_ans:
                if base_ans == cand_ans:
                    lines.append("**Answer text**: identical.")
                else:
                    truncated = False
                    if max_answer_diff_chars and (
                        len(base_ans) > max_answer_diff_chars
                        or len(cand_ans) > max_answer_diff_chars
                    ):
                        base_ans = base_ans[:max_answer_diff_chars] + (
                            "…[truncated]" if len(base_ans) > max_answer_diff_chars else ""
                        )
                        cand_ans = cand_ans[:max_answer_diff_chars] + (
                            "…[truncated]" if len(cand_ans) > max_answer_diff_chars else ""
                        )
                        truncated = True
                    block = _diff_block(base_ans, cand_ans, base_label, cand_label)
                    if block:
                        if truncated:
                            lines.append(
                                f"_Diff truncated to first {max_answer_diff_chars} chars per side._"
                            )
                            lines.append("")
                        lines.append(block)
                lines.append("")
            lines.append("---")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff two eval_retrieval.py snapshots and write a markdown report."
    )
    parser.add_argument("baseline", help="Path to baseline JSON snapshot.")
    parser.add_argument("candidate", help="Path to candidate JSON snapshot.")
    parser.add_argument(
        "--out",
        help="Write the markdown report here. Defaults to stdout.",
    )
    parser.add_argument(
        "--only",
        choices=["flips", "changed", "all", "none"],
        default="flips",
        help=(
            "Which queries to include in the per-query detail section: "
            "'flips' = regressions+fixes (default); "
            "'changed' = every query whose answer text differs; "
            "'all' = every query; 'none' = summary table only."
        ),
    )
    parser.add_argument(
        "--max-answer-diff-chars",
        type=int,
        default=4000,
        help="Truncate each side of the unified diff to this many chars. 0 disables truncation. Default: 4000.",
    )
    args = parser.parse_args()

    base = _load_snapshot(args.baseline)
    cand = _load_snapshot(args.candidate)

    report = _render_report(base, cand, only=args.only, max_answer_diff_chars=args.max_answer_diff_chars)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Wrote {args.out} ({len(report)} chars)")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

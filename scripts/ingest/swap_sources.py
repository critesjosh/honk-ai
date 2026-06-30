"""Generate the SQL to swap an agent's source list to the freshly-uploaded corpora.

This script does NOT execute SQL by default. It prints the UPDATE
statements you need to run via ``psql`` against the production
Postgres, plus the ``AZTEC_SOURCE_IDS`` block to paste into ``.env``.

For the two-version KB it ALSO emits ``UPDATE sources SET metadata`` lines
stamping ``{version, network}`` on each uploaded source (from ``corpora.py``).
That stamp is the producer half the retrieval version-scoping resolver
(``application/retriever/version_scope.py``) reads — without it, every source
looks unversioned and the per-request narrowing is a permanent no-op.

Why dry-run by default
----------------------
Production agent edits via the UI are blocked
(``VITE_DISABLE_AGENT_EDIT=true``) and SQL-driven changes are
explicitly the operating model. We don't want a bug here to silently
clobber an agent — so the default is "show me the SQL" and the user
runs it.

Usage::

    python -m scripts.ingest.swap_sources \
        --upload-manifest /tmp/aztec-corpora-build/upload_manifest.json \
        --agent-id <uuid>
        [--apiref-only]                 # rotate apiref UUIDs in place
        [--prompt-id <uuid>]             # also pin the system prompt
        [--out /tmp/swap.sql]            # write SQL to a file

After running the printed SQL, also update ``AZTEC_SOURCE_IDS`` in
``.env`` to the printed canonical-order block, then re-run
``up -d --force-recreate backend worker``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from scripts.ingest.corpora import CORPORA, _vslug

# Version suffixes used by the per-version corpus slugs (e.g. ``_v5_0_0_rc_1``),
# derived from the active versions so this stays in sync with corpora.py.
_VERSION_SUFFIXES = tuple(
    "_" + _vslug(v) for v in sorted({c.version for c in CORPORA if c.version})
)


def _base_kind(slug: str) -> str:
    """Strip the version suffix from a per-version slug
    (``aztec_nr_apiref_v5_0_0_rc_1`` -> ``aztec_nr_apiref``); shared/unversioned
    slugs (``awesome_aztec``) are returned unchanged."""
    for suf in _VERSION_SUFFIXES:
        if slug.endswith(suf):
            return slug[: -len(suf)]
    return slug


# Developer-question-weighted retrieval order, by corpus KIND (version-agnostic).
# Mirror of the AZTEC_SOURCE_IDS comment block in .env. Each kind expands to its
# per-version variants in ``_CANONICAL_ORDER`` below; shared corpora appear once.
_BASE_KIND_ORDER: tuple = (
    "aztec_developer_docs",
    "aztec_nr_apiref",
    "noir_language_docs",
    "aztec_example_contracts",
    "aztec_js_sdk",
    "aztec_typescript_api",
    "noir_stdlib_apiref",
    "aztec_cli",
    "aztec_network_docs",
    # Single-file companion to aztec_network_docs — the unversioned networks
    # page (mainnet vs. testnet L1 contract address table).
    "aztec_site_networks",
    # Unversioned Participate docs (curated token/ + governance/).
    "aztec_participate_docs",
    "aztec_e2e_tests",
    "aztec_protocol_circuits",
    "aztec_l1_contracts",
    # Hand-authored operator-troubleshooting (shared/unversioned). Supplementary.
    "aztec_operator_troubleshooting",
    # Hand-authored token/fees boundaries doc (shared/unversioned). Supplementary.
    "aztec_token_and_fees_curated",
    # Community resource list (awesome-aztec README). Supplementary, ordered last.
    "awesome_aztec",
)


# Concrete canonical order over ALL production corpora (both versions), DERIVED
# from CORPORA so a newly-added corpus or version can't silently drift out of
# the swap order (the old hand-maintained list let that happen). Within a kind,
# the two version variants are ordered by version string (deterministic).
_CANONICAL_ORDER: tuple = tuple(
    c.slug
    for c in sorted(
        (c for c in CORPORA if c.in_production_agent),
        key=lambda c: (_BASE_KIND_ORDER.index(_base_kind(c.slug)), c.version),
    )
)


def _ordered(uploads: dict) -> List[dict]:
    by_slug = {u["slug"]: u for u in uploads if u.get("source_id")}
    return [by_slug[s] for s in _CANONICAL_ORDER if s in by_slug]


# slug → (version, network) from the single source of truth. Used to stamp
# ``sources.metadata`` so the retrieval version-scoping resolver
# (``application/retriever/version_scope.py``) can narrow per request.
_SLUG_META = {c.slug: (c.version, c.network) for c in CORPORA}


def _metadata_stamp_lines(uploads_to_stamp: List[dict]) -> List[str]:
    """SQL to stamp ``sources.metadata.{version,network}`` for uploaded sources.

    THIS is the producer half of the two-version KB: the retrieval narrowing
    reads ``metadata.version`` / ``metadata.network``, so without this stamp
    every source looks unversioned and narrowing is a permanent no-op (an
    unstamped source is always kept). ``jsonb ||`` shallow-merges, so any other
    metadata keys are preserved and a re-run overwrites a stale stamp.

    Shared corpora get ``{"network":"shared"}`` and NO version key (so they're
    retrieved for either active version); versioned corpora get both.
    """
    lines = [
        "-- Stamp sources.metadata.{version,network} for the version-scoping",
        "-- resolver (application/retriever/version_scope.py). WITHOUT this the",
        "-- narrowing is a no-op and both versions are returned for every query.",
    ]
    stamped = 0
    for u in uploads_to_stamp:
        sid = u.get("source_id")
        meta = _SLUG_META.get(u["slug"])
        if not sid or meta is None:
            continue
        version, network = meta
        obj = {"network": network}
        if version:
            obj["version"] = version
        payload = json.dumps(obj)
        lines.append(
            f"UPDATE sources SET metadata = COALESCE(metadata, '{{}}'::jsonb) "
            f"|| '{payload}'::jsonb WHERE id = '{sid}'::uuid;  -- {u['slug']}"
        )
        stamped += 1
    if not stamped:
        return []
    lines.append("")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate SQL to swap agent source lists after re-ingest."
    )
    parser.add_argument("--upload-manifest", required=True,
                        help="Path to the upload manifest produced by scripts.ingest.upload")
    parser.add_argument("--agent-id", required=True,
                        help="UUID of the production agent (target of the UPDATE)")
    parser.add_argument("--apiref-only", action="store_true",
                        help="Only rotate the apiref UUIDs in extra_source_ids; "
                             "preserves the rest of the agent's source list")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Allow default-mode SQL generation even when the "
                             "upload manifest is missing some canonical corpora "
                             f"(currently {len(_CANONICAL_ORDER)}). WITHOUT this "
                             "flag, default mode refuses to emit SQL that would "
                             "truncate the agent's source list.")
    parser.add_argument("--prompt-id", default=None,
                        help="If set, also UPDATE prompt_id on the agent")
    parser.add_argument("--out", default=None,
                        help="Write the SQL to this file (default: stdout)")
    args = parser.parse_args(argv)

    uploads = json.loads(Path(args.upload_manifest).read_text())
    ordered = _ordered(uploads)
    if not ordered:
        print("ERROR: upload manifest contains no successful uploads.", file=sys.stderr)
        return 2

    # Default-mode safety: refuse to emit a partial UPDATE that would
    # silently truncate the agent's source list. The full UPDATE
    # overwrites extra_source_ids, so a manifest missing most of the
    # canonical corpora would leave the agent with only the uploaded one.
    # The operator must opt in via --allow-partial OR --apiref-only.
    if not args.apiref_only and not args.allow_partial:
        present = {u["slug"] for u in ordered}
        missing = [s for s in _CANONICAL_ORDER if s not in present]
        if missing:
            print(
                "ERROR: upload manifest is missing canonical corpora:\n  "
                + ", ".join(missing)
                + "\n\nDefault-mode SQL would truncate the agent's source "
                + "list to only the uploaded corpora. Use one of:\n"
                + "  --apiref-only      (rotate just the apiref UUIDs)\n"
                + "  --allow-partial    (acknowledge the partial set)\n"
                + f"  or upload all {len(_CANONICAL_ORDER)} production corpora "
                + "before generating SQL.",
                file=sys.stderr,
            )
            return 2

    primary = ordered[0]
    extras = ordered[1:]
    extras_array = "ARRAY[" + ",".join(f"'{u['source_id']}'::uuid" for u in extras) + "]"

    sql_lines = [
        "-- Generated by scripts/ingest/swap_sources.py",
        "-- Review carefully before executing.",
        "BEGIN;",
        "",
    ]

    if args.apiref_only:
        sql_lines.append(
            "-- TODO(operator): replace the OLD aztec-nr / noir-stdlib UUIDs "
            "in the agent's extra_source_ids with the NEW apiref UUIDs:"
        )
        apiref_uploads = [
            u for u in uploads
            if _base_kind(u["slug"]) in ("aztec_nr_apiref", "noir_stdlib_apiref")
        ]
        for u in apiref_uploads:
            sql_lines.append(f"--   {u['slug']:25s} → {u['source_id']}")
        sql_lines.append(
            "-- Inspect the current array first:"
        )
        sql_lines.append(
            f"-- SELECT source_id, extra_source_ids FROM agents "
            f"WHERE id = '{args.agent_id}';"
        )
        sql_lines.append("")
        # The rotated apiref sources still need their version/network stamp.
        sql_lines += _metadata_stamp_lines(apiref_uploads)
    else:
        sql_lines += [
            "UPDATE agents",
            f"   SET source_id        = '{primary['source_id']}'::uuid,",
            f"       extra_source_ids = {extras_array}::uuid[]",
            f" WHERE id = '{args.agent_id}'::uuid;",
            "",
        ]
        if args.prompt_id:
            sql_lines += [
                f"UPDATE agents SET prompt_id = '{args.prompt_id}'::uuid",
                f" WHERE id = '{args.agent_id}'::uuid;",
                "",
            ]
        sql_lines += _metadata_stamp_lines(ordered)

    sql_lines += ["COMMIT;", ""]

    env_lines = [
        "# AZTEC_SOURCE_IDS canonical-order block",
        "# Paste this into .env (replacing the existing block) and",
        "# `docker compose up -d --force-recreate backend worker` to apply.",
        "AZTEC_SOURCE_IDS=" + ",".join(u["source_id"] for u in ordered),
        "",
    ]
    for u in ordered:
        env_lines.append(f"#   {u['slug']:25s} {u['name']:50s} → {u['source_id']}")

    output = "\n".join(sql_lines + ["", "-- ---------- .env block ----------"] + env_lines)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"wrote: {args.out}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())

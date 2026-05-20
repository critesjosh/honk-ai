"""Build all 13 Aztec corpora as zip files ready for upload.

Usage::

    python -m scripts.ingest.build \
        --aztec-pkg      /tmp/aztec-v4.3.0 \
        --aztec-pkg-docs /tmp/aztec-v4.3.0-docs \
        --noir           /tmp/noir-v4.3.0 \
        --out            /tmp/aztec-corpora-build \
        [--corpus aztec_nr_apiref]   # optional: limit to one corpus

Three source roots are accepted (Option B per ``PLAN-v4.3.0-bump.md``)
because the corpora are pinned at different upstream commits:

  * ``--aztec-pkg``      → aztec-packages at the release tag
                           (``v4.3.0``). Used for code corpora + the
                           auto-generated TypeScript API reference.
  * ``--aztec-pkg-docs`` → aztec-packages at a ``next``-branch snapshot
                           commit that contains the new
                           ``version-vX.Y.Z/`` Docusaurus folder. The
                           docs version snapshot is taken from a
                           moving branch so the literal release tag
                           does NOT have it.
  * ``--noir``           → noir-lang/noir at the commit pinned by
                           aztec-packages' ``noir/noir-repo`` submodule
                           at the release tag.

A flag is required only if one of the selected corpora actually needs
that root — single-corpus builds (``--corpus aztec_nr_apiref``) can
omit unrelated flags.

For each corpus this writes:
  ``<out>/zips/<slug>.zip``         the upload-ready zip
  ``<out>/manifests/<slug>.json``   per-corpus build manifest
  ``<out>/build_manifest.json``     overall build summary

Re-run is idempotent: existing outputs are overwritten.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import shutil
import sys
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from scripts.ingest.corpora import CORPORA, Corpus, SourceTree, get_corpus
from scripts.ingest import noir_apiref

logger = logging.getLogger(__name__)


# Path segments excluded from every corpus build — mirrors the deny
# list enforced by ``application/parser/file/bulk.py:_IGNORED_PATH_SEGMENTS``,
# but applied at build time so the zip stays small. This matters for
# embedding cost (every chunk we don't ingest is a chunk we don't pay
# OpenAI for).
_IGNORED_DIR_NAMES = {
    "fixtures", "dumps", "node_modules", "target", "dist", "build",
    "_out", "__pycache__", ".git",
    # Apiref-corpus extras
    "test", "tests",  # drop trait/impl test trees in noir_stdlib
    # Compiled or auto-generated:
    "codegenCache",
}


_SOURCE_ROOT_TO_FLAG = {
    "aztec-packages": "--aztec-pkg",
    "aztec-packages-docs": "--aztec-pkg-docs",
    "noir": "--noir",
}


def _resolve_source_dir(corpus: Corpus, tree: SourceTree, roots: dict) -> Path:
    root = roots.get(corpus.source_root)
    if root is None:
        flag = _SOURCE_ROOT_TO_FLAG.get(corpus.source_root, "<unknown>")
        raise SystemExit(
            f"corpus {corpus.slug!r}: missing root for {corpus.source_root!r}; "
            f"pass {flag} on the CLI"
        )
    full = Path(root) / tree.path
    if not full.is_dir():
        raise SystemExit(
            f"corpus {corpus.slug!r}: source path does not exist: {full}"
        )
    return full


def _walk_files(
    src_dir: Path,
    extensions: tuple,
    exclude_paths: tuple = (),
    include_paths: tuple = (),
) -> List[Path]:
    """Walk ``src_dir`` and return matching files in stable order.

    ``exclude_paths`` is a tuple of fnmatch patterns evaluated against
    each file's relative path under ``src_dir`` (forward-slash form,
    no leading slash). Patterns may target directories — e.g.
    ``"foo/bar/*"`` excludes every file under ``foo/bar``.

    ``include_paths`` is an optional fnmatch-style allowlist with the
    same path semantics. When non-empty, only files matching one of
    its patterns are kept (extension + exclude filters still apply).
    Empty is "no allowlist" — every file passes this stage.
    """
    out: List[Path] = []
    ext_lower = tuple(e.lower() for e in extensions)
    for path in src_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src_dir)
        rel_posix = rel.as_posix()
        # Skip if any ignored segment appears in the path
        if any(p in _IGNORED_DIR_NAMES for p in rel.parts):
            continue
        # Skip per-corpus exclusions
        if exclude_paths and any(
            fnmatch.fnmatch(rel_posix, pat) for pat in exclude_paths
        ):
            continue
        # Apply per-tree allowlist if one is configured
        if include_paths and not any(
            fnmatch.fnmatch(rel_posix, pat) for pat in include_paths
        ):
            continue
        if path.suffix.lower() in ext_lower:
            out.append(path)
    out.sort()
    return out


def _build_passthrough(
    corpus: Corpus, src_dirs: List[Path], staging: Path,
) -> dict:
    """Copy files into ``<staging>/<tree.zip_prefix>/...`` preserving
    each file's relative path under its source dir. Each tree contributes
    its own top-level prefix inside the zip — for the CLI corpus this
    is ``cli/`` and ``cli-wallet/``."""
    files_copied = 0
    for src_dir, tree in zip(src_dirs, corpus.trees):
        rel_root = staging / tree.zip_prefix.rstrip("/")
        rel_root.mkdir(parents=True, exist_ok=True)
        for f in _walk_files(
            src_dir,
            corpus.include_extensions,
            tree.exclude_paths,
            tree.include_paths,
        ):
            rel = f.relative_to(src_dir)
            target = rel_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            files_copied += 1
    return {"files_copied": files_copied}


def _build_rename_code_to_txt(
    corpus: Corpus, src_dirs: List[Path], staging: Path,
) -> dict:
    """Like passthrough, but each code file gets ``.txt`` appended to
    its filename so the DocsGPT parser allowlist accepts it."""
    files_copied = 0
    for src_dir, tree in zip(src_dirs, corpus.trees):
        rel_root = staging / tree.zip_prefix.rstrip("/")
        rel_root.mkdir(parents=True, exist_ok=True)
        for f in _walk_files(
            src_dir,
            corpus.include_extensions,
            tree.exclude_paths,
            tree.include_paths,
        ):
            rel = f.relative_to(src_dir)
            target = rel_root / (str(rel) + ".txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            files_copied += 1
    return {"files_copied": files_copied}


def _build_noir_apiref(
    corpus: Corpus, src_dirs: List[Path], staging: Path,
) -> dict:
    """Run the Noir → apiref Markdown transform over each source dir
    and place results under ``<staging>/<tree.zip_prefix>/...``."""
    aggregate = {
        "files_seen": 0, "files_emitted": 0,
        "files_with_parse_errors": 0, "files_with_zero_items": 0,
        "totals_by_kind": {},
    }
    for src_dir, tree in zip(src_dirs, corpus.trees):
        if tree.include_paths:
            # noir_apiref.transform_tree doesn't take an allowlist yet;
            # fail loud so a config typo can't silently produce an
            # under-included corpus.
            raise SystemExit(
                f"corpus {corpus.slug!r}: include_paths is not yet supported "
                f"for the noir_apiref transform"
            )
        out_dir = staging / tree.zip_prefix.rstrip("/")
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = noir_apiref.transform_tree(
            input_root=src_dir,
            output_root=out_dir,
            rel_prefix=tree.zip_prefix.rstrip("/"),
            exclude_paths=tree.exclude_paths,
        )
        aggregate["files_seen"] += manifest["files_seen"]
        aggregate["files_emitted"] += manifest["files_emitted"]
        aggregate["files_with_parse_errors"] += manifest["files_with_parse_errors"]
        aggregate["files_with_zero_items"] += manifest["files_with_zero_items"]
        for k, v in manifest["totals_by_kind"].items():
            aggregate["totals_by_kind"][k] = aggregate["totals_by_kind"].get(k, 0) + v
    return aggregate


def _zip_directory(staging: Path, zip_path: Path) -> int:
    """Zip everything under ``staging`` into ``zip_path``. Returns the
    number of file entries written."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            arcname = str(path.relative_to(staging))
            z.write(path, arcname)
            n += 1
    return n


def build_corpus(
    corpus: Corpus,
    out_dir: Path,
    roots: dict,
) -> dict:
    """Build a single corpus. Returns a per-corpus manifest dict."""
    logger.info("Building corpus %s (transform=%s)", corpus.slug, corpus.transform)

    src_dirs = [_resolve_source_dir(corpus, t, roots) for t in corpus.trees]

    with tempfile.TemporaryDirectory(prefix=f"corpus-{corpus.slug}-") as td:
        staging = Path(td)

        if corpus.transform == "passthrough":
            stats = _build_passthrough(corpus, src_dirs, staging)
        elif corpus.transform == "rename_code_to_txt":
            stats = _build_rename_code_to_txt(corpus, src_dirs, staging)
        elif corpus.transform == "noir_apiref":
            stats = _build_noir_apiref(corpus, src_dirs, staging)
        else:
            raise SystemExit(f"unknown transform: {corpus.transform}")

        zip_path = out_dir / "zips" / f"{corpus.slug}.zip"
        zip_files = _zip_directory(staging, zip_path)

    # asdict() ships only declared fields; rel_prefix is a @property on
    # Corpus, so inject it explicitly. upload.py reads it from the
    # manifest to send the right ``rel_prefix`` form-field with each
    # upload (the backend uses it as the parser ``input_dir`` so
    # metadata.source ends up zip-relative).
    corpus_dict = asdict(corpus)
    corpus_dict["rel_prefix"] = corpus.rel_prefix
    manifest = {
        "corpus": corpus_dict,
        "source_dirs": [str(d) for d in src_dirs],
        "zip_path": str(zip_path),
        "zip_files": zip_files,
        "transform_stats": stats,
    }
    manifest_path = out_dir / "manifests" / f"{corpus.slug}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    logger.info(
        "  → %s (%d zip entries, transform: %s)",
        zip_path, zip_files, stats,
    )
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build all Aztec DocsGPT corpora as upload-ready zips."
    )
    parser.add_argument(
        "--aztec-pkg",
        required=False,
        help="Path to a checkout of aztec-packages at the desired release "
             "tag (e.g. /tmp/aztec-v4.3.0). Required for any corpus whose "
             "source_root is 'aztec-packages' (code corpora + the "
             "auto-generated TypeScript API reference).",
    )
    parser.add_argument(
        "--aztec-pkg-docs",
        required=False,
        help="Path to a checkout of aztec-packages at the ``next``-branch "
             "commit containing the new ``version-vX.Y.Z/`` docs folder "
             "(e.g. /tmp/aztec-v4.3.0-docs). Required for the rendered-"
             "docs corpora (developer / network / site-networks).",
    )
    parser.add_argument(
        "--noir",
        required=False,
        help="Path to a checkout of noir-lang/noir at the commit pinned "
             "by the aztec-packages release (via the noir/noir-repo "
             "submodule). Required for any corpus whose source_root is "
             "'noir'.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory; zips land in <out>/zips/, manifests in "
             "<out>/manifests/",
    )
    parser.add_argument(
        "--corpus",
        action="append",
        help="Build only the named corpus slug(s) (repeatable). "
             "Defaults to all 13.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    roots = {}
    if args.aztec_pkg:
        roots["aztec-packages"] = Path(args.aztec_pkg).resolve()
    if args.aztec_pkg_docs:
        roots["aztec-packages-docs"] = Path(args.aztec_pkg_docs).resolve()
    if args.noir:
        roots["noir"] = Path(args.noir).resolve()

    selected = (
        [get_corpus(s) for s in args.corpus] if args.corpus else list(CORPORA)
    )

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    overall = {"out": str(out_dir), "corpora": []}
    for c in selected:
        overall["corpora"].append(build_corpus(c, out_dir, roots))

    overall_path = out_dir / "build_manifest.json"
    overall_path.write_text(json.dumps(overall, indent=2), encoding="utf-8")
    print(f"\nbuilt {len(selected)} corpora → {out_dir}")
    print(f"build manifest: {overall_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

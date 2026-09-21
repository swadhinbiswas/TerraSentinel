"""Enumerate which bronze prefixes actually exist, and hand dbt explicit globs.

Why this exists. The staging models used to glob both layouts directly:

    read_parquet(['<root>/<source>/**/*.parquet', '<root>/backfill/<source>/**/*.parquet'])

Two problems, both discovered against real data:

1. **A missing prefix is fatal.** Right after a backfill there is no `<source>/`
   directory at all, so that glob 404s and the model dies even though every row it
   needs sits under `backfill/`. That is exactly the post-Phase-0 state, and for a
   weekly source it lasts up to a week.
2. **`**` is not portable.** The single-pattern workaround
   `<root>/**/<source>/**/*.parquet` works over `hf://` but DuckDB's *local* glob
   rejects it with "Cannot use multiple '**' in one path".

So the listing happens here instead: one authenticated, retried API call, and dbt
receives only globs that are known to match. A source with no files at all is then
reported as a clear compiler error ("run the backfill first") rather than as a
filesystem error from four layers down.

    python -m tools.bronze_manifest --repo-id swadhinbiswas/TerraSentinel \\
        --root hf://datasets/swadhinbiswas/TerraSentinel --out transform/target/bronze_globs.json

    # same code path against a local lake (used by CI)
    python -m tools.bronze_manifest --local-root data/bronze --root data/bronze
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from collectors.config import SOURCES, hf_repo
from storage.hf_dataset_reader import HFDatasetReader


def manifest_from_paths(
    paths: Iterable[str],
    root: str,
    *,
    source_ids: Sequence[str] = tuple(SOURCES),
) -> dict[str, list[str]]:
    """Map each source to the globs for the layouts that actually contain files.

    ``paths`` are repo-relative (``firms/region=.../file.parquet``), which is what
    both the Hub file listing and a local tree walk can produce.
    """
    materialised = list(paths)
    manifest: dict[str, list[str]] = {}
    for source_id in source_ids:
        globs: list[str] = []
        if any(path.startswith(f"{source_id}/") for path in materialised):
            globs.append(f"{root}/{source_id}/**/*.parquet")
        if any(path.startswith(f"backfill/{source_id}/") for path in materialised):
            globs.append(f"{root}/backfill/{source_id}/**/*.parquet")
        manifest[source_id] = globs
    return manifest


def hub_manifest(
    repo_id: str | None = None,
    *,
    root: str | None = None,
    reader: HFDatasetReader | None = None,
) -> dict[str, list[str]]:
    """List a Hub dataset repo once and derive the per-source globs."""
    resolved_repo = repo_id or hf_repo("bronze")
    resolved_root = root or f"hf://datasets/{resolved_repo}"
    files = (reader or HFDatasetReader(repo_id=resolved_repo)).list_files("")
    return manifest_from_paths(files, resolved_root)


def local_manifest(local_root: str | Path, *, root: str | None = None) -> dict[str, list[str]]:
    """Same manifest derived from a local lake, so CI exercises the production path."""
    base = Path(local_root)
    if not base.is_dir():
        raise FileNotFoundError(f"local bronze root {base} does not exist")
    relative = [
        path.relative_to(base).as_posix() for path in base.rglob("*.parquet")
    ]
    return manifest_from_paths(relative, root or str(base))


def missing_sources(manifest: dict[str, list[str]]) -> list[str]:
    return sorted(source_id for source_id, globs in manifest.items() if not globs)


#: Staging model per source. A source with no files would make its staging model resolve to
#: a self-describing missing relation, which fails `dbt build` — and a failed build skips
#: everything downstream of it, including the sync that publishes the marts that *are*
#: ready. Excluding an absent source's branch keeps its failure to itself, which is the same
#: principle the one-mart-per-source split applied one level down.
STAGING_MODEL_BY_SOURCE: dict[str, str] = {
    "firms": "stg_firms",
    "sentinel": "stg_sentinel",
    "noaa_nsidc": "stg_noaa",
}


def exclusion_selector(manifest: dict[str, list[str]]) -> str:
    """A dbt ``--exclude`` selector covering absent sources and their descendants."""
    return " ".join(
        f"{STAGING_MODEL_BY_SOURCE[source]}+"
        for source in missing_sources(manifest)
        if source in STAGING_MODEL_BY_SOURCE
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo-id", help="Hub dataset repo to list, e.g. swadhinbiswas/TerraSentinel")
    source.add_argument("--local-root", help="local bronze root to walk instead")
    parser.add_argument("--root", default=None, help="glob prefix (default: derived from --repo-id)")
    parser.add_argument("--out", default=None, help="write the manifest JSON here as well as stdout")
    parser.add_argument(
        "--emit-exclude",
        default=None,
        help="write a dbt --exclude selector for sources with no data (may be empty)",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="do not exit non-zero when a source has no files",
    )
    args = parser.parse_args(argv)

    try:
        if args.repo_id:
            manifest = hub_manifest(args.repo_id, root=args.root)
        else:
            manifest = local_manifest(args.local_root, root=args.root)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear CLI failure
        print(f"could not build bronze manifest: {exc}", file=sys.stderr)
        return 2

    payload = {"bronze_globs": manifest}
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2))

    absent = missing_sources(manifest)
    selector = exclusion_selector(manifest)
    if args.emit_exclude:
        destination = Path(args.emit_exclude)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(selector, encoding="utf-8")

    if absent:
        # Printed on stdout so it lands in the workflow log rather than only in a step
        # summary: "which marts did not build, and why" is the first question a reader has.
        print(
            f"\nno bronze files for source(s) {absent}: run the backfill, or exclude their "
            f"branches from the dbt build"
            + (f" (--exclude {selector})" if selector else ""),
            file=sys.stderr,
        )
        if len(absent) == len(manifest):
            print(
                "every source is absent — run the backfill before the transform",
                file=sys.stderr,
            )
            return 1
    if absent and not args.allow_missing and not args.emit_exclude:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

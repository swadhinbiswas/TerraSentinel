"""Mirror the bronze lake locally before transforming.

Why this exists. DuckDB's ``hf://`` filesystem lists the Hub repository tree on
*every* query that references a remote path, and the Hub allows 1000 API requests
per 5 minutes. That makes remote parquet the wrong thing for a transform job to
sit on:

* with staging as views, each of ~50 dbt tests re-queried the remote path and each
  query re-listed the tree — one build attempted 50+ listings and was rate-limited
  immediately;
* as tables it is still several listings per build, and any HTTP hiccup mid-run
  fails a model four layers deep;
* and every read pays HTTPS round trips per file instead of local I/O.

A one-time mirror turns all of that into **one listing plus parallel file fetches
over the CDN**, after which the whole build is local, deterministic, and faster.
The lake is small by design — numeric features, not imagery — so this is cheap.

    python -m tools.sync_bronze --local-dir data/bronze_hf
    python -m tools.sync_bronze --local-dir data/bronze_hf --revision <commit-sha>

Pinning ``--revision`` makes a run reproducible against a known lake state, which
is also what a model's dataset-commit lineage refers to.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from collectors.config import hf_repo
from ops.redact import redact_text
from ops.resilience import RetryPolicy, TransientError, retry_call
from storage.hf_dataset_reader import HFDatasetReader


def mirror_size(directory: Path) -> tuple[int, int]:
    """``(lake_file_count, lake_bytes)`` of a mirrored tree.

    Hugging Face's client keeps download metadata under ``.cache/``; counting that
    would overstate the lake and make "how big is the lake" unanswerable.
    """
    files = [
        path
        for path in directory.rglob("*")
        if path.is_file() and ".cache" not in path.parts
    ]
    return len(files), sum(path.stat().st_size for path in files)


def sync_bronze(
    *,
    local_dir: str | Path,
    repo_id: str | None = None,
    revision: str = "main",
    allow_patterns: list[str] | None = None,
    reader: HFDatasetReader | None = None,
    policy: RetryPolicy | None = None,
    sleep: object = None,
) -> dict[str, object]:
    """Download (or refresh) the bronze repo into ``local_dir``.

    Returns a small report rather than raising on an already-populated directory:
    the Hub client skips unchanged files, so re-running is cheap and idempotent.
    """
    resolved_repo = repo_id or hf_repo("bronze")
    target = Path(local_dir)
    reader = reader or HFDatasetReader(repo_id=resolved_repo, revision=revision)

    attempt_policy = policy or RetryPolicy(attempts=4, base_delay=5.0, max_delay=60.0)
    kwargs = {"sleep": sleep} if sleep is not None else {}

    def download() -> Path:
        return reader.snapshot(target, allow_patterns=allow_patterns, revision=revision)

    path = retry_call(
        download,
        policy=attempt_policy,
        description=f"mirror {resolved_repo}",
        **kwargs,  # type: ignore[arg-type]
    )
    files, total_bytes = mirror_size(path)
    return {
        "repo_id": resolved_repo,
        "revision": revision,
        "local_dir": str(path),
        "files": files,
        "bytes": total_bytes,
        "megabytes": round(total_bytes / 1e6, 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local-dir", default="data/bronze_hf", help="destination (default: %(default)s)")
    parser.add_argument("--repo-id", default=None, help="defaults to HF_NAMESPACE/TerraSentinel")
    parser.add_argument("--revision", default=os.environ.get("HF_REVISION", "main"))
    parser.add_argument(
        "--include",
        action="append",
        default=None,
        help="only mirror matching paths (repeatable), e.g. --include 'backfill/firms/**'",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        report = sync_bronze(
            local_dir=args.local_dir,
            repo_id=args.repo_id,
            revision=args.revision,
            allow_patterns=args.include,
        )
    except TransientError as exc:
        print(f"could not mirror the bronze lake: {redact_text(exc)}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"mirrored {report['files']} file(s), {report['megabytes']} MB "
            f"from {report['repo_id']}@{report['revision']} into {report['local_dir']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

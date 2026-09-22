"""Databricks Workflow entry: batch-score the latest features.

Wraps ``ml.scoring.batch_score`` with the Databricks path conventions: the
gold DuckDB file comes from ``$DATABRICKS_DUCKDB_PATH`` (the UC Volume written
by the transform job), predictions land wherever ``--out`` points, and the
credentials the scorer *publishes* with (Hub snapshot, Turso write) are
resolved through ``collectors.config.get_secret`` — environment first, secret
scope second — then exported for the libraries that read ``os.environ``
directly.

Self-contained like the other entry scripts: ``spark_python_task`` stages only
this file, so the repo root resolves from ``--repo-dir`` / ``TERRASENTINEL_ROOT``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: Env vars batch_score publishes with; exported from get_secret so both the
#: env-first and the secret-scope paths work for code reading os.environ.
_PUBLISH_ENV = (
    "HF_TOKEN",
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "TURSO_TOKEN_RO",
)


def _repo_root(explicit: str | None) -> Path:
    """Locate the bundle-synced repo root (the driver has only this file)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_root = (os.environ.get("TERRASENTINEL_ROOT") or "").strip()
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parents[2])
    for candidate in candidates:
        for path in (candidate, Path("/Workspace") / str(candidate).lstrip("/")):
            if (path / "collectors" / "config.py").is_file():
                return path
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise SystemExit(f"repo root not found (tried: {tried}); pass --repo-dir or set TERRASENTINEL_ROOT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=None, help="bundle-synced repo root")
    parser.add_argument("--duckdb-path", default=None, help="default: $DATABRICKS_DUCKDB_PATH")
    parser.add_argument("--out", default="/tmp/terrasentinel/ml/artifacts/predictions.parquet")
    parser.add_argument("--model-dir", default=None, help="local bundle dir (default: snapshot from the Hub)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = _repo_root(args.repo_dir)
    sys.path.insert(0, str(root))

    from collectors.config import get_secret

    for name in _PUBLISH_ENV:
        value = get_secret(name, required=False)
        if value:
            os.environ.setdefault(name, value)

    duckdb_path = (
        args.duckdb_path
        or os.environ.get("DATABRICKS_DUCKDB_PATH")
        or os.environ.get("DUCKDB_PATH")
        or "transform/terrasentinel.duckdb"
    )

    from ml.scoring.batch_score import main as score_main

    score_args = ["--duckdb-path", duckdb_path, "--out", args.out]
    if args.model_dir:
        score_args += ["--model-dir", args.model_dir]
    if args.dry_run:
        score_args.append("--dry-run")
    print(f"[batch_score] duckdb={duckdb_path} out={args.out}")
    return score_main(score_args)


if __name__ == "__main__":
    raise SystemExit(main())

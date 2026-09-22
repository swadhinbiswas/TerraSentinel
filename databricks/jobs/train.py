"""Databricks Workflow entry: staged ML dispatch (features -> train -> register).

One script, three task invocations (see ``databricks/resources/jobs.yml``):

* ``--stage features``  build the feature table inside the gold DuckDB file
* ``--stage train``     fit Isolation Forest (``--mlflow-uri databricks``
  logs the run to the workspace's MLflow; omit to keep local tracking)
* ``--stage register``  publish the artifact bundle to the Hub model registry

Hand-offs: features live in the DuckDB file on the UC Volume; the trained
bundle moves through ``--out-dir`` / ``--bundle-dir`` (cluster-local ``/tmp``
on the shared single-node job cluster).

Self-contained like the other entry scripts: ``spark_python_task`` stages only
this file, so the repo root resolves from ``--repo-dir`` / ``TERRASENTINEL_ROOT``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


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


def _default_duckdb() -> str:
    return (
        os.environ.get("DATABRICKS_DUCKDB_PATH")
        or os.environ.get("DUCKDB_PATH")
        or "transform/terrasentinel.duckdb"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=["features", "train", "register"])
    parser.add_argument("--repo-dir", default=None, help="bundle-synced repo root")
    parser.add_argument("--duckdb-path", default=None, help="default: $DATABRICKS_DUCKDB_PATH")
    parser.add_argument("--kind", default="fire")
    parser.add_argument("--out-dir", default="/tmp/terrasentinel/ml/artifacts/isolation_forest_fire")
    parser.add_argument("--bundle-dir", default="/tmp/terrasentinel/ml/artifacts/isolation_forest_fire")
    parser.add_argument("--mlflow-uri", default=None, help='e.g. "databricks" on a job cluster')
    args = parser.parse_args(argv)

    root = _repo_root(args.repo_dir)
    sys.path.insert(0, str(root))

    # Both the Hub registry and the bronze-revision lookup read os.environ.
    from collectors.config import get_secret

    token = get_secret("HF_TOKEN", required=False)
    if token:
        os.environ.setdefault("HF_TOKEN", token)

    duckdb_path = args.duckdb_path or _default_duckdb()

    if args.stage == "features":
        from ml.features.build_feature_table import main as stage_main

        return stage_main(["--duckdb-path", duckdb_path, "--kind", args.kind])

    if args.stage == "train":
        from ml.train.train_isolation_forest import main as stage_main

        stage_args = ["--duckdb-path", duckdb_path, "--out-dir", args.out_dir]
        if args.mlflow_uri:
            stage_args += ["--mlflow-uri", args.mlflow_uri]
        print(f"[train] stage=train duckdb={duckdb_path} out={args.out_dir}")
        return stage_main(stage_args)

    from ml.registry.push_model_to_hf import main as stage_main

    print(f"[train] stage=register bundle={args.bundle_dir}")
    return stage_main(["--bundle-dir", args.bundle_dir])


if __name__ == "__main__":
    raise SystemExit(main())

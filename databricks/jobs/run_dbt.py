"""Databricks Workflow entry: mirror bronze, then dbt build into the UC Volume.

Step-for-step parity with ``.github/workflows/transform.yml``:

1. seeds must match the Python registries (``tools.export_seeds --check``)
2. mirror the bronze lake from the Hub once (one listing + parallel fetches
   instead of DuckDB re-listing the repo tree per query)
3. enumerate what actually exists (``tools.bronze_manifest``) and hand dbt the
   glob list, excluding sources that were never backfilled
4. ``dbt build`` with the profile target chosen by ``--target``

Cluster differences from Actions: the bundle-synced workspace tree is
read-only, so dbt's target/log output goes to ``/tmp`` (``DBT_TARGET_PATH`` /
``DBT_LOG_PATH``), and the DuckDB database file lives on a Unity Catalog
Volume (profile target ``databricks``, path in ``DATABRICKS_DUCKDB_PATH``).

The next task in the job (``merge_gold``) publishes those marts into Unity
Catalog with the generated MERGE statements.
"""

from __future__ import annotations

import argparse
import json
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=None, help="bundle-synced repo root")
    parser.add_argument("--target", default="dev", help="dbt target from transform/profiles.yml")
    parser.add_argument("--bronze-dir", default="/tmp/terrasentinel/bronze_hf", help="cluster-local bronze mirror")
    parser.add_argument("--work-dir", default="/tmp/terrasentinel/dbt", help="cluster-local dbt target/log dir")
    args = parser.parse_args(argv)

    root = _repo_root(args.repo_dir)
    sys.path.insert(0, str(root))

    from collectors.config import get_secret

    token = get_secret("HF_TOKEN", required=False)
    if token:
        os.environ.setdefault("HF_TOKEN", token)

    bronze = Path(args.bronze_dir)
    work = Path(args.work_dir)
    target_path = work / "target"
    logs_path = work / "logs"
    for directory in (bronze, target_path, logs_path):
        directory.mkdir(parents=True, exist_ok=True)

    # 1. Seeds must match the Python region/source registries — the same guard
    # CI runs, cheap enough to keep here so a drifted deploy fails before dbt.
    from tools.export_seeds import main as seeds_main

    code = seeds_main(["--check"])
    if code:
        print("[run_dbt] seed drift: regenerate with `python -m tools.export_seeds`", file=sys.stderr)
        return code

    # 2. One listing + parallel CDN fetches instead of httpfs re-listing hf://
    # paths on every query (see transform.yml for the quota history).
    from tools.sync_bronze import main as sync_main

    code = sync_main(["--local-dir", str(bronze)])
    if code:
        return code

    # 3. What prefixes exist is a local walk — no API calls. Absent sources
    # are excluded rather than failed, so a not-yet-backfilled branch cannot
    # blank the whole build.
    from tools.bronze_manifest import main as manifest_main

    globs_file = target_path / "bronze_globs.json"
    exclude_file = target_path / "dbt_exclude.txt"
    code = manifest_main(
        [
            "--local-root",
            str(bronze),
            "--root",
            str(bronze),
            "--out",
            str(globs_file),
            "--emit-exclude",
            str(exclude_file),
            "--allow-missing",
        ]
    )
    if code:
        return code

    globs = json.loads(globs_file.read_text(encoding="utf-8"))
    dbt_vars = {"bronze_root": str(bronze), "bronze_globs": globs}
    exclude_tokens = exclude_file.read_text(encoding="utf-8").split() if exclude_file.exists() else []
    if exclude_tokens:
        print(f"[run_dbt] excluding absent sources: {' '.join(exclude_tokens)}")

    os.environ["DBT_TARGET_PATH"] = str(target_path)
    os.environ["DBT_LOG_PATH"] = str(logs_path)

    # 4. dbt build. The programmatic runner avoids depending on a console
    # script being on PATH in the cluster image.
    from dbt.cli.main import dbtRunner

    dbt_args = [
        "build",
        "--project-dir",
        str(root / "transform"),
        "--profiles-dir",
        str(root / "transform"),
        "--target",
        args.target,
        "--vars",
        json.dumps(dbt_vars),
        "--no-partial-parse",
    ]
    if exclude_tokens:
        dbt_args += ["--exclude", *exclude_tokens]

    print(f"[run_dbt] dbt {' '.join(dbt_args[:6])}... (target={args.target})")
    result = dbtRunner().invoke(dbt_args)
    if result.exception is not None:
        print(f"[run_dbt] dbt raised: {result.exception}", file=sys.stderr)
        return 1
    if result.success is not True:
        print("[run_dbt] dbt build failed", file=sys.stderr)
        return 1
    duckdb_path = os.environ.get("DATABRICKS_DUCKDB_PATH", "")
    print(f"[run_dbt] build ok -> {duckdb_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

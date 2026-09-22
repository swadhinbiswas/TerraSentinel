"""Databricks Workflow entry: run one TerraSentinel collector.

Deliberately self-contained — ``spark_python_task`` stages only this file on
the driver, so sibling modules cannot be imported. The repo root (needed for
``collectors.*`` imports) resolves from ``--repo-dir``, then the
``TERRASENTINEL_ROOT`` env var set by the bundle, then this file's own
location (local development).

Only the *requested* source's module is imported: the collectors pull in
source-specific dependencies (earthengine-api, pydap) that other collect
tasks deliberately do not install.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

#: source id -> module owning its collector class. Tested equal to
#: ``collectors.backfill_historical.COLLECTOR_CLASSES`` in tests/test_databricks.py,
#: so the backfill registry stays the source of truth for the set of sources.
SOURCE_MODULES: dict[str, str] = {
    "firms": "collectors.firms_collector",
    "sentinel": "collectors.sentinel_gee_collector",
    "noaa_nsidc": "collectors.noaa_nsidc_collector",
    "entsoe": "collectors.entsoe_collector",
}


def _repo_root(explicit: str | None) -> Path:
    """Locate the bundle-synced repo root (the driver has only this file)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_root = (os.environ.get("TERRASENTINEL_ROOT") or "").strip()
    if env_root:
        candidates.append(Path(env_root))
    # Local development: the script lives at <root>/databricks/jobs/collect.py.
    candidates.append(Path(__file__).resolve().parents[2])
    for candidate in candidates:
        # Workspace files are FUSE-mounted under /Workspace on clusters.
        for path in (candidate, Path("/Workspace") / str(candidate).lstrip("/")):
            if (path / "collectors" / "config.py").is_file():
                return path
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise SystemExit(f"repo root not found (tried: {tried}); pass --repo-dir or set TERRASENTINEL_ROOT")


def _export_hub_env() -> None:
    """The Hub client reads ``os.environ`` directly; mirror what get_secret resolves."""
    from collectors.config import get_secret

    token = get_secret("HF_TOKEN", required=False)
    if token:
        os.environ.setdefault("HF_TOKEN", token)


def _materialise_gee_account() -> None:
    """The GEE collector wants a JSON *file path*; the secret scope stores the JSON text."""
    from collectors.config import get_secret

    raw = get_secret("GEE_SERVICE_ACCOUNT_JSON", required=False)
    if not raw or not raw.lstrip().startswith("{"):
        return  # already a path (the .env / GitHub Secrets shape), or absent
    path = Path("/tmp/terrasentinel/gee-service-account.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    os.environ["GEE_SERVICE_ACCOUNT_JSON"] = str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source id: firms | sentinel | noaa_nsidc | entsoe | ...")
    parser.add_argument(
        "--repo-dir",
        default=None,
        help="bundle-synced repo root (default: $TERRASENTINEL_ROOT)",
    )
    # Forward everything we do not recognise verbatim to the collector's own CLI.
    args, forwarded = parser.parse_known_args(argv)

    root = _repo_root(args.repo_dir)
    sys.path.insert(0, str(root))
    _export_hub_env()
    _materialise_gee_account()

    module_name = SOURCE_MODULES.get(args.source)
    if module_name is None:
        raise SystemExit(f"unknown source {args.source!r}; known: {sorted(SOURCE_MODULES)}")

    from collectors.base_collector import BaseCollector, main_for

    module = importlib.import_module(module_name)
    classes = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BaseCollector)
        and value is not BaseCollector
    ]
    if not classes:
        raise SystemExit(f"no BaseCollector subclass in {module_name}")
    collector_cls = classes[0]

    print(f"[collect] source={args.source} collector={collector_cls.__name__} args={forwarded}")
    return main_for(collector_cls, forwarded)


if __name__ == "__main__":
    raise SystemExit(main())

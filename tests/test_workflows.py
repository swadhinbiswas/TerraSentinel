"""The workflows in ``.github/workflows`` must agree with the code they run.

Two contracts live here:

1. **Installs come from ``requirements/*.lock``.** Nothing resolves
   ``pyproject.toml`` at job time, so a fresh release on PyPI cannot silently
   change what CI tests or what the collectors run. Alert steps may install one
   package directly, but even they are pinned.
2. **The schedule crons in ``collect_data.yml`` are exactly the cadences
   registered in** ``collectors.config.SOURCES`` — as the triggers *and* as the
   gate that decides which matrix leg fires for a given cron.

YAML note: PyYAML parses the bare mapping key ``on:`` as the boolean ``True``,
so the trigger block is read with both spellings.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from collectors.config import SOURCES

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
LOCK_DIR = ROOT / "requirements"

#: Any mention of a lock file, wherever it appears: a ``-r`` path, a ``LOCK=…``
#: shell assignment, or a matrix ``lock:`` field.
_LOCK_NAME = re.compile(r"([A-Za-z0-9_.]+\.lock)")
#: The gate in ``collect_data.yml`` decides a leg with
#: ``[ "$SOURCE" = … ] && [ "$SCHEDULE" = … ]``.
_GATE_PAIR = re.compile(r'\[ "\$SOURCE" = "([^"]+)" \] && \[ "\$SCHEDULE" = "([^"]+)" \]')
#: ``-r requirements/ci.lock`` and ``-r "requirements/$LOCK"`` both count.
_LOCK_ARG = re.compile(r'-r "?requirements/')


def load_all() -> dict[str, dict[str, Any]]:
    """Parse every workflow in the repo. Skips if PyYAML is not installed."""
    yaml = pytest.importorskip("yaml")
    parsed: dict[str, dict[str, Any]] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a mapping"
        parsed[path.name] = document
    assert parsed, f"no workflows found under {WORKFLOW_DIR}"
    return parsed


def load(name: str) -> dict[str, Any]:
    return load_all()[name]


def run_scripts(workflow: dict[str, Any]) -> list[str]:
    """Every ``run:`` script in a workflow, job order preserved."""
    scripts: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if run:
                scripts.append(str(run))
    return scripts


class TestInstallsComeFromLockFiles:
    def test_every_lock_referenced_by_a_workflow_exists(self) -> None:
        for path in WORKFLOW_DIR.glob("*.yml"):
            # GitHub expressions (`${{ matrix.lock }}`) are not file names.
            text = re.sub(r"\$\{\{[^}]*\}\}", "", path.read_text(encoding="utf-8"))
            for lock in _LOCK_NAME.findall(text):
                assert (LOCK_DIR / lock).is_file(), (
                    f"{path.name} mentions requirements/{lock}, which does not exist"
                )

    def test_the_documented_lock_set_is_complete(self) -> None:
        expected = {"base.lock", "gee.lock", "noaa.lock", "transform.lock", "ml.lock", "ci.lock"}
        present = {path.name for path in LOCK_DIR.glob("*.lock")}
        assert expected <= present, f"missing lock files: {expected - present}"

    def test_every_install_line_is_pinned_and_resolves_nothing(self) -> None:
        """Each install either reads a lock with ``--no-deps`` or pins one package."""
        installs = 0
        for name, workflow in load_all().items():
            for script in run_scripts(workflow):
                for line in script.splitlines():
                    if "pip install" not in line:
                        continue
                    installs += 1
                    stripped = line.strip()
                    if stripped.startswith("python -m pip install"):
                        # The failure-only alert steps: one package, one version.
                        assert "requests==" in stripped, (
                            f"{name}: alert step must pin requests, got: {stripped}"
                        )
                        continue
                    assert stripped.startswith("uv pip install"), (
                        f"{name}: unmanaged install (use uv + a lock): {stripped}"
                    )
                    assert "--no-deps" in stripped, (
                        f"{name}: install resolves pyproject.toml at job time: {stripped}"
                    )
                    assert _LOCK_ARG.search(stripped), (
                        f"{name}: install does not read a lock file: {stripped}"
                    )
        assert installs >= 6, "expected at least one install per workflow"

    def test_uv_caching_is_keyed_on_the_lock_files(self) -> None:
        """A cache keyed only on pyproject.toml would not notice a lock change."""
        checked = 0
        for name, workflow in load_all().items():
            for job in (workflow.get("jobs") or {}).values():
                for step in job.get("steps") or []:
                    uses = str(step.get("uses") or "")
                    if not uses.startswith("astral-sh/setup-uv"):
                        continue
                    with_block = step.get("with") or {}
                    if not with_block.get("enable-cache"):
                        continue
                    checked += 1
                    assert "requirements" in str(with_block.get("cache-dependency-glob", "")), (
                        f"{name}: setup-uv cache is not keyed on requirements/*.lock"
                    )
        assert checked >= 1, "expected at least one cached setup-uv step"

    def test_collect_matrix_rows_point_at_real_locks(self) -> None:
        rows = load("collect_data.yml")["jobs"]["collect"]["strategy"]["matrix"]["include"]
        locks = {row["lock"] for row in rows}
        assert locks == {"base.lock", "gee.lock", "noaa.lock"}
        for lock in locks:
            assert (LOCK_DIR / lock).is_file()


class TestSourceCronsMatchTheSourceSpec:
    """collect_data.yml schedules exactly what collectors/config.py registers."""

    @staticmethod
    def _triggers(name: str) -> dict[str, Any]:
        workflow = load(name)
        triggers = workflow.get("on", workflow.get(True))
        assert isinstance(triggers, dict), f"{name} has no readable `on:` block"
        return triggers

    def test_schedule_crons_are_the_registered_cadences(self) -> None:
        crons = [entry["cron"] for entry in self._triggers("collect_data.yml")["schedule"]]
        registered = [spec.cadence_cron for spec in SOURCES.values()]
        assert sorted(crons) == sorted(registered), (
            f"collect_data.yml schedules {sorted(crons)} but "
            f"SOURCES registers {sorted(registered)}"
        )

    def test_gate_pairs_each_source_with_its_own_cron(self) -> None:
        # A run of `15 */6 * * *` must select FIRMS — not merely *some* source
        # whose cron string appears somewhere in the script.
        script = "\n".join(run_scripts(load("collect_data.yml")))
        pairs = dict(_GATE_PAIR.findall(script))
        expected = {source: spec.cadence_cron for source, spec in SOURCES.items()}
        assert pairs == expected, f"gate cron pairs {pairs} != registered {expected}"

    def test_every_registered_source_is_a_matrix_leg(self) -> None:
        rows = load("collect_data.yml")["jobs"]["collect"]["strategy"]["matrix"]["include"]
        assert {row["source"] for row in rows} == set(SOURCES)

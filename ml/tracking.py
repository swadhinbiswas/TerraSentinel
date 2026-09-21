"""Experiment tracking: MLflow when available, a JSON file when not.

MLflow is the project's tracker, but making it a hard dependency of every training run
would mean CI cannot exercise training at all — and a training script that only runs in
one environment is a training script that is rarely run. So the tracker degrades to a
JSON run record and says so, rather than silently logging nowhere.

The one field that is not optional is the **dataset commit hash**. Without it you can
never answer "which data produced this model", and it costs one parameter to record.
``require_dataset_commit`` enforces that at the API level so a future caller cannot
forget it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_EXPERIMENT = "terrasentinel-anomaly"
LOCAL_RUNS_DIR = Path("ml/runs")


def mlflow_available() -> bool:
    try:
        import mlflow  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class TrackedRun:
    """A run being tracked, with a uniform API across backends."""

    name: str
    backend: str
    run_id: str
    params: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    #: The mlflow module, when the mlflow backend is active. The fluent API is used
    #: rather than the ActiveRun object, which does not expose log_* methods.
    _mlflow: Any = field(default=None, repr=False)

    def log_params(self, values: Mapping[str, Any]) -> None:
        clean = {key: value for key, value in values.items() if value is not None}
        self.params.update({key: str(value) for key, value in clean.items()})
        if self.backend == "mlflow":
            self._mlflow.log_params(clean)  # type: ignore[union-attr]

    def log_metrics(self, values: Mapping[str, float]) -> None:
        numeric = {key: float(value) for key, value in values.items() if value is not None}
        self.metrics.update(numeric)
        if self.backend == "mlflow":
            self._mlflow.log_metrics(numeric)  # type: ignore[union-attr]

    def set_tags(self, values: Mapping[str, str]) -> None:
        self.tags.update(values)
        if self.backend == "mlflow":
            self._mlflow.set_tags(dict(values))  # type: ignore[union-attr]

    def log_artifact(self, path: str | Path) -> None:
        self.artifacts.append(str(path))
        if self.backend == "mlflow":
            self._mlflow.log_artifact(str(path))  # type: ignore[union-attr]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend": self.backend,
            "run_id": self.run_id,
            "params": self.params,
            "metrics": self.metrics,
            "tags": self.tags,
            "artifacts": self.artifacts,
            "finished_at": datetime.now(UTC).isoformat(),
        }


class tracked_run:
    """Context manager yielding a :class:`TrackedRun`.

    ``dataset_commit`` is required: it is the one piece of lineage the whole pipeline
    depends on, so it is validated here rather than remembered at each call site.
    """

    def __init__(
        self,
        name: str,
        *,
        dataset_commit: str | None,
        experiment: str | None = None,
        tracking_uri: str | None = None,
        local_dir: Path = LOCAL_RUNS_DIR,
        prefer_mlflow: bool = True,
    ) -> None:
        if not dataset_commit:
            raise ValueError(
                "dataset_commit is required: a model that cannot be traced to the data "
                "that produced it is not reproducible"
            )
        self.name = name
        self.dataset_commit = dataset_commit
        self.experiment = experiment or os.environ.get("MLFLOW_EXPERIMENT_NAME", DEFAULT_EXPERIMENT)
        self.tracking_uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db")
        self.local_dir = local_dir
        self.prefer_mlflow = prefer_mlflow and mlflow_available()
        self.run: TrackedRun | None = None

    def __enter__(self) -> TrackedRun:
        if self.prefer_mlflow:
            import mlflow

            mlflow.set_tracking_uri(self.tracking_uri)
            mlflow.set_experiment(self.experiment)
            mlflow.start_run(run_name=self.name)
            active = mlflow.active_run()
            self.run = TrackedRun(
                name=self.name,
                backend="mlflow",
                run_id=active.info.run_id if active else "unknown",
                _mlflow=mlflow,
            )
            self.run.set_tags({"dataset_commit": self.dataset_commit, "project": "TerraSentinel"})
            return self.run

        LOGGER.warning(
            "mlflow is not installed; writing the run record as JSON under %s instead",
            self.local_dir,
        )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.run = TrackedRun(name=self.name, backend="json", run_id=f"{self.name}-{stamp}")
        self.run.set_tags({"dataset_commit": self.dataset_commit, "project": "TerraSentinel"})
        return self.run

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.run is not None
        if self.run.backend == "mlflow":
            import mlflow

            mlflow.end_run(status="FAILED" if exc_type else "FINISHED")
            return

        self.local_dir.mkdir(parents=True, exist_ok=True)
        payload = self.run.as_dict()
        payload["status"] = "FAILED" if exc_type else "FINISHED"
        if exc is not None:
            payload["error"] = f"{type(exc).__name__}: {exc}"[:500]
        (self.local_dir / f"{self.run.run_id}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

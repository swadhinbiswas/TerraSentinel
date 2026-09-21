"""Hugging Face Hub dataset reader.

Three read shapes, each used by a different layer:

* :meth:`HFDatasetReader.parquet_urls` — direct ``resolve/main`` URLs, which DuckDB
  reads over HTTPS without an intermediate download. This is what keeps the
  transform layer from needing a disk copy of the lake.
* :meth:`HFDatasetReader.commit_sha` — the exact repo revision, logged into MLflow
  with every training run so a model can always be traced back to the data that
  produced it.
* :meth:`HFDatasetReader.snapshot` — a pinned local copy for training, so a
  retrain started on Tuesday and a retrain started on Friday cannot silently
  diverge because new data landed in between.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

from collectors.config import get_secret, hf_repo, parquet_uri
from ops.resilience import PermanentError, TransientError

LOGGER = logging.getLogger(__name__)

_REPO_TYPE_BY_KIND = {"bronze": "dataset", "silver": "dataset", "models": "model"}


class HFDatasetReader:
    """Read-only view over one Hub repo (bronze, silver or models)."""

    def __init__(
        self,
        *,
        repo_id: str | None = None,
        repo_kind: str = "silver",
        token: str | None = None,
        revision: str = "main",
        api: Any | None = None,
    ) -> None:
        if repo_kind not in _REPO_TYPE_BY_KIND:
            raise ValueError(f"repo_kind must be one of {sorted(_REPO_TYPE_BY_KIND)}, got {repo_kind!r}")
        self.repo_kind = repo_kind
        self.repo_type = _REPO_TYPE_BY_KIND[repo_kind]
        self.repo_id = repo_id or hf_repo(repo_kind)  # type: ignore[arg-type]
        self.revision = revision
        self.token = token or get_secret("HF_TOKEN", required=False)
        self._api = api

    @property
    def api(self) -> HfApi:
        if self._api is None:
            self._api = HfApi(token=self.token)
        return self._api

    def list_files(self, prefix: str = "", *, suffix: str | None = None) -> list[str]:
        try:
            files = self.api.list_repo_files(
                self.repo_id, repo_type=self.repo_type, revision=self.revision
            )
        except RepositoryNotFoundError as exc:
            raise PermanentError(f"Hub repo {self.repo_id!r} not found: {exc}") from exc
        except HfHubHTTPError as exc:
            raise TransientError(f"could not list {self.repo_id!r}: {exc}") from exc

        selected = [path for path in files if path.startswith(prefix)]
        if suffix:
            selected = [path for path in selected if path.endswith(suffix)]
        return sorted(selected)

    def resolve_url(self, path_in_repo: str) -> str:
        """URL that DuckDB can read directly (no download step)."""
        if self.repo_type != "dataset":
            raise PermanentError(
                f"resolve_url is defined for dataset repos, not {self.repo_type!r}"
            )
        return parquet_uri(self.repo_id, path_in_repo, revision=self.revision)

    def parquet_urls(self, prefix: str = "") -> list[str]:
        return [self.resolve_url(path) for path in self.list_files(prefix, suffix=".parquet")]

    def commit_sha(self) -> str:
        """Current revision sha — log this with every training run."""
        try:
            info = self.api.repo_info(
                self.repo_id, repo_type=self.repo_type, revision=self.revision
            )
        except RepositoryNotFoundError as exc:
            raise PermanentError(f"Hub repo {self.repo_id!r} not found: {exc}") from exc
        except HfHubHTTPError as exc:
            raise TransientError(f"could not read {self.repo_id!r} info: {exc}") from exc
        return str(info.sha)

    def snapshot(
        self,
        local_dir: str | Path,
        *,
        allow_patterns: list[str] | None = None,
        revision: str | None = None,
    ) -> Path:
        """Download (or reuse) a pinned local copy of part of the repo."""
        target = Path(local_dir)
        try:
            path = snapshot_download(
                repo_id=self.repo_id,
                repo_type=self.repo_type,
                revision=revision or self.revision,
                local_dir=str(target),
                allow_patterns=allow_patterns,
                token=self.token,
            )
        except RepositoryNotFoundError as exc:
            raise PermanentError(f"Hub repo {self.repo_id!r} not found: {exc}") from exc
        except HfHubHTTPError as exc:
            raise TransientError(f"snapshot of {self.repo_id!r} failed: {exc}") from exc
        LOGGER.info("snapshot %s@%s -> %s", self.repo_id, revision or self.revision, path)
        return Path(path)

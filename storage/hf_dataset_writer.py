"""Hugging Face Hub dataset writer.

Writes parquet landings into a Hub dataset repo, which is where the "lake" part
of this architecture actually comes from: the repo is git-backed, so every landing
is versioned, diffable and attributable to a commit — properties a bucket of files
does not have.

Two upload modes, because they have genuinely different cost profiles:

* **single file** — one commit per landing, used by the live daily/weekly
  collectors where a run produces one partition per source per day.
* **folder** — one commit for an entire backfill tree. Pushing 48 monthly
  partitions as 48 commits would waste the Hub's commit budget for no benefit.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi, create_repo
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError, RevisionNotFoundError
from huggingface_hub.utils import EntryNotFoundError

from collectors.config import get_secret, hf_repo
from ops.resilience import PermanentError, RetryPolicy, TransientError, retry_call

LOGGER = logging.getLogger(__name__)

_REPO_TYPE_BY_KIND = {"bronze": "dataset", "silver": "dataset", "models": "model"}


class HFDatasetWriter:
    """Token-authenticated writer for one Hub repo."""

    def __init__(
        self,
        *,
        repo_id: str | None = None,
        repo_kind: str = "bronze",
        token: str | None = None,
        revision: str = "main",
        create_if_missing: bool = True,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        api: Any | None = None,
    ) -> None:
        if repo_kind not in _REPO_TYPE_BY_KIND:
            raise ValueError(f"repo_kind must be one of {sorted(_REPO_TYPE_BY_KIND)}, got {repo_kind!r}")
        self.repo_kind = repo_kind
        self.repo_type = _REPO_TYPE_BY_KIND[repo_kind]
        self.repo_id = repo_id or hf_repo(repo_kind)  # type: ignore[arg-type]
        self.revision = revision
        self.token = token or get_secret("HF_TOKEN")
        self.policy = policy or RetryPolicy(attempts=3, base_delay=2.0, max_delay=30.0)
        self._sleep = sleep
        self._api = api
        self._prepared = False
        self._create_if_missing = create_if_missing

    # -- internals ---------------------------------------------------------

    @property
    def api(self) -> HfApi:
        if self._api is None:
            self._api = HfApi(token=self.token)
        return self._api

    def prepare(self) -> None:
        """Create the repo if absent. Idempotent, and safe to call per run."""
        if self._prepared:
            return
        try:
            self.api.repo_info(self.repo_id, repo_type=self.repo_type)
        except RepositoryNotFoundError:
            if not self._create_if_missing:
                raise PermanentError(
                    f"Hub repo {self.repo_id!r} does not exist and create_if_missing=False"
                ) from None
            LOGGER.info("creating Hugging Face %s repo %s", self.repo_type, self.repo_id)
            create_repo(
                self.repo_id,
                repo_type=self.repo_type,
                token=self.token,
                exist_ok=True,
                private=False,
            )
        self._prepared = True

    def _call(self, fn: Callable[[], Any], description: str) -> Any:
        """Run an API call with retry/backoff, mapping Hub errors to our taxonomy."""

        def attempt() -> Any:
            try:
                return fn()
            except (RepositoryNotFoundError, EntryNotFoundError, RevisionNotFoundError) as exc:
                raise PermanentError(
                    f"{description}: {exc}. If the repo is new, run prepare() first."
                ) from exc
            except HfHubHTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 429 or (status is not None and status >= 500):
                    raise TransientError(f"{description}: HTTP {status}: {exc}") from exc
                raise PermanentError(f"{description}: {exc}") from exc
            except OSError as exc:
                raise TransientError(f"{description}: network error: {exc}") from exc

        return retry_call(attempt, policy=self.policy, sleep=self._sleep, description=description)

    @staticmethod
    def _validate_path(path_in_repo: str) -> str:
        """Reject anything that is not a plain relative repo path.

        Absolute paths and traversal segments are refused rather than silently
        normalised, so a bad caller cannot scatter files across the repo root.
        """
        clean = path_in_repo.strip()
        if not clean or clean.startswith("/") or "\\" in clean:
            raise ValueError(f"invalid path_in_repo {path_in_repo!r}")
        if any(part in {"..", ".", ""} for part in clean.split("/")):
            raise ValueError(f"path_in_repo must not traverse: {path_in_repo!r}")
        return clean

    # -- writes ------------------------------------------------------------

    def upload_file(
        self, local_path: str | Path, path_in_repo: str, *, commit_message: str = ""
    ) -> Any:
        """Upload one file; returns the Hub ``CommitInfo`` (``.oid`` is the sha)."""
        source = Path(local_path)
        if not source.is_file():
            raise PermanentError(f"cannot upload missing file {source}")
        target = self._validate_path(path_in_repo)
        self.prepare()
        info = self._call(
            lambda: self.api.upload_file(
                path_or_fileobj=str(source),
                path_in_repo=target,
                repo_id=self.repo_id,
                repo_type=self.repo_type,
                revision=self.revision,
                token=self.token,
                commit_message=commit_message or f"add {target}",
            ),
            description=f"upload {target} -> {self.repo_id}",
        )
        LOGGER.info("uploaded %s (%d bytes) to %s", target, source.stat().st_size, self.repo_id)
        return info

    def upload_bytes(
        self, payload: bytes, path_in_repo: str, *, commit_message: str = ""
    ) -> Any:
        """Upload an in-memory payload (run manifests, metrics snapshots)."""
        target = self._validate_path(path_in_repo)
        self.prepare()
        return self._call(
            lambda: self.api.upload_file(
                path_or_fileobj=payload,
                path_in_repo=target,
                repo_id=self.repo_id,
                repo_type=self.repo_type,
                revision=self.revision,
                token=self.token,
                commit_message=commit_message or f"add {target}",
            ),
            description=f"upload bytes -> {target}",
        )

    def upload_json(
        self,
        record: dict[str, Any] | Sequence[dict[str, Any]],
        path_in_repo: str,
        *,
        commit_message: str = "",
    ) -> Any:
        payload = json.dumps(record, indent=2, default=str).encode("utf-8")
        return self.upload_bytes(payload, path_in_repo, commit_message=commit_message)

    def upload_folder(
        self,
        local_dir: str | Path,
        path_in_repo: str = "",
        *,
        commit_message: str = "",
        allow_patterns: Sequence[str] | None = None,
    ) -> str:
        """Upload a whole tree in a single commit; returns the commit sha."""
        source = Path(local_dir)
        if not source.is_dir():
            raise PermanentError(f"cannot upload missing directory {source}")
        self.prepare()
        target = self._validate_path(path_in_repo) if path_in_repo else ""

        def call() -> Any:
            self.api.upload_folder(
                folder_path=str(source),
                path_in_repo=target,
                repo_id=self.repo_id,
                repo_type=self.repo_type,
                revision=self.revision,
                token=self.token,
                commit_message=commit_message or f"add {target or '.'}",
                allow_patterns=list(allow_patterns) if allow_patterns else None,
            )
            return self.api.repo_info(
                self.repo_id, repo_type=self.repo_type, revision=self.revision
            ).sha

        sha = self._call(call, description=f"upload folder {source} -> {self.repo_id}/{target}")
        LOGGER.info("uploaded folder %s to %s/%s at %s", source, self.repo_id, target, sha)
        return str(sha)

    def list_files(self, prefix: str = "") -> list[str]:
        files: Iterable[str] = self._call(
            lambda: self.api.list_repo_files(
                self.repo_id, repo_type=self.repo_type, revision=self.revision
            ),
            description=f"list {self.repo_id}",
        )
        return sorted(path for path in files if path.startswith(prefix))

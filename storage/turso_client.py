"""Minimal libSQL/Turso client over the HTTP pipeline API.

Why not the official client: Turso's HTTP endpoint is a documented JSON protocol
(``POST /v2/pipeline``) that works identically from CPython here, from a GitHub
Action, and from Cloudflare ``workerd`` — which is exactly why the dashboard's
Pages Functions can talk to the same database with no proxy service in between.
Depending on it directly means the sync job and the serving layer share one
transport, one typing rule set and one set of failure modes.

Everything is an upsert. GitHub Actions jobs get re-run and partially fail, so a
blind INSERT would double-count rows in the gold layer and silently corrupt every
anomaly score computed from them.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import requests

from collectors.config import get_secret
from ops.redact import redact_text
from ops.resilience import PermanentError, RetryPolicy, TransientError, retry_call

LOGGER = logging.getLogger(__name__)

__all__ = ["TursoClient", "TursoError", "TursoStatement"]

#: libSQL rejects statements with too many bound parameters; stay well clear.
MAX_PARAMS_PER_STATEMENT = 500
#: Separate budget for how much to pack into one HTTP round trip. Nothing is
#: gained by sending one statement per request, so several small statements share
#: a request while the per-statement ceiling is still respected.
MAX_PARAMS_PER_REQUEST = 10_000
MAX_STATEMENTS_PER_REQUEST = 50


class TursoError(PermanentError):
    """A libSQL request was rejected or returned an error result."""


TursoStatement = tuple[str, tuple[Any, ...]]


def http_url_for(database_url: str) -> str:
    """Convert a Turso database URL into its HTTP pipeline endpoint."""
    url = (database_url or "").strip()
    if not url:
        raise TursoError("TURSO_DATABASE_URL is empty")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://") :]
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    elif not url.startswith("https://"):
        raise TursoError(
            f"unsupported Turso URL scheme in {url!r}: expected libsql://, https:// or http://. "
            "Local file: URLs cannot be reached over the HTTP pipeline API."
        )
    return url.rstrip("/") + "/v2/pipeline"


def encode_arg(value: Any) -> dict[str, Any]:
    """Encode a Python value as a libSQL typed argument."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return {"type": "null"}
        # A JSON *number*, not a string. libSQL deserialises the `float` variant as
        # f64 and rejects `"value": "1.5"` with a JSON parse error — while `integer`
        # does accept a string, which is exactly why this is easy to get wrong and
        # then only discover against the real endpoint.
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "value": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def decode_cell(cell: Mapping[str, Any] | None) -> Any:
    if not cell:
        return None
    kind = cell.get("type")
    raw = cell.get("value")
    if kind == "null" or raw is None:
        return None
    if kind == "integer":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "blob":
        return base64.b64decode(raw)
    return raw


class TursoClient:
    """Thin, retrying wrapper over Turso's HTTP pipeline API."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        auth_token: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.database_url = database_url or get_secret("TURSO_DATABASE_URL")
        self.auth_token = auth_token or get_secret("TURSO_AUTH_TOKEN")
        self.endpoint = http_url_for(self.database_url)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.policy = policy or RetryPolicy(attempts=3, base_delay=1.5, max_delay=20.0)
        self._sleep = sleep

    # -- transport ---------------------------------------------------------

    def _post(self, requests_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def attempt() -> list[dict[str, Any]]:
            try:
                response = self.session.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.auth_token}",
                        "Content-Type": "application/json",
                    },
                    json={"requests": requests_payload},
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                raise TransientError(f"turso timeout after {self.timeout}s") from exc
            except requests.ConnectionError as exc:
                raise TransientError(f"turso connection error: {exc}") from exc
            except requests.RequestException as exc:
                raise TransientError(f"turso request failed: {exc}") from exc

            if response.status_code == 429 or response.status_code >= 500:
                raise TransientError(
                    f"turso HTTP {response.status_code}: {redact_text(response.text[:200])}"
                )
            if response.status_code >= 400:
                raise TursoError(
                    f"turso HTTP {response.status_code}: {redact_text(response.text[:300])}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise TursoError(f"turso returned non-JSON: {redact_text(response.text[:200])}") from exc

            results = payload.get("results")
            if results is None:
                raise TursoError(f"turso response has no results: {redact_text(json.dumps(payload))}")
            return results

        return retry_call(
            attempt, policy=self.policy, sleep=self._sleep, description="turso pipeline"
        )

    # -- statements --------------------------------------------------------

    def execute(self, sql: str, args: Sequence[Any] = ()) -> dict[str, Any]:
        """Run one statement, returning ``{"rows": [...], "affected": int}``."""
        results = self._execute_batch([(sql, tuple(args))])
        return results[0]

    def _execute_batch(self, statements: Sequence[TursoStatement]) -> list[dict[str, Any]]:
        if not statements:
            return []
        payload = [
            {
                "type": "execute",
                "stmt": {"sql": sql, "args": [encode_arg(value) for value in args]},
            }
            for sql, args in statements
        ]
        payload.append({"type": "close"})
        results = self._post(payload)[: len(statements)]

        decoded: list[dict[str, Any]] = []
        for index, result in enumerate(results):
            if result.get("type") != "ok":
                error = result.get("error", {})
                raise TursoError(
                    f"turso error on statement {index}: {error.get('message', result)}"
                )
            inner = result.get("response", {}).get("result", {})
            columns = [column.get("name") for column in inner.get("cols", [])]
            rows = [
                {name: decode_cell(cell) for name, cell in zip(columns, row, strict=False)}
                for row in inner.get("rows", [])
            ]
            decoded.append(
                {"rows": rows, "affected": int(inner.get("affected_row_count", 0) or 0)}
            )
        return decoded

    def execute_many(self, statements: Sequence[TursoStatement]) -> list[dict[str, Any]]:
        """Run several statements in one round trip (still one HTTP request)."""
        out: list[dict[str, Any]] = []
        for batch in _batched(statements, self._statements_per_request(statements)):
            out.extend(self._execute_batch(batch))
        return out

    @staticmethod
    def _statements_per_request(statements: Sequence[TursoStatement]) -> int:
        widest = max((len(args) for _, args in statements), default=0)
        if widest == 0:
            return MAX_STATEMENTS_PER_REQUEST
        return max(1, min(MAX_STATEMENTS_PER_REQUEST, MAX_PARAMS_PER_REQUEST // widest))

    # -- convenience -------------------------------------------------------

    def fetch_all(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return self.execute(sql, args)["rows"]

    def ping(self) -> bool:
        """Cheap connectivity check used by the health endpoint and CI."""
        try:
            self.execute("SELECT 1 AS ok")
        except (TursoError, TransientError):
            return False
        return True

    def upsert(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        key_columns: Sequence[str],
        chunk_rows: int | None = None,
    ) -> int:
        """Idempotent multi-row upsert on ``key_columns``. Returns rows affected.

        Re-running a partially failed sync must converge to the same table
        contents, which is why this is the only write path exposed.
        """
        if not key_columns:
            raise ValueError("key_columns is required for upsert")
        materialised = [dict(row) for row in rows]
        if not materialised:
            return 0

        columns = list(materialised[0].keys())
        missing = [column for column in key_columns if column not in columns]
        if missing:
            raise ValueError(f"key column(s) {missing} absent from row keys {columns}")
        for row in materialised:
            extra = set(row) - set(columns)
            if extra:
                raise ValueError(f"row has inconsistent columns: {sorted(extra)}")

        updates = [column for column in columns if column not in set(key_columns)]
        if updates:
            assignment = ", ".join(f"{column} = excluded.{column}" for column in updates)
            conflict = f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {assignment}"
        else:
            conflict = f"ON CONFLICT ({', '.join(key_columns)}) DO NOTHING"

        placeholders = f"({', '.join('?' for _ in columns)})"
        rows_per_batch = chunk_rows or max(1, MAX_PARAMS_PER_STATEMENT // max(1, len(columns)))
        statements: list[TursoStatement] = []
        for batch in _batched(materialised, rows_per_batch):
            values = ", ".join(placeholders for _ in batch)
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES {values} {conflict}"
            )
            args: list[Any] = []
            for row in batch:
                args.extend(row[column] for column in columns)
            statements.append((sql, tuple(args)))

        affected = sum(result["affected"] for result in self.execute_many(statements))
        LOGGER.info("upserted %d row(s) into %s", len(materialised), table)
        return affected


def _batched(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    return [items[start : start + size] for start in range(0, len(items), size)]

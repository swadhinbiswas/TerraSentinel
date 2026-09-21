"""Register the Hugging Face token as a DuckDB secret, outside of dbt.

Why this is a separate step rather than a dbt `on-run-start` hook: doing it in SQL
inside dbt would interpolate the token into compiled SQL, and therefore into
`dbt.log`, `run_results.json` and any uploaded artifact. Once a secret is in an
artifact it is out of your control. Creating it here keeps the token out of dbt's
output entirely.

Why it is needed at all: DuckDB's `hf://` filesystem reads **anonymously** unless a
secret of type `HUGGINGFACE` exists. Anonymous reads share the Hub's public
rate-limit pool, which a scheduled job can exhaust (HTTP 429 on the repo tree API).
Authenticating also makes a private bronze repo work.

The secret is written to DuckDB's persistent store (`~/.duckdb/stored_secrets`), so
every later connection picks it up with no change to the dbt profile.

    HF_TOKEN=... python -m tools.configure_duckdb_secret
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

SECRET_NAME = "hf_token"


def build_secret_statement(
    token: str,
    *,
    name: str = SECRET_NAME,
    persistent: bool = True,
) -> str:
    """Build the ``CREATE SECRET`` statement.

    Split out so the statement can be asserted on in tests without writing a real
    secret into the developer's persistent store.
    """
    if not token.strip():
        raise ValueError("token must not be empty")
    if "'" in token:
        # A quote would terminate the literal; refuse rather than escape silently.
        raise ValueError("token contains a quote character, which cannot be safely inlined")
    kind = "PERSISTENT " if persistent else ""
    return f"CREATE OR REPLACE {kind}SECRET {name} (TYPE HUGGINGFACE, TOKEN '{token}')"


def configure(
    token: str | None = None,
    *,
    database: str | Path | None = None,
    persistent: bool = True,
    connect: Callable[..., Any] = duckdb.connect,
) -> bool:
    """Create the secret. Returns ``False`` when no token is available."""
    resolved = (token or os.environ.get("HF_TOKEN") or "").strip()
    if not resolved:
        print("HF_TOKEN is not set; hf:// reads will be anonymous", file=sys.stderr)
        return False

    connection = connect(str(database) if database else ":memory:")
    try:
        connection.execute("INSTALL httpfs; LOAD httpfs;")
        connection.execute(build_secret_statement(resolved, persistent=persistent))
    finally:
        connection.close()

    scope = "persistent" if persistent else "session"
    print(f"registered {scope} DuckDB secret '{SECRET_NAME}' (type HUGGINGFACE)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", default=None, help="optional database to create the secret in")
    parser.add_argument(
        "--require-token",
        action="store_true",
        help="exit non-zero when HF_TOKEN is absent (use in jobs reading a private repo)",
    )
    args = parser.parse_args(argv)

    if not configure(database=args.database) and args.require_token:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

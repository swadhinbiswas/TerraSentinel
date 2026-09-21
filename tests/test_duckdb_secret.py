"""DuckDB HF secret configuration.

Two things must hold: the token never reaches dbt's compiled SQL or artifacts, and
tests must not write a real secret into the developer's persistent store.
"""

from __future__ import annotations

import pytest

from tools.configure_duckdb_secret import (
    SECRET_NAME,
    build_secret_statement,
    configure,
    main,
)

FAKE_TOKEN = "hf_NotARealTokenForTestsOnly000000"


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.statements.append(sql)

    def close(self) -> None:
        self.closed = True


class TestBuildStatement:
    def test_uses_the_huggingface_secret_type(self) -> None:
        statement = build_secret_statement(FAKE_TOKEN)
        assert "TYPE HUGGINGFACE" in statement
        assert SECRET_NAME in statement

    def test_defaults_to_persistent(self) -> None:
        assert "CREATE OR REPLACE PERSISTENT SECRET" in build_secret_statement(FAKE_TOKEN)

    def test_can_build_a_session_only_secret(self) -> None:
        statement = build_secret_statement(FAKE_TOKEN, persistent=False)
        assert "PERSISTENT" not in statement
        assert "CREATE OR REPLACE SECRET" in statement

    def test_empty_token_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            build_secret_statement("   ")

    def test_quote_in_token_is_refused_rather_than_escaped(self) -> None:
        # Silently escaping would produce a statement whose behaviour differs from
        # what the caller asked for.
        with pytest.raises(ValueError, match="quote"):
            build_secret_statement("hf_abc'def")


class TestConfigure:
    def test_registers_httpfs_then_the_secret(self) -> None:
        connection = RecordingConnection()
        assert configure(FAKE_TOKEN, connect=lambda _: connection) is True
        assert "INSTALL httpfs" in connection.statements[0]
        assert "TYPE HUGGINGFACE" in connection.statements[1]
        assert connection.closed

    def test_no_token_is_reported_not_raised(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("HF_TOKEN", raising=False)
        assert configure(None, connect=lambda _: RecordingConnection()) is False
        assert "HF_TOKEN is not set" in capsys.readouterr().err

    def test_token_is_read_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("HF_TOKEN", FAKE_TOKEN)
        connection = RecordingConnection()
        assert configure(None, connect=lambda _: connection) is True
        assert FAKE_TOKEN in connection.statements[1]

    def test_explicit_token_wins_over_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("HF_TOKEN", "hf_FromTheEnvironment0000000000")
        connection = RecordingConnection()
        configure(FAKE_TOKEN, connect=lambda _: connection)
        assert FAKE_TOKEN in connection.statements[1]

    def test_connection_is_closed_even_when_execution_fails(self) -> None:
        class Exploding(RecordingConnection):
            def execute(self, sql: str) -> None:
                raise RuntimeError("boom")

        connection = Exploding()
        with pytest.raises(RuntimeError):
            configure(FAKE_TOKEN, connect=lambda _: connection)
        assert connection.closed


class TestCli:
    def test_missing_token_is_not_an_error_by_default(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("HF_TOKEN", raising=False)
        assert main([]) == 0
        capsys.readouterr()

    def test_require_token_fails_when_absent(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("HF_TOKEN", raising=False)
        assert main(["--require-token"]) == 1
        capsys.readouterr()

    def test_never_prints_the_token(self, capsys) -> None:
        configure(FAKE_TOKEN, connect=lambda _: RecordingConnection())
        captured = capsys.readouterr()
        assert FAKE_TOKEN not in captured.out
        assert FAKE_TOKEN not in captured.err

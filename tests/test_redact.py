"""Secret redaction: credentials must never reach a log, alert or error string."""

from __future__ import annotations

from ops.redact import REDACTED, redact_mapping, redact_text, redact_url, register_secret

HF_TOKEN = "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
FIRMS_KEY = "b4c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5"
TURSO_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJvcmciOiJkZW1vIn0.c2lnbmF0dXJlLWJ5dGVz"


class TestRedactText:
    def test_masks_hugging_face_token(self) -> None:
        assert HF_TOKEN not in redact_text(f"using {HF_TOKEN} for auth")

    def test_masks_bearer_token(self) -> None:
        assert "abcdef1234567890" not in redact_text("Authorization: Bearer abcdef1234567890")

    def test_masks_libsql_jwt(self) -> None:
        assert TURSO_JWT not in redact_text(f"token={TURSO_JWT}")

    def test_masks_slack_webhook(self) -> None:
        url = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX"
        assert "T000/B000" not in redact_text(f"posting to {url}")

    def test_masks_discord_webhook(self) -> None:
        url = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop"
        assert "123456789" not in redact_text(f"posting to {url}")

    def test_masks_sensitive_query_parameters(self) -> None:
        redacted = redact_text("https://example.test/v1?api_key=supersecret&other=1")
        assert "supersecret" not in redacted
        assert "other=1" in redacted

    def test_masks_registered_secret_value(self) -> None:
        register_secret("my-unique-secret-value-1234")
        assert "my-unique-secret-value-1234" not in redact_text(
            "leaked: my-unique-secret-value-1234 here"
        )

    def test_ignores_short_values(self) -> None:
        # Registering a three-character string would redact half the log.
        register_secret("abc")
        assert redact_text("abc") == "abc"

    def test_accepts_non_string_input(self) -> None:
        assert "boom" in redact_text(ValueError("boom"))

    def test_leaves_ordinary_text_alone(self) -> None:
        assert redact_text("collected 412 rows for iberia_fire") == (
            "collected 412 rows for iberia_fire"
        )


class TestRedactUrl:
    def test_masks_firms_map_key_in_path(self) -> None:
        url = (
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_KEY}"
            "/VIIRS_SNPP_NRT/-10,35.5,3.5,43.9/5/2024-08-15"
        )
        redacted = redact_url(url)
        assert FIRMS_KEY not in redacted
        assert "VIIRS_SNPP_NRT" in redacted
        assert "2024-08-15" in redacted

    def test_masks_registered_value_in_query(self) -> None:
        register_secret("zzzz-yyyy-xxxx-wwww-1234")
        assert "zzzz-yyyy-xxxx-wwww-1234" not in redact_url(
            "https://api.test/data?token=zzzz-yyyy-xxxx-wwww-1234"
        )

    def test_leaves_keyless_urls_intact(self) -> None:
        url = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/N_seaice_extent_daily_v4.0.csv"
        assert redact_url(url) == url


class TestRedactMapping:
    def test_reports_presence_not_value_for_credentials(self) -> None:
        result = redact_mapping({"HF_TOKEN": HF_TOKEN, "TURSO_AUTH_TOKEN": "", "rows": 12})
        assert result["HF_TOKEN"] == "set"
        assert result["TURSO_AUTH_TOKEN"] == "missing"
        assert result["rows"] == 12

    def test_redacts_nested_values(self) -> None:
        result = redact_mapping({"context": {"note": f"token {HF_TOKEN}"}})
        assert HF_TOKEN not in str(result)

    def test_handles_arbitrary_sensitive_key_names(self) -> None:
        result = redact_mapping({"my_webhook_url": "https://hooks.slack.com/services/A/B/C"})
        assert result["my_webhook_url"] == "set"

    def test_output_is_json_serialisable(self) -> None:
        import json

        json.dumps(redact_mapping({"a": HF_TOKEN, "b": [1, 2], "c": None}))
        assert REDACTED

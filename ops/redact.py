"""Secret redaction for logs, alerts and error messages.

Credentials leak through the boring paths: a logged request URL, an exception
repr containing a query string, a stack trace that includes a webhook body.
Everything that formats text for a human goes through here first.

Two rules:
  * known secret *values* are replaced wherever they appear, and
  * known secret *shapes* (FIRMS map keys, HF tokens, bearer tokens, libSQL
    tokens) are replaced even when we do not hold the value — e.g. a URL built
    from an env var that the current process was not given.

Nothing here ever returns a real credential, and unset values are never treated
as redactable (an empty string would match everywhere).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

__all__ = [
    "REDACTED",
    "redact_mapping",
    "redact_text",
    "redact_url",
    "register_secret",
    "secret_values_from_env",
]

REDACTED = "***redacted***"

#: Environment variable names whose values must never appear in output.
SECRET_ENV_VARS: tuple[str, ...] = (
    "HF_TOKEN",
    "FIRMS_MAP_KEY",
    "TURSO_AUTH_TOKEN",
    "TURSO_TOKEN_RO",
    "ALERT_WEBHOOK_URL",
    "GEE_SERVICE_ACCOUNT_JSON",
    "GEE_SERVICE_ACCOUNT_EMAIL",
    "CLOUDFLARE_API_TOKEN",
    "MLFLOW_TRACKING_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "ENTSOE_API_KEY",
)

#: Shape-based rules: catch credentials we were never handed directly.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Hugging Face user access tokens
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), REDACTED),
    # Bearer / Authorization headers
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{12,}"), rf"\1{REDACTED}"),
    # libSQL / Turso auth tokens (JWT-ish, three dot-separated segments)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), REDACTED),
    # Slack incoming webhooks
    (re.compile(r"https://hooks\.slack\.com/services/\S+"), REDACTED),
    # Discord incoming webhooks
    (re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\S+"), REDACTED),
    # Generic sensitive query parameters. `security[_-]?token` has to be named
    # explicitly: the bare `token` alternative cannot match inside
    # `securityToken=` because \b sits between "security" and "Token", where
    # there is no word boundary.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|map[_-]?key|security[_-]?token|token|auth|password|secret|access[_-]?key)"
            r"(=|%3D)([^&\s\"']{4,})"
        ),
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",  # type: ignore[arg-type]
    ),
)

#: FIRMS embeds MAP_KEY as a path segment: /api/area/csv/<KEY>/<SOURCE>/...
_FIRMS_PATH_KEY = re.compile(
    r"(?i)(/api/(?:area|data)/[a-z_]*/?)([A-Za-z0-9]{20,})(/)"
)

_registered_secrets: set[str] = set()
_env_secrets_registered = False


def register_secret(value: str | None) -> None:
    """Register a literal secret value to redact, e.g. one just read from env."""
    if value and len(value) >= 8:
        _registered_secrets.add(value)


def _ensure_env_registered() -> None:
    """Register every credential-shaped env var once, so redaction is automatic.

    Wiring this in explicitly is the kind of thing that gets forgotten in a new
    code path; doing it lazily on first format means a token cannot leak just
    because someone logged a URL before calling an initialiser.
    """
    global _env_secrets_registered
    if not _env_secrets_registered:
        for value in secret_values_from_env():
            register_secret(value)
        _env_secrets_registered = True


def secret_values_from_env(environ: Mapping[str, str] | None = None) -> list[str]:
    env: Mapping[str, str] = os.environ if environ is None else environ
    values = [env[name] for name in SECRET_ENV_VARS if env.get(name)]
    for name, value in env.items():
        if value and any(token in name.upper() for token in ("SECRET", "TOKEN", "PASSWORD", "MAP_KEY")):
            values.append(value)
    return values


def _registered(values: Iterable[str] | None) -> set[str]:
    candidates = set(_registered_secrets)
    if values is not None:
        candidates.update(value for value in values if value and len(value) >= 8)
    return candidates


def redact_text(text: object, *, extra_secrets: Iterable[str] | None = None) -> str:
    """Replace every known secret value and credential-shaped substring."""
    _ensure_env_registered()
    rendered = text if isinstance(text, str) else repr(text)

    for secret in sorted(_registered(extra_secrets), key=len, reverse=True):
        rendered = rendered.replace(secret, REDACTED)

    for pattern, replacement in _PATTERNS:
        rendered = pattern.sub(replacement, rendered)  # type: ignore[arg-type]

    return rendered


def redact_url(url: str, *, extra_secrets: Iterable[str] | None = None) -> str:
    """Redact a URL for logging, including credentials embedded in the path."""
    redacted = _FIRMS_PATH_KEY.sub(rf"\g<1>{REDACTED}\g<3>", url)
    return redact_text(redacted, extra_secrets=extra_secrets)


def redact_mapping(
    mapping: Mapping[str, object],
    *,
    sensitive_keys: Iterable[str] = SECRET_ENV_VARS,
    extra_secrets: Iterable[str] | None = None,
) -> dict[str, object]:
    """Redact a dict (e.g. a webhook payload or run summary) before logging."""
    lowered = {key.lower() for key in sensitive_keys}
    out: dict[str, object] = {}
    for key, value in mapping.items():
        if key.lower() in lowered or any(
            token in key.lower() for token in ("secret", "token", "password", "map_key", "webhook")
        ):
            out[key] = "set" if value else "missing"
        elif isinstance(value, str):
            out[key] = redact_text(value, extra_secrets=extra_secrets)
        elif isinstance(value, dict):
            out[key] = redact_mapping(value, extra_secrets=extra_secrets)
        else:
            out[key] = value
    return out

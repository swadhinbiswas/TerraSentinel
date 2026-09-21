"""Failure alerting to Slack or Discord.

A scheduled pipeline that fails quietly is worse than one that does not run: the
dashboard keeps serving stale numbers and nobody knows. Every workflow calls this
on failure.

Two rules the implementation holds to:

* **Alerting must never mask the original failure.** Nothing here raises; a dead
  webhook returns ``delivered=False`` and logs, leaving the workflow's own exit
  code and error message intact.
* **Alert bodies are redacted.** A webhook payload is an outbound copy of your
  logs, so it goes through the same redaction path as everything else.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import requests

from ops.redact import redact_mapping, redact_text
from ops.resilience import PermanentError, RetryPolicy, TransientError, retry_call

LOGGER = logging.getLogger(__name__)

__all__ = ["NotifyResult", "build_payload", "detect_channel", "notify", "notify_workflow_failure"]

_LEVEL_EMOJI = {"info": "ℹ️", "warning": "⚠️", "error": "🛑", "critical": "🚨"}


@dataclass(slots=True)
class NotifyResult:
    delivered: bool
    channel: str
    status_code: int | None = None
    error: str | None = None
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "channel": self.channel,
            "status_code": self.status_code,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
        }


def detect_channel(webhook_url: str) -> str:
    url = (webhook_url or "").lower()
    if "hooks.slack.com" in url:
        return "slack"
    if "discord" in url:
        return "discord"
    return "generic"


def build_payload(
    channel: str,
    message: str,
    *,
    level: str = "error",
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a channel-appropriate payload with a redacted body."""
    safe_message = redact_text(message)
    safe_context = redact_mapping(context or {})
    prefix = _LEVEL_EMOJI.get(level.lower(), "")
    headline = f"{prefix} {safe_message}".strip()

    lines = [headline]
    for key, value in safe_context.items():
        lines.append(f"• *{key}*: {value}")
    body = "\n".join(lines) if lines else headline

    if channel == "slack":
        return {"text": body}
    if channel == "discord":
        return {"content": body[:1900]}
    return {"message": safe_message, "level": level, "context": safe_context}


def notify(
    message: str,
    *,
    level: str = "error",
    context: Mapping[str, Any] | None = None,
    webhook_url: str | None = None,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> NotifyResult:
    """Post an alert. Never raises — returns a result describing what happened."""
    try:
        if webhook_url:
            url: str | None = webhook_url
        else:
            # Read the environment directly rather than importing the collectors
            # package: this code runs in the failure path, where the dependency
            # graph that just broke must not be required to report the breakage.
            url = (os.environ.get("ALERT_WEBHOOK_URL") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - never let alerting break the caller
        LOGGER.warning("alerting disabled: %s", redact_text(exc))
        return NotifyResult(False, "none", error=redact_text(exc))

    if not url:
        LOGGER.warning(
            "ALERT_WEBHOOK_URL is not set — alert not delivered: %s", redact_text(message)
        )
        return NotifyResult(False, "none", skipped_reason="no webhook configured")

    channel = detect_channel(url)
    payload = build_payload(channel, message, level=level, context=context)
    client = session or requests.Session()
    retry = policy or RetryPolicy(attempts=3, base_delay=1.0, max_delay=8.0)

    def post() -> int:
        try:
            response = client.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise TransientError(f"webhook request failed: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"webhook HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PermanentError(f"webhook HTTP {response.status_code}: {response.text[:200]}")
        return response.status_code

    try:
        status = retry_call(post, policy=retry, sleep=sleep, description=f"{channel} webhook")
    except Exception as exc:  # noqa: BLE001 - alerting is best-effort by design
        LOGGER.error("alert delivery failed (%s): %s", channel, redact_text(exc))
        return NotifyResult(False, channel, error=redact_text(exc))

    LOGGER.info("alert delivered to %s (HTTP %s)", channel, status)
    return NotifyResult(True, channel, status_code=status)


def workflow_context(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """GitHub Actions run metadata, safe to include in an alert body."""
    env = os.environ if environ is None else environ
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY")
    run_id = env.get("GITHUB_RUN_ID")
    context: dict[str, Any] = {
        "workflow": env.get("GITHUB_WORKFLOW"),
        "job": env.get("GITHUB_JOB"),
        "event": env.get("GITHUB_EVENT_NAME"),
        "ref": env.get("GITHUB_REF_NAME"),
        "sha": (env.get("GITHUB_SHA") or "")[:8] or None,
        "actor": env.get("GITHUB_ACTOR"),
    }
    if repo and run_id:
        context["run_url"] = f"{server}/{repo}/actions/runs/{run_id}"
    return {key: value for key, value in context.items() if value}


def notify_workflow_failure(
    *,
    message: str | None = None,
    extra_context: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> NotifyResult:
    """Alert that a workflow step failed, enriched with Actions metadata."""
    context: dict[str, Any] = dict(workflow_context())
    if extra_context:
        context.update(redact_mapping(extra_context))
    headline = message or f"TerraSentinel workflow failed: {context.get('workflow', 'unknown')}"
    return notify(headline, level="error", context=context, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a TerraSentinel alert to Slack/Discord.")
    parser.add_argument("--message", required=True)
    parser.add_argument("--level", default="error", choices=["info", "warning", "error", "critical"])
    parser.add_argument("--context", default=None, help="JSON object of extra context")
    parser.add_argument("--require-delivery", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    context: dict[str, Any] = {}
    if args.context:
        try:
            context = json.loads(args.context)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--context must be valid JSON: {exc}") from exc

    result = notify_workflow_failure(message=args.message, extra_context=context)
    print(json.dumps(result.as_dict(), indent=2))
    if args.require_delivery and not result.delivered:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Assert the training run evaluated the documented events, whatever the answer.

CI cannot gate on the real-world known events (they are real, dated, and the CI lake is
synthetic), but it must not let the check silently disappear either — a validation step
that stops running looks exactly like a validation step that passes. This asserts the
checks were *produced*, and prints the verdicts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ARTIFACT = "training_report.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="ml/artifacts/ci")
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args(argv)

    report_path = Path(args.out_dir) / ARTIFACT
    if not report_path.is_file():
        print(
            f"{report_path} not found — the training run did not write its report, so the "
            "known-events check cannot be confirmed to have run",
            file=sys.stderr,
        )
        return 2

    report = json.loads(report_path.read_text(encoding="utf-8"))
    events = report.get("known_events") or []
    if not events:
        print("the training report contains no known-event outcomes", file=sys.stderr)
        return 1

    for event in events:
        print(f"  {'PASS' if event.get('passed') else 'FAIL'}  {event.get('window')}: {event.get('verdict')}")

    passed = sum(1 for event in events if event.get("passed"))
    print(f"\n{passed}/{len(events)} known-event checks passed")

    if args.require_pass and passed != len(events):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

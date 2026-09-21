"""Export Python-side registries into dbt seeds, so there is exactly one source of truth.

Study regions are defined once, in `collectors/config.py`. dbt needs them as a
relation to build a dense region x date spine (without which a quiet region simply
vanishes from a `GROUP BY`, which looks identical to "no anomaly"). Rather than
duplicating the list in SQL — where it would silently drift — this writes a seed
that CI regenerates and diffs.

    python -m tools.export_seeds            # write transform/seeds/*.csv
    python -m tools.export_seeds --check    # fail if the committed seed has drifted
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

from collectors.config import REGIONS, SOURCES

REPO_ROOT = Path(__file__).resolve().parents[1]
SEEDS_DIR = REPO_ROOT / "transform" / "seeds"

REGION_COLUMNS = ("region_id", "name", "anomaly_type", "west", "south", "east", "north", "h3_resolution")
SOURCE_COLUMNS = ("source_id", "label", "metric_types", "anomaly_types", "h3_resolution", "cadence_cron", "attribution")


def regions_csv() -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REGION_COLUMNS)
    for region in REGIONS:
        west, south, east, north = region.bbox
        writer.writerow(
            [
                region.region_id,
                region.name,
                region.anomaly_type,
                f"{west}",
                f"{south}",
                f"{east}",
                f"{north}",
                region.h3_resolution,
            ]
        )
    return buffer.getvalue()


def sources_csv() -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SOURCE_COLUMNS)
    for spec in SOURCES.values():
        writer.writerow(
            [
                spec.source_id,
                spec.label,
                "|".join(spec.metric_types),
                "|".join(spec.anomaly_types),
                spec.h3_resolution,
                spec.cadence_cron,
                spec.attribution,
            ]
        )
    return buffer.getvalue()


def expected_seeds() -> dict[str, str]:
    return {"regions.csv": regions_csv(), "sources.csv": sources_csv()}


def write_seeds(destination: Path = SEEDS_DIR) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in expected_seeds().items():
        path = destination / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def check_seeds(destination: Path = SEEDS_DIR) -> list[str]:
    """Return the names of seeds that are missing or out of date."""
    drifted = []
    for name, content in expected_seeds().items():
        path = destination / name
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            drifted.append(name)
    return drifted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit non-zero if seeds have drifted")
    parser.add_argument("--out", default=str(SEEDS_DIR))
    args = parser.parse_args(argv)

    destination = Path(args.out)
    if args.check:
        drifted = check_seeds(destination)
        if drifted:
            print(
                f"seed drift detected in {drifted}: run `python -m tools.export_seeds` and commit the result",
                file=sys.stderr,
            )
            return 1
        print("seeds are in sync with collectors/config.py")
        return 0

    for path in write_seeds(destination):
        print(f"wrote {path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

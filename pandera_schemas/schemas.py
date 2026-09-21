"""Pandera schemas: the data contracts between pipeline layers.

Three tiers of contract:

  * **bronze**  — source-shaped, append-only raw landings. Only the invariants
    that must hold for *any* downstream use are asserted (coordinates in range,
    a real timestamp, an H3 index that is actually a valid cell).
  * **envelope** — the provenance columns the collection layer adds to every
    landing. Asserted on write so a collector can never publish an unlabelled or
    unindexed row.
  * **silver** — the unified observation shape (one row = one reading) that the
    transform layer reads. Defined here so collectors and dbt tests agree on one
    contract instead of drifting.

Validation failures are re-raised as ``SchemaValidationError`` with a compact,
specific message: column, check, offending value, and how many rows failed.
"""

from __future__ import annotations

from typing import Any

import h3
import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

from ops.resilience import SchemaValidationError

__all__ = [
    "BRONZE_ENVELOPE",
    "BRONZE_FIRMS",
    "BRONZE_NOAA_NSIDC",
    "BRONZE_SENTINEL",
    "FIRMS_CONFIDENCE_LEVELS",
    "METRIC_UNITS",
    "SILVER_OBSERVATIONS",
    "conform_to_schema",
    "validate_frame",
]

_UTC = "datetime64[ns, UTC]"

#: FIRMS reports VIIRS confidence as l/n/h and MODIS as 0-100; both normalise here.
FIRMS_CONFIDENCE_LEVELS = ("low", "nominal", "high")

#: Canonical unit per metric, kept in one place so dashboards and models agree.
METRIC_UNITS: dict[str, str] = {
    "fire_radiative_power": "MW",
    "brightness": "K",
    "ndvi": "index",
    "sar_backscatter": "dB",
    "sst": "degC",
    "sst_anomaly": "degC",
    "sea_ice_extent": "10^6 km^2",
    "sea_ice_extent_climatology": "10^6 km^2",
    "sea_ice_area": "10^6 km^2",
}


def _valid_h3(series: Any) -> Any:
    return series.map(lambda value: isinstance(value, str) and h3.is_valid_cell(value))


BRONZE_ENVELOPE = pa.DataFrameSchema(
    {
        "source_id": pa.Column(str, checks=pa.Check.str_length(min_value=1)),
        "metric_type": pa.Column(str, checks=pa.Check.str_length(min_value=1)),
        "region_id": pa.Column(str, nullable=True),
        "h3_index": pa.Column(str, checks=pa.Check(_valid_h3, name="valid_h3_cell")),
        "ingested_at": pa.Column(_UTC),
        "run_id": pa.Column(str, checks=pa.Check.str_length(min_value=6)),
    },
    name="bronze_envelope",
    coerce=True,
)

#: NASA FIRMS active fire detections.
#: Required columns are the ones every FIRMS product provides; the rest are
#: product-specific (MODIS has ``brightness``, VIIRS has ``bright_ti4``/``ti5``)
#: and stay optional so one schema covers MODIS_SP, VIIRS_SNPP_* and NOAA-20/21.
BRONZE_FIRMS = pa.DataFrameSchema(
    {
        "latitude": pa.Column(float, checks=pa.Check.in_range(-90.0, 90.0), coerce=True),
        "longitude": pa.Column(float, checks=pa.Check.in_range(-180.0, 180.0), coerce=True),
        "acq_datetime": pa.Column(_UTC),
        "acq_date": pa.Column(str, checks=pa.Check.str_matches(r"^\d{4}-\d{2}-\d{2}$")),
        "acq_time": pa.Column(str, checks=pa.Check.str_matches(r"^\d{4}$")),
        "product": pa.Column(str, checks=pa.Check.str_length(min_value=3)),
        "confidence": pa.Column(str, checks=pa.Check.isin(FIRMS_CONFIDENCE_LEVELS)),
        "confidence_pct": pa.Column(float, checks=pa.Check.in_range(0.0, 100.0), coerce=True),
        "satellite": pa.Column(str, nullable=True, required=False),
        "instrument": pa.Column(str, nullable=True, required=False),
        "daynight": pa.Column(
            str, nullable=True, required=False, checks=pa.Check.isin(["D", "N"])
        ),
        "frp": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.ge(0.0), coerce=True
        ),
        "brightness": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.gt(0.0), coerce=True
        ),
        "bright_ti4": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.gt(0.0), coerce=True
        ),
        "bright_ti5": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.gt(0.0), coerce=True
        ),
        "scan": pa.Column(float, nullable=True, required=False, coerce=True),
        "track": pa.Column(float, nullable=True, required=False, coerce=True),
        "version": pa.Column(str, nullable=True, required=False),
        "type": pa.Column(float, nullable=True, required=False, coerce=True),
    },
    name="bronze_firms",
    coerce=True,
)

#: Sentinel-derived composites: one row per (spatial unit, window, product).
#: Deliberately statistics rather than pixels — raw imagery never touches HF.
#: ``spatial_scope="region"`` rows carry a full multi-window time series for one
#: region; ``spatial_scope="grid"`` rows carry a single year-over-year change
#: value for one H3 cell and therefore have no pixel count of their own.
BRONZE_SENTINEL = pa.DataFrameSchema(
    {
        "window_start": pa.Column(_UTC),
        "window_end": pa.Column(_UTC),
        "latitude": pa.Column(float, checks=pa.Check.in_range(-90.0, 90.0), coerce=True),
        "longitude": pa.Column(float, checks=pa.Check.in_range(-180.0, 180.0), coerce=True),
        "product": pa.Column(str, checks=pa.Check.isin(["S2_SR_HARMONIZED", "S1_GRD"])),
        "metric_type": pa.Column(str, checks=pa.Check.isin(["ndvi", "sar_backscatter"])),
        "mean": pa.Column(float, nullable=True, coerce=True),
        #: Region-grain composites carry a dispersion summary; grid-grain change
        #: cells carry a single signed delta and legitimately have none, so these
        #: are optional rather than null-filled.
        "stddev": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.ge(0.0), coerce=True
        ),
        "min": pa.Column(float, nullable=True, required=False, coerce=True),
        "max": pa.Column(float, nullable=True, required=False, coerce=True),
        "observation_count": pa.Column(
            # Declared as the pandas nullable integer rather than numpy `int`:
            # numpy int64 cannot hold NA, so an all-null optional column (every
            # grid-grain change row) fails coercion instead of validating.
            "Int64",
            nullable=True,
            required=False,
            checks=pa.Check.ge(0),
            coerce=True,
        ),
        "cloud_pct": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.in_range(0.0, 100.0), coerce=True
        ),
        "spatial_scope": pa.Column(
            str,
            nullable=True,
            required=False,
            checks=pa.Check.isin(["region", "grid"]),
        ),
        "baseline_mean": pa.Column(float, nullable=True, required=False, coerce=True),
        "recent_mean": pa.Column(float, nullable=True, required=False, coerce=True),
    },
    name="bronze_sentinel",
    coerce=True,
)

#: NOAA OISST SST anomalies and NSIDC Sea Ice Index rows share this shape.
#: ``spatial_scope`` is explicit because a hemispheric sea-ice index row has a
#: nominal coordinate (the basin centre) rather than a measurement location —
#: pretending it is a point would corrupt any spatial aggregation downstream.
BRONZE_NOAA_NSIDC = pa.DataFrameSchema(
    {
        "timestamp": pa.Column(_UTC),
        "latitude": pa.Column(float, checks=pa.Check.in_range(-90.0, 90.0), coerce=True),
        "longitude": pa.Column(float, checks=pa.Check.in_range(-180.0, 180.0), coerce=True),
        "metric_type": pa.Column(
            str,
            checks=pa.Check.isin(
                ["sst_anomaly", "sea_ice_extent", "sea_ice_extent_climatology"]
            ),
        ),
        "product": pa.Column(str, checks=pa.Check.str_length(min_value=3)),
        "value": pa.Column(float, coerce=True),
        "anomaly": pa.Column(float, nullable=True, required=False, coerce=True),
        "unit": pa.Column(str, nullable=True),
        "spatial_scope": pa.Column(
            str,
            nullable=True,
            required=False,
            checks=pa.Check.isin(["point", "gridded", "hemispheric"]),
        ),
        "baseline_stddev": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.ge(0.0), coerce=True
        ),
        "day_of_year": pa.Column(
            float, nullable=True, required=False, checks=pa.Check.in_range(1, 366), coerce=True
        ),
    },
    name="bronze_noaa_nsidc",
    coerce=True,
)

#: The unified silver contract consumed by dbt staging models.
SILVER_OBSERVATIONS = pa.DataFrameSchema(
    {
        "observed_at": pa.Column(_UTC),
        "metric_type": pa.Column(str, checks=pa.Check.isin(sorted(METRIC_UNITS))),
        "value": pa.Column(float, coerce=True),
        "unit": pa.Column(str, checks=pa.Check.isin(sorted(set(METRIC_UNITS.values())))),
        "source_id": pa.Column(str, checks=pa.Check.isin(["firms", "sentinel", "noaa_nsidc"])),
        "region_id": pa.Column(str),
        "h3_index": pa.Column(str, checks=pa.Check(_valid_h3, name="valid_h3_cell")),
        "latitude": pa.Column(float, checks=pa.Check.in_range(-90.0, 90.0), coerce=True),
        "longitude": pa.Column(float, checks=pa.Check.in_range(-180.0, 180.0), coerce=True),
        "product": pa.Column(str),
        "ingested_at": pa.Column(_UTC),
    },
    name="silver_observations",
    coerce=True,
)


#: Null-filled stand-ins must be created with a usable dtype: pandera cannot coerce
#: an all-null ``object`` column to a numeric or timestamp type, which would turn a
#: harmless "this product did not supply that column" into a validation failure.
_NA_DTYPE_BY_DECLARED: dict[Any, str] = {
    float: "float64",
    "float64": "float64",
    "float32": "float64",
    int: "Int64",
    "int64": "Int64",
    str: "object",
    "str": "object",
    "object": "object",
    "bool": "boolean",
    _UTC: _UTC,
}


#: The null literal has to match the column dtype: pandas refuses to place ``pd.NA``
#: in a float64 series, and an all-null ``object`` column cannot be coerced to a
#: numeric type by pandera — either one turns a harmless "this product did not
#: supply that column" into a validation failure.
_NULL_BY_DTYPE: dict[str, Any] = {
    "float64": float("nan"),
    _UTC: pd.NaT,
    "object": None,
}


def _null_series(index: Any, declared_dtype: Any) -> pd.Series:
    dtype = _NA_DTYPE_BY_DECLARED.get(declared_dtype)
    if dtype is None:
        dtype = _UTC if "datetime" in str(declared_dtype) else "object"
    null = _NULL_BY_DTYPE.get(dtype, pd.NA)
    try:
        return pd.Series(null, index=index, dtype=dtype)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return pd.Series(None, index=index, dtype="object")


def conform_to_schema(frame: Any, schema: pa.DataFrameSchema) -> Any:
    """Ensure every declared column exists, without dropping undeclared ones.

    Sources differ in what they provide — MODIS ships ``brightness`` where VIIRS
    ships ``bright_ti4``, and a lake containing only one product would otherwise
    have no ``brightness`` column at all. That asymmetry is invisible until a
    transform references the missing column and breaks, so absent declared columns
    are materialised as null at write time. Validation then decides whether any
    *required* column being null is acceptable, with an error that names it.

    Columns present in the data but not declared are preserved: bronze is
    append-only and source-shaped, so upstream additions must not be silently
    dropped by an out-of-date contract.
    """
    declared = list(schema.columns.keys())
    missing = [column for column in declared if column not in frame.columns]
    if not missing:
        return frame

    conformed = frame.copy()
    for column in missing:
        conformed[column] = _null_series(conformed.index, schema.columns[column].dtype)
    ordered = declared + [column for column in conformed.columns if column not in declared]
    return conformed[ordered]


def validate_frame(
    frame: Any,
    schema: pa.DataFrameSchema,
    *,
    label: str | None = None,
) -> Any:
    """Validate ``frame`` against ``schema``, raising a specific readable error.

    Collects *all* failures (``lazy=True``) so one run reports every broken
    column instead of stopping at the first.
    """
    name = label or schema.name or "frame"
    try:
        return schema.validate(frame, lazy=True)
    except SchemaErrors as exc:
        raise SchemaValidationError(_format_failures(exc, name, frame)) from exc


def _format_failures(exc: pa.errors.SchemaErrors, name: str, frame: Any) -> str:
    cases = getattr(exc, "failure_cases", None)
    lines = [f"{name}: schema validation failed ({_row_count(frame)} row(s) checked)"]

    if cases is not None and len(cases):
        grouped = cases.groupby("column", dropna=False)["check"].unique()
        for column, checks in grouped.items():
            lines.append(f"  - column {column!r} failed: {', '.join(map(str, checks))}")
        sample = cases.head(5)
        for _, row in sample.iterrows():
            lines.append(
                f"    e.g. index={row.get('index')} column={row.get('column')!r} "
                f"value={row.get('failure_case')!r}"
            )
        extra = len(cases) - len(sample)
        if extra > 0:
            lines.append(f"    ... and {extra} more failing case(s)")
    else:
        lines.append(f"  - {exc}")

    return "\n".join(lines)


def _row_count(frame: Any) -> int:
    try:
        return len(frame)
    except TypeError:
        return 0

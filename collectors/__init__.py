"""TerraSentinel collectors: free-tier satellite/sensor ingestion."""

from collectors.config import (
    REGIONS,
    SOURCES,
    MissingCredential,
    Region,
    SourceSpec,
    get_secret,
)

__all__ = ["REGIONS", "SOURCES", "MissingCredential", "Region", "SourceSpec", "get_secret"]

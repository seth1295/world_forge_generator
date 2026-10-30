# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Verifiable terrain-sampling interface for draping source road centerlines."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .models import CancellationToken

TERRAIN_SAMPLER_INTERFACE_ID = "worldforge_terrain_sampler_v1"


class TerrainSampleOutcome(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HeightReference(StrEnum):
    """The only vertical reference accepted for a ready draped road."""

    WGS84_ELLIPSOIDAL = "EPSG:4979"


@dataclass(frozen=True)
class TerrainSampleTrace:
    """Per-coordinate source and conversion evidence for a drape height."""

    source_height_m: float | None
    source_vertical_reference: str
    geoid_undulation_m: float | None
    ellipsoidal_height_m: float
    terrain_artifact_id: str
    terrain_artifact_sha256: str
    sampler_method: str
    conversion_method: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerrainSamples:
    outcome: TerrainSampleOutcome
    heights_m: tuple[float, ...] = ()
    vertical_reference: HeightReference | None = None
    sample_method: str | None = None
    source_lineage: tuple[dict[str, object], ...] = ()
    sample_trace: tuple[TerrainSampleTrace, ...] = ()
    error_code: str | None = None


class TerrainSampler(Protocol):
    """Consumer-provided sampler; must report datum, method and real source lineage."""

    def sample_heights(
        self,
        longitude_latitude: Sequence[tuple[float, float]],
        cancellation: CancellationToken,
    ) -> TerrainSamples:
        """Return one source-backed height for each input coordinate or unavailable/cancelled."""


@dataclass(frozen=True)
class DrapeResult:
    """All-or-nothing feature result with explicit sample-gap accounting.

    ``requested_sample_count`` is the number of input line coordinates,
    ``returned_height_count`` is the raw count supplied by the sampler, and
    ``gap_count`` counts missing or invalid source samples. Partial/failed
    results never include draped geometry.
    """

    outcome: TerrainSampleOutcome
    geometry: dict[str, Any] | None
    vertical_reference: HeightReference | None
    sample_method: str | None
    source_lineage: tuple[dict[str, object], ...]
    sample_trace: tuple[TerrainSampleTrace, ...] = ()
    error_code: str | None = None
    requested_sample_count: int = 0
    returned_height_count: int = 0
    gap_count: int = 0


def drape_road_centerline(
    geometry: dict[str, Any],
    sampler: TerrainSampler,
    cancellation: CancellationToken,
) -> DrapeResult:
    """Apply only returned terrain samples; never synthesize heights.

    The coordinates are supplied in GeoJSON/WGS84 axis order (longitude,
    latitude). Height is appended as Z only when a sampler returns a complete
    sample set and a declared vertical reference. This helper does not convert
    to engine-local coordinates or choose a vertical datum for the sampler.
    """

    if cancellation.cancelled:
        return DrapeResult(
            TerrainSampleOutcome.CANCELLED,
            None,
            None,
            None,
            (),
            (),
            cancellation.reason,
        )
    line_parts = _line_parts(geometry)
    if any(len(point) != 2 for part in line_parts for point in part):
        raise ValueError("road drape input must be 2D source coordinates")
    coordinates = [
        tuple(float(value) for value in point[:2])
        for part in line_parts
        for point in part
    ]
    if not coordinates:
        raise ValueError(
            "road centerline must contain at least one line with two coordinates"
        )
    for longitude, latitude in coordinates:
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise ValueError("road coordinates must be finite")
        if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
            raise ValueError(
                "road coordinates must be WGS84 longitude/latitude degrees"
            )
    samples = sampler.sample_heights(coordinates, cancellation)
    requested_count = len(coordinates)
    returned_count = len(samples.heights_m)
    if samples.outcome is TerrainSampleOutcome.CANCELLED or cancellation.cancelled:
        return DrapeResult(
            outcome=TerrainSampleOutcome.CANCELLED,
            geometry=None,
            vertical_reference=samples.vertical_reference,
            sample_method=samples.sample_method,
            source_lineage=samples.source_lineage,
            sample_trace=samples.sample_trace,
            error_code=samples.error_code or cancellation.reason,
            requested_sample_count=requested_count,
            returned_height_count=returned_count,
            gap_count=max(0, requested_count - min(returned_count, requested_count)),
        )
    if samples.outcome is TerrainSampleOutcome.UNAVAILABLE:
        return DrapeResult(
            outcome=TerrainSampleOutcome.UNAVAILABLE,
            geometry=None,
            vertical_reference=samples.vertical_reference,
            sample_method=samples.sample_method,
            source_lineage=samples.source_lineage,
            sample_trace=samples.sample_trace,
            error_code=samples.error_code or "terrain_sample_unavailable",
            requested_sample_count=requested_count,
            returned_height_count=returned_count,
            gap_count=max(0, requested_count - min(returned_count, requested_count)),
        )
    if samples.outcome in {
        TerrainSampleOutcome.PARTIAL,
        TerrainSampleOutcome.FAILED,
    }:
        available_count = min(
            returned_count, len(samples.sample_trace), requested_count
        )
        return DrapeResult(
            outcome=samples.outcome,
            geometry=None,
            vertical_reference=samples.vertical_reference,
            sample_method=samples.sample_method,
            source_lineage=samples.source_lineage,
            sample_trace=samples.sample_trace,
            error_code=samples.error_code or "terrain_sampler_incomplete",
            requested_sample_count=requested_count,
            returned_height_count=returned_count,
            gap_count=max(0, requested_count - available_count),
        )
    if samples.outcome is not TerrainSampleOutcome.READY:
        raise ValueError("unknown terrain sample outcome")
    if samples.vertical_reference is not HeightReference.WGS84_ELLIPSOIDAL:
        return DrapeResult(
            outcome=TerrainSampleOutcome.FAILED,
            geometry=None,
            vertical_reference=samples.vertical_reference,
            sample_method=samples.sample_method,
            source_lineage=samples.source_lineage,
            sample_trace=samples.sample_trace,
            error_code="vertical_reference_invalid",
            requested_sample_count=requested_count,
            returned_height_count=returned_count,
            gap_count=requested_count,
        )
    if not samples.sample_method or not samples.source_lineage:
        return DrapeResult(
            outcome=TerrainSampleOutcome.FAILED,
            geometry=None,
            vertical_reference=samples.vertical_reference,
            sample_method=samples.sample_method,
            source_lineage=samples.source_lineage,
            sample_trace=samples.sample_trace,
            error_code="terrain_lineage_missing",
            requested_sample_count=requested_count,
            returned_height_count=returned_count,
            gap_count=requested_count,
        )
    available_count = min(returned_count, len(samples.sample_trace), requested_count)
    if (
        returned_count != requested_count
        or len(samples.sample_trace) != requested_count
    ):
        missing_count = requested_count - available_count
        outcome = (
            TerrainSampleOutcome.PARTIAL
            if missing_count > 0
            else TerrainSampleOutcome.FAILED
        )
        return DrapeResult(
            outcome=outcome,
            geometry=None,
            vertical_reference=samples.vertical_reference,
            sample_method=samples.sample_method,
            source_lineage=samples.source_lineage,
            sample_trace=samples.sample_trace,
            error_code="terrain_sample_count_mismatch",
            requested_sample_count=requested_count,
            returned_height_count=returned_count,
            gap_count=max(0, missing_count),
        )
    invalid_sample_gaps = 0
    for height, trace in zip(samples.heights_m, samples.sample_trace, strict=True):
        try:
            finite_height = math.isfinite(float(height))
        except (TypeError, ValueError, OverflowError):
            finite_height = False
        if not finite_height:
            invalid_sample_gaps += 1
            continue
        if (
            not isinstance(trace.source_vertical_reference, str)
            or not trace.source_vertical_reference
            or not isinstance(trace.sampler_method, str)
            or not trace.sampler_method
        ):
            invalid_sample_gaps += 1
            continue
        if (
            not isinstance(trace.terrain_artifact_id, str)
            or not trace.terrain_artifact_id
        ):
            invalid_sample_gaps += 1
            continue
        try:
            trace_height_valid = math.isfinite(float(trace.ellipsoidal_height_m)) and (
                float(trace.ellipsoidal_height_m) == float(height)
            )
        except (TypeError, ValueError, OverflowError):
            trace_height_valid = False
        if not trace_height_valid:
            invalid_sample_gaps += 1
            continue
        if (
            not isinstance(trace.terrain_artifact_sha256, str)
            or len(trace.terrain_artifact_sha256) != 64
            or any(
                char not in "0123456789abcdef" for char in trace.terrain_artifact_sha256
            )
        ):
            invalid_sample_gaps += 1
            continue
        try:
            source_height_valid = trace.source_height_m is not None and math.isfinite(
                float(trace.source_height_m)
            )
        except (TypeError, ValueError, OverflowError):
            source_height_valid = False
        if not source_height_valid:
            invalid_sample_gaps += 1
            continue
        source_reference = trace.source_vertical_reference.casefold()
        if "egm2008" in source_reference or "orthometric" in source_reference:
            try:
                geoid_valid = trace.geoid_undulation_m is not None and math.isfinite(
                    float(trace.geoid_undulation_m)
                )
            except (TypeError, ValueError, OverflowError):
                geoid_valid = False
            if not geoid_valid:
                invalid_sample_gaps += 1
                continue
            if (
                not isinstance(trace.conversion_method, str)
                or not trace.conversion_method
            ):
                invalid_sample_gaps += 1
    if invalid_sample_gaps:
        return DrapeResult(
            outcome=TerrainSampleOutcome.FAILED,
            geometry=None,
            vertical_reference=samples.vertical_reference,
            sample_method=samples.sample_method,
            source_lineage=samples.source_lineage,
            sample_trace=samples.sample_trace,
            error_code="terrain_sample_provenance_or_conversion_invalid",
            requested_sample_count=requested_count,
            returned_height_count=returned_count,
            gap_count=invalid_sample_gaps,
        )
    iterator = iter(float(height) for height in samples.heights_m)
    draped_parts = [
        [[float(point[0]), float(point[1]), next(iterator)] for point in part]
        for part in line_parts
    ]
    if geometry.get("type") == "LineString":
        output = {"type": "LineString", "coordinates": draped_parts[0]}
    else:
        output = {"type": "MultiLineString", "coordinates": draped_parts}
    return DrapeResult(
        TerrainSampleOutcome.READY,
        output,
        samples.vertical_reference,
        samples.sample_method,
        samples.source_lineage,
        samples.sample_trace,
        requested_sample_count=requested_count,
        returned_height_count=returned_count,
        gap_count=0,
    )


def _line_parts(geometry: dict[str, Any]) -> list[list[list[float]]]:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "LineString" and isinstance(coordinates, (list, tuple)):
        return [list(coordinates)]
    if kind == "MultiLineString" and isinstance(coordinates, (list, tuple)):
        return [list(line) for line in coordinates if isinstance(line, (list, tuple))]
    raise ValueError(
        "terrain drape interface accepts LineString or MultiLineString geometry"
    )

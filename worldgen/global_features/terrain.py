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
    UNAVAILABLE = "unavailable"
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
    outcome: TerrainSampleOutcome
    geometry: dict[str, Any] | None
    vertical_reference: HeightReference | None
    sample_method: str | None
    source_lineage: tuple[dict[str, object], ...]
    sample_trace: tuple[TerrainSampleTrace, ...] = ()
    error_code: str | None = None


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
    if samples.outcome is TerrainSampleOutcome.CANCELLED or cancellation.cancelled:
        return DrapeResult(
            TerrainSampleOutcome.CANCELLED,
            None,
            samples.vertical_reference,
            samples.sample_method,
            samples.source_lineage,
            samples.sample_trace,
            samples.error_code or cancellation.reason,
        )
    if samples.outcome is TerrainSampleOutcome.UNAVAILABLE:
        return DrapeResult(
            TerrainSampleOutcome.UNAVAILABLE,
            None,
            samples.vertical_reference,
            samples.sample_method,
            samples.source_lineage,
            samples.sample_trace,
            samples.error_code,
        )
    if samples.outcome is not TerrainSampleOutcome.READY:
        raise ValueError("unknown terrain sample outcome")
    if samples.vertical_reference is not HeightReference.WGS84_ELLIPSOIDAL:
        raise ValueError(
            "ready road drapes require WGS84 ellipsoidal height (EPSG:4979); orthometric heights are not accepted"
        )
    if not samples.sample_method or not samples.source_lineage:
        raise ValueError(
            "ready terrain samples must carry a sampler method and source lineage"
        )
    if len(samples.heights_m) != len(coordinates):
        raise ValueError("terrain sampler must return one height per road coordinate")
    if len(samples.sample_trace) != len(coordinates):
        raise ValueError(
            "terrain sampler must return one provenance trace per road coordinate"
        )
    if any(not math.isfinite(float(height)) for height in samples.heights_m):
        raise ValueError("terrain sampler returned a non-finite height")
    for height, trace in zip(samples.heights_m, samples.sample_trace, strict=True):
        if not trace.source_vertical_reference or not trace.sampler_method:
            raise ValueError(
                "terrain sample trace must name its source vertical reference and method"
            )
        if not trace.terrain_artifact_id:
            raise ValueError("terrain sample trace must identify its source artifact")
        if not math.isfinite(float(trace.ellipsoidal_height_m)) or float(
            trace.ellipsoidal_height_m
        ) != float(height):
            raise ValueError(
                "terrain trace ellipsoidal height must match the returned sample"
            )
        if len(trace.terrain_artifact_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in trace.terrain_artifact_sha256
        ):
            raise ValueError(
                "terrain sample trace must carry a lowercase SHA-256 artifact hash"
            )
        if trace.source_height_m is None or not math.isfinite(
            float(trace.source_height_m)
        ):
            raise ValueError("terrain sample trace must preserve its source height")
        source_reference = trace.source_vertical_reference.casefold()
        if "egm2008" in source_reference or "orthometric" in source_reference:
            if trace.geoid_undulation_m is None or not math.isfinite(
                float(trace.geoid_undulation_m)
            ):
                raise ValueError(
                    "orthometric terrain samples require the geoid undulation used in conversion"
                )
            if not trace.conversion_method:
                raise ValueError(
                    "orthometric terrain samples require the height-conversion method"
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

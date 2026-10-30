# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Shared source, outcome, and cancellation types for global feature bundles."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum


class LayerOutcome(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    NO_SOURCE_COVERAGE = "no_source_coverage"
    NOT_GENERATED = "not_generated"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceError(RuntimeError):
    """A bounded and classified provider failure safe to record in a manifest."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class CancelledError(RuntimeError):
    """Raised at an explicit cancellation or timeout boundary."""

    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """Thread-safe cooperative cancellation checked between bounded I/O calls."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = "cancelled"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason if reason in {"cancelled", "timeout"} else "cancelled"
            self._event.set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError(self._reason)


@dataclass(frozen=True)
class FeatureLayerSpec:
    name: str
    overture_theme: str
    overture_type: str
    geometry_dimension: int  # Overture output component: 2=line, 3=polygon
    geometry_semantics: str
    license_expression: str
    attribution: tuple[str, ...]
    source_url: str
    theme_sources: tuple[str, ...] = ()


OVERTURE_LAYERS: dict[str, FeatureLayerSpec] = {
    "roads": FeatureLayerSpec(
        name="roads",
        overture_theme="transportation",
        overture_type="segment",
        geometry_dimension=2,
        geometry_semantics="road and transportation segment centerlines; no width or elevation is invented",
        license_expression="ODbL-1.0",
        attribution=("© OpenStreetMap contributors, Overture Maps Foundation",),
        source_url="https://docs.overturemaps.org/attribution/",
        theme_sources=("OpenStreetMap contributors (ODbL)", "TomTom"),
    ),
    "buildings": FeatureLayerSpec(
        name="buildings",
        overture_theme="buildings",
        overture_type="building",
        geometry_dimension=3,
        geometry_semantics="building footprints; height, roof and other source semantics only when supplied",
        license_expression="ODbL-1.0",
        attribution=("© OpenStreetMap contributors, Overture Maps Foundation",),
        source_url="https://docs.overturemaps.org/attribution/",
        theme_sources=(
            "OpenStreetMap contributors (ODbL)",
            "Esri Community Maps contributors (CC BY 4.0)",
            "Global ML Building Footprints (ODbL)",
            "Google Open Buildings (CC BY 4.0)",
            "USGS 3D Elevation Program Digital Elevation Program",
            "Qian Shi et al. building data (CC BY 4.0)",
            "BTN 2024-derived work (CC BY 4.0)",
        ),
    ),
    "water": FeatureLayerSpec(
        name="water",
        overture_theme="base",
        overture_type="water",
        geometry_dimension=3,
        geometry_semantics="mapped water features; absence is not proof of dry land or complete water coverage",
        license_expression="ODbL-1.0",
        attribution=("© OpenStreetMap contributors, Overture Maps Foundation",),
        source_url="https://docs.overturemaps.org/attribution/",
        theme_sources=(
            "OpenStreetMap contributors (ODbL)",
            "Daylight Map Distribution",
            "ESA WorldCover project 2020 (CC BY 4.0)",
            "ETOPO1 (Open Data Commons Public Domain Dedication and License)",
            "GLOBathy (CC0 1.0 assumed by Overture attribution documentation)",
        ),
    ),
}


@dataclass(frozen=True)
class LayerArtifact:
    layer_id: str
    outcome: LayerOutcome
    payloads: tuple[dict[str, object], ...] = field(default_factory=tuple)
    feature_count: int | None = None
    pixel_count: int | None = None
    coverage_claim: str = "no_completeness_claim"
    coverage_confidence: str = "source response only; completeness not established"
    query_bounds_wgs84: tuple[float, float, float, float] | None = None
    source_lineage: tuple[dict[str, object], ...] = field(default_factory=tuple)
    attribution: tuple[str, ...] = field(default_factory=tuple)
    transform: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True)
class GenerationLimits:
    max_request_tiles: int = 64
    max_features_per_layer: int = 1_000_000
    max_payload_bytes_per_layer: int = 512 * 1024 * 1024
    max_shard_features: int = 10_000
    max_shard_bytes: int = 16 * 1024 * 1024
    max_feature_bytes: int = 8 * 1024 * 1024
    max_raster_pixels: int = 10_000_000
    max_bundle_bytes: int = 2 * 1024 * 1024 * 1024
    max_cache_bytes: int = 8 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if self.max_shard_bytes > self.max_payload_bytes_per_layer:
            raise ValueError(
                "max_shard_bytes cannot exceed max_payload_bytes_per_layer"
            )

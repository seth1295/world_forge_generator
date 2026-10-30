# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Standalone global-feature bundle generation and cache resolution API."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Any

from .addressing import ADDRESS_SCHEME, TileAddress, tiles_for_bbox
from .cache import SCHEMA_ID, CacheError, CacheStore, _sha256_file
from .geometry import canonical_json
from .models import (
    OVERTURE_LAYERS,
    CancellationToken,
    CancelledError,
    GenerationLimits,
    LayerArtifact,
    LayerOutcome,
    SourceError,
)
from .overture import (
    OVERTURE_ATTRIBUTION_URL,
    OVERTURE_RELEASE,
    OVERTURE_RELEASE_NOTES,
    OvertureProvider,
)
from .vegetation import ALGORITHM_ID as VEGETATION_ALGORITHM_ID
from .vegetation import build_placement_parameters
from .worldcover import (
    MAX_COG_CACHE_BYTES,
    WORLDCOVER_ATTRIBUTION,
    WORLDCOVER_CITATION,
    WORLDCOVER_LICENSE,
    WORLDCOVER_PIXEL_CENTER_FORMULA,
    WORLDCOVER_PRODUCT,
    WORLDCOVER_RELEASE,
    WORLDCOVER_TERMS_URL,
    WorldCoverProvider,
    WorldCoverWindow,
    worldcover_source_for_tile,
)

PRODUCER_ID = "WorldForge Global Geographic Feature Generator"
PRODUCER_VERSION = "1.2.0"
DEFAULT_LAYERS = ("roads", "buildings", "water", "landcover", "vegetation")
VECTOR_LAYERS = ("roads", "buildings", "water")
ALL_LAYERS = frozenset(DEFAULT_LAYERS)
CREATED_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def resolve_source_revision() -> str:
    """Return the explicit build revision or the current Git revision/state."""

    configured = os.environ.get("WORLDFORGE_SOURCE_REVISION", "").strip()
    if configured:
        if any(char.isspace() for char in configured):
            raise ValueError("WORLDFORGE_SOURCE_REVISION must not contain whitespace")
        return configured
    repository_root = Path(__file__).resolve().parents[2]
    try:
        revision_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        revision = revision_result.stdout.strip().lower()
        if not revision:
            return "source-revision-unavailable"
        state_result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if state_result.stdout.strip():
            return f"{revision}+dirty"
        return revision
    except (OSError, subprocess.SubprocessError):
        return "source-revision-unavailable"


@dataclass(frozen=True)
class BundleResult:
    bundle_id: str
    address: TileAddress
    outcome: LayerOutcome
    path: Path | None
    manifest: dict[str, Any]
    cache_hit: bool = False


class GlobalFeatureGenerator:
    """Resolve WFG-1 tile bundles from public pinned providers, without BeamNG."""

    def __init__(
        self,
        cache_root: str | Path,
        *,
        limits: GenerationLimits | None = None,
        overture: OvertureProvider | None = None,
        worldcover: WorldCoverProvider | None = None,
        source_revision: str | None = None,
    ) -> None:
        self.source_revision = source_revision or resolve_source_revision()
        if any(char.isspace() for char in self.source_revision):
            raise ValueError("source_revision must not contain whitespace")
        self.limits = limits or GenerationLimits()
        self.cache = CacheStore(cache_root, max_cache_bytes=self.limits.max_cache_bytes)
        self.overture = overture or OvertureProvider(
            max_remote_bytes_per_layer=self.limits.max_payload_bytes_per_layer * 2
        )
        self.worldcover = worldcover or WorldCoverProvider(
            cache_dir=Path(cache_root)
            / "worldforge_global_features"
            / "source_cache"
            / "worldcover",
            max_cache_bytes=max(
                1, min(MAX_COG_CACHE_BYTES, self.limits.max_cache_bytes // 4)
            ),
        )

    def resolve_tile(
        self,
        address: TileAddress | str,
        *,
        layers: Iterable[str] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> BundleResult:
        """Return a verified cached bundle or generate one atomically."""

        if isinstance(address, str):
            address = TileAddress.parse(address)
        token = cancellation or CancellationToken()
        requested = self._normalize_layers(layers)
        key = self._cache_key(address, requested)
        cached = self.cache.existing(key)
        if cached is not None:
            manifest = self.cache.verify(cached, expected_key=key)
            return BundleResult(
                key,
                address,
                LayerOutcome(manifest["bundle_outcome"]),
                cached,
                manifest,
                True,
            )
        lock = self.cache.acquire(key)
        stage: Path | None = None
        try:
            cached = self.cache.existing(key)
            if cached is not None:
                manifest = self.cache.verify(cached, expected_key=key)
                return BundleResult(
                    key,
                    address,
                    LayerOutcome(manifest["bundle_outcome"]),
                    cached,
                    manifest,
                    True,
                )
            stage = self.cache.create_stage(key)
            token.raise_if_cancelled()
            layer_artifacts = self._generate_layers(address, requested, stage, token)
            if token.cancelled or any(
                layer.outcome is LayerOutcome.CANCELLED for layer in layer_artifacts
            ):
                cancelled_layers = [
                    self._empty_layer(
                        layer_id,
                        LayerOutcome.CANCELLED
                        if layer_id in requested
                        else LayerOutcome.NOT_GENERATED,
                        requested=layer_id in requested,
                        error_code="cancelled" if layer_id in requested else None,
                    )
                    for layer_id in DEFAULT_LAYERS
                ]
                cancelled_manifest = self._build_manifest(
                    address, key, requested, cancelled_layers, None
                )
                self.cache.cleanup_stage(stage, key)
                stage = None
                return BundleResult(
                    key,
                    address,
                    LayerOutcome.CANCELLED,
                    None,
                    cancelled_manifest,
                    False,
                )
            manifest = self._build_manifest(
                address, key, requested, layer_artifacts, stage
            )
            self._write_manifest(stage, manifest)
            output_bytes = self._bundle_size(stage)
            if output_bytes > self.limits.max_bundle_bytes:
                raise CacheError("generated bundle exceeds max_bundle_bytes")
            if output_bytes > self.limits.max_cache_bytes:
                raise CacheError("generated bundle exceeds max_cache_bytes")
            final_path = self.cache.publish(key, stage)
            stage = None
            final_manifest = self.cache.verify(final_path, expected_key=key)
            return BundleResult(
                key,
                address,
                LayerOutcome(final_manifest["bundle_outcome"]),
                final_path,
                final_manifest,
                False,
            )
        except CancelledError:
            if stage is not None:
                self.cache.cleanup_stage(stage, key)
                stage = None
            empty_layers = [
                self._empty_layer(
                    layer_id,
                    LayerOutcome.CANCELLED,
                    requested=True,
                    error_code="cancelled",
                )
                for layer_id in DEFAULT_LAYERS
            ]
            manifest = self._build_manifest(address, key, requested, empty_layers, None)
            return BundleResult(
                key, address, LayerOutcome.CANCELLED, None, manifest, False
            )
        finally:
            if stage is not None and stage.exists():
                self.cache.cleanup_stage(stage, key)
            self.cache.release(lock)

    def resolve_bbox(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        *,
        level: int = 0,
        layers: Iterable[str] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> list[BundleResult]:
        """Resolve an AOI bbox into bounded WFG-1 tile bundles.

        `west > east` denotes an antimeridian-crossing request. The default
        request cap prevents accidentally scheduling a world-sized download.
        """

        addresses = tiles_for_bbox(
            west,
            south,
            east,
            north,
            level,
            max_tiles=self.limits.max_request_tiles,
        )
        token = cancellation or CancellationToken()
        results: list[BundleResult] = []
        for address in addresses:
            if token.cancelled:
                break
            results.append(
                self.resolve_tile(address, layers=layers, cancellation=token)
            )
        return results

    def _normalize_layers(self, layers: Iterable[str] | None) -> tuple[str, ...]:
        if layers is None:
            return DEFAULT_LAYERS
        result = tuple(sorted(set(layers)))
        if not result:
            raise ValueError("at least one layer must be requested")
        invalid = set(result) - ALL_LAYERS
        if invalid:
            raise ValueError(f"unsupported layers: {', '.join(sorted(invalid))}")
        return result

    def _cache_key(self, address: TileAddress, requested: tuple[str, ...]) -> str:
        identity = {
            "schema_id": SCHEMA_ID,
            "producer_id": PRODUCER_ID,
            "producer_version": PRODUCER_VERSION,
            "producer_source_revision": self.source_revision,
            "address_scheme": ADDRESS_SCHEME,
            "tile_id": address.tile_id,
            "requested_layers": requested,
            "overture_release": OVERTURE_RELEASE,
            "overture_theme_types": {
                name: [
                    OVERTURE_LAYERS[name].overture_theme,
                    OVERTURE_LAYERS[name].overture_type,
                ]
                for name in VECTOR_LAYERS
            },
            "worldcover_release": WORLDCOVER_RELEASE,
            "vegetation_algorithm": VEGETATION_ALGORITHM_ID,
            "generation_limits": self.limits.__dict__,
            "vector_clip_method": "wgs84-tile-intersection-v1",
            "raster_sampling_method": "source-pixel-center-half-open-v1",
        }
        return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()

    def _generate_layers(
        self,
        address: TileAddress,
        requested: tuple[str, ...],
        stage: Path,
        token: CancellationToken,
    ) -> list[LayerArtifact]:
        result: dict[str, LayerArtifact] = {}
        for layer_id in VECTOR_LAYERS:
            if layer_id in requested:
                if token.cancelled:
                    result[layer_id] = self._empty_layer(
                        layer_id, LayerOutcome.CANCELLED, requested=True
                    )
                else:
                    result[layer_id] = self._generate_vector_layer(
                        layer_id, address, stage, token
                    )
            else:
                result[layer_id] = self._empty_layer(
                    layer_id, LayerOutcome.NOT_GENERATED, requested=False
                )
        wants_landcover = "landcover" in requested
        wants_vegetation = "vegetation" in requested
        if not (wants_landcover or wants_vegetation):
            result["landcover"] = self._empty_layer(
                "landcover", LayerOutcome.NOT_GENERATED, requested=False
            )
            result["vegetation"] = self._empty_layer(
                "vegetation", LayerOutcome.NOT_GENERATED, requested=False
            )
        elif token.cancelled:
            result["landcover"] = self._empty_layer(
                "landcover",
                LayerOutcome.CANCELLED
                if wants_landcover
                else LayerOutcome.NOT_GENERATED,
                requested=wants_landcover,
            )
            result["vegetation"] = self._empty_layer(
                "vegetation",
                LayerOutcome.CANCELLED
                if wants_vegetation
                else LayerOutcome.NOT_GENERATED,
                requested=wants_vegetation,
            )
        else:
            try:
                window = self.worldcover.read_tile(
                    address,
                    token,
                    max_pixels=self.limits.max_raster_pixels,
                )
                if wants_landcover:
                    result["landcover"] = self._write_landcover(window, address, stage)
                else:
                    result["landcover"] = self._empty_layer(
                        "landcover", LayerOutcome.NOT_GENERATED, requested=False
                    )
                if wants_vegetation:
                    result["vegetation"] = self._write_vegetation(
                        window, address, stage
                    )
                else:
                    result["vegetation"] = self._empty_layer(
                        "vegetation", LayerOutcome.NOT_GENERATED, requested=False
                    )
            except CancelledError:
                result["landcover"] = self._empty_layer(
                    "landcover",
                    LayerOutcome.CANCELLED
                    if wants_landcover
                    else LayerOutcome.NOT_GENERATED,
                    requested=wants_landcover,
                )
                result["vegetation"] = self._empty_layer(
                    "vegetation",
                    LayerOutcome.CANCELLED
                    if wants_vegetation
                    else LayerOutcome.NOT_GENERATED,
                    requested=wants_vegetation,
                )
            except SourceError as error:
                outcome = (
                    LayerOutcome.NO_SOURCE_COVERAGE
                    if error.code == "worldcover_tile_missing"
                    else LayerOutcome.FAILED
                )
                lineage = (self._worldcover_lineage(address=address),)
                result["landcover"] = (
                    self._empty_layer(
                        "landcover",
                        outcome,
                        requested=True,
                        error_code=error.code,
                        source_lineage=lineage,
                    )
                    if wants_landcover
                    else self._empty_layer(
                        "landcover", LayerOutcome.NOT_GENERATED, requested=False
                    )
                )
                result["vegetation"] = (
                    self._empty_layer(
                        "vegetation",
                        outcome,
                        requested=True,
                        error_code=error.code,
                        source_lineage=lineage,
                    )
                    if wants_vegetation
                    else self._empty_layer(
                        "vegetation", LayerOutcome.NOT_GENERATED, requested=False
                    )
                )
        return [result[name] for name in DEFAULT_LAYERS]

    def _generate_vector_layer(
        self,
        layer_id: str,
        address: TileAddress,
        stage: Path,
        token: CancellationToken,
    ) -> LayerArtifact:
        spec = OVERTURE_LAYERS[layer_id]
        source_lineage: tuple[dict[str, object], ...] = (
            self._overture_lineage(layer_id),
        )
        audit: Any = None
        iterator: Any = None
        error_code: str | None = None
        limit_reason: str | None = None
        limit_observation: dict[str, int] | None = None
        cancelled = False
        count = 0
        raw_bytes = 0
        try:
            iterator, audit = self.overture.open_tile_query(layer_id, address, token)
            if audit.catalog_candidate_file_count == 0:
                source_lineage = (
                    self._overture_lineage(layer_id) | {"query_audit": audit.as_dict()},
                )
                return LayerArtifact(
                    layer_id=layer_id,
                    outcome=LayerOutcome.NO_SOURCE_COVERAGE,
                    feature_count=None,
                    coverage_claim="catalog_no_intersecting_data_files; no completeness claim",
                    coverage_confidence="pinned catalog reports no source file overlapping this tile",
                    query_bounds_wgs84=tuple(address.bounds.as_list()),
                    source_lineage=source_lineage,
                    attribution=spec.attribution,
                    transform=self._vector_transform(layer_id, address),
                    metadata={
                        "requested": True,
                        "query_audit": audit.as_dict(),
                        "emitted_feature_count": 0,
                    },
                    error_code="catalog_no_source_file",
                )
            with tempfile.TemporaryDirectory(
                prefix=f".{layer_id}-features-", dir=stage
            ) as temp_dir:
                database_path = Path(temp_dir) / "features.sqlite3"
                connection = sqlite3.connect(database_path)
                try:
                    connection.execute("PRAGMA journal_mode=OFF")
                    connection.execute("PRAGMA synchronous=OFF")
                    connection.execute(
                        "CREATE TABLE features (source_id TEXT NOT NULL, fragment_id TEXT NOT NULL, payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL, PRIMARY KEY (source_id, fragment_id))"
                    )
                    try:
                        for feature in iterator:
                            token.raise_if_cancelled()
                            serialized = canonical_json(feature)
                            encoded_size = len(serialized.encode("utf-8"))
                            if encoded_size > self.limits.max_feature_bytes:
                                limit_reason = "max_feature_bytes"
                                limit_observation = {
                                    "source_rows_scanned": audit.parquet_rows_scanned,
                                    "features_staged": count,
                                    "serialized_bytes_staged": raw_bytes,
                                    "next_feature_bytes": encoded_size,
                                    "configured_limit_bytes": self.limits.max_feature_bytes,
                                }
                                break
                            if count >= self.limits.max_features_per_layer:
                                limit_reason = "max_features_per_layer"
                                limit_observation = {
                                    "source_rows_scanned": audit.parquet_rows_scanned,
                                    "features_staged": count,
                                    "serialized_bytes_staged": raw_bytes,
                                    "next_feature_bytes": encoded_size,
                                    "configured_limit_features": self.limits.max_features_per_layer,
                                }
                                break
                            if (
                                raw_bytes + encoded_size
                                > self.limits.max_payload_bytes_per_layer
                            ):
                                limit_reason = "max_payload_bytes_per_layer"
                                limit_observation = {
                                    "source_rows_scanned": audit.parquet_rows_scanned,
                                    "features_staged": count,
                                    "serialized_bytes_staged": raw_bytes,
                                    "next_feature_bytes": encoded_size,
                                    "configured_limit_bytes": self.limits.max_payload_bytes_per_layer,
                                }
                                break
                            source_id = feature.get("id")
                            wf_metadata = feature.get("properties", {}).get(
                                "worldforge", {}
                            )
                            fragment_id = wf_metadata.get("fragment_id")
                            if not isinstance(source_id, str) or not isinstance(
                                fragment_id, str
                            ):
                                audit.invalid_features += 1
                                continue
                            cursor = connection.execute(
                                "INSERT OR IGNORE INTO features (source_id, fragment_id, payload, payload_bytes) VALUES (?, ?, ?, ?)",
                                (source_id, fragment_id, serialized, encoded_size),
                            )
                            if cursor.rowcount == 1:
                                count += 1
                                raw_bytes += encoded_size
                            else:
                                audit.duplicate_fragments += 1
                            if count % 1024 == 0:
                                connection.commit()
                    except CancelledError:
                        cancelled = True
                    except SourceError as error:
                        error_code = error.code
                    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
                        error_code = f"provider_{type(error).__name__.lower()}"
                    finally:
                        if iterator is not None and hasattr(iterator, "close"):
                            iterator.close()
                    connection.commit()
                    if audit is not None:
                        source_lineage = (
                            self._overture_lineage(layer_id)
                            | {"query_audit": audit.as_dict()},
                        )
                    if cancelled or token.cancelled:
                        return LayerArtifact(
                            layer_id=layer_id,
                            outcome=LayerOutcome.CANCELLED,
                            feature_count=None,
                            coverage_claim="cancelled before complete layer output",
                            coverage_confidence="incomplete query",
                            query_bounds_wgs84=tuple(address.bounds.as_list()),
                            source_lineage=source_lineage,
                            attribution=spec.attribution,
                            transform=self._vector_transform(layer_id, address),
                            metadata={
                                "requested": True,
                                "query_audit": audit.as_dict() if audit else {},
                                "staged_feature_count_discarded": count,
                                "staged_feature_bytes_discarded": raw_bytes,
                            },
                            error_code="cancelled",
                        )
                    if audit is not None and audit.invalid_features:
                        error_code = error_code or "invalid_source_features"
                    if error_code or limit_reason:
                        return LayerArtifact(
                            layer_id=layer_id,
                            outcome=LayerOutcome.FAILED,
                            feature_count=None,
                            coverage_claim="source query did not complete within declared bounds; no vector payload was published",
                            coverage_confidence="incomplete source query; no completeness claim",
                            query_bounds_wgs84=tuple(address.bounds.as_list()),
                            source_lineage=source_lineage,
                            attribution=spec.attribution,
                            transform=self._vector_transform(layer_id, address),
                            metadata={
                                "requested": True,
                                "query_audit": audit.as_dict() if audit else {},
                                "staged_feature_count_discarded": count,
                                "staged_feature_bytes_discarded": raw_bytes,
                                "max_features_per_layer": self.limits.max_features_per_layer,
                                "max_payload_bytes_per_layer": self.limits.max_payload_bytes_per_layer,
                                "max_feature_bytes": self.limits.max_feature_bytes,
                                "limit_reason": limit_reason,
                                "limit_observation": limit_observation,
                                "error_code": error_code,
                            },
                            error_code=error_code or limit_reason,
                        )
                    payloads = self._write_vector_shards(connection, stage, layer_id)
                    if count and not payloads:
                        return LayerArtifact(
                            layer_id=layer_id,
                            outcome=LayerOutcome.FAILED,
                            feature_count=None,
                            coverage_claim="shard writing failed; no vector payload was published",
                            coverage_confidence="incomplete output",
                            query_bounds_wgs84=tuple(address.bounds.as_list()),
                            source_lineage=source_lineage,
                            attribution=spec.attribution,
                            transform=self._vector_transform(layer_id, address),
                            metadata={
                                "requested": True,
                                "query_audit": audit.as_dict() if audit else {},
                                "staged_feature_count_discarded": count,
                            },
                            error_code="payload_generation_failed",
                        )
                    return LayerArtifact(
                        layer_id=layer_id,
                        outcome=LayerOutcome.READY,
                        payloads=tuple(payloads),
                        feature_count=count,
                        coverage_claim="successful query result only; no provider completeness claim",
                        coverage_confidence="catalog-selected GeoParquet row groups and exact bbox filtering; global completeness unknown",
                        query_bounds_wgs84=tuple(address.bounds.as_list()),
                        source_lineage=source_lineage,
                        attribution=spec.attribution,
                        transform=self._vector_transform(layer_id, address),
                        metadata={
                            "requested": True,
                            "query_audit": audit.as_dict() if audit else {},
                            "distinct_feature_count": count,
                            "serialized_feature_bytes": raw_bytes,
                            "max_features_per_layer": self.limits.max_features_per_layer,
                            "max_payload_bytes_per_layer": self.limits.max_payload_bytes_per_layer,
                            "limit_reason": limit_reason,
                            "error_code": error_code,
                        },
                        error_code=error_code or limit_reason,
                    )
                finally:
                    connection.close()
        except CancelledError:
            return self._empty_layer(
                layer_id, LayerOutcome.CANCELLED, requested=True, error_code="cancelled"
            )
        except SourceError as error:
            return self._empty_layer(
                layer_id,
                LayerOutcome.FAILED,
                requested=True,
                error_code=error.code,
                source_lineage=source_lineage,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, RuntimeError) as error:
            return self._empty_layer(
                layer_id,
                LayerOutcome.FAILED,
                requested=True,
                error_code=f"producer_{type(error).__name__.lower()}",
                source_lineage=source_lineage,
            )

    def _write_vector_shards(
        self, connection: sqlite3.Connection, stage: Path, layer_id: str
    ) -> list[dict[str, object]]:
        directory = stage / layer_id
        if directory.is_symlink():
            raise CacheError("refusing to write a vector layer through a symlink")
        directory.mkdir(exist_ok=True)
        cursor = connection.execute(
            "SELECT source_id, fragment_id, payload, payload_bytes FROM features ORDER BY source_id COLLATE BINARY, fragment_id COLLATE BINARY"
        )
        first_row = cursor.fetchone()
        if first_row is None:
            return []
        descriptors: list[dict[str, object]] = []
        index = 0
        count = 0
        current_bytes = len(b'{"type":"FeatureCollection","features":[]}')
        stream: Any = None
        digest: Any = None
        size = 0
        path: Path | None = None

        def open_shard() -> None:
            nonlocal stream, digest, size, count, path, current_bytes
            path = directory / f"shard-{index:05d}.geojson"
            temporary = path.with_suffix(".geojson.part")
            stream = temporary.open("xb")
            digest = hashlib.sha256()
            size = 0
            count = 0
            current_bytes = len(b'{"type":"FeatureCollection","features":[')
            _write_hashed(stream, digest, b'{"type":"FeatureCollection","features":[')
            size = stream.tell()

        def close_shard() -> None:
            nonlocal stream, size, index, path, count
            if stream is None or path is None:
                return
            _write_hashed(stream, digest, b"]}")
            size = stream.tell()
            stream.flush()
            os.fsync(stream.fileno())
            temporary = path.with_suffix(".geojson.part")
            stream.close()
            os.replace(temporary, path)
            descriptors.append(
                {
                    "path": path.relative_to(stage).as_posix(),
                    "media_type": "application/geo+json",
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                    "feature_count": count,
                    "serialization": "RFC 7946 FeatureCollection; UTF-8 compact JSON",
                }
            )
            index += 1
            stream = None
            path = None
            count = 0

        try:
            open_shard()
            for _, _, payload, _ in chain((first_row,), cursor):
                encoded = payload.encode("utf-8")
                separator_size = 1 if count else 0
                if count and (
                    count >= self.limits.max_shard_features
                    or current_bytes + len(encoded) + separator_size + 2
                    > self.limits.max_shard_bytes
                ):
                    close_shard()
                    open_shard()
                    separator_size = 0
                if (
                    len(encoded) + len(b'{"type":"FeatureCollection","features":[]}')
                    > self.limits.max_shard_bytes
                ):
                    raise CacheError(
                        "one feature cannot fit within the configured shard byte limit"
                    )
                if separator_size:
                    _write_hashed(stream, digest, b",")
                    current_bytes += 1
                _write_hashed(stream, digest, encoded)
                current_bytes += len(encoded)
                count += 1
            close_shard()
        except Exception:
            if stream is not None:
                stream.close()
                temporary = (
                    path.with_suffix(".geojson.part") if path is not None else None
                )
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            if directory.exists() and not directory.is_symlink():
                shutil.rmtree(directory)
            raise
        return descriptors

    def _write_landcover(
        self, window: WorldCoverWindow, address: TileAddress, stage: Path
    ) -> LayerArtifact:
        relative = Path("landcover") / "classes.u8"
        descriptor = self._write_payload(
            stage,
            relative.as_posix(),
            window.data,
            "application/octet-stream",
            "uint8 categorical class samples, row-major",
        )
        return LayerArtifact(
            layer_id="landcover",
            outcome=LayerOutcome.READY,
            payloads=(descriptor,),
            pixel_count=window.pixel_count,
            coverage_claim="requested WorldCover window read; nodata pixels are preserved",
            coverage_confidence="source COG grid and window metadata validated; classification completeness remains source-defined",
            query_bounds_wgs84=tuple(address.bounds.as_list()),
            source_lineage=(self._worldcover_lineage(window),),
            attribution=(
                WORLDCOVER_ATTRIBUTION,
                "ESA WorldCover 10 m 2021 v200; CC BY 4.0",
            ),
            transform={
                "crs": "EPSG:4326",
                "axis_order": ["longitude", "latitude"],
                "tile_bounds_wgs84": address.bounds.as_list(),
                "raster_affine_lon_lat": list(window.affine),
                "origin_reference": "pixel_corner",
                "pixel_registration": window.pixel_registration,
                "source_area_or_point_tag": "Area",
                "row_column_order": "row-major; rows north-to-south, columns west-to-east",
                "pixel_center_formula": WORLDCOVER_PIXEL_CENTER_FORMULA,
                "source_pixel_spacing_degrees_lon_lat": list(
                    window.source_pixel_spacing_degrees
                ),
                "width": window.width,
                "height": window.height,
                "nodata": window.nodata,
                "resampling": "none; source samples copied without reprojection",
                "pixel_ownership": "source pixel centers in half-open WFG-1 tile bounds",
            },
            metadata=window.as_metadata()
            | {"requested": True, "payload_path": relative.as_posix()},
        )

    def _write_vegetation(
        self, window: WorldCoverWindow, address: TileAddress, stage: Path
    ) -> LayerArtifact:
        parameters = build_placement_parameters(window.data)
        relative = Path("vegetation") / "placement_support.u8"
        descriptor = self._write_payload(
            stage,
            relative.as_posix(),
            parameters.payload,
            "application/octet-stream",
            "uint8 class-preserving procedural placement-support mask, row-major; no plant instances",
        )
        metadata = (
            window.as_metadata()
            | parameters.as_metadata()
            | {"requested": True, "payload_path": relative.as_posix()}
        )
        return LayerArtifact(
            layer_id="vegetation",
            outcome=LayerOutcome.READY,
            payloads=(descriptor,),
            pixel_count=window.pixel_count,
            coverage_claim="procedural placement-support classes derived from a source classification; no individual plant positions",
            coverage_confidence="source land-cover labels preserved; procedural support rules are deterministic but not surveyed",
            query_bounds_wgs84=tuple(address.bounds.as_list()),
            source_lineage=(self._worldcover_lineage(window),),
            attribution=(
                WORLDCOVER_ATTRIBUTION,
                "Derived procedural placement-support mask; no claim of observed individual vegetation",
            ),
            transform={
                "crs": "EPSG:4326",
                "axis_order": ["longitude", "latitude"],
                "tile_bounds_wgs84": address.bounds.as_list(),
                "raster_affine_lon_lat": list(window.affine),
                "origin_reference": "pixel_corner",
                "pixel_registration": window.pixel_registration,
                "source_area_or_point_tag": "Area",
                "row_column_order": "row-major; rows north-to-south, columns west-to-east",
                "pixel_center_formula": WORLDCOVER_PIXEL_CENTER_FORMULA,
                "source_pixel_spacing_degrees_lon_lat": list(
                    window.source_pixel_spacing_degrees
                ),
                "width": window.width,
                "height": window.height,
                "nodata": 0,
                "resampling": "none; placement support is generated one-to-one from class samples",
            },
            metadata=metadata,
        )

    def _write_payload(
        self,
        stage: Path,
        relative_path: str,
        data: bytes,
        media_type: str,
        representation: str,
    ) -> dict[str, object]:
        if len(data) > self.limits.max_payload_bytes_per_layer:
            raise CacheError("payload exceeds max_payload_bytes_per_layer")
        path = stage.joinpath(*Path(relative_path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.resolve(strict=False).is_relative_to(stage.resolve(strict=True)):
            raise CacheError("payload path escaped the staging directory")
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        digest, size = _sha256_file(path)
        return {
            "path": Path(relative_path).as_posix(),
            "media_type": media_type,
            "size_bytes": size,
            "sha256": digest,
            "representation": representation,
        }

    def _worldcover_lineage(
        self,
        window: WorldCoverWindow | None = None,
        *,
        address: TileAddress | None = None,
    ) -> dict[str, object]:
        lineage: dict[str, object] = {
            "provider": "ESA WorldCover",
            "dataset": WORLDCOVER_PRODUCT,
            "release": WORLDCOVER_RELEASE,
            "access_method": "public AWS S3 COG downloaded on demand via certifi-verified HTTPS to a bounded per-tile source cache; requested source window read locally",
            "terms_url": WORLDCOVER_TERMS_URL,
            "citation": WORLDCOVER_CITATION,
            "license": WORLDCOVER_LICENSE,
            "attribution": WORLDCOVER_ATTRIBUTION,
            "classification_semantics": "global land-cover classes, not individual vegetation inventory",
        }
        if window is not None:
            lineage.update(
                {
                    "source_tile": window.source_tile,
                    "source_url": window.source_url,
                    "etag": window.source_etag,
                    "source_object_size_bytes": window.source_size_bytes,
                    "source_sha256": window.source_sha256,
                    "source_download_bytes_this_request": window.source_download_bytes,
                    "source_cache_hit": window.source_cache_hit,
                    "source_window": list(window.source_window),
                    "source_pixel_spacing_degrees_lon_lat": list(
                        window.source_pixel_spacing_degrees
                    ),
                }
            )
        elif address is not None:
            source_tile, source_url = worldcover_source_for_tile(address)
            lineage.update({"source_tile": source_tile, "source_url": source_url})
        return lineage

    def _overture_lineage(self, layer_id: str) -> dict[str, object]:
        spec = OVERTURE_LAYERS[layer_id]
        return {
            "provider": "Overture Maps Foundation",
            "release": OVERTURE_RELEASE,
            "schema_version": "v2.0.0",
            "theme": spec.overture_theme,
            "feature_type": spec.overture_type,
            "access_method": "pinned STAC collection index + public HTTPS GeoParquet range reads; request AOI only",
            "release_notes_url": OVERTURE_RELEASE_NOTES,
            "attribution_terms_url": OVERTURE_ATTRIBUTION_URL,
            "theme_license": spec.license_expression,
            "theme_source_inventory": list(spec.theme_sources),
            "theme_source_inventory_scope": "official theme-level attribution metadata; not proof that every listed provider contributed to this tile",
            "source_records": "Overture per-feature sources[] preserved verbatim in GeoJSON properties and worldforge.source_records",
            "feature_source_licenses": "source item license is preserved when supplied; omitted upstream licenses are not inferred",
        }

    def _vector_transform(
        self, layer_id: str, address: TileAddress
    ) -> dict[str, object]:
        spec = OVERTURE_LAYERS[layer_id]
        transform: dict[str, object] = {
            "crs": "EPSG:4326",
            "horizontal_datum": "WGS 84 as declared by Overture GeoParquet; source coordinate precision/accuracy is not upgraded",
            "axis_order": ["longitude", "latitude"],
            "coordinate_units": "degrees",
            "tile_bounds_wgs84": address.bounds.as_list(),
            "geometry_semantics": spec.geometry_semantics,
            "operation": "clip source geometry to the WFG-1 WGS84 tile rectangle using Shapely/GEOS; no reprojection or simplification",
            "source_feature_id": "GeoJSON feature id preserves Overture id; worldforge.fragment_id is stable for the clipped tile fragment",
        }
        if layer_id == "roads":
            transform["boundary_ownership"] = (
                "line pieces on east/north internal edges are owned by the adjacent half-open tile; west/south-edge pieces stay here"
            )
            transform["terrain_drape"] = {
                "interface_id": "worldforge_terrain_sampler_v1",
                "status": "not_sampled_by_feature_generator",
                "request_geometry": "source road centerline coordinates [longitude, latitude]",
                "required_height_reference": {
                    "crs": "EPSG:4979",
                    "horizontal_datum": "WGS 84",
                    "height_type": "ellipsoidal",
                    "units": "metres",
                },
                "height_representation": "none in this bundle; orthometric values are not accepted by the ECEF drape interface without a source-backed conversion trace",
                "required_sample_trace": "source H/reference, geoid N when converted, resulting ellipsoidal h, terrain artifact ID/SHA-256, sampler method and warnings",
                "unavailable_or_cancelled": "preserve 2D road data and report unavailable/cancelled; never insert guessed height",
            }
        else:
            transform["polygon_edge_ownership"] = (
                "positive-area polygon fragments are clipped per tile; edge-only intersections are omitted"
            )
        return transform

    def _empty_layer(
        self,
        layer_id: str,
        outcome: LayerOutcome,
        *,
        requested: bool,
        error_code: str | None = None,
        source_lineage: tuple[dict[str, object], ...] = (),
    ) -> LayerArtifact:
        attribution = (
            OVERTURE_LAYERS[layer_id].attribution if layer_id in OVERTURE_LAYERS else ()
        )
        return LayerArtifact(
            layer_id=layer_id,
            outcome=outcome,
            feature_count=None,
            pixel_count=None,
            coverage_claim="no source data was emitted",
            coverage_confidence="not generated or source unavailable; no completeness claim",
            source_lineage=source_lineage,
            attribution=attribution,
            metadata={"requested": requested},
            error_code=error_code,
        )

    def _layer_transform_context(
        self,
        layer_id: str,
        address: TileAddress,
        artifact: LayerArtifact,
    ) -> dict[str, object]:
        transform = dict(artifact.transform)
        transform.setdefault("crs", "EPSG:4326")
        transform.setdefault("axis_order", ["longitude", "latitude"])
        transform.setdefault("tile_bounds_wgs84", address.bounds.as_list())
        transform.setdefault("address_scheme", ADDRESS_SCHEME)
        if layer_id in OVERTURE_LAYERS:
            transform.setdefault(
                "geometry_semantics", OVERTURE_LAYERS[layer_id].geometry_semantics
            )
            if not artifact.payloads:
                transform.setdefault(
                    "geometry_method_status",
                    "no geometry payload emitted; inspect layer outcome/error code",
                )
        if (
            layer_id in {"landcover", "vegetation"}
            and "raster_affine_lon_lat" not in transform
        ):
            transform.setdefault(
                "pixel_grid_status",
                "no raster samples emitted; affine is not available",
            )
        return transform

    def _build_manifest(
        self,
        address: TileAddress,
        key: str,
        requested: tuple[str, ...],
        artifacts: list[LayerArtifact],
        stage: Path | None,
    ) -> dict[str, Any]:
        artifact_by_id = {artifact.layer_id: artifact for artifact in artifacts}
        layers = []
        for layer_id in DEFAULT_LAYERS:
            artifact = artifact_by_id.get(layer_id)
            if artifact is None:
                artifact = self._empty_layer(
                    layer_id, LayerOutcome.NOT_GENERATED, requested=False
                )
            payloads = []
            for payload in artifact.payloads if stage is not None else ():
                item = dict(payload)
                if stage is not None and item.get("path"):
                    payload_path = stage.joinpath(*Path(str(item["path"])).parts)
                    digest, size = _sha256_file(payload_path)
                    item["sha256"] = digest
                    item["size_bytes"] = size
                payloads.append(item)
            layers.append(
                {
                    "layer_id": layer_id,
                    "outcome": artifact.outcome.value,
                    "requested": bool(
                        artifact.metadata.get("requested", layer_id in requested)
                    ),
                    "payloads": payloads,
                    "feature_count": artifact.feature_count,
                    "pixel_count": artifact.pixel_count,
                    "coverage_claim": artifact.coverage_claim,
                    "coverage_confidence": artifact.coverage_confidence,
                    "request_bounds_wgs84": address.bounds.as_list(),
                    "query_bounds_wgs84": list(artifact.query_bounds_wgs84)
                    if artifact.query_bounds_wgs84
                    else None,
                    "source_lineage": list(artifact.source_lineage),
                    "attribution": list(artifact.attribution),
                    "transform": self._layer_transform_context(
                        layer_id, address, artifact
                    ),
                    "metadata": artifact.metadata,
                    "error_code": artifact.error_code,
                }
            )
        requested_outcomes = [
            LayerOutcome(layer["outcome"]) for layer in layers if layer["requested"]
        ]
        if any(outcome is LayerOutcome.CANCELLED for outcome in requested_outcomes):
            bundle_outcome = LayerOutcome.CANCELLED
        elif requested_outcomes and all(
            outcome is LayerOutcome.READY for outcome in requested_outcomes
        ):
            bundle_outcome = LayerOutcome.READY
        elif not requested_outcomes or all(
            outcome in {LayerOutcome.FAILED, LayerOutcome.NO_SOURCE_COVERAGE}
            for outcome in requested_outcomes
        ):
            bundle_outcome = LayerOutcome.FAILED
        else:
            bundle_outcome = LayerOutcome.PARTIAL
        return {
            "schema_id": SCHEMA_ID,
            "schema_version": "1.0.0",
            "producer": {
                "id": PRODUCER_ID,
                "version": PRODUCER_VERSION,
                "source_revision": self.source_revision,
                "runtime_dependencies": ["Python", "PyArrow", "Shapely", "Rasterio"],
            },
            "bundle_id": key,
            "bundle_outcome": bundle_outcome.value,
            "created_at_utc": datetime.now(UTC).strftime(CREATED_TIME_FORMAT),
            "address": {
                "scheme": ADDRESS_SCHEME,
                "tile_id": address.tile_id,
                "level": address.level,
                "x": address.x,
                "y": address.y,
                "bounds_wgs84": address.bounds.as_list(),
                "coordinate_reference_system": "EPSG:4326",
                "horizontal_datum": "WGS 84",
                "axis_order": ["longitude", "latitude"],
                "edge_ownership": "half-open west/south inclusive and east/north exclusive; +90 latitude and normalized +180 longitude follow WFG-1 global limits",
                "sample_spacing": "separate from logical tile level; land-cover retains its native source grid spacing",
            },
            "request": {
                "requested_layers": list(requested),
                "max_limits": self.limits.__dict__,
                "request_extent": "one addressable WFG-1 tile; use the bounded bbox resolver for AOI requests",
            },
            "rights_and_provenance": {
                "overture": {
                    "release": OVERTURE_RELEASE,
                    "attribution_terms_url": OVERTURE_ATTRIBUTION_URL,
                    "transportation_theme_license": "ODbL-1.0",
                    "buildings_theme_license": "ODbL-1.0",
                    "base_water_theme_license": "ODbL-1.0",
                    "feature_source_records": "each Overture sources[] array and source feature id are retained",
                    "feature_database_separation": "keep Overture-derived feature databases separable; assess ODbL attribution/share-alike obligations for public use",
                    "source_item_license": "retain source item license values as received; omitted upstream terms are not inferred",
                },
                "worldcover": {
                    "release": WORLDCOVER_RELEASE,
                    "license": WORLDCOVER_LICENSE,
                    "terms_url": WORLDCOVER_TERMS_URL,
                    "citation": WORLDCOVER_CITATION,
                    "required_attribution": WORLDCOVER_ATTRIBUTION,
                    "classification_warning": "land-cover classification only; not individual trees, vegetation instances, or surveyed botanical inventory",
                },
                "runtime_data_rule": "public remote sources only; no credentials; cache contains requested bounded tile outputs only",
            },
            "coverage_gaps": [
                {
                    "layer_id": layer["layer_id"],
                    "outcome": layer["outcome"],
                    "error_code": layer["error_code"],
                }
                for layer in layers
                if layer["requested"] and layer["outcome"] != LayerOutcome.READY.value
            ],
            "layers": layers,
            "consumer_notes": {
                "geojson": "each file is a deterministic RFC 7946 FeatureCollection shard",
                "landcover_payload": "classes.u8 stores uint8 class IDs row-major; width/height/affine/nodata/class table are on the layer object",
                "vegetation_payload": "placement_support.u8 preserves WorldCover class IDs 10/20/30/40/60/90/95/100 for procedural class proxies and zeros NoData/built-up/snow/water/unknown; it contains no individual vegetation locations",
                "road_drape": "roads remain two-dimensional until a consumer supplies a real terrain sampler result with source and vertical reference",
            },
        }

    def _write_manifest(self, stage: Path, manifest: dict[str, Any]) -> None:
        path = stage / "manifest.json"
        encoded = (canonical_json(manifest) + "\n").encode("utf-8")
        if len(encoded) > 16 * 1024 * 1024:
            raise CacheError("manifest exceeds 16 MiB")
        with path.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def _bundle_size(self, stage: Path) -> int:
        total = 0
        for path in stage.rglob("*"):
            if path.is_symlink():
                raise CacheError("bundle staging cannot contain symbolic links")
            if path.is_file():
                total += path.stat().st_size
        return total


def _write_hashed(stream: Any, digest: Any, data: bytes) -> None:
    stream.write(data)
    digest.update(data)

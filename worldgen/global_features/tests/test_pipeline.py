# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Bundle cache, layer outcome, shard and no-truncation integration tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from worldgen.global_features.addressing import TileAddress
from worldgen.global_features.cache import CacheError, CacheStore
from worldgen.global_features.models import (
    CancellationToken,
    GenerationLimits,
    LayerOutcome,
    SourceError,
)
from worldgen.global_features.overture import QueryAudit
from worldgen.global_features.pipeline import GlobalFeatureGenerator
from worldgen.global_features.worldcover import WorldCoverWindow


def _feature(feature_id: str) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": {"type": "LineString", "coordinates": [[0.05, 0.05], [0.15, 0.15]]},
        "properties": {
            "class": "residential",
            "sources": [
                {
                    "dataset": "OpenStreetMap",
                    "license": "ODbL-1.0",
                    "record_id": feature_id,
                }
            ],
            "worldforge": {"fragment_id": f"wff:{feature_id:0<32}"},
        },
    }


class FakeOverture:
    def __init__(
        self,
        features: list[dict[str, object]] | None,
        *,
        candidate_files: int = 1,
        fail_after: int | None = None,
    ) -> None:
        self.features = features
        self.candidate_files = candidate_files
        self.fail_after = fail_after
        self.calls = 0

    def open_tile_query(self, layer_id, address, token):
        self.calls += 1
        audit = QueryAudit(
            catalog_url="https://stac.overturemaps.org/2026-10-21.0/collections.parquet",
            catalog_sha256="a" * 64,
            catalog_bytes=12,
            catalog_candidate_file_count=self.candidate_files,
            selected_file_count=self.candidate_files,
        )

        def stream() -> Iterator[dict[str, object]]:
            if self.features is None:
                return
            for index, feature in enumerate(self.features):
                if self.fail_after is not None and index >= self.fail_after:
                    raise SourceError("remote_network_error", "test source outage")
                yield feature

        return stream(), audit


class FakeWorldCover:
    def __init__(self) -> None:
        self.calls = 0
        self.data = bytes([10, 20, 30, 80, 0, 40])

    def read_tile(self, address, token, *, max_pixels):
        token.raise_if_cancelled()
        self.calls += 1
        self.asserted_pixel_limit = max_pixels
        return WorldCoverWindow(
            data=self.data,
            width=3,
            height=2,
            affine=(1 / 12_000, 0.0, 0.0, 0.0, -1 / 12_000, 0.25),
            pixel_registration="area",
            nodata=0,
            source_tile="N00E000",
            source_url="https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/example.tif",
            source_etag='"fixture"',
            source_size_bytes=100,
            source_sha256=hashlib.sha256(self.data).hexdigest(),
            source_download_bytes=0,
            source_cache_hit=True,
            source_window=(0, 0, 2, 3),
            source_pixel_spacing_degrees=(1 / 12_000, 1 / 12_000),
            class_counts={0: 1, 10: 1, 20: 1, 30: 1, 40: 1, 80: 1},
            valid_pixel_count=5,
        )


def _limits(**overrides) -> GenerationLimits:
    values = {
        "max_request_tiles": 16,
        "max_features_per_layer": 10,
        "max_payload_bytes_per_layer": 4096,
        "max_shard_features": 2,
        "max_shard_bytes": 1024,
        "max_feature_bytes": 512,
        "max_raster_pixels": 10,
        "max_bundle_bytes": 256 * 1024,
        "max_cache_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return GenerationLimits(**values)


class PipelineTests(unittest.TestCase):
    def test_bundle_is_atomic_deterministic_sharded_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as cache_root:
            provider = FakeOverture([_feature("z"), _feature("a"), _feature("b")])
            worldcover = FakeWorldCover()
            generator = GlobalFeatureGenerator(
                cache_root,
                limits=_limits(),
                overture=provider,
                worldcover=worldcover,
                source_revision="a" * 40,
            )
            result = generator.resolve_tile(
                TileAddress.parse("wfg1:0:720:360"),
                layers=("roads", "landcover", "vegetation"),
            )
            self.assertEqual(result.outcome, LayerOutcome.READY)
            self.assertIsNotNone(result.path)
            self.assertEqual(result.manifest["producer"]["source_revision"], "a" * 40)
            layers = {layer["layer_id"]: layer for layer in result.manifest["layers"]}
            self.assertEqual(layers["roads"]["outcome"], "ready")
            self.assertEqual(layers["roads"]["feature_count"], 3)
            self.assertEqual(len(layers["roads"]["payloads"]), 2)
            self.assertEqual(
                layers["landcover"]["payloads"][0]["path"], "landcover/classes.u8"
            )
            for layer_id in ("landcover", "vegetation"):
                transform = layers[layer_id]["transform"]
                metadata = layers[layer_id]["metadata"]
                self.assertEqual(transform["origin_reference"], "pixel_corner")
                self.assertEqual(transform["pixel_registration"], "area")
                self.assertEqual(transform["source_area_or_point_tag"], "Area")
                self.assertEqual(
                    transform["row_column_order"],
                    "row-major; rows north-to-south, columns west-to-east",
                )
                self.assertEqual(
                    transform["pixel_center_formula"],
                    metadata["pixel_center_formula"],
                )
                self.assertEqual(metadata["origin_reference"], "pixel_corner")
                self.assertEqual(metadata["pixel_registration"], "area")
            self.assertEqual(
                layers["vegetation"]["payloads"][0]["path"],
                "vegetation/placement_support.u8",
            )
            self.assertEqual(layers["buildings"]["outcome"], "not_generated")
            road_files = sorted((result.path / "roads").glob("*.geojson"))
            emitted_ids = []
            for path in road_files:
                payload = json.loads(path.read_text(encoding="utf-8"))
                emitted_ids.extend(feature["id"] for feature in payload["features"])
            self.assertEqual(emitted_ids, ["a", "b", "z"])
            verify_manifest = generator.cache.verify(
                result.path, expected_key=result.bundle_id
            )
            self.assertEqual(verify_manifest["bundle_id"], result.bundle_id)
            second = generator.resolve_tile(
                TileAddress.parse("wfg1:0:720:360"),
                layers=("roads", "landcover", "vegetation"),
            )
            self.assertTrue(second.cache_hit)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(worldcover.calls, 1)

    def test_producer_source_revision_participates_in_cache_identity(self) -> None:
        address = TileAddress.parse("wfg1:0:720:360")
        with tempfile.TemporaryDirectory() as cache_root:
            first = GlobalFeatureGenerator(
                Path(cache_root) / "first",
                limits=_limits(),
                overture=FakeOverture([]),
                worldcover=FakeWorldCover(),
                source_revision="a" * 40,
            )
            second = GlobalFeatureGenerator(
                Path(cache_root) / "second",
                limits=_limits(),
                overture=FakeOverture([]),
                worldcover=FakeWorldCover(),
                source_revision="b" * 40,
            )
            requested = ("roads",)
            self.assertNotEqual(
                first._cache_key(address, requested),
                second._cache_key(address, requested),
            )

    def test_ready_empty_is_not_confused_with_no_catalog_coverage(self) -> None:
        address = TileAddress.parse("wfg1:0:720:360")
        with tempfile.TemporaryDirectory() as cache_root:
            empty = GlobalFeatureGenerator(
                Path(cache_root) / "empty",
                limits=_limits(),
                overture=FakeOverture([]),
                worldcover=FakeWorldCover(),
            ).resolve_tile(address, layers=("roads",))
            empty_roads = empty.manifest["layers"][0]
            self.assertEqual(empty_roads["outcome"], "ready")
            self.assertEqual(empty_roads["feature_count"], 0)
            self.assertEqual(
                empty_roads["coverage_claim"],
                "successful query result only; no provider completeness claim",
            )
            self.assertEqual(empty_roads["payloads"], [])
            no_source = GlobalFeatureGenerator(
                Path(cache_root) / "no-source",
                limits=_limits(),
                overture=FakeOverture(None, candidate_files=0),
                worldcover=FakeWorldCover(),
            ).resolve_tile(address, layers=("roads",))
            no_source_roads = no_source.manifest["layers"][0]
            self.assertEqual(no_source_roads["outcome"], "no_source_coverage")
            self.assertIsNone(no_source_roads["feature_count"])
            self.assertEqual(
                no_source_roads["metadata"]["query_audit"][
                    "catalog_candidate_file_count"
                ],
                0,
            )

    def test_bound_failure_publishes_no_truncated_shard_set_and_can_retry_higher(
        self,
    ) -> None:
        address = TileAddress.parse("wfg1:0:720:360")
        with tempfile.TemporaryDirectory() as cache_root:
            features = [_feature("a"), _feature("b"), _feature("c")]
            bounded = GlobalFeatureGenerator(
                Path(cache_root) / "bounded",
                limits=_limits(max_features_per_layer=2),
                overture=FakeOverture(features),
                worldcover=FakeWorldCover(),
            ).resolve_tile(address, layers=("roads",))
            layer = bounded.manifest["layers"][0]
            self.assertEqual(layer["outcome"], "failed")
            self.assertEqual(layer["payloads"], [])
            self.assertIsNone(layer["feature_count"])
            self.assertEqual(layer["metadata"]["staged_feature_count_discarded"], 2)
            self.assertEqual(layer["error_code"], "max_features_per_layer")
            self.assertFalse((bounded.path / "roads").exists())
            higher = GlobalFeatureGenerator(
                Path(cache_root) / "higher",
                limits=_limits(max_features_per_layer=4),
                overture=FakeOverture(features),
                worldcover=FakeWorldCover(),
            ).resolve_tile(address, layers=("roads",))
            self.assertEqual(higher.manifest["layers"][0]["outcome"], "ready")
            self.assertEqual(higher.manifest["layers"][0]["feature_count"], 3)

    def test_cancellation_does_not_publish_a_bundle(self) -> None:
        address = TileAddress.parse("wfg1:0:720:360")
        token = CancellationToken()

        class CancellingProvider(FakeOverture):
            def open_tile_query(self, layer_id, address, cancellation):
                audit = QueryAudit("https://example.invalid/index", "a" * 64, 12, 1, 1)

                def stream():
                    yield _feature("first")
                    token.cancel("cancelled")
                    yield _feature("second")

                return stream(), audit

        with tempfile.TemporaryDirectory() as cache_root:
            result = GlobalFeatureGenerator(
                cache_root,
                limits=_limits(),
                overture=CancellingProvider([]),
                worldcover=FakeWorldCover(),
            ).resolve_tile(address, layers=("roads",), cancellation=token)
            self.assertEqual(result.outcome, LayerOutcome.CANCELLED)
            self.assertIsNone(result.path)
            self.assertEqual(result.manifest["layers"][0]["outcome"], "cancelled")
            store = CacheStore(cache_root, max_cache_bytes=_limits().max_cache_bytes)
            self.assertIsNone(store.existing(result.bundle_id))

    def test_corrupt_payload_is_rejected_on_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as cache_root:
            generator = GlobalFeatureGenerator(
                cache_root,
                limits=_limits(),
                overture=FakeOverture([_feature("a")]),
                worldcover=FakeWorldCover(),
            )
            result = generator.resolve_tile(
                TileAddress.parse("wfg1:0:720:360"), layers=("roads",)
            )
            payload = result.path / result.manifest["layers"][0]["payloads"][0]["path"]
            payload.write_bytes(payload.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(CacheError, "checksum"):
                generator.resolve_tile(
                    TileAddress.parse("wfg1:0:720:360"), layers=("roads",)
                )


if __name__ == "__main__":
    unittest.main()

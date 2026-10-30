# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Road drape contract tests; heights are only accepted from injected samples."""

from __future__ import annotations

import unittest

from worldgen.global_features.models import CancellationToken
from worldgen.global_features.terrain import (
    HeightReference,
    TerrainSampleOutcome,
    TerrainSamples,
    TerrainSampleTrace,
    drape_road_centerline,
)


class FakeSampler:
    def __init__(self, samples: TerrainSamples) -> None:
        self.samples = samples
        self.received: list[tuple[float, float]] | None = None

    def sample_heights(self, longitude_latitude, cancellation):
        self.received = list(longitude_latitude)
        return self.samples


class TerrainDrapeTests(unittest.TestCase):
    def test_ready_samples_preserve_lon_lat_order_and_require_vertical_reference(
        self,
    ) -> None:
        sampler = FakeSampler(
            TerrainSamples(
                outcome=TerrainSampleOutcome.READY,
                heights_m=(14.5, 15.25),
                vertical_reference=HeightReference.WGS84_ELLIPSOIDAL,
                sample_method="bilinear source-grid sample",
                source_lineage=({"provider": "test terrain fixture"},),
                sample_trace=(
                    TerrainSampleTrace(
                        10.0,
                        "EGM2008 orthometric metres",
                        4.5,
                        14.5,
                        "terrain-tile-a",
                        "a" * 64,
                        "bilinear source-grid sample",
                        "h = H + N",
                        ("GEBCO source is approximate mean sea level",),
                    ),
                    TerrainSampleTrace(
                        11.0,
                        "EPSG:4979 ellipsoidal metres",
                        None,
                        15.25,
                        "terrain-tile-a",
                        "a" * 64,
                        "bilinear source-grid sample",
                    ),
                ),
            )
        )
        result = drape_road_centerline(
            {"type": "LineString", "coordinates": [[151.1, -33.9], [151.2, -33.8]]},
            sampler,
            CancellationToken(),
        )
        self.assertEqual(sampler.received, [(151.1, -33.9), (151.2, -33.8)])
        self.assertEqual(
            result.geometry["coordinates"],
            [[151.1, -33.9, 14.5], [151.2, -33.8, 15.25]],
        )
        self.assertEqual(result.vertical_reference, HeightReference.WGS84_ELLIPSOIDAL)
        self.assertEqual(result.requested_sample_count, 2)
        self.assertEqual(result.returned_height_count, 2)
        self.assertEqual(result.gap_count, 0)
        self.assertEqual(result.source_lineage[0]["provider"], "test terrain fixture")
        self.assertEqual(result.sample_trace[0].source_height_m, 10.0)
        self.assertEqual(result.sample_trace[0].geoid_undulation_m, 4.5)
        self.assertEqual(result.sample_trace[0].ellipsoidal_height_m, 14.5)
        self.assertIn(
            "GEBCO source is approximate mean sea level",
            result.sample_trace[0].warnings,
        )

    def test_unavailable_or_cancelled_sampler_returns_no_draped_geometry(self) -> None:
        geometry = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.1, 0.1]]}
        unavailable = drape_road_centerline(
            geometry,
            FakeSampler(
                TerrainSamples(
                    TerrainSampleOutcome.UNAVAILABLE, error_code="tile_missing"
                )
            ),
            CancellationToken(),
        )
        self.assertEqual(unavailable.outcome, TerrainSampleOutcome.UNAVAILABLE)
        self.assertIsNone(unavailable.geometry)
        self.assertEqual(unavailable.requested_sample_count, 2)
        self.assertEqual(unavailable.gap_count, 2)
        token = CancellationToken()
        token.cancel()
        cancelled = drape_road_centerline(
            geometry,
            FakeSampler(TerrainSamples(TerrainSampleOutcome.UNAVAILABLE)),
            token,
        )
        self.assertEqual(cancelled.outcome, TerrainSampleOutcome.CANCELLED)
        self.assertIsNone(cancelled.geometry)
        self.assertEqual(cancelled.requested_sample_count, 0)

    def test_sampler_cannot_return_partial_or_nonfinite_heights(self) -> None:
        geometry = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.1, 0.1]]}
        partial = drape_road_centerline(
            geometry,
            FakeSampler(
                TerrainSamples(
                    TerrainSampleOutcome.READY,
                    (12.0,),
                    HeightReference.WGS84_ELLIPSOIDAL,
                    sample_method="test sampler",
                    source_lineage=({"artifact_id": "test-terrain"},),
                    sample_trace=(
                        TerrainSampleTrace(
                            12.0,
                            "EPSG:4979 ellipsoidal metres",
                            None,
                            12.0,
                            "terrain",
                            "a" * 64,
                            "sample",
                        ),
                    ),
                )
            ),
            CancellationToken(),
        )
        self.assertEqual(partial.outcome, TerrainSampleOutcome.PARTIAL)
        self.assertEqual(partial.requested_sample_count, 2)
        self.assertEqual(partial.returned_height_count, 1)
        self.assertEqual(partial.gap_count, 1)
        self.assertEqual(partial.error_code, "terrain_sample_count_mismatch")
        self.assertIsNone(partial.geometry)
        nonfinite = drape_road_centerline(
            geometry,
            FakeSampler(
                TerrainSamples(
                    TerrainSampleOutcome.READY,
                    (12.0, float("nan")),
                    HeightReference.WGS84_ELLIPSOIDAL,
                    sample_method="test sampler",
                    source_lineage=({"artifact_id": "test-terrain"},),
                    sample_trace=(
                        TerrainSampleTrace(
                            12.0,
                            "EPSG:4979 ellipsoidal metres",
                            None,
                            12.0,
                            "terrain",
                            "a" * 64,
                            "sample",
                        ),
                        TerrainSampleTrace(
                            float("nan"),
                            "EPSG:4979 ellipsoidal metres",
                            None,
                            float("nan"),
                            "terrain",
                            "a" * 64,
                            "sample",
                        ),
                    ),
                )
            ),
            CancellationToken(),
        )
        self.assertEqual(nonfinite.outcome, TerrainSampleOutcome.FAILED)
        self.assertEqual(nonfinite.gap_count, 1)
        self.assertIsNone(nonfinite.geometry)

    def test_ready_sampler_rejects_orthometric_height_without_conversion(self) -> None:
        geometry = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.1, 0.1]]}
        rejected = drape_road_centerline(
            geometry,
            FakeSampler(TerrainSamples(TerrainSampleOutcome.READY, (12.0, 13.0), None)),
            CancellationToken(),
        )
        self.assertEqual(rejected.outcome, TerrainSampleOutcome.FAILED)
        self.assertEqual(rejected.error_code, "vertical_reference_invalid")
        self.assertEqual(rejected.requested_sample_count, 2)
        self.assertEqual(rejected.returned_height_count, 2)
        self.assertEqual(rejected.gap_count, 2)
        self.assertIsNone(rejected.geometry)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Line edge ownership and stable source-fragment identity tests."""

from __future__ import annotations

import unittest

from worldgen.global_features.addressing import TileAddress
from worldgen.global_features.geometry import (
    apply_half_open_line_ownership,
    feature_to_geojson,
)


class GeometryTests(unittest.TestCase):
    def test_internal_east_and_north_edge_segments_have_one_owner(self) -> None:
        bounds = TileAddress.parse("wfg1:0:720:360").bounds
        east_edge = {"type": "LineString", "coordinates": [[0.25, 0.05], [0.25, 0.20]]}
        north_edge = {"type": "LineString", "coordinates": [[0.05, 0.25], [0.20, 0.25]]}
        west_edge = {"type": "LineString", "coordinates": [[0.0, 0.05], [0.0, 0.20]]}
        crossing = {"type": "LineString", "coordinates": [[0.20, 0.10], [0.25, 0.10]]}
        self.assertIsNone(apply_half_open_line_ownership(east_edge, bounds))
        self.assertIsNone(apply_half_open_line_ownership(north_edge, bounds))
        # The exact same line is retained on each adjacent tile's included west/south edge.
        self.assertEqual(
            apply_half_open_line_ownership(
                east_edge, TileAddress.parse("wfg1:0:721:360").bounds
            ),
            east_edge,
        )
        self.assertEqual(
            apply_half_open_line_ownership(
                north_edge, TileAddress.parse("wfg1:0:720:361").bounds
            ),
            north_edge,
        )
        self.assertEqual(apply_half_open_line_ownership(west_edge, bounds), west_edge)
        self.assertEqual(apply_half_open_line_ownership(crossing, bounds), crossing)

    def test_fragment_metadata_preserves_overture_id_and_source_license(self) -> None:
        address = TileAddress.parse("wfg1:0:720:360")
        geometry = {"type": "LineString", "coordinates": [[0.1, 0.1], [0.2, 0.2]]}
        sources = [
            {
                "property": "/geometry",
                "dataset": "OpenStreetMap",
                "license": "ODbL-1.0",
                "record_id": "w123@4",
                "provider": "osm",
            }
        ]
        first = feature_to_geojson(
            "stable-source-id",
            geometry,
            {"class": "secondary", "sources": sources},
            address=address,
            theme="transportation",
            feature_type="segment",
            version={"effective": "2026-10-21"},
            sources=sources,
            license_expression="ODbL-1.0",
        )
        second = feature_to_geojson(
            "stable-source-id",
            geometry,
            {"class": "secondary", "sources": sources},
            address=address,
            theme="transportation",
            feature_type="segment",
            version={"effective": "2026-10-21"},
            sources=sources,
            license_expression="ODbL-1.0",
        )
        self.assertEqual(first["id"], "stable-source-id")
        self.assertEqual(first["properties"]["sources"], sources)
        self.assertEqual(first["properties"]["worldforge"]["source_records"], sources)
        self.assertEqual(
            first["properties"]["worldforge"]["source_theme_license"], "ODbL-1.0"
        )
        self.assertEqual(
            first["properties"]["worldforge"]["fragment_id"],
            second["properties"]["worldforge"]["fragment_id"],
        )

    def test_nonfinite_source_property_is_rejected(self) -> None:
        address = TileAddress.parse("wfg1:0:720:360")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            feature_to_geojson(
                "id",
                {"type": "LineString", "coordinates": [[0.1, 0.1], [0.2, 0.2]]},
                {"confidence": float("nan")},
                address=address,
                theme="transportation",
                feature_type="segment",
                version=None,
                sources=[],
                license_expression="ODbL-1.0",
            )


if __name__ == "__main__":
    unittest.main()

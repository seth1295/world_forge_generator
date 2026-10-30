# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""WFG-1 address and request-boundary tests."""

from __future__ import annotations

import unittest

from worldgen.global_features.addressing import (
    TileAddress,
    point_to_tile,
    tiles_for_bbox,
)


class AddressingTests(unittest.TestCase):
    def test_global_edges_and_exact_half_open_ownership(self) -> None:
        self.assertEqual(point_to_tile(-180.0, -90.0).tile_id, "wfg1:0:720:0")
        self.assertEqual(point_to_tile(180.0, -90.0).tile_id, "wfg1:0:720:0")
        self.assertEqual(point_to_tile(0.0, 0.0).tile_id, "wfg1:0:720:360")
        self.assertEqual(point_to_tile(0.25, 0.25).tile_id, "wfg1:0:721:361")
        self.assertEqual(point_to_tile(180.0, 90.0).tile_id, "wfg1:0:720:719")

    def test_poles_have_one_canonical_point_owner_for_every_longitude(self) -> None:
        for latitude, expected_y in ((90.0, 719), (-90.0, 0)):
            expected = f"wfg1:0:720:{expected_y}"
            # Exercise every L0 longitude interval at its center and both edges.
            # The pole special case must own these identically, including ±180.
            longitudes = {-180.0 + index * 0.25 for index in range(1441)} | {
                -180.0 + (index + 0.5) * 0.25 for index in range(1440)
            }
            for longitude in longitudes:
                self.assertEqual(point_to_tile(longitude, latitude).tile_id, expected)
                bbox_owners = tiles_for_bbox(longitude, latitude, longitude, latitude)
                self.assertEqual([item.tile_id for item in bbox_owners], [expected])
            for west, east in ((-180.0, 180.0), (75.0, -75.0)):
                self.assertEqual(
                    [
                        item.tile_id
                        for item in tiles_for_bbox(west, latitude, east, latitude)
                    ],
                    [expected],
                )

    def test_non_degenerate_polar_area_keeps_all_longitude_cells(self) -> None:
        polar_cap = tiles_for_bbox(-180.0, 89.75, 180.0, 90.0, max_tiles=1440)
        self.assertEqual(len(polar_cap), 1440)
        self.assertEqual({item.y for item in polar_cap}, {719})
        self.assertEqual({item.x for item in polar_cap}, set(range(1440)))

    def test_canonical_id_roundtrip_and_rejects_noncanonical_forms(self) -> None:
        address = TileAddress(level=16, x=47_185_920, y=23_592_960)
        self.assertEqual(TileAddress.parse(address.tile_id), address)
        for invalid in (
            "WFG1:0:0:0",
            "wfg1:00:0:0",
            "wfg1:17:0:0",
            "wfg1:0:1440:0",
            "wfg1:0:0:-1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                TileAddress.parse(invalid)

    def test_bbox_ends_on_internal_edge_without_claiming_adjacent_cell(self) -> None:
        addresses = tiles_for_bbox(0.0, 0.0, 0.25, 0.25)
        self.assertEqual([item.tile_id for item in addresses], ["wfg1:0:720:360"])
        point_owner = tiles_for_bbox(0.25, 0.25, 0.25, 0.25)
        self.assertEqual([item.tile_id for item in point_owner], ["wfg1:0:721:361"])

    def test_antimeridian_bbox_splits_without_duplicates(self) -> None:
        addresses = tiles_for_bbox(179.9, 70.1, -179.9, 70.4)
        self.assertEqual(
            [item.tile_id for item in addresses],
            [
                "wfg1:0:0:640",
                "wfg1:0:1439:640",
                "wfg1:0:0:641",
                "wfg1:0:1439:641",
            ],
        )
        self.assertEqual(len(addresses), len(set(addresses)))

    def test_tile_cap_blocks_accidental_world_sized_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_tiles"):
            tiles_for_bbox(-180.0, -90.0, 180.0, 90.0, max_tiles=64)


if __name__ == "__main__":
    unittest.main()

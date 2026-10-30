# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Procedural vegetation-support mask determinism and exclusion tests."""

from __future__ import annotations

import hashlib
import unittest

from worldgen.global_features.vegetation import (
    ALGORITHM_ID,
    build_placement_parameters,
    stable_tile_seed,
)


class VegetationSupportTests(unittest.TestCase):
    def test_class_mask_preserves_tree_shrub_grass_codes_and_excludes_water(
        self,
    ) -> None:
        classes = bytes([10, 20, 30, 80, 0, 40, 50, 60, 70, 90, 95, 100, 255])
        result = build_placement_parameters(classes)
        self.assertEqual(
            result.payload, bytes([10, 20, 30, 0, 0, 40, 0, 60, 0, 90, 95, 100, 0])
        )
        self.assertEqual(result.supported_pixels, 8)
        self.assertEqual(result.water_pixels_excluded, 1)
        self.assertEqual(result.built_up_pixels_excluded, 1)
        self.assertEqual(result.snow_pixels_excluded, 1)
        self.assertEqual(result.invalid_pixels_excluded, 2)

    def test_mask_is_reproducible_and_does_not_mutate_source_bytes(self) -> None:
        classes = bytes([10, 20, 30, 80, 0])
        first = build_placement_parameters(classes)
        second = build_placement_parameters(classes)
        self.assertEqual(first.payload, second.payload)
        self.assertEqual(classes, bytes([10, 20, 30, 80, 0]))

    def test_seed_is_stable_for_canonical_tile_and_algorithm(self) -> None:
        tile_id = "wfg1:0:0:0"
        expected = int.from_bytes(
            hashlib.sha256(f"{ALGORITHM_ID}\0{tile_id}".encode()).digest()[:8],
            "big",
        )
        self.assertEqual(stable_tile_seed(tile_id), expected)
        self.assertEqual(stable_tile_seed(tile_id), stable_tile_seed(tile_id))
        self.assertNotEqual(stable_tile_seed(tile_id), stable_tile_seed("wfg1:0:1:0"))


if __name__ == "__main__":
    unittest.main()

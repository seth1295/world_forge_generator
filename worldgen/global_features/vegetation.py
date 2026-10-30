# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Deterministic vegetation placement strata derived from WorldCover classes.

The output is a set of procedural placement parameters, not individual plants
and not a surveyed tree inventory. A Godot-side sampler may use the stable seed
and class code to place explicitly synthetic vegetation later.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

ALGORITHM_ID = "worldforge_procedural_vegetation_support_v2"
CLASS_TO_PLACEMENT_CLASS: dict[int, int] = {
    10: 10,
    20: 20,
    30: 30,
    40: 40,
    60: 60,
    90: 90,
    95: 95,
    100: 100,
}
PLACEMENT_CLASS_TABLE: dict[str, str] = {
    "0": "no_vegetation_placement_support",
    "10": "tree_cover_class; procedural placement support only",
    "20": "shrubland_class; procedural placement support only",
    "30": "grassland_class; procedural placement support only",
    "40": "cropland_class; procedural agricultural vegetation support only",
    "60": "bare_sparse_vegetation_class; procedural sparse vegetation support only",
    "90": "herbaceous_wetland_class; procedural wetland vegetation support only",
    "95": "mangroves_class; procedural mangrove vegetation support only",
    "100": "moss_lichen_class; procedural moss_lichen support only",
}


@dataclass(frozen=True)
class VegetationParameters:
    payload: bytes
    valid_input_pixels: int
    supported_pixels: int
    water_pixels_excluded: int
    built_up_pixels_excluded: int
    snow_pixels_excluded: int
    invalid_pixels_excluded: int

    def as_metadata(self) -> dict[str, object]:
        return {
            "algorithm_id": ALGORITHM_ID,
            "class_to_placement_value": {
                str(key): value for key, value in CLASS_TO_PLACEMENT_CLASS.items()
            },
            "placement_support_class_values": PLACEMENT_CLASS_TABLE,
            "seed_derivation": "Godot derives a stable seed from canonical tile_id plus this algorithm_id; no plant coordinates are stored",
            "pixel_count": len(self.payload),
            "valid_input_pixels": self.valid_input_pixels,
            "supported_pixels": self.supported_pixels,
            "water_pixels_excluded": self.water_pixels_excluded,
            "built_up_pixels_excluded": self.built_up_pixels_excluded,
            "snow_pixels_excluded": self.snow_pixels_excluded,
            "invalid_pixels_excluded": self.invalid_pixels_excluded,
            "excluded_class_values": {
                "0": "NoData",
                "50": "Built-up",
                "70": "Snow and ice",
                "80": "Permanent water bodies",
            },
            "representation": "uint8 class-preserving support mask: retain source class codes 10/20/30/40/60/90/95/100; zero all other values",
            "provenance": "procedural derived parameters; WorldCover labels are observed classifications, not plant instances",
        }


def stable_tile_seed(tile_id: str) -> int:
    digest = hashlib.sha256(f"{ALGORITHM_ID}\0{tile_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def build_placement_parameters(class_bytes: bytes) -> VegetationParameters:
    """Map categorical class bytes to procedural support without plant instances."""

    if not isinstance(class_bytes, bytes):
        raise TypeError("class_bytes must be bytes")
    valid_classes = {0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100}
    values = bytearray(len(class_bytes))
    supported = 0
    water = 0
    built_up = 0
    snow = 0
    invalid = 0
    valid = 0
    for index, class_code in enumerate(class_bytes):
        if class_code not in valid_classes:
            invalid += 1
            continue
        if class_code == 0:
            invalid += 1
            continue
        valid += 1
        if class_code == 80:
            water += 1
        elif class_code == 50:
            built_up += 1
        elif class_code == 70:
            snow += 1
        placement_class = CLASS_TO_PLACEMENT_CLASS.get(class_code, 0)
        values[index] = placement_class
        supported += int(placement_class != 0)
    return VegetationParameters(
        payload=bytes(values),
        valid_input_pixels=valid,
        supported_pixels=supported,
        water_pixels_excluded=water,
        built_up_pixels_excluded=built_up,
        snow_pixels_excluded=snow,
        invalid_pixels_excluded=invalid,
    )

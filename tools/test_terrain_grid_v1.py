"""Focused functional and cross-language contract tests for terrain_grid_v1."""

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest

import numpy as np
import rasterio
from jsonschema import Draft202012Validator, FormatChecker
from rasterio.crs import CRS
from rasterio.windows import Window
from rasterio.warp import transform as warp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
import terrain_grid_v1 as producer  # noqa: E402


def _vincenty_inverse(
    lon1_deg: float, lat1_deg: float, lon2_deg: float, lat2_deg: float
) -> tuple[float, float]:
    """Independent GRS80 ellipsoidal inverse geodesic: return distance and forward azimuth."""
    semi_major = 6_378_137.0
    flattening = 1.0 / 298.257222101
    semi_minor = semi_major * (1.0 - flattening)
    phi1, phi2 = math.radians(lat1_deg), math.radians(lat2_deg)
    longitude_delta = math.radians(lon2_deg - lon1_deg)
    reduced1 = math.atan((1.0 - flattening) * math.tan(phi1))
    reduced2 = math.atan((1.0 - flattening) * math.tan(phi2))
    sin_u1, cos_u1 = math.sin(reduced1), math.cos(reduced1)
    sin_u2, cos_u2 = math.sin(reduced2), math.cos(reduced2)
    lam = longitude_delta
    for _ in range(100):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.sqrt(
            (cos_u2 * sin_lam) ** 2
            + (cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam) ** 2
        )
        if sin_sigma == 0.0:
            return 0.0, 0.0
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
        cos_two_sigma_m = (
            0.0
            if cos_sq_alpha < 1e-30
            else cos_sigma - 2.0 * sin_u1 * sin_u2 / cos_sq_alpha
        )
        coefficient = flattening / 16.0 * cos_sq_alpha * (
            4.0 + flattening * (4.0 - 3.0 * cos_sq_alpha)
        )
        previous = lam
        lam = longitude_delta + (1.0 - coefficient) * flattening * sin_alpha * (
            sigma
            + coefficient
            * sin_sigma
            * (
                cos_two_sigma_m
                + coefficient
                * cos_sigma
                * (-1.0 + 2.0 * cos_two_sigma_m**2)
            )
        )
        if abs(lam - previous) <= 1e-13:
            break
    else:
        raise AssertionError("Vincenty inverse did not converge for the short pilot distance")
    u_sq = cos_sq_alpha * (semi_major**2 - semi_minor**2) / semi_minor**2
    coefficient_a = 1.0 + u_sq / 16384.0 * (
        4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq))
    )
    coefficient_b = u_sq / 1024.0 * (
        256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq))
    )
    delta_sigma = coefficient_b * sin_sigma * (
        cos_two_sigma_m
        + coefficient_b
        / 4.0
        * (
            cos_sigma * (-1.0 + 2.0 * cos_two_sigma_m**2)
            - coefficient_b
            / 6.0
            * cos_two_sigma_m
            * (-3.0 + 4.0 * sin_sigma**2)
            * (-3.0 + 4.0 * cos_two_sigma_m**2)
        )
    )
    distance = semi_minor * coefficient_a * (sigma - delta_sigma)
    azimuth = math.atan2(
        cos_u2 * math.sin(lam),
        cos_u1 * sin_u2 - sin_u1 * cos_u2 * math.cos(lam),
    )
    return distance, azimuth


def _vincenty_direct(
    lon1_deg: float, lat1_deg: float, distance_m: float, azimuth_rad: float
) -> tuple[float, float]:
    """Independent GRS80 ellipsoidal direct geodesic for the inverse round-trip check."""
    semi_major = 6_378_137.0
    flattening = 1.0 / 298.257222101
    semi_minor = semi_major * (1.0 - flattening)
    alpha1 = azimuth_rad
    phi1 = math.radians(lat1_deg)
    reduced1 = math.atan((1.0 - flattening) * math.tan(phi1))
    sin_u1, cos_u1 = math.sin(reduced1), math.cos(reduced1)
    sin_alpha1, cos_alpha1 = math.sin(alpha1), math.cos(alpha1)
    sigma1 = math.atan2(math.tan(reduced1), cos_alpha1)
    sin_alpha = cos_u1 * sin_alpha1
    cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
    u_sq = cos_sq_alpha * (semi_major**2 - semi_minor**2) / semi_minor**2
    coefficient_a = 1.0 + u_sq / 16384.0 * (
        4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq))
    )
    coefficient_b = u_sq / 1024.0 * (
        256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq))
    )
    sigma = distance_m / (semi_minor * coefficient_a)
    for _ in range(100):
        cos_two_sigma_m = math.cos(2.0 * sigma1 + sigma)
        sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
        delta_sigma = coefficient_b * sin_sigma * (
            cos_two_sigma_m
            + coefficient_b
            / 4.0
            * (
                cos_sigma * (-1.0 + 2.0 * cos_two_sigma_m**2)
                - coefficient_b
                / 6.0
                * cos_two_sigma_m
                * (-3.0 + 4.0 * sin_sigma**2)
                * (-3.0 + 4.0 * cos_two_sigma_m**2)
            )
        )
        updated = distance_m / (semi_minor * coefficient_a) + delta_sigma
        if abs(updated - sigma) <= 1e-13:
            sigma = updated
            break
        sigma = updated
    else:
        raise AssertionError("Vincenty direct did not converge for the short pilot distance")
    sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
    cos_two_sigma_m = math.cos(2.0 * sigma1 + sigma)
    tmp = sin_u1 * sin_sigma - cos_u1 * cos_sigma * cos_alpha1
    phi2 = math.atan2(
        sin_u1 * cos_sigma + cos_u1 * sin_sigma * cos_alpha1,
        (1.0 - flattening)
        * math.sqrt(sin_alpha**2 + tmp**2),
    )
    lam = math.atan2(
        sin_sigma * sin_alpha1,
        cos_u1 * cos_sigma - sin_u1 * sin_sigma * cos_alpha1,
    )
    coefficient_c = flattening / 16.0 * cos_sq_alpha * (
        4.0 + flattening * (4.0 - 3.0 * cos_sq_alpha)
    )
    longitude_correction = lam - (1.0 - coefficient_c) * flattening * sin_alpha * (
        sigma
        + coefficient_c
        * sin_sigma
        * (
            cos_two_sigma_m
            + coefficient_c
            * cos_sigma
            * (-1.0 + 2.0 * cos_two_sigma_m**2)
        )
    )
    return lon1_deg + math.degrees(longitude_correction), math.degrees(phi2)


def _test_paths() -> tuple[Path, Path, Path | None, Path | None]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--example", type=Path, required=True)
    parser.add_argument("--bundles", type=Path)
    parser.add_argument("--source-dir", type=Path)
    args, _ = parser.parse_known_args()
    return args.schema, args.example, args.bundles, args.source_dir


SCHEMA_PATH, EXAMPLE_DIR, REAL_BUNDLE_ROOT, SOURCE_DIR = _test_paths()


class TerrainGridV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = producer.load_json_unique(SCHEMA_PATH)
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def test_synthetic_cross_language_hash_vector_and_payloads(self) -> None:
        example_manifest = producer.load_json_unique(EXAMPLE_DIR / "manifest.json")
        self.validator.validate(example_manifest)
        self.assertEqual(
            producer.frame_definition_sha256(example_manifest["frame"]),
            "5994afc6d0c0db512eebb63279e1cd0dc8a7aac299294be8c603766a0642340f",
        )
        self.assertEqual(
            producer.artifact_id(example_manifest),
            "0510e711d4207c51389940cca33a3a8acc9c89da36d6ec03986a29c2614eb7d8",
        )
        horizontal = (EXAMPLE_DIR / "horizontal_xy.f64le").read_bytes()
        heights = (EXAMPLE_DIR / "height_samples.f32le").read_bytes()
        self.assertEqual(
            hashlib.sha256(horizontal).hexdigest(),
            example_manifest["payloads"]["horizontal_xy"]["sha256"],
        )
        self.assertEqual(
            hashlib.sha256(heights).hexdigest(),
            example_manifest["payloads"]["height_samples"]["sha256"],
        )
        self.assertEqual(len(horizontal), 64)
        self.assertEqual(len(heights), 16)

    def test_rejects_duplicate_json_keys_and_invalid_height_samples(self) -> None:
        with self.assertRaises(producer.ProducerError):
            producer._reject_duplicate_pairs([("a", 1), ("a", 2)])
        for invalid in (
            np.asarray([producer.NODATA_VALUE], dtype=np.float32),
            np.asarray([np.nan], dtype=np.float32),
            np.asarray([np.inf], dtype=np.float32),
            np.asarray([12_001.0], dtype=np.float32),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(producer.ProducerError):
                    producer.validate_sample_array(invalid, producer.NODATA_VALUE, "fixture")
        with self.assertRaises(producer.ProducerError):
            producer.validate_relative_heights(
                np.asarray([0.0], dtype=np.float32), 12_001.0, "fixture"
            )
        with self.assertRaises(producer.ProducerError):
            producer.validate_relative_heights(
                np.asarray([24_001.0], dtype=np.float32), 0.0, "fixture"
            )

    def test_real_bundle_integrity_source_bits_and_independent_geodesy(self) -> None:
        if REAL_BUNDLE_ROOT is None or SOURCE_DIR is None:
            self.skipTest("pass --bundles and --source-dir to verify acquired source outputs")
        projection_versions: list[str] = []
        for region_key, spec in producer.REGIONS.items():
            with self.subTest(region=region_key):
                radial_errors: list[float] = []
                transverse_errors: list[float] = []
                roundtrip_errors: list[float] = []
                bundle = REAL_BUNDLE_ROOT / region_key
                manifest = producer.validate_bundle(bundle, SCHEMA_PATH)
                self.validator.validate(manifest)
                self.assertEqual(manifest["source"]["product_id"], spec.product_id)
                self.assertEqual(
                    manifest["source"]["acquisition_time_basis"],
                    "filesystem_creation_time",
                )
                expected_acquired = {
                    "baltimore": "2026-10-29T13:34:02Z",
                    "golden": "2026-10-29T13:41:19Z",
                }[region_key]
                self.assertEqual(manifest["source"]["acquired_at_utc"], expected_acquired)
                self.assertNotIn("C:\\", json.dumps(manifest))
                self.assertTrue(manifest["source"]["source_uri"].startswith("https://"))
                self.assertEqual(manifest["frame"]["vertical"]["local_up_expression"], "H-H0")
                pipeline = manifest["frame"]["horizontal"]["operation_proj_pipeline"]
                self.assertIn("+proj=pipeline", pipeline)
                self.assertNotIn("WGS84", pipeline.upper())
                projection_versions.append(manifest["generation"]["tool_versions"]["proj"])

                payload_xy = (bundle / "horizontal_xy.f64le").read_bytes()
                payload_heights = (bundle / "height_samples.f32le").read_bytes()
                grid = manifest["grid"]
                rows, columns = grid["rows"], grid["columns"]
                xy = np.frombuffer(payload_xy, dtype="<f8").reshape(rows, columns, 2)
                heights = np.frombuffer(payload_heights, dtype="<f4").reshape(rows, columns)
                self.assertEqual(len(payload_xy), rows * columns * 16)
                self.assertEqual(len(payload_heights), rows * columns * 4)

                row_offset, column_offset, _, _ = spec.source_window
                # The manifest and ledger hashes bind these source samples. Re-read
                # the window to prove the bundle retains their Float32 bit patterns.
                raster_path = SOURCE_DIR / spec.raster_name
                with rasterio.open(raster_path) as dataset:
                    source_samples = dataset.read(
                        1,
                        window=Window(column_offset, row_offset, columns, rows),
                        masked=False,
                    )
                    source_heights_bytes = np.ascontiguousarray(
                        source_samples, dtype="<f4"
                    ).tobytes()
                    self.assertEqual(payload_heights, source_heights_bytes)
                    row_indices = np.arange(row_offset, row_offset + rows, dtype=np.float64)
                    column_indices = np.arange(
                        column_offset, column_offset + columns, dtype=np.float64
                    )
                    lon_grid, lat_grid = np.meshgrid(
                        dataset.transform.c + (column_indices + 0.5) * dataset.transform.a,
                        dataset.transform.f + (row_indices + 0.5) * dataset.transform.e,
                    )

                anchor_r = spec.anchor_row - row_offset
                anchor_c = spec.anchor_column - column_offset
                anchor_xy = xy[anchor_r, anchor_c]
                self.assertLessEqual(abs(float(anchor_xy[0])), 1e-7)
                self.assertLessEqual(abs(float(anchor_xy[1])), 1e-7)
                self.assertAlmostEqual(
                    float(heights[anchor_r, anchor_c]),
                    manifest["frame"]["origin"]["height_navd88_m"],
                    places=6,
                )

                # Compare sampled PROJ outputs to independent GRS80 Vincenty
                # forward vectors.
                selected_rows = sorted({0, rows // 2, rows - 1})
                selected_columns = sorted({0, columns // 2, columns - 1})
                inverse_lon: list[float] = []
                inverse_lat: list[float] = []
                for r in selected_rows:
                    for c in selected_columns:
                        lon = float(lon_grid[r, c])
                        lat = float(lat_grid[r, c])
                        distance, azimuth = _vincenty_inverse(
                            spec.anchor_lon, spec.anchor_lat, lon, lat
                        )
                        expected_east = distance * math.sin(azimuth)
                        expected_north = distance * math.cos(azimuth)
                        radial_errors.append(
                            math.hypot(
                                float(xy[r, c, 0]) - expected_east,
                                float(xy[r, c, 1]) - expected_north,
                            )
                        )
                        projected_distance = math.hypot(float(xy[r, c, 0]), float(xy[r, c, 1]))
                        radial_errors.append(abs(projected_distance - distance))
                        inverse_lon.append(float(xy[r, c, 0]))
                        inverse_lat.append(float(xy[r, c, 1]))

                # Compare the library inverse with independent direct geodesics
                # for the same 3x3 points.
                inverse_crs = CRS.from_wkt(manifest["frame"]["horizontal"]["projection_wkt2"])
                expected_crs = CRS.from_epsg(4269)
                back_lon, back_lat = warp_transform(
                    inverse_crs, expected_crs, inverse_lon, inverse_lat
                )
                coordinate_index = 0
                for r in selected_rows:
                    for c in selected_columns:
                        east, north = xy[r, c]
                        distance = math.hypot(float(east), float(north))
                        bearing = math.atan2(float(east), float(north))
                        direct_lon, direct_lat = _vincenty_direct(
                            spec.anchor_lon, spec.anchor_lat, distance, bearing
                        )
                        roundtrip_errors.append(
                            _vincenty_inverse(
                                direct_lon,
                                direct_lat,
                                float(back_lon[coordinate_index]),
                                float(back_lat[coordinate_index]),
                            )[0]
                        )
                        coordinate_index += 1

                pairs = (
                    (0, 0, rows // 2, columns - 1),
                    (0, columns // 2, rows - 1, columns - 1),
                    (0, 0, rows - 1, columns // 2),
                )
                for r1, c1, r2, c2 in pairs:
                    e1, n1 = xy[r1, c1]
                    e2, n2 = xy[r2, c2]
                    projected_pair = math.hypot(float(e2 - e1), float(n2 - n1))
                    geodesic_pair = _vincenty_inverse(
                        float(lon_grid[r1, c1]), float(lat_grid[r1, c1]),
                        float(lon_grid[r2, c2]), float(lat_grid[r2, c2]),
                    )[0]
                    transverse_errors.append(abs(projected_pair - geodesic_pair))

                self.assertLess(max(radial_errors), 1e-4)
                self.assertLess(max(roundtrip_errors), 1e-5)
                self.assertTrue(all(math.isfinite(error) for error in transverse_errors))
                print(
                    f"{region_key} independent GRS80 samples (not bounds): "
                    f"radial/vector max={max(radial_errors):.12g} m, "
                    f"cross-pair max={max(transverse_errors):.12g} m, "
                    f"inverse/direct round-trip max={max(roundtrip_errors):.12g} m"
                )
        self.assertEqual(len(set(projection_versions)), 1)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)

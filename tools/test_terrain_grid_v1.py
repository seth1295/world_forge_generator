"""Focused functional and cross-language contract tests for terrain_grid_v1."""

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors

from __future__ import annotations

import argparse
import contextlib
from dataclasses import replace
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Iterator
import uuid
import unittest
from unittest import mock

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
FIXTURE_RECEIPT = "2026-10-29T00:00:00Z"
FIXTURE_COMMIT = "a" * 40


@contextlib.contextmanager
def _temporary_directory(prefix: str = "terrain-grid-v1-") -> Iterator[str]:
    """Use the OS temp root or a caller-provided task-local root for sandboxed runs."""
    configured_root = os.environ.get("WF_TEST_TEMP_ROOT")
    if configured_root is None:
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary_directory:
            yield temporary_directory
        return

    root = Path(configured_root)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("WF_TEST_TEMP_ROOT must name an existing regular directory")
    temporary_directory = root / f"{prefix}{uuid.uuid4().hex}"
    temporary_directory.mkdir()
    try:
        yield str(temporary_directory)
    finally:
        shutil.rmtree(temporary_directory)


def _small_fixture(
    key: str, anchor_lon: float, anchor_lat: float, height_base: float
) -> tuple[producer.RegionSpec, producer.VerifiedSource, np.ndarray, float]:
    spec = producer.RegionSpec(
        key=key,
        region_id=f"wf-vs01:{key}",
        frame_id=f"wf-vs01:{key}-grs80-aeqd",
        raster_name=f"{key}.tif",
        xml_name=f"{key}.xml",
        record_name=f"{key}-record.json",
        product_id=f"WorldForge local fixture {key}",
        product_date="2026-10-29",
        source_window=(0, 0, 2, 2),
        anchor_row=0,
        anchor_column=0,
        anchor_lon=anchor_lon,
        anchor_lat=anchor_lat,
        ledger_h0=height_base,
        ledger_crop_bounds=(
            anchor_lon - 0.01,
            anchor_lat - 0.01,
            anchor_lon + 0.01,
            anchor_lat + 0.01,
        ),
    )
    samples = np.asarray(
        [[height_base, height_base + 0.25], [height_base + 0.5, height_base + 0.75]],
        dtype=np.float32,
    )
    base_uri = f"https://example.invalid/worldforge/{key}"
    record = {
        "title": spec.product_id,
        "publicationDate": spec.product_date,
        "sizeInBytes": 128,
        "format": "GeoTIFF",
        "downloadURL": f"{base_uri}.tif",
        "vendorMetaUrl": f"{base_uri}.xml",
        "metaUrl": f"{base_uri}.json",
    }
    source = producer.VerifiedSource(
        raster_path=Path(spec.raster_name),
        xml_path=Path(spec.xml_name),
        record_path=Path(spec.record_name),
        raster_sha256=hashlib.sha256(f"{key}-raster".encode()).hexdigest(),
        xml_sha256=hashlib.sha256(f"{key}-xml".encode()).hexdigest(),
        record_sha256=hashlib.sha256(f"{key}-record".encode()).hexdigest(),
        raster_bytes=128,
        xml_bytes=256,
        record_bytes=192,
        record=record,
        acquired_at_utc=FIXTURE_RECEIPT,
        affine=(0.01, 0.0, anchor_lon - 0.005, 0.0, -0.01, anchor_lat + 0.005),
        source_width=2,
        source_height=2,
        nodata=producer.NODATA_VALUE,
        raster_min=float(samples.min()),
        raster_max=float(samples.max()),
    )
    return spec, source, samples, height_base


def _small_source_ledger(root: Path, spec: producer.RegionSpec) -> tuple[Path, Path]:
    source_dir = root / "source-fixture"
    source_dir.mkdir()
    raster = b"small raster fixture"
    xml = (
        f"<metadata><title>{spec.product_id}</title>"
        "<horizontal>North American Datum of 1983</horizontal>"
        "<vertical>North American Vertical Datum of 1988</vertical></metadata>"
    ).encode()
    urls = (
        "https://example.invalid/tile.tif",
        "https://example.invalid/tile.xml",
        "https://example.invalid/tile.json",
    )
    record = {
        "title": spec.product_id,
        "publicationDate": spec.product_date,
        "sizeInBytes": len(raster),
        "format": "GeoTIFF",
        "downloadURL": urls[0],
        "vendorMetaUrl": urls[1],
        "metaUrl": urls[2],
    }
    record_bytes = json.dumps(record, sort_keys=True).encode()
    contents = (raster, xml, record_bytes)
    paths = (spec.raster_name, spec.xml_name, spec.record_name)
    for name, content in zip(paths, contents, strict=True):
        (source_dir / name).write_bytes(content)
    columns = [
        f"[{label}]({url}); {len(content)} bytes; SHA-256 `{hashlib.sha256(content).hexdigest()}`"
        for label, url, content in zip(
            ("GeoTIFF", "XML", "TNM JSON"), urls, contents, strict=True
        )
    ]
    ledger = root / "vertical-slice-fixture.md"
    ledger.write_text(
        f"| {spec.product_id} | "
        + " | ".join(columns)
        + f" | local snapshot file-creation receipt `{FIXTURE_RECEIPT}` |\n",
        encoding="utf-8",
    )
    return source_dir, ledger


def _small_adjacent_fixture() -> tuple[
    producer.RegionSpec, producer.VerifiedSource, np.ndarray, float
]:
    base, source, _base_samples, _base_h0 = _small_fixture(
        "golden", -105.2, 39.7, 10.0
    )
    samples = np.asarray(
        [
            [10.0, 10.25, 10.5, 10.75],
            [11.0, 11.25, 11.5, 11.75],
        ],
        dtype=np.float32,
    )
    affine_x, _rot_x, x_origin, _rot_y, pixel_height, y_origin = source.affine
    anchor_lon = x_origin + (2 + 0.5) * affine_x
    anchor_lat = y_origin + 0.5 * pixel_height
    ledger_crop_bounds = (
        x_origin,
        y_origin + 2 * pixel_height,
        x_origin + 4 * affine_x,
        y_origin,
    )
    h0 = float(samples[0, 2])
    parent = replace(
        base,
        region_id="wf-vs01:golden-fixture",
        frame_id="wf-vs01:golden-fixture-grs80-aeqd",
        source_window=(0, 0, 2, 4),
        anchor_column=2,
        anchor_lon=anchor_lon,
        anchor_lat=anchor_lat,
        ledger_h0=h0,
        ledger_crop_bounds=ledger_crop_bounds,
    )
    source = replace(
        source,
        source_width=4,
        source_height=2,
        raster_min=float(samples.min()),
        raster_max=float(samples.max()),
    )
    return parent, source, samples, h0


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

    def test_source_sizes_are_bounded_before_hashing_and_receipt_comes_from_ledger(self) -> None:
        spec = producer.REGIONS["baltimore"]
        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir, ledger_path = _small_source_ledger(root, spec)
            _record, expected, acquired_at_utc = producer._verify_record_and_ledger(
                source_dir, ledger_path, spec
            )
            self.assertEqual(acquired_at_utc, FIXTURE_RECEIPT)
            self.assertEqual(
                [size for size, _digest in expected],
                [
                    (source_dir / spec.raster_name).stat().st_size,
                    (source_dir / spec.xml_name).stat().st_size,
                    (source_dir / spec.record_name).stat().st_size,
                ],
            )

            (source_dir / spec.raster_name).write_bytes(b"oversized raster fixture")
            with mock.patch.object(producer, "sha256_file") as hash_file:
                with self.assertRaisesRegex(
                    producer.ProducerError, "size mismatch before hashing"
                ):
                    producer._verify_record_and_ledger(source_dir, ledger_path, spec)
                hash_file.assert_not_called()

            ledger_text = ledger_path.read_text(encoding="utf-8")
            first_size = expected[0][0]
            ledger_path.write_text(
                ledger_text.replace(
                    f"{first_size} bytes; SHA-256",
                    f"{producer.MAX_SOURCE_RASTER_BYTES + 1} bytes; SHA-256",
                    1,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(producer, "sha256_file") as hash_file:
                with self.assertRaisesRegex(producer.ProducerError, "v1 byte limit"):
                    producer._verify_record_and_ledger(source_dir, ledger_path, spec)
                hash_file.assert_not_called()

    def test_producer_commit_rejects_tracked_and_untracked_status_output(self) -> None:
        repo = Path("fixture-repo")
        for status in (" M tracked.txt\n", "?? untracked.txt\n"):
            with self.subTest(status=status.strip()):
                dirty_status = subprocess.CompletedProcess(
                    ["git", "status"], 0, stdout=status, stderr=""
                )
                with mock.patch.object(
                    producer.subprocess, "run", return_value=dirty_status
                ) as run_git:
                    with self.assertRaisesRegex(producer.ProducerError, "worktree is dirty"):
                        producer._producer_commit(repo)
                command = run_git.call_args.args[0]
                self.assertEqual(command[:2], ["git", "status"])
                self.assertIn("--untracked-files=all", command)
                self.assertEqual(run_git.call_args.kwargs["cwd"], repo)

        clean_status = subprocess.CompletedProcess(
            ["git", "status"], 0, stdout="", stderr=""
        )
        head_revision = subprocess.CompletedProcess(
            ["git", "rev-parse", "HEAD"], 0, stdout=f"{FIXTURE_COMMIT}\n", stderr=""
        )
        with mock.patch.object(
            producer.subprocess, "run", side_effect=(clean_status, head_revision)
        ) as run_git:
            self.assertEqual(producer._producer_commit(repo), FIXTURE_COMMIT)
        self.assertEqual(
            run_git.call_args_list[1].args[0], ["git", "rev-parse", "HEAD"]
        )

    def test_cli_builds_fixture_and_samples_match_declared_grs80_aeqd(self) -> None:
        spec, source, samples, _h0 = _small_fixture("fixture-one", -76.5, 39.2, 3.0)
        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "inputs"
            source_dir.mkdir()
            ledger = root / "ledger.md"
            ledger.write_text("local deterministic fixture\n", encoding="utf-8")
            output_root = root / "output"
            args = [
                "--source-dir", str(source_dir),
                "--ledger", str(ledger),
                "--schema", str(SCHEMA_PATH),
                "--output-root", str(output_root),
                "--region", spec.key,
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(producer.REGIONS, {spec.key: spec}, clear=True),
                mock.patch.object(producer, "_producer_commit", return_value=FIXTURE_COMMIT),
                mock.patch.object(
                    producer,
                    "_verify_raster_window",
                    return_value=(source, samples, spec.ledger_h0),
                ),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result_code = producer.main(args)
            self.assertEqual(result_code, 0, stderr.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertEqual(len(result), 1)
            bundle_dir = output_root / spec.key
            manifest = producer.validate_bundle(bundle_dir, SCHEMA_PATH)
            self.assertEqual(manifest["producer"]["commit"], FIXTURE_COMMIT)
            self.assertEqual(
                manifest["source"]["acquisition_time_basis"],
                "recorded_download_receipt",
            )
            self.assertEqual(manifest["source"]["acquired_at_utc"], FIXTURE_RECEIPT)
            self.assertEqual(manifest["generation"]["created_utc"], FIXTURE_RECEIPT)

            pipeline = manifest["frame"]["horizontal"]["operation_proj_pipeline"]
            self.assertEqual(
                pipeline,
                "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
                f"+step +proj=aeqd +lat_0={spec.anchor_lat!r} +lon_0={spec.anchor_lon!r} "
                "+ellps=GRS80 +x_0=0 +y_0=0",
            )
            xy_bytes = (bundle_dir / "horizontal_xy.f64le").read_bytes()
            xy = np.frombuffer(xy_bytes, dtype="<f8").reshape(2, 2, 2)
            pixel_width, _rot_x, x_origin, _rot_y, pixel_height, y_origin = source.affine
            for row in range(2):
                for column in range(2):
                    longitude = x_origin + (column + 0.5) * pixel_width
                    latitude = y_origin + (row + 0.5) * pixel_height
                    distance, azimuth = _vincenty_inverse(
                        spec.anchor_lon, spec.anchor_lat, longitude, latitude
                    )
                    self.assertAlmostEqual(
                        float(xy[row, column, 0]), distance * math.sin(azimuth), delta=1e-5
                    )
                    self.assertAlmostEqual(
                        float(xy[row, column, 1]), distance * math.cos(azimuth), delta=1e-5
                    )

            repeated_output_root = root / "output-repeat"
            with (
                mock.patch.object(
                    producer,
                    "_verify_raster_window",
                    return_value=(source, samples, spec.ledger_h0),
                ),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
            ):
                repeated_dir, _repeated_result, _repeated_bounds = producer.build_bundle(
                    spec,
                    source_dir,
                    ledger,
                    SCHEMA_PATH,
                    repeated_output_root,
                    FIXTURE_COMMIT,
                )
            for filename in (
                "manifest.json",
                "horizontal_xy.f64le",
                "height_samples.f32le",
            ):
                self.assertEqual(
                    (bundle_dir / filename).read_bytes(),
                    (repeated_dir / filename).read_bytes(),
                )

    def test_golden_adjacent_windows_share_only_the_declared_boundary_column(self) -> None:
        parent = producer.REGIONS["golden"]
        west, east = producer.golden_adjacent_tile_specs()
        producer.validate_adjacent_tile_specs(parent, west, east)
        self.assertEqual(west.source_window, (2544, 8214, 216, 217))
        self.assertEqual(east.source_window, (2544, 8430, 216, 216))
        self.assertEqual(west.frame_id, parent.frame_id)
        self.assertEqual(east.frame_id, parent.frame_id)
        self.assertEqual(west.region_id, parent.region_id)

        west_row, west_column, rows, west_columns = west.source_window
        east_row, east_column, east_rows, east_columns = east.source_window
        gap = replace(
            east,
            source_window=(east_row, east_column + 1, east_rows, east_columns - 1),
        )
        overlap = replace(
            east,
            source_window=(
                east_row,
                west_column + west_columns - 2,
                east_rows,
                east_columns + 1,
            ),
        )
        outside_parent = replace(
            west,
            source_window=(west_row, west_column - 1, rows, west_columns),
        )
        mismatched_rows = replace(
            east,
            source_window=(east_row + 1, east_column, east_rows - 1, east_columns),
        )
        mismatched_frame = replace(east, frame_id="wf-vs01:other-frame")

        with self.assertRaisesRegex(producer.ProducerError, "exactly one boundary column"):
            producer.validate_adjacent_tile_specs(parent, west, gap)
        with self.assertRaisesRegex(producer.ProducerError, "exactly one boundary column"):
            producer.validate_adjacent_tile_specs(parent, west, overlap)
        with self.assertRaisesRegex(producer.ProducerError, "outside the parent source window"):
            producer.validate_adjacent_tile_specs(parent, outside_parent, east)
        with self.assertRaisesRegex(producer.ProducerError, "same complete parent row range"):
            producer.validate_adjacent_tile_specs(parent, west, mismatched_rows)
        with self.assertRaisesRegex(producer.ProducerError, "region and frame identity"):
            producer.validate_adjacent_tile_specs(parent, west, mismatched_frame)

    def test_golden_tiles_cli_checks_frame_seam_hashes_and_reproducibility(self) -> None:
        parent, source, samples, h0 = _small_adjacent_fixture()
        west_spec, east_spec = producer.golden_adjacent_tile_specs(parent)

        def verified_tile(_source_dir, _ledger, spec):
            row, column, rows, columns = spec.source_window
            return source, samples[row : row + rows, column : column + columns], h0

        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "inputs"
            source_dir.mkdir()
            ledger = root / "ledger.md"
            ledger.write_text("local deterministic fixture\n", encoding="utf-8")
            args = [
                "--source-dir", str(source_dir),
                "--ledger", str(ledger),
                "--schema", str(SCHEMA_PATH),
                "--output-root", str(root / "output"),
                "--region", "golden-tiles",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(producer.REGIONS, {"golden": parent}, clear=True),
                mock.patch.object(producer, "_producer_commit", return_value=FIXTURE_COMMIT),
                mock.patch.object(producer, "_verify_raster_window", side_effect=verified_tile),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(producer.main(args), 0, stderr.getvalue())
                results = json.loads(stdout.getvalue())
                self.assertEqual(len(results), 2)
                output_root = root / "output"
                west_bundle = output_root / "golden-west"
                east_bundle = output_root / "golden-east"
                west, east = producer.validate_adjacent_tile_bundles(
                    parent,
                    west_spec,
                    east_spec,
                    west_bundle,
                    east_bundle,
                    SCHEMA_PATH,
                )
                self.validator.validate(west)
                self.validator.validate(east)
                self.assertEqual(west["frame"], east["frame"])
                self.assertEqual(
                    west["frame_definition_sha256"], east["frame_definition_sha256"]
                )
                self.assertEqual(west["grid"]["region_id"], parent.region_id)
                self.assertEqual(
                    west["grid"]["source_window"],
                    {"row_offset": 0, "column_offset": 0, "rows": 2, "columns": 3},
                )
                self.assertEqual(
                    east["grid"]["source_window"],
                    {"row_offset": 0, "column_offset": 2, "rows": 2, "columns": 2},
                )
                for result, manifest, bundle in (
                    (results[0], west, west_bundle),
                    (results[1], east, east_bundle),
                ):
                    self.assertEqual(result["artifact_id"], manifest["artifact_id"])
                    self.assertEqual(
                        result["frame_definition_sha256"],
                        manifest["frame_definition_sha256"],
                    )
                    for payload_key, filename in (
                        ("horizontal_xy", "horizontal_xy.f64le"),
                        ("height_samples", "height_samples.f32le"),
                    ):
                        payload = (bundle / filename).read_bytes()
                        self.assertEqual(
                            hashlib.sha256(payload).hexdigest(),
                            manifest["payloads"][payload_key]["sha256"],
                        )

                west_height_bytes = (west_bundle / "height_samples.f32le").read_bytes()
                east_height_bytes = (east_bundle / "height_samples.f32le").read_bytes()
                self.assertEqual(
                    west_height_bytes,
                    np.ascontiguousarray(samples[:, :3], dtype="<f4").tobytes(),
                )
                self.assertEqual(
                    east_height_bytes,
                    np.ascontiguousarray(samples[:, 2:], dtype="<f4").tobytes(),
                )
                self.assertEqual(
                    (west_bundle / "horizontal_xy.f64le").read_bytes()[
                        2 * 16 : 3 * 16
                    ],
                    (east_bundle / "horizontal_xy.f64le").read_bytes()[:16],
                )
                self.assertEqual(west_height_bytes[2 * 4 : 3 * 4], east_height_bytes[:4])

                west_height_path = west_bundle / "height_samples.f32le"
                real_validate_bundle = producer.validate_bundle
                for mutation, expected_error in (
                    ("same-size", "payload SHA-256 mismatch during seam validation"),
                    ("oversize", "payload byte length changed during seam validation"),
                ):
                    with self.subTest(seam_payload_mutation=mutation):
                        mutation_done = False

                        def validate_then_mutate(bundle_dir, schema_path):
                            nonlocal mutation_done
                            manifest = real_validate_bundle(bundle_dir, schema_path)
                            if bundle_dir == west_bundle and not mutation_done:
                                mutation_done = True
                                if mutation == "same-size":
                                    changed = bytearray(west_height_bytes)
                                    changed[0] ^= 0x01
                                    west_height_path.write_bytes(changed)
                                else:
                                    west_height_path.write_bytes(west_height_bytes + b"\x00")
                            return manifest

                        try:
                            with mock.patch.object(
                                producer,
                                "validate_bundle",
                                side_effect=validate_then_mutate,
                            ):
                                with self.assertRaisesRegex(
                                    producer.ProducerError, expected_error
                                ):
                                    producer.validate_adjacent_tile_bundles(
                                        parent,
                                        west_spec,
                                        east_spec,
                                        west_bundle,
                                        east_bundle,
                                        SCHEMA_PATH,
                                    )
                        finally:
                            west_height_path.write_bytes(west_height_bytes)

                repeated_args = list(args)
                repeated_args[repeated_args.index(str(root / "output"))] = str(
                    root / "output-repeat"
                )
                repeated_stdout = io.StringIO()
                with contextlib.redirect_stdout(repeated_stdout):
                    self.assertEqual(producer.main(repeated_args), 0)
                repeated_results = json.loads(repeated_stdout.getvalue())
                for key in ("golden-west", "golden-east"):
                    original = output_root / key
                    repeated = root / "output-repeat" / key
                    for filename in (
                        "manifest.json",
                        "horizontal_xy.f64le",
                        "height_samples.f32le",
                    ):
                        self.assertEqual(
                            (original / filename).read_bytes(),
                            (repeated / filename).read_bytes(),
                        )
                self.assertEqual(
                    [entry["artifact_id"] for entry in results],
                    [entry["artifact_id"] for entry in repeated_results],
                )

    def test_golden_tiles_failure_rolls_back_only_new_output(self) -> None:
        parent, source, samples, h0 = _small_adjacent_fixture()

        def fail_east(_source_dir, _ledger, spec):
            if spec.key == "golden-east":
                raise producer.ProducerError("injected east-tile failure")
            row, column, rows, columns = spec.source_window
            return source, samples[row : row + rows, column : column + columns], h0

        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "inputs"
            source_dir.mkdir()
            ledger = root / "ledger.md"
            ledger.write_text("local deterministic fixture\n", encoding="utf-8")
            output_root = root / "partial-output"
            output_root.mkdir()
            user_file = output_root / "user-owned-note.txt"
            user_file.write_text("keep this file\n", encoding="utf-8")
            args = [
                "--source-dir", str(source_dir),
                "--ledger", str(ledger),
                "--schema", str(SCHEMA_PATH),
                "--output-root", str(output_root),
                "--region", "golden-tiles",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(producer.REGIONS, {"golden": parent}, clear=True),
                mock.patch.object(producer, "_producer_commit", return_value=FIXTURE_COMMIT),
                mock.patch.object(producer, "_verify_raster_window", side_effect=fail_east),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(producer.main(args), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("injected east-tile failure", stderr.getvalue())
            self.assertEqual(
                sorted(path.name for path in output_root.iterdir()), [user_file.name]
            )
            self.assertEqual(user_file.read_text(encoding="utf-8"), "keep this file\n")

    def test_golden_tiles_seam_failure_rolls_back_both_new_outputs(self) -> None:
        parent, source, samples, h0 = _small_adjacent_fixture()

        def verify_tile(_source_dir, _ledger, spec):
            row, column, rows, columns = spec.source_window
            return source, samples[row : row + rows, column : column + columns], h0

        def fail_after_both_bundles(
            _parent,
            _west_spec,
            _east_spec,
            west_bundle,
            east_bundle,
            _schema_path,
        ):
            expected_files = {
                "manifest.json",
                "horizontal_xy.f64le",
                "height_samples.f32le",
            }
            self.assertEqual(
                {path.name for path in west_bundle.iterdir()}, expected_files
            )
            self.assertEqual(
                {path.name for path in east_bundle.iterdir()}, expected_files
            )
            raise producer.ProducerError("injected seam-validation failure")

        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "inputs"
            source_dir.mkdir()
            ledger = root / "ledger.md"
            ledger.write_text("local deterministic fixture\n", encoding="utf-8")
            output_root = root / "partial-output"
            output_root.mkdir()
            user_file = output_root / "user-owned-note.txt"
            user_file.write_text("keep this file\n", encoding="utf-8")
            args = [
                "--source-dir", str(source_dir),
                "--ledger", str(ledger),
                "--schema", str(SCHEMA_PATH),
                "--output-root", str(output_root),
                "--region", "golden-tiles",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(producer.REGIONS, {"golden": parent}, clear=True),
                mock.patch.object(producer, "_producer_commit", return_value=FIXTURE_COMMIT),
                mock.patch.object(producer, "_verify_raster_window", side_effect=verify_tile),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
                mock.patch.object(
                    producer,
                    "validate_adjacent_tile_bundles",
                    side_effect=fail_after_both_bundles,
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(producer.main(args), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("injected seam-validation failure", stderr.getvalue())
            self.assertEqual(
                sorted(path.name for path in output_root.iterdir()), [user_file.name]
            )
            self.assertEqual(user_file.read_text(encoding="utf-8"), "keep this file\n")

    def test_both_regions_roll_back_new_outputs_and_preserve_existing_user_files(self) -> None:
        first = _small_fixture("fixture-first", -76.5, 39.2, 3.0)
        second = _small_fixture("fixture-second", -105.2, 39.7, 100.0)
        specs = {first[0].key: first[0], second[0].key: second[0]}
        sources = {first[0].key: first[1:], second[0].key: second[1:]}

        def verify(_source_dir: Path, _ledger: Path, spec: producer.RegionSpec):
            if spec.key == second[0].key:
                raise producer.ProducerError("injected later-region failure")
            return sources[spec.key]

        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "inputs"
            source_dir.mkdir()
            ledger = root / "ledger.md"
            ledger.write_text("local deterministic fixture\n", encoding="utf-8")
            output_root = root / "output"
            output_root.mkdir()
            user_file = output_root / "user-owned-note.txt"
            user_file.write_text("keep this file\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            args = [
                "--source-dir", str(source_dir),
                "--ledger", str(ledger),
                "--schema", str(SCHEMA_PATH),
                "--output-root", str(output_root),
                "--region", "both",
            ]
            with (
                mock.patch.dict(producer.REGIONS, specs, clear=True),
                mock.patch.object(producer, "_producer_commit", return_value=FIXTURE_COMMIT),
                mock.patch.object(producer, "_verify_raster_window", side_effect=verify),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result_code = producer.main(args)
            self.assertEqual(result_code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("injected later-region failure", stderr.getvalue())
            self.assertEqual(sorted(path.name for path in output_root.iterdir()), [user_file.name])
            self.assertEqual(user_file.read_text(encoding="utf-8"), "keep this file\n")

    def test_partial_payload_write_is_removed_without_touching_other_output(self) -> None:
        spec, source, samples, _h0 = _small_fixture("fixture-partial", -76.5, 39.2, 3.0)
        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "inputs"
            source_dir.mkdir()
            ledger = root / "ledger.md"
            ledger.write_text("local deterministic fixture\n", encoding="utf-8")
            output_root = root / "output"
            output_root.mkdir()
            user_file = output_root / "user-owned-note.txt"
            user_file.write_text("preserve\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            args = [
                "--source-dir", str(source_dir),
                "--ledger", str(ledger),
                "--schema", str(SCHEMA_PATH),
                "--output-root", str(output_root),
                "--region", spec.key,
            ]
            real_open = Path.open

            class PartialWriter:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    return self.handle.__exit__(exc_type, exc_value, traceback)

                def write(self, data: bytes) -> int:
                    self.handle.write(data[:8])
                    self.handle.flush()
                    raise OSError("injected partial write")

                def flush(self) -> None:
                    self.handle.flush()

                def fileno(self) -> int:
                    return self.handle.fileno()

            def fail_first_payload(path: Path, mode: str = "r", *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if path.name == "horizontal_xy.f64le" and mode == "xb":
                    return PartialWriter(handle)
                return handle

            with (
                mock.patch.dict(producer.REGIONS, {spec.key: spec}, clear=True),
                mock.patch.object(producer, "_producer_commit", return_value=FIXTURE_COMMIT),
                mock.patch.object(
                    producer,
                    "_verify_raster_window",
                    return_value=(source, samples, spec.ledger_h0),
                ),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
                mock.patch.object(Path, "open", new=fail_first_payload),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result_code = producer.main(args)
            self.assertEqual(result_code, 1)
            self.assertIn("injected partial write", stderr.getvalue())
            self.assertEqual(sorted(path.name for path in output_root.iterdir()), [user_file.name])
            self.assertEqual(user_file.read_text(encoding="utf-8"), "preserve\n")

    def test_parser_corruption_and_existing_output_fail_without_overwrite(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                producer.parse_args([
                    "--source-dir", "source",
                    "--ledger", "ledger.md",
                    "--schema", str(SCHEMA_PATH),
                    "--output-root", "output",
                    "--region", "unknown-region",
                ])

        spec, source, samples, _h0 = _small_fixture("fixture-existing", -76.5, 39.2, 3.0)
        with _temporary_directory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "inputs"
            source_dir.mkdir()
            ledger = root / "ledger.md"
            ledger.write_text("local deterministic fixture\n", encoding="utf-8")
            output_root = root / "output"
            args = (spec, source_dir, ledger, SCHEMA_PATH, output_root, FIXTURE_COMMIT)
            with (
                mock.patch.object(
                    producer,
                    "_verify_raster_window",
                    return_value=(source, samples, spec.ledger_h0),
                ),
                mock.patch.object(producer, "_iso_utc_now", return_value=FIXTURE_RECEIPT),
            ):
                bundle_dir, _result, _bounds = producer.build_bundle(*args)
                original_manifest = (bundle_dir / "manifest.json").read_bytes()
                with self.assertRaisesRegex(producer.ProducerError, "refusing to overwrite"):
                    producer.build_bundle(*args)
                self.assertEqual((bundle_dir / "manifest.json").read_bytes(), original_manifest)

            payload = bundle_dir / "height_samples.f32le"
            with payload.open("ab") as output:
                output.write(b"x")
            with self.assertRaisesRegex(producer.ProducerError, "payload byte length mismatch"):
                producer.validate_bundle(bundle_dir, SCHEMA_PATH)

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
                self.assertRegex(
                    manifest["source"]["acquired_at_utc"],
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                )
                self.assertNotIn("C:\\", json.dumps(manifest))
                self.assertTrue(manifest["source"]["source_uri"].startswith("https://"))
                self.assertEqual(manifest["frame"]["vertical"]["local_up_expression"], "H-H0")
                pipeline = manifest["frame"]["horizontal"]["operation_proj_pipeline"]
                self.assertEqual(
                    pipeline,
                    "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
                    f"+step +proj=aeqd +lat_0={spec.anchor_lat!r} +lon_0={spec.anchor_lon!r} "
                    "+ellps=GRS80 +x_0=0 +y_0=0",
                )
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

        west_bundle = REAL_BUNDLE_ROOT / "golden-west"
        east_bundle = REAL_BUNDLE_ROOT / "golden-east"
        if west_bundle.exists() or east_bundle.exists():
            self.assertTrue(west_bundle.is_dir() and east_bundle.is_dir())
            parent = producer.REGIONS["golden"]
            west_spec, east_spec = producer.golden_adjacent_tile_specs(parent)
            west, east = producer.validate_adjacent_tile_bundles(
                parent,
                west_spec,
                east_spec,
                west_bundle,
                east_bundle,
                SCHEMA_PATH,
            )
            self.assertEqual(west["frame"], east["frame"])
            self.assertEqual(
                west["frame_definition_sha256"], east["frame_definition_sha256"]
            )
            self.assertEqual(west["source"], east["source"])

            row_offset, column_offset, rows, columns = parent.source_window
            west_heights = np.fromfile(
                west_bundle / "height_samples.f32le", dtype="<f4"
            ).reshape(rows, west_spec.source_window[3])
            east_heights = np.fromfile(
                east_bundle / "height_samples.f32le", dtype="<f4"
            ).reshape(rows, east_spec.source_window[3])
            west_xy = np.fromfile(
                west_bundle / "horizontal_xy.f64le", dtype="<f8"
            ).reshape(rows, west_spec.source_window[3], 2)
            east_xy = np.fromfile(
                east_bundle / "horizontal_xy.f64le", dtype="<f8"
            ).reshape(rows, east_spec.source_window[3], 2)
            combined_heights = np.concatenate((west_heights, east_heights[:, 1:]), axis=1)
            combined_xy = np.concatenate((west_xy, east_xy[:, 1:, :]), axis=1)
            self.assertEqual(combined_heights.shape, (rows, columns))
            with rasterio.open(SOURCE_DIR / parent.raster_name) as dataset:
                source_samples = dataset.read(
                    1,
                    window=Window(column_offset, row_offset, columns, rows),
                    masked=False,
                )
                self.assertEqual(
                    np.ascontiguousarray(combined_heights, dtype="<f4").tobytes(),
                    np.ascontiguousarray(source_samples, dtype="<f4").tobytes(),
                )

                selected_rows = np.linspace(0, rows - 1, 13, dtype=np.int64)
                selected_columns = np.linspace(0, columns - 1, 13, dtype=np.int64)
                points: list[tuple[float, float, float, float]] = []
                radial_vector_errors: list[float] = []
                for row in selected_rows:
                    for column in selected_columns:
                        lon = dataset.transform.c + (
                            column_offset + int(column) + 0.5
                        ) * dataset.transform.a
                        lat = dataset.transform.f + (
                            row_offset + int(row) + 0.5
                        ) * dataset.transform.e
                        east_value, north_value = combined_xy[int(row), int(column)]
                        distance, azimuth = _vincenty_inverse(
                            parent.anchor_lon, parent.anchor_lat, lon, lat
                        )
                        radial_vector_errors.append(
                            math.hypot(
                                float(east_value) - distance * math.sin(azimuth),
                                float(north_value) - distance * math.cos(azimuth),
                            )
                        )
                        points.append(
                            (lon, lat, float(east_value), float(north_value))
                        )

            pair_errors: list[float] = []
            for first_index, first in enumerate(points):
                for second in points[first_index + 1 :]:
                    chart_distance = math.hypot(
                        second[2] - first[2], second[3] - first[3]
                    )
                    geodesic_distance = _vincenty_inverse(
                        first[0], first[1], second[0], second[1]
                    )[0]
                    pair_errors.append(abs(chart_distance - geodesic_distance))

            # Main Lead's crop-specific limit is the 70.3 µm curvature bound
            # plus a 1 mm independent numerical allowance; it is not a source
            # accuracy statement or a global geographic tolerance.
            self.assertLessEqual(max(radial_vector_errors), 0.0011)
            self.assertLessEqual(max(pair_errors), 0.0011)
            print(
                "Golden adjacent tiles independent 13x13 GRS80 checks: "
                f"max radial-vector error={max(radial_vector_errors):.12g} m, "
                f"max pairwise chart/geodesic error={max(pair_errors):.12g} m; "
                "0.0011 m crop criterion (not source accuracy)."
            )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)

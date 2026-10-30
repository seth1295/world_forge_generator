#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Build source-verified WorldForge terrain-grid v1 bundles from the WF-VS-01 tiles.

This pilot producer is independent of BeamNG and keeps the original NAVD88
Float32 samples. It intentionally supports only the two approved USGS 3DEP
source windows; the projection and serialization functions are general to the
versioned contract, while source identities and windows remain explicit data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import numpy as np
import rasterio
from jsonschema import Draft202012Validator, FormatChecker
from rasterio.crs import CRS
from rasterio.windows import Window
from rasterio.warp import transform as warp_transform


CONTRACT_ID = "urn:worldforge:contract:terrain-grid:1.0.0"
PRODUCER_NAME = "WorldForge Terrain Grid Producer"
PRODUCER_VERSION = "1.0.0"
SOURCE_VERIFIED_DATE = "2026-10-29"
MANIFEST_LIMIT_BYTES = 64 * 1024
# Match source.raster.byte_length and metadata/product_record.byte_length in Terrain Grid v1.
MAX_SOURCE_RASTER_BYTES = 4_294_967_295
MAX_SOURCE_METADATA_BYTES = 10 * 1024 * 1024
MAX_SAMPLES = 4_194_304
MAX_SOURCE_HEIGHT_M = 12_000.0
MAX_RELATIVE_HEIGHT_M = 24_000.0
MAX_HORIZONTAL_COMPONENT_M = 100_000.0
NODATA_VALUE = -999999.0


@dataclass(frozen=True)
class RegionSpec:
    key: str
    region_id: str
    frame_id: str
    raster_name: str
    xml_name: str
    record_name: str
    product_id: str
    product_date: str
    source_window: tuple[int, int, int, int]  # row, column, rows, columns
    anchor_row: int
    anchor_column: int
    anchor_lon: float
    anchor_lat: float
    ledger_h0: float
    ledger_crop_bounds: tuple[float, float, float, float]
    validation_source_window: tuple[int, int, int, int] | None = None


REGIONS: dict[str, RegionSpec] = {
    "baltimore": RegionSpec(
        key="baltimore",
        region_id="wf-vs01:baltimore",
        frame_id="wf-vs01:baltimore-grs80-aeqd",
        raster_name="USGS_13_n40w077_20260512.tif",
        xml_name="USGS_13_n40w077_20260512.xml",
        record_name="tnm-product-metadata.json",
        product_id="USGS 1/3 Arc Second n40w077 20260512",
        product_date="2026-05-12",
        source_window=(7566, 4110, 216, 216),
        anchor_row=7674,
        anchor_column=4218,
        anchor_lon=-76.60995370372139,
        anchor_lat=39.28995370377841,
        ledger_h0=5.8492488861,
        ledger_crop_bounds=(-76.6200, 39.2800, -76.6000, 39.3000),
    ),
    "golden": RegionSpec(
        key="golden",
        region_id="wf-vs01:golden-front-range",
        frame_id="wf-vs01:golden-grs80-aeqd",
        raster_name="USGS_13_n40w106_20260630.tif",
        xml_name="USGS_13_n40w106_20260630.xml",
        record_name="tnm-product-metadata-golden.json",
        product_id="USGS 1/3 Arc Second n40w106 20260630",
        product_date="2026-07-01",
        source_window=(2544, 8214, 216, 432),
        anchor_row=2652,
        anchor_column=8430,
        anchor_lon=-105.21995370310027,
        anchor_lat=39.754953704278655,
        ledger_h0=1729.523193359375,
        ledger_crop_bounds=(-105.2400, 39.7450, -105.2000, 39.7650),
    ),
}


def _validated_window(spec: RegionSpec, label: str) -> tuple[int, int, int, int]:
    """Return a safe row/column window before any raster read or allocation."""
    window = spec.source_window
    if len(window) != 4 or any(type(value) is not int for value in window):
        raise ProducerError(f"{label} source window must contain four integers")
    row, column, rows, columns = window
    if row < 0 or column < 0 or rows < 2 or columns < 2:
        raise ProducerError(f"{label} source window has negative offsets or too few samples")
    if row > 99_999 or column > 99_999 or rows > 4096 or columns > 4096:
        raise ProducerError(f"{label} source window exceeds Terrain Grid v1 limits")
    if rows > MAX_SAMPLES // columns:
        raise ProducerError(f"{label} source window exceeds the v1 sample cap")
    return window


def validate_adjacent_tile_specs(
    parent: RegionSpec, west: RegionSpec, east: RegionSpec
) -> None:
    """Require two windows to cover one parent window with exactly one shared column."""
    parent_row, parent_column, parent_rows, parent_columns = _validated_window(
        parent, "parent"
    )
    west_row, west_column, west_rows, west_columns = _validated_window(west, "west tile")
    east_row, east_column, east_rows, east_columns = _validated_window(east, "east tile")

    for tile, label in ((west, "west tile"), (east, "east tile")):
        if tile.region_id != parent.region_id or tile.frame_id != parent.frame_id:
            raise ProducerError(f"{label} must retain the parent region and frame identity")
        if tile.validation_source_window != parent.source_window:
            raise ProducerError(f"{label} must validate against the complete parent crop")
        if (
            tile.raster_name != parent.raster_name
            or tile.xml_name != parent.xml_name
            or tile.record_name != parent.record_name
            or tile.product_id != parent.product_id
            or tile.product_date != parent.product_date
            or tile.anchor_row != parent.anchor_row
            or tile.anchor_column != parent.anchor_column
            or tile.anchor_lon != parent.anchor_lon
            or tile.anchor_lat != parent.anchor_lat
            or tile.ledger_h0 != parent.ledger_h0
            or tile.ledger_crop_bounds != parent.ledger_crop_bounds
        ):
            raise ProducerError(f"{label} source or regional origin differs from the parent")
        tile_row, tile_column, tile_rows, tile_columns = _validated_window(tile, label)
        if (
            tile_row < parent_row
            or tile_column < parent_column
            or tile_row + tile_rows > parent_row + parent_rows
            or tile_column + tile_columns > parent_column + parent_columns
        ):
            raise ProducerError(f"{label} lies outside the parent source window")
    if (
        west_row != parent_row
        or east_row != parent_row
        or west_rows != parent_rows
        or east_rows != parent_rows
    ):
        raise ProducerError("adjacent tiles must use the same complete parent row range")
    if west_column != parent_column:
        raise ProducerError("west tile must start at the parent window's first column")
    if east_column != west_column + west_columns - 1:
        raise ProducerError("adjacent tile windows must share exactly one boundary column")
    if east_column + east_columns != parent_column + parent_columns:
        raise ProducerError("east tile must end at the parent window's final column")
    for tile, label in ((west, "west tile"), (east, "east tile")):
        tile_row, tile_column, tile_rows, tile_columns = tile.source_window
        if not (tile_row <= parent.anchor_row < tile_row + tile_rows):
            raise ProducerError(f"{label} does not contain the regional anchor row")
        if not (tile_column <= parent.anchor_column < tile_column + tile_columns):
            raise ProducerError(f"{label} does not contain the shared anchor column")


def golden_adjacent_tile_specs(
    parent: RegionSpec | None = None,
) -> tuple[RegionSpec, RegionSpec]:
    """Split the selected Golden crop into west/east sample windows with one shared column."""
    parent = REGIONS["golden"] if parent is None else parent
    row, column, rows, columns = parent.source_window
    if columns < 4:
        raise ProducerError("Golden crop is too narrow to create two v1 terrain tiles")
    seam_offset = columns // 2
    west_columns = seam_offset + 1
    east = replace(
        parent,
        key="golden-east",
        source_window=(row, column + seam_offset, rows, columns - seam_offset),
        validation_source_window=parent.source_window,
    )
    west = replace(
        parent,
        key="golden-west",
        source_window=(row, column, rows, west_columns),
        validation_source_window=parent.source_window,
    )
    validate_adjacent_tile_specs(parent, west, east)
    return west, east


class ProducerError(RuntimeError):
    """Raised when source, projection, schema, or output invariants fail."""


@dataclass(frozen=True)
class VerifiedSource:
    raster_path: Path
    xml_path: Path
    record_path: Path
    raster_sha256: str
    xml_sha256: str
    record_sha256: str
    raster_bytes: int
    xml_bytes: int
    record_bytes: int
    record: dict[str, Any]
    acquired_at_utc: str
    affine: tuple[float, float, float, float, float, float]
    source_width: int
    source_height: int
    nodata: float
    raster_min: float
    raster_max: float


def _sha256_stream(file_obj: BinaryIO, *, max_bytes: int | None = None) -> tuple[str, int]:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be nonnegative")
    digest = hashlib.sha256()
    total = 0
    while True:
        read_size = (
            1024 * 1024
            if max_bytes is None
            else min(1024 * 1024, max_bytes - total + 1)
        )
        chunk = file_obj.read(read_size)
        if not chunk:
            break
        if max_bytes is not None and total + len(chunk) > max_bytes:
            raise ProducerError(f"file exceeds {max_bytes} byte limit while hashing")
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def sha256_file(path: Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    """Return a bounded streaming SHA-256 and byte count for one file."""
    try:
        size = path.stat().st_size
        if max_bytes is not None and size > max_bytes:
            raise ProducerError(f"file exceeds {max_bytes} byte limit: {path.name}")
        with path.open("rb") as source_file:
            digest, actual_size = _sha256_stream(source_file, max_bytes=max_bytes)
            return digest, actual_size
    except ProducerError:
        raise
    except OSError as exc:
        raise ProducerError(f"cannot read source file {path.name}: {exc}") from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProducerError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_json_unique(path: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if max_bytes is not None and size > max_bytes:
            raise ProducerError(f"JSON file exceeds {max_bytes} byte limit: {path.name}")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_pairs)
    except ProducerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerError(f"cannot parse JSON file {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProducerError(f"JSON root must be an object: {path.name}")
    return value


def _ledger_row(
    ledger_path: Path, spec: RegionSpec
) -> tuple[list[tuple[int, str]], list[str], str]:
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProducerError(f"cannot read source ledger: {exc}") from exc
    matches = [line for line in text.splitlines() if spec.product_id in line]
    if len(matches) != 1:
        message = (
            f"source ledger must contain exactly one row for {spec.product_id}; "
            f"found {len(matches)}"
        )
        raise ProducerError(message)
    line = matches[0]
    record_matches = re.findall(
        r"([0-9][0-9,]*) bytes; SHA-256 `([0-9A-Fa-f]{64})`", line
    )
    if len(record_matches) != 3:
        raise ProducerError(
            f"ledger row must record raster, XML, and TNM JSON hashes for {spec.key}"
        )
    limits = (
        MAX_SOURCE_RASTER_BYTES,
        MAX_SOURCE_METADATA_BYTES,
        MAX_SOURCE_METADATA_BYTES,
    )
    records: list[tuple[int, str]] = []
    for (size_text, digest), limit in zip(record_matches, limits, strict=True):
        digits = size_text.replace(",", "")
        if len(digits) > 10:
            raise ProducerError(f"source ledger size exceeds the v1 limit for {spec.key}")
        try:
            size = int(digits)
        except ValueError as exc:
            raise ProducerError(f"source ledger has an invalid size for {spec.key}") from exc
        if size < 1 or size > limit:
            raise ProducerError(f"source ledger size is outside the v1 byte limit for {spec.key}")
        records.append((size, digest.lower()))
    urls = re.findall(r"\]\((https?://[^)]+)\)", line)
    snapshot_timestamps = re.findall(
        r"local snapshot file-creation receipt `([^`]+)`", line
    )
    if len(snapshot_timestamps) != 1 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", snapshot_timestamps[0]
    ):
        raise ProducerError(
            f"source ledger must contain one UTC local snapshot timestamp for {spec.key}"
        )
    try:
        datetime.fromisoformat(snapshot_timestamps[0][:-1] + "+00:00")
    except ValueError as exc:
        raise ProducerError(
            f"source ledger local snapshot timestamp is not a valid UTC time for {spec.key}"
        ) from exc
    return records, urls, snapshot_timestamps[0]


def _read_xml_text(path: Path) -> list[str]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ProducerError(f"cannot parse XML metadata {path.name}: {exc}") from exc
    return [" ".join(node.text.split()) for node in root.iter() if node.text and node.text.strip()]


def _verify_record_and_ledger(
    source_dir: Path, ledger_path: Path, spec: RegionSpec
) -> tuple[dict[str, Any], tuple[tuple[int, str], ...], str]:
    _reject_reparse(source_dir)
    raster_path = source_dir / spec.raster_name
    xml_path = source_dir / spec.xml_name
    record_path = source_dir / spec.record_name
    if not raster_path.is_file() or not xml_path.is_file() or not record_path.is_file():
        missing = [p.name for p in (raster_path, xml_path, record_path) if not p.is_file()]
        raise ProducerError(f"missing acquired input(s) for {spec.key}: {', '.join(missing)}")
    for path in (raster_path, xml_path, record_path):
        _reject_reparse(path)

    expected, ledger_urls, acquired_at_utc = _ledger_row(ledger_path, spec)
    source_paths = (raster_path, xml_path, record_path)
    limits = (
        MAX_SOURCE_RASTER_BYTES,
        MAX_SOURCE_METADATA_BYTES,
        MAX_SOURCE_METADATA_BYTES,
    )
    for label, path, (expected_size, _expected_digest), max_bytes in zip(
        ("raster", "XML metadata", "TNM product record"),
        source_paths,
        expected,
        limits,
        strict=True,
    ):
        if expected_size < 1 or expected_size > max_bytes:
            raise ProducerError(f"{label} exceeds the v1 byte limit for {spec.key}")
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise ProducerError(f"cannot inspect source file {path.name}: {exc}") from exc
        if actual_size > max_bytes:
            raise ProducerError(f"{label} exceeds the v1 byte limit for {spec.key}")
        if actual_size != expected_size:
            raise ProducerError(
                f"{label} size mismatch before hashing for {spec.key}: "
                f"expected {expected_size} bytes, got {actual_size}"
            )

    actual_hashes = [
        sha256_file(path, max_bytes=expected_size)
        for path, (expected_size, _expected_digest) in zip(source_paths, expected, strict=True)
    ]
    for label, expected_pair, actual_pair in zip(
        ("raster", "XML metadata", "TNM product record"), expected, actual_hashes, strict=True
    ):
        expected_size, expected_digest = expected_pair
        actual_digest, actual_size = actual_pair
        if actual_size != expected_size or actual_digest != expected_digest:
            raise ProducerError(
                f"{label} does not match vertical-slice ledger for {spec.key}: "
                f"expected {expected_size} bytes / {expected_digest}, "
                f"got {actual_size} bytes / {actual_digest}"
            )

    record = load_json_unique(record_path, max_bytes=10 * 1024 * 1024)
    expected_fields = {
        "title": spec.product_id,
        "publicationDate": spec.product_date,
        "sizeInBytes": expected[0][0],
        "format": "GeoTIFF",
    }
    for name, value in expected_fields.items():
        if record.get(name) != value:
            raise ProducerError(f"TNM record {name} mismatch for {spec.key}: {record.get(name)!r}")
    public_urls = [record.get("downloadURL"), record.get("vendorMetaUrl"), record.get("metaUrl")]
    if any(not isinstance(url, str) or url not in ledger_urls for url in public_urls):
        raise ProducerError(f"TNM source URLs do not match the public ledger row for {spec.key}")
    for url in public_urls:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ProducerError(f"non-HTTPS public source URL in TNM record for {spec.key}")

    xml_text = "\n".join(_read_xml_text(xml_path))
    if spec.product_id not in xml_text:
        raise ProducerError(f"XML metadata title does not match product ID for {spec.key}")
    if "North American Datum of 1983" not in xml_text:
        raise ProducerError(f"XML metadata does not verify NAD83 for {spec.key}")
    if "North American Vertical Datum of 1988" not in xml_text:
        raise ProducerError(f"XML metadata does not verify NAVD88 for {spec.key}")
    return record, tuple(expected), acquired_at_utc


def _verify_raster_window(
    source_dir: Path, ledger_path: Path, spec: RegionSpec
) -> tuple[VerifiedSource, np.ndarray, float]:
    record, expected, acquired_at_utc = _verify_record_and_ledger(source_dir, ledger_path, spec)
    raster_path = source_dir / spec.raster_name
    xml_path = source_dir / spec.xml_name
    record_path = source_dir / spec.record_name
    try:
        with rasterio.open(raster_path, "r") as dataset:
            if dataset.driver != "GTiff":
                raise ProducerError(f"source driver must be GTiff for {spec.key}")
            if dataset.count != 1 or dataset.dtypes != ("float32",):
                raise ProducerError(f"source must be one Float32 band for {spec.key}")
            if dataset.width != 10_812 or dataset.height != 10_812:
                raise ProducerError(f"source dimensions differ from ledger for {spec.key}")
            if dataset.crs != CRS.from_epsg(4269):
                raise ProducerError(f"source CRS is not EPSG:4269 for {spec.key}")
            if dataset.nodata != NODATA_VALUE:
                raise ProducerError(f"source no-data value differs from ledger for {spec.key}")
            if dataset.scales != (1.0,) or dataset.offsets != (0.0,):
                raise ProducerError(f"source scale/offset must be 1/0 for {spec.key}")
            if dataset.tags().get("AREA_OR_POINT") != "Area":
                raise ProducerError(f"source pixel interpretation must be Area for {spec.key}")
            transform_affine = dataset.transform
            if transform_affine.b != 0.0 or transform_affine.d != 0.0:
                raise ProducerError(f"rotated source rasters are not supported for {spec.key}")
            if transform_affine.a <= 0.0 or transform_affine.e >= 0.0:
                raise ProducerError(f"source raster must be north-up for {spec.key}")
            row_offset, column_offset, rows, columns = _validated_window(spec, spec.key)
            if row_offset + rows > dataset.height or column_offset + columns > dataset.width:
                raise ProducerError(f"source window exceeds raster dimensions for {spec.key}")
            validation_spec = replace(
                spec,
                source_window=spec.validation_source_window or spec.source_window,
                validation_source_window=None,
            )
            (
                validation_row,
                validation_column,
                validation_rows,
                validation_columns,
            ) = _validated_window(validation_spec, f"{spec.key} validation")
            if (
                row_offset < validation_row
                or column_offset < validation_column
                or row_offset + rows > validation_row + validation_rows
                or column_offset + columns > validation_column + validation_columns
            ):
                raise ProducerError(f"source window lies outside approved crop for {spec.key}")
            if (
                validation_row + validation_rows > dataset.height
                or validation_column + validation_columns > dataset.width
            ):
                raise ProducerError(f"approved crop exceeds raster dimensions for {spec.key}")
            window = Window(column_offset, row_offset, columns, rows)
            samples = dataset.read(1, window=window, masked=False)
            if samples.dtype != np.dtype("float32") or samples.shape != (rows, columns):
                raise ProducerError(
                    f"rasterio changed source sample representation for {spec.key}"
                )
            validate_sample_array(samples, dataset.nodata, spec.key)

            for source_row, source_column, crop_value in (
                (row_offset, column_offset, samples[0, 0]),
                (row_offset + rows - 1, column_offset + columns - 1, samples[-1, -1]),
            ):
                edge_value = dataset.read(
                    1,
                    window=Window(source_column, source_row, 1, 1),
                    masked=False,
                )[0, 0]
                if np.asarray(edge_value, dtype=np.float32).tobytes() != np.asarray(
                    crop_value, dtype=np.float32
                ).tobytes():
                    raise ProducerError(
                        f"crop edge sample differs from original raster for {spec.key}"
                    )

            anchor_window = Window(spec.anchor_column, spec.anchor_row, 1, 1)
            anchor = float(dataset.read(1, window=anchor_window, masked=False)[0, 0])
            validate_sample_array(np.asarray([anchor], dtype=np.float32), dataset.nodata, spec.key)
            if not math.isclose(anchor, spec.ledger_h0, rel_tol=0.0, abs_tol=5e-9):
                raise ProducerError(
                    f"anchor H0 differs from source ledger for {spec.key}: {anchor}"
                )
            if not (row_offset <= spec.anchor_row < row_offset + rows):
                raise ProducerError(f"anchor row falls outside the selected window for {spec.key}")
            if not (column_offset <= spec.anchor_column < column_offset + columns):
                raise ProducerError(
                    f"anchor column falls outside the selected window for {spec.key}"
                )

            anchor_lon, anchor_lat = transform_affine * (
                spec.anchor_column + 0.5, spec.anchor_row + 0.5
            )
            if not math.isclose(anchor_lon, spec.anchor_lon, rel_tol=0.0, abs_tol=5e-11):
                raise ProducerError(
                    f"anchor longitude differs from ledger for {spec.key}: {anchor_lon}"
                )
            if not math.isclose(anchor_lat, spec.anchor_lat, rel_tol=0.0, abs_tol=5e-11):
                raise ProducerError(
                    f"anchor latitude differs from ledger for {spec.key}: {anchor_lat}"
                )

            expected_crop = spec.ledger_crop_bounds
            west, south = transform_affine * (
                validation_column,
                validation_row + validation_rows,
            )
            east, north = transform_affine * (
                validation_column + validation_columns,
                validation_row,
            )
            derived_crop = (west, south, east, north)
            if not all(
                math.isclose(actual, expected_value, rel_tol=0.0, abs_tol=1.1e-4)
                for actual, expected_value in zip(derived_crop, expected_crop, strict=True)
            ):
                raise ProducerError(f"source window does not match ledger crop for {spec.key}")

            affine = (
                transform_affine.a,
                transform_affine.b,
                transform_affine.c,
                transform_affine.d,
                transform_affine.e,
                transform_affine.f,
            )
            verified_source = VerifiedSource(
                raster_path=raster_path,
                xml_path=xml_path,
                record_path=record_path,
                raster_sha256=expected[0][1],
                xml_sha256=expected[1][1],
                record_sha256=expected[2][1],
                raster_bytes=expected[0][0],
                xml_bytes=expected[1][0],
                record_bytes=expected[2][0],
                record=record,
                acquired_at_utc=acquired_at_utc,
                affine=affine,
                source_width=dataset.width,
                source_height=dataset.height,
                nodata=float(dataset.nodata),
                raster_min=float(samples.min()),
                raster_max=float(samples.max()),
            )
    except rasterio.errors.RasterioError as exc:
        raise ProducerError(f"cannot read source raster for {spec.key}: {exc}") from exc
    return verified_source, samples, anchor


def validate_sample_array(samples: np.ndarray, nodata: float, label: str) -> None:
    """Reject no-data, non-finite, or out-of-contract Float32 heights."""
    if samples.dtype != np.dtype("float32"):
        raise ProducerError(f"{label}: heights must remain source Float32")
    if not np.isfinite(samples).all():
        raise ProducerError(f"{label}: crop contains a NaN or infinite height")
    if np.equal(samples, np.float32(nodata)).any():
        raise ProducerError(f"{label}: crop contains source no-data")
    if np.greater(np.abs(samples.astype(np.float64)), MAX_SOURCE_HEIGHT_M).any():
        raise ProducerError(f"{label}: source height exceeds ±{MAX_SOURCE_HEIGHT_M:g} m")


def validate_relative_heights(samples: np.ndarray, h0: float, label: str) -> None:
    if not math.isfinite(h0) or abs(h0) > MAX_SOURCE_HEIGHT_M:
        raise ProducerError(f"{label}: H0 is outside the v1 source-height limit")
    relative = samples.astype(np.float64) - h0
    if not np.isfinite(relative).all() or np.greater(
        np.abs(relative), MAX_RELATIVE_HEIGHT_M
    ).any():
        raise ProducerError(f"{label}: H-H0 exceeds the v1 relative-height limit")


def _projection(spec: RegionSpec) -> tuple[CRS, str, str]:
    proj4 = (
        f"+proj=aeqd +lat_0={spec.anchor_lat!r} +lon_0={spec.anchor_lon!r} "
        "+datum=NAD83 +x_0=0 +y_0=0 +units=m +no_defs +type=crs"
    )
    pipeline = (
        "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
        f"+step +proj=aeqd +lat_0={spec.anchor_lat!r} +lon_0={spec.anchor_lon!r} "
        "+ellps=GRS80 +x_0=0 +y_0=0"
    )
    if "+proj=pipeline" not in pipeline or "WGS84" in pipeline.upper():
        raise ProducerError("invalid or datum-shifting AEQD operation pipeline")
    try:
        destination = CRS.from_proj4(proj4)
        wkt2 = destination.to_wkt(version="WKT2_2019")
    except Exception as exc:  # Rasterio/PROJ may expose several native error classes.
        raise ProducerError(f"cannot construct NAD83/GRS80 AEQD CRS: {exc}") from exc
    if not wkt2 or len(wkt2) > 8192:
        raise ProducerError("generated WKT2 is empty or exceeds the contract limit")
    wkt_normalized = " ".join(wkt2.split()).casefold()
    if 'datum["north american datum 1983"' not in wkt_normalized:
        raise ProducerError("projection WKT2 does not preserve the NAD83 datum")
    if 'ellipsoid["grs 1980",6378137,298.257222101' not in wkt_normalized:
        raise ProducerError("projection WKT2 does not define the GRS80 ellipsoid")
    if 'method["azimuthal equidistant"' not in wkt_normalized:
        raise ProducerError("projection WKT2 does not define Azimuthal Equidistant")
    if 'axis["(e)",east' not in wkt_normalized or 'axis["(n)",north' not in wkt_normalized:
        raise ProducerError("projection WKT2 axes are not East/North")
    if "wgs 84" in wkt_normalized:
        raise ProducerError("projection WKT2 unexpectedly contains WGS 84")
    if destination.linear_units != "metre":
        raise ProducerError(f"projection unit is not metre: {destination.linear_units}")
    return destination, pipeline, wkt2


def project_source_grid(
    spec: RegionSpec, source: VerifiedSource, rows: int, columns: int
) -> tuple[np.ndarray, np.ndarray, dict[str, float], CRS, str, str]:
    """Project every source pixel centre directly from EPSG:4269 into local GRS80 AEQD."""
    destination, pipeline, wkt2 = _projection(spec)
    row_offset, column_offset, expected_rows, expected_columns = spec.source_window
    if (rows, columns) != (expected_rows, expected_columns):
        raise ProducerError(f"grid dimensions differ from approved source window for {spec.key}")
    affine = source.affine
    pixel_width, _, x_origin, _, pixel_height, y_origin = affine
    row_indices = np.arange(row_offset, row_offset + rows, dtype=np.float64)
    column_indices = np.arange(column_offset, column_offset + columns, dtype=np.float64)
    longitudes = x_origin + (column_indices + 0.5) * pixel_width
    latitudes = y_origin + (row_indices + 0.5) * pixel_height
    lon_grid, lat_grid = np.meshgrid(longitudes, latitudes)
    try:
        east_values, north_values = warp_transform(
            CRS.from_epsg(4269), destination, lon_grid.ravel().tolist(), lat_grid.ravel().tolist()
        )
    except Exception as exc:
        raise ProducerError(f"GDAL/PROJ transform failed for {spec.key}: {exc}") from exc
    east = np.asarray(east_values, dtype=np.float64).reshape(rows, columns)
    north = np.asarray(north_values, dtype=np.float64).reshape(rows, columns)
    if not np.isfinite(east).all() or not np.isfinite(north).all():
        raise ProducerError(f"projection produced a non-finite coordinate for {spec.key}")
    if np.greater(np.abs(east), MAX_HORIZONTAL_COMPONENT_M).any() or np.greater(
        np.abs(north), MAX_HORIZONTAL_COMPONENT_M
    ).any():
        raise ProducerError(f"projection exceeds ±{MAX_HORIZONTAL_COMPONENT_M:g} m for {spec.key}")
    anchor_east, anchor_north = warp_transform(
        CRS.from_epsg(4269), destination, [spec.anchor_lon], [spec.anchor_lat]
    )
    if abs(anchor_east[0]) > 1e-7 or abs(anchor_north[0]) > 1e-7:
        raise ProducerError(
            f"projection anchor does not map to zero within 1e-7 m: "
            f"({anchor_east[0]}, {anchor_north[0]})"
        )
    bounds = {
        "easting_min": float(east.min()),
        "northing_min": float(north.min()),
        "easting_max": float(east.max()),
        "northing_max": float(north.max()),
    }
    return east, north, bounds, destination, pipeline, wkt2


def _frame_strings(frame: dict[str, Any]) -> list[str]:
    horizontal = frame["horizontal"]
    vertical = frame["vertical"]
    origin = frame["origin"]
    return [
        frame["frame_id"],
        horizontal["source_crs"],
        horizontal["source_axis_order"],
        horizontal["datum"],
        horizontal["ellipsoid"],
        horizontal["realization_epoch"],
        horizontal["projection_method"],
        horizontal["projection_wkt2"],
        horizontal["operation_proj_pipeline"],
        horizontal["projected_axis_order"],
        horizontal["unit"],
        vertical["datum"],
        vertical["height_type"],
        vertical["unit"],
        vertical["local_up_expression"],
        origin["source_product_id"],
    ]


def frame_definition_sha256(frame: dict[str, Any]) -> str:
    data = bytearray(b"WF-TERRAIN-FRAME-1\0")
    for value in _frame_strings(frame):
        encoded = value.encode("utf-8")
        data.extend(struct.pack("<I", len(encoded)))
        data.extend(encoded)
    origin = frame["origin"]
    data.extend(bytes.fromhex(origin["source_raster_sha256"]))
    data.extend(struct.pack("<II", origin["source_row"], origin["source_column"]))
    data.extend(
        struct.pack(
            "<ddd",
            origin["longitude_deg"],
            origin["latitude_deg"],
            origin["height_navd88_m"],
        )
    )
    return hashlib.sha256(data).hexdigest()


def artifact_id(manifest: dict[str, Any]) -> str:
    data = bytearray(b"WF-TERRAIN-ARTIFACT-1\0")
    contract_bytes = manifest["contract_id"].encode("utf-8")
    data.extend(struct.pack("<I", len(contract_bytes)))
    data.extend(contract_bytes)
    source = manifest["source"]
    payloads = manifest["payloads"]
    for digest in (
        manifest["frame_definition_sha256"],
        source["raster"]["sha256"],
        source["metadata"]["sha256"],
        source["product_record"]["sha256"],
        payloads["horizontal_xy"]["sha256"],
        payloads["height_samples"]["sha256"],
    ):
        data.extend(bytes.fromhex(digest))
    window = manifest["grid"]["source_window"]
    data.extend(
        struct.pack(
            "<IIII",
            window["row_offset"],
            window["column_offset"],
            window["rows"],
            window["columns"],
        )
    )
    return hashlib.sha256(data).hexdigest()


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _manifest_source(
    spec: RegionSpec, source: VerifiedSource
) -> dict[str, Any]:
    record = source.record
    a, b, c, d, e, f = source.affine
    title = record["title"]
    return {
        "provider": "U.S. Geological Survey (USGS)",
        "dataset": "3D Elevation Program (3DEP), 1/3 arc-second bare-earth elevation",
        "product_id": spec.product_id,
        "product_title": title,
        "product_date": spec.product_date,
        "source_uri": record["downloadURL"],
        "acquired_at_utc": source.acquired_at_utc,
        "acquisition_time_basis": "filesystem_creation_time",
        "snapshot_verified_date": SOURCE_VERIFIED_DATE,
        "raster": {
            "byte_length": source.raster_bytes,
            "sha256": source.raster_sha256,
            "width": source.source_width,
            "height": source.source_height,
            "band": 1,
            "dtype": "float32",
            "scale": 1,
            "offset": 0,
            "nodata_value": source.nodata,
            "pixel_interpretation": "area",
            "horizontal_crs": "EPSG:4269",
            "horizontal_datum": "NAD83",
            "ellipsoid": "GRS80",
            "realization_epoch": "unspecified_by_source_metadata",
            "vertical_datum": "NAVD88",
            "vertical_height_type": "orthometric",
            "vertical_unit": "metre",
            "affine": {
                "x_origin": c,
                "y_origin": f,
                "pixel_width": a,
                "rotation_x": b,
                "rotation_y": d,
                "pixel_height": e,
            },
        },
        "metadata": {
            "uri": record["vendorMetaUrl"],
            "byte_length": source.xml_bytes,
            "sha256": source.xml_sha256,
        },
        "product_record": {
            "uri": record["metaUrl"],
            "byte_length": source.record_bytes,
            "sha256": source.record_sha256,
        },
        "rights_statement": (
            "USGS 3DEP source elevation data are public-domain U.S. Government data. "
            f"Attribution: U.S. Geological Survey (USGS), {title}. This statement applies only "
            "to the identified source product and does not clear other datasets or assets."
        ),
        "uncertainty_statement": (
            "The source XML disclaims an accuracy warranty and gives no tile-specific accuracy "
            "bound. The product is a 1/3 arc-second DEM with NAVD88 orthometric metres; source "
            "product dates are not per-pixel survey dates. The source metadata does not identify "
            "a NAD83 realization or epoch."
        ),
    }


def _manifest_frame(
    spec: RegionSpec,
    source: VerifiedSource,
    anchor_height: float,
    pipeline: str,
    projection_wkt2: str,
) -> dict[str, Any]:
    return {
        "frame_id": spec.frame_id,
        "horizontal": {
            "source_crs": "EPSG:4269",
            "source_axis_order": "longitude_latitude",
            "datum": "NAD83",
            "ellipsoid": "GRS80",
            "realization_epoch": "unspecified_by_source_metadata",
            "projection_method": "ellipsoidal_azimuthal_equidistant",
            "projection_wkt2": projection_wkt2,
            "operation_proj_pipeline": pipeline,
            "projected_axis_order": "easting_northing",
            "unit": "metre",
        },
        "vertical": {
            "datum": "NAVD88",
            "height_type": "orthometric",
            "unit": "metre",
            "local_up_expression": "H-H0",
        },
        "origin": {
            "source_product_id": spec.product_id,
            "source_raster_sha256": source.raster_sha256,
            "source_row": spec.anchor_row,
            "source_column": spec.anchor_column,
            "longitude_deg": spec.anchor_lon,
            "latitude_deg": spec.anchor_lat,
            "height_navd88_m": anchor_height,
        },
    }


def _projected_footprint(source: VerifiedSource, spec: RegionSpec) -> dict[str, Any]:
    row_offset, column_offset, rows, columns = spec.source_window
    a, _, c, _, e, f = source.affine
    west = c + column_offset * a
    east = c + (column_offset + columns) * a
    north = f + row_offset * e
    south = f + (row_offset + rows) * e
    return {
        "west_deg": min(west, east),
        "south_deg": min(south, north),
        "east_deg": max(west, east),
        "north_deg": max(south, north),
        "crs": "EPSG:4269",
        "meaning": "outer_pixel_edges",
    }


def _build_manifest(
    spec: RegionSpec,
    source: VerifiedSource,
    h0: float,
    east: np.ndarray,
    north: np.ndarray,
    projected_bounds: dict[str, float],
    pipeline: str,
    projection_wkt2: str,
    horizontal_payload: bytes,
    height_payload: bytes,
    producer_commit: str,
) -> dict[str, Any]:
    row_offset, column_offset, rows, columns = spec.source_window
    source_window = {
        "row_offset": row_offset,
        "column_offset": column_offset,
        "rows": rows,
        "columns": columns,
    }
    frame = _manifest_frame(spec, source, h0, pipeline, projection_wkt2)
    manifest: dict[str, Any] = {
        "contract_id": CONTRACT_ID,
        "artifact_id": "0" * 64,
        "frame_definition_sha256": frame_definition_sha256(frame),
        "producer": {
            "name": PRODUCER_NAME,
            "version": PRODUCER_VERSION,
            "commit": producer_commit,
        },
        "source": _manifest_source(spec, source),
        "frame": frame,
        "grid": {
            "region_id": spec.region_id,
            "rows": rows,
            "columns": columns,
            "source_window": source_window,
            "sample_order": "row_major_north_to_south_west_to_east",
            "sample_stride": 1,
            "resampling": "none",
            "source_pixel_footprint_bounds": _projected_footprint(source, spec),
            "projected_sample_bounds_m": {
                **projected_bounds,
                "crs": "regional_frame",
                "frame_id": spec.frame_id,
                "meaning": "sample_centres",
            },
        },
        "payloads": {
            "horizontal_xy": {
                "path": "horizontal_xy.f64le",
                "encoding": "ieee754_binary64_le_easting_northing_pairs",
                "byte_length": len(horizontal_payload),
                "sha256": hashlib.sha256(horizontal_payload).hexdigest(),
            },
            "height_samples": {
                "path": "height_samples.f32le",
                "encoding": "ieee754_binary32_le_metres",
                "byte_length": len(height_payload),
                "sha256": hashlib.sha256(height_payload).hexdigest(),
            },
        },
        "generation": {
            "created_utc": _iso_utc_now(),
            "source_window": source_window,
            "resampling": "none",
            "tool_versions": {
                "python": ".".join(map(str, sys.version_info[:3])),
                "rasterio": rasterio.__version__,
                "gdal": rasterio.__gdal_version__,
                "proj": rasterio.__proj_version__,
            },
        },
    }
    manifest["artifact_id"] = artifact_id(manifest)
    return manifest


def _producer_commit(repo_root: Path | None = None) -> str:
    """Return HEAD only when the producer tree has no tracked or untracked changes."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]
    try:
        state = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if state.stdout.strip():
            raise ProducerError(
                "producer worktree is dirty; commit or remove tracked and untracked "
                "changes before generation"
            )
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except ProducerError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProducerError(f"cannot determine producer commit: {exc}") from exc
    commit = completed.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ProducerError("git did not return a full producer commit ID")
    return commit


def _reject_reparse(path: Path) -> None:
    is_junction = getattr(path, "is_junction", lambda: False)
    if path.is_symlink() or is_junction():
        raise ProducerError(f"refusing a symlink or reparse point: {path.name}")


def _write_exclusive(path: Path, data: bytes, created_paths: list[Path]) -> None:
    try:
        with path.open("xb") as output:
            created_paths.append(path)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise ProducerError(f"cannot create output file {path.name}: {exc}") from exc


def validate_bundle(bundle_dir: Path, schema_path: Path) -> dict[str, Any]:
    """Validate structure, schema, semantic dimensions, and exact payload bytes."""
    if not bundle_dir.is_dir():
        raise ProducerError(f"bundle directory does not exist: {bundle_dir}")
    _reject_reparse(bundle_dir)
    expected_names = {"manifest.json", "horizontal_xy.f64le", "height_samples.f32le"}
    actual_names = {entry.name for entry in bundle_dir.iterdir()}
    if actual_names != expected_names:
        raise ProducerError(f"bundle must contain exactly the v1 files: {sorted(actual_names)}")
    for path in bundle_dir.iterdir():
        _reject_reparse(path)
        if (
            not path.is_file()
            or path.resolve(strict=True).parent != bundle_dir.resolve(strict=True)
        ):
            raise ProducerError(f"bundle file must be a direct regular child: {path.name}")
    manifest_path = bundle_dir / "manifest.json"
    manifest = load_json_unique(manifest_path, max_bytes=MANIFEST_LIMIT_BYTES)
    try:
        schema = load_json_unique(schema_path, max_bytes=1024 * 1024)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(manifest), key=lambda err: list(err.absolute_path))
    except Exception as exc:
        raise ProducerError(f"schema validation setup failed: {exc}") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "manifest"
        raise ProducerError(f"schema validation failed at {location}: {error.message}")

    grid = manifest["grid"]
    rows, columns = grid["rows"], grid["columns"]
    if rows < 2 or columns < 2 or rows > 4096 or columns > 4096:
        raise ProducerError("grid rows/columns are outside the v1 semantic limits")
    if rows > MAX_SAMPLES // columns:
        raise ProducerError("grid sample count exceeds the v1 semantic limit")
    window = grid["source_window"]
    if window["rows"] != rows or window["columns"] != columns:
        raise ProducerError("grid and source-window dimensions differ")
    source = manifest["source"]
    raster = source["raster"]
    if (
        window["row_offset"] + rows > raster["height"]
        or window["column_offset"] + columns > raster["width"]
    ):
        raise ProducerError("source window lies outside source raster dimensions")
    origin = manifest["frame"]["origin"]
    if (
        not math.isfinite(origin["height_navd88_m"])
        or abs(origin["height_navd88_m"]) > MAX_SOURCE_HEIGHT_M
    ):
        raise ProducerError("H0 is outside the v1 source-height limit")

    payload_specs = (
        ("horizontal_xy", "horizontal_xy.f64le", 16),
        ("height_samples", "height_samples.f32le", 4),
    )
    payload_bytes: dict[str, bytes] = {}
    for key, filename, bytes_per_sample in payload_specs:
        expected_length = rows * columns * bytes_per_sample
        payload = manifest["payloads"][key]
        if payload["path"] != filename or payload["byte_length"] != expected_length:
            raise ProducerError(f"payload length or fixed path mismatch for {filename}")
        payload_bytes[key] = _read_manifest_payload_bytes(
            bundle_dir,
            manifest,
            key,
            filename,
            bytes_per_sample,
            purpose="bundle validation",
        )

    horizontal = np.frombuffer(payload_bytes["horizontal_xy"], dtype="<f8").reshape(
        rows * columns, 2
    )
    heights = np.frombuffer(payload_bytes["height_samples"], dtype="<f4")
    if not np.isfinite(horizontal).all() or np.greater(
        np.abs(horizontal), MAX_HORIZONTAL_COMPONENT_M
    ).any():
        raise ProducerError("horizontal payload contains invalid or out-of-range coordinates")
    validate_sample_array(heights, raster["nodata_value"], grid["region_id"])
    validate_relative_heights(heights, origin["height_navd88_m"], grid["region_id"])
    if frame_definition_sha256(manifest["frame"]) != manifest["frame_definition_sha256"]:
        raise ProducerError("frame definition hash mismatch")
    if artifact_id(manifest) != manifest["artifact_id"]:
        raise ProducerError("artifact ID mismatch")
    return manifest


def _validate_shared_column_bytes(
    west_payload: bytes,
    east_payload: bytes,
    rows: int,
    west_columns: int,
    east_columns: int,
    bytes_per_sample: int,
    label: str,
) -> None:
    for row in range(rows):
        west_start = (row * west_columns + west_columns - 1) * bytes_per_sample
        east_start = row * east_columns * bytes_per_sample
        if (
            west_payload[west_start : west_start + bytes_per_sample]
            != east_payload[east_start : east_start + bytes_per_sample]
        ):
            raise ProducerError(f"adjacent tile shared {label} differs at row {row}")


def _read_manifest_payload_bytes(
    bundle_dir: Path,
    manifest: dict[str, Any],
    payload_key: str,
    filename: str,
    bytes_per_sample: int,
    *,
    purpose: str,
) -> bytes:
    """Read bounded bytes and authenticate the exact content used by the caller."""
    try:
        rows = manifest["grid"]["rows"]
        columns = manifest["grid"]["columns"]
        payload = manifest["payloads"][payload_key]
        expected_length = payload["byte_length"]
        expected_sha256 = payload["sha256"]
    except (KeyError, TypeError) as exc:
        raise ProducerError(f"missing {payload_key} payload metadata") from exc

    if (
        type(rows) is not int
        or type(columns) is not int
        or rows < 2
        or columns < 2
        or rows > 4096
        or columns > 4096
        or rows > MAX_SAMPLES // columns
    ):
        raise ProducerError(f"invalid {payload_key} grid dimensions")
    if (
        type(expected_length) is not int
        or expected_length < 1
        or expected_length > MAX_SAMPLES * bytes_per_sample
    ):
        raise ProducerError(f"invalid {payload_key} payload byte length")
    if expected_length != rows * columns * bytes_per_sample:
        raise ProducerError(f"invalid {payload_key} payload length")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise ProducerError(f"invalid {payload_key} payload SHA-256")

    path = bundle_dir / filename
    try:
        _reject_reparse(path)
        with path.open("rb") as payload_file:
            if os.fstat(payload_file.fileno()).st_size != expected_length:
                raise ProducerError(
                    f"payload byte length changed during {purpose} for {filename}"
                )
            # The v1 sample cap bounds this read to 64 MiB + 1 byte for XY or
            # 16 MiB + 1 byte for heights; the extra byte detects post-fstat growth.
            payload_bytes = payload_file.read(expected_length + 1)
    except ProducerError:
        raise
    except OSError as exc:
        raise ProducerError(f"cannot read payload {filename}: {exc}") from exc

    if len(payload_bytes) != expected_length:
        raise ProducerError(
            f"payload byte length changed during {purpose} for {filename}"
        )
    if hashlib.sha256(payload_bytes).hexdigest() != expected_sha256:
        raise ProducerError(f"payload SHA-256 mismatch during {purpose} for {filename}")
    return payload_bytes


def validate_adjacent_tile_bundles(
    parent: RegionSpec,
    west_spec: RegionSpec,
    east_spec: RegionSpec,
    west_bundle: Path,
    east_bundle: Path,
    schema_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate v1 bundles and exact shared-frame, coordinate, and height seam bytes."""
    validate_adjacent_tile_specs(parent, west_spec, east_spec)
    west = validate_bundle(west_bundle, schema_path)
    east = validate_bundle(east_bundle, schema_path)
    if west["frame"] != east["frame"]:
        raise ProducerError("adjacent tile manifests do not have identical frame definitions")
    if west["frame_definition_sha256"] != east["frame_definition_sha256"]:
        raise ProducerError("adjacent tile frame definition hashes differ")
    if west["source"] != east["source"]:
        raise ProducerError("adjacent tiles do not identify the same verified source")
    if west["grid"]["region_id"] != east["grid"]["region_id"]:
        raise ProducerError("adjacent tile region identities differ")

    for manifest, spec in ((west, west_spec), (east, east_spec)):
        row, column, rows, columns = spec.source_window
        expected_window = {
            "row_offset": row,
            "column_offset": column,
            "rows": rows,
            "columns": columns,
        }
        if manifest["grid"]["source_window"] != expected_window:
            raise ProducerError(f"tile {spec.key} manifest window differs from its specification")

    west_row, _west_column, rows, west_columns = west_spec.source_window
    east_row, _east_column, east_rows, east_columns = east_spec.source_window
    if west_row != east_row or rows != east_rows:
        raise ProducerError("adjacent tile manifests do not have matching row ranges")
    west_xy = _read_manifest_payload_bytes(
        west_bundle,
        west,
        "horizontal_xy",
        "horizontal_xy.f64le",
        16,
        purpose="seam validation",
    )
    east_xy = _read_manifest_payload_bytes(
        east_bundle,
        east,
        "horizontal_xy",
        "horizontal_xy.f64le",
        16,
        purpose="seam validation",
    )
    west_heights = _read_manifest_payload_bytes(
        west_bundle,
        west,
        "height_samples",
        "height_samples.f32le",
        4,
        purpose="seam validation",
    )
    east_heights = _read_manifest_payload_bytes(
        east_bundle,
        east,
        "height_samples",
        "height_samples.f32le",
        4,
        purpose="seam validation",
    )
    _validate_shared_column_bytes(
        west_xy, east_xy, rows, west_columns, east_columns, 16, "Float64 XY"
    )
    _validate_shared_column_bytes(
        west_heights, east_heights, rows, west_columns, east_columns, 4, "NAVD88 height"
    )
    return west, east


def build_bundle(
    spec: RegionSpec,
    source_dir: Path,
    ledger_path: Path,
    schema_path: Path,
    output_root: Path,
    producer_commit: str,
) -> tuple[Path, dict[str, Any], dict[str, float], str]:
    """Build a bundle and return its serialized-manifest SHA for ownership tracking."""
    source, samples, h0 = _verify_raster_window(source_dir, ledger_path, spec)
    row_offset, column_offset, rows, columns = spec.source_window
    validate_relative_heights(samples, h0, spec.key)
    east, north, projected_bounds, _destination, pipeline, wkt2 = project_source_grid(
        spec, source, rows, columns
    )

    xy = np.empty((rows * columns, 2), dtype="<f8")
    xy[:, 0] = east.ravel(order="C")
    xy[:, 1] = north.ravel(order="C")
    horizontal_payload = xy.tobytes(order="C")
    height_payload = np.ascontiguousarray(samples, dtype="<f4").tobytes(order="C")
    manifest = _build_manifest(
        spec,
        source,
        h0,
        east,
        north,
        projected_bounds,
        pipeline,
        wkt2,
        horizontal_payload,
        height_payload,
        producer_commit,
    )

    try:
        serialized_manifest = json.dumps(
            manifest, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProducerError(f"cannot serialize manifest: {exc}") from exc
    if len(serialized_manifest) > MANIFEST_LIMIT_BYTES:
        raise ProducerError("manifest exceeds the 64 KiB contract limit")
    serialized_manifest_sha256 = hashlib.sha256(serialized_manifest).hexdigest()

    schema = load_json_unique(schema_path, max_bytes=1024 * 1024)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
    except Exception as exc:
        raise ProducerError(f"generated manifest does not satisfy schema: {exc}") from exc

    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProducerError(f"cannot create output root: {exc}") from exc
    _reject_reparse(output_root)
    bundle_dir = output_root / spec.key
    if bundle_dir.exists() or bundle_dir.is_symlink():
        raise ProducerError(f"refusing to overwrite existing bundle directory: {bundle_dir.name}")
    created_files: list[Path] = []
    created_bundle_dir = False
    try:
        bundle_dir.mkdir()
        created_bundle_dir = True
        _write_exclusive(bundle_dir / "horizontal_xy.f64le", horizontal_payload, created_files)
        _write_exclusive(bundle_dir / "height_samples.f32le", height_payload, created_files)
        _write_exclusive(bundle_dir / "manifest.json", serialized_manifest, created_files)
        validated = validate_bundle(bundle_dir, schema_path)
    except BaseException as exc:
        cleanup_failures: list[str] = []
        for created in created_files:
            try:
                created.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_failures.append(created.name)
        if created_bundle_dir:
            try:
                bundle_dir.rmdir()
            except OSError:
                cleanup_failures.append(bundle_dir.name)
        if cleanup_failures:
            raise ProducerError(
                f"bundle creation failed for {bundle_dir.name}; cleanup incomplete for "
                f"{', '.join(cleanup_failures)}; inspect that output directory"
            ) from exc
        raise

    result = {
        "region": spec.key,
        "bundle_directory_name": spec.key,
        "artifact_id": validated["artifact_id"],
        "frame_definition_sha256": validated["frame_definition_sha256"],
        "raster_sha256": source.raster_sha256,
        "metadata_sha256": source.xml_sha256,
        "product_record_sha256": source.record_sha256,
        "horizontal_payload_sha256": validated["payloads"]["horizontal_xy"]["sha256"],
        "height_payload_sha256": validated["payloads"]["height_samples"]["sha256"],
        "manifest_sha256": serialized_manifest_sha256,
        "source_height_min_m": source.raster_min,
        "source_height_max_m": source.raster_max,
        "acquired_at_utc": source.acquired_at_utc,
    }
    return bundle_dir, result, projected_bounds, serialized_manifest_sha256


def _remove_generated_bundle(
    bundle_dir: Path,
    expected_result: dict[str, Any],
    expected_manifest_sha256: str,
    schema_path: Path,
) -> None:
    """Remove only a complete bundle whose manifest and payload hashes still match."""
    manifest = validate_bundle(bundle_dir, schema_path)
    manifest_sha256, _manifest_size = sha256_file(
        bundle_dir / "manifest.json", max_bytes=MANIFEST_LIMIT_BYTES
    )
    if (
        manifest_sha256 != expected_manifest_sha256
        or manifest["artifact_id"] != expected_result["artifact_id"]
        or manifest["payloads"]["horizontal_xy"]["sha256"]
        != expected_result["horizontal_payload_sha256"]
        or manifest["payloads"]["height_samples"]["sha256"]
        != expected_result["height_payload_sha256"]
    ):
        raise ProducerError(
            f"preserving output directory {bundle_dir.name} because it changed after generation"
        )
    for filename in ("manifest.json", "horizontal_xy.f64le", "height_samples.f32le"):
        (bundle_dir / filename).unlink()
    bundle_dir.rmdir()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        required=True,
        type=Path,
        help="local-only USGS input snapshot directory",
    )
    parser.add_argument(
        "--ledger", required=True, type=Path, help="managed parent vertical-slice ledger"
    )
    parser.add_argument(
        "--schema", required=True, type=Path, help="managed parent terrain-grid v1 schema"
    )
    parser.add_argument(
        "--output-root", required=True, type=Path, help="directory for new bundle subdirectories"
    )
    parser.add_argument(
        "--region", choices=["both", *REGIONS, "golden-tiles"], default="both"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.source_dir.is_dir() or not args.ledger.is_file() or not args.schema.is_file():
        print("error: source directory, ledger, and schema must exist", file=sys.stderr)
        return 2
    try:
        producer_commit = _producer_commit()
        if args.region == "both":
            selected = list(REGIONS.values())
        elif args.region == "golden-tiles":
            selected = list(golden_adjacent_tile_specs())
        else:
            selected = [REGIONS[args.region]]
        results = []
        generated: list[tuple[Path, dict[str, Any], str]] = []
        try:
            for spec in selected:
                (
                    bundle_dir,
                    result,
                    projected_bounds,
                    expected_manifest_sha256,
                ) = build_bundle(
                    spec,
                    args.source_dir,
                    args.ledger,
                    args.schema,
                    args.output_root,
                    producer_commit,
                )
                # Record this invocation's ownership proof before the fallible file read.
                generated.append((bundle_dir, result, expected_manifest_sha256))
                manifest_sha256, _manifest_size = sha256_file(
                    bundle_dir / "manifest.json", max_bytes=MANIFEST_LIMIT_BYTES
                )
                if manifest_sha256 != expected_manifest_sha256:
                    raise ProducerError(
                        f"manifest changed after bundle creation for {spec.key}"
                    )
                result["projected_sample_bounds_m"] = projected_bounds
                results.append(result)
            if args.region == "golden-tiles":
                validate_adjacent_tile_bundles(
                    REGIONS["golden"],
                    selected[0],
                    selected[1],
                    generated[0][0],
                    generated[1][0],
                    args.schema,
                )
            print(json.dumps(results, indent=2, sort_keys=True))
        except BaseException as exc:
            rollback_failures: list[str] = []
            for bundle_dir, result, manifest_sha256 in reversed(generated):
                try:
                    _remove_generated_bundle(
                        bundle_dir, result, manifest_sha256, args.schema
                    )
                except (OSError, ProducerError, ValueError, KeyError, struct.error):
                    rollback_failures.append(bundle_dir.name)
            if rollback_failures:
                raise ProducerError(
                    f"generation failed: {exc}; rollback incomplete, preserved output(s): "
                    f"{', '.join(rollback_failures)}"
                ) from exc
            if generated and isinstance(exc, Exception):
                raise ProducerError(
                    f"generation failed: {exc}; rolled back "
                    f"{len(generated)} newly created bundle(s)"
                ) from exc
            raise
    except (ProducerError, OSError, ValueError, KeyError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

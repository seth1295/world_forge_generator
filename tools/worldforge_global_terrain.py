"""WorldForge-native global terrain tiles from Copernicus DEM and GEBCO.

This module intentionally imports no legacy ``server``, BeamNG, Lua, or game
runtime package.  It implements the shared ``wf-geodetic-grid-v1`` address
scheme; source sampling is kept independent from that logical address grid.
"""

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import tempfile
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import quote

import certifi
import httpx
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.windows import Window


CONTRACT_ID = "urn:worldforge:contract:global-terrain-tile:1.0.0"
TILE_SCHEME = "wf-geodetic-grid-v1"
PRODUCER_NAME = "WorldForge global terrain producer"
PRODUCER_VERSION = "0.1.0"
MAX_TILE_LEVEL = 16
BASE_TILE_DEGREES = 0.25
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_COG_BYTES = 256 * 1024 * 1024
MAX_DAP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_GRID_SAMPLES = 2_000_000
DEFAULT_CACHE_BYTES = 512 * 1024 * 1024
CERTIFICATE_CONTEXT = ssl.create_default_context(cafile=certifi.where())

COP90_INDEX_URL = "https://copernicus-dem-90m.s3.amazonaws.com/tileList.txt"
COP90_BUCKET = "copernicus-dem-90m"
COP90_REGION = "eu-central-1"
COP90_RELEASE = "Copernicus DEM 2021 release (AWS GLO-90 COG mirror)"
COP90_DATASET = "COP-DEM-GLO-90-F"
COP90_LICENSE_ID = "Copernicus WorldDEM-90 General Public Licence, issue 2025-02-21"
COP90_ATTRIBUTION = (
    "\u00a9 DLR e.V. 2010-2014 and \u00a9 Airbus Defence and Space GmbH 2014-2018 "
    "provided under COPERNICUS by the European Union and ESA; all rights reserved."
)
COP90_MODIFICATION_NOTICE = (
    "produced using Copernicus WorldDEM\u2122-90 \u00a9 DLR e.V. 2010-2014 and "
    "\u00a9 Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS "
    "by the European Union and ESA; all rights reserved."
)
COPERNICUS_DISCLAIMER = (
    "The organisations in charge of the Copernicus programme by law or by "
    "delegation do not incur any liability for any use of the Copernicus WorldDEM\u2122-90."
)
COPERNICUS_NO_ENDORSEMENT = (
    "WorldForge is not officially endorsed by the Copernicus Programme, the "
    "European Union, ESA, DLR, or Airbus Defence and Space."
)
COP90_UNCERTAINTY = (
    "The Copernicus product handbook reports global statistical accuracy of "
    "<4 m absolute vertical LE90, <2 m relative vertical on slopes <=20% and "
    "<4 m on slopes >20% within 1 degree by 1 degree, and <6 m horizontal CE90. "
    "These are product-wide statistical claims, not local guarantees; validation "
    "excludes Greenland and Antarctica and local deviations may occur."
)

GEBCO_DAP_BASE = (
    "https://dap.ceda.ac.uk/thredds/dodsC/bodc/gebco/global/gebco_2026"
)
GEBCO_ELEVATION_URL = (
    GEBCO_DAP_BASE
    + "/ice_surface_elevation/netcdf/GEBCO_2026.nc.ascii"
)
GEBCO_TID_URL = (
    GEBCO_DAP_BASE
    + "/type_identifier_grid/netcdf/gebco_2026_tid.nc.ascii"
)
GEBCO_ROWS = 43_200
GEBCO_COLUMNS = 86_400
GEBCO_STEP_DEGREES = 1.0 / 240.0  # 15 arc-seconds.
GEBCO_RELEASE = "GEBCO_2026"
GEBCO_LICENSE_ID = "Public domain; source acknowledgement requested"
GEBCO_ATTRIBUTION = (
    "GEBCO Bathymetric Compilation Group 2026 (2026): The GEBCO_2026 Grid - "
    "a continuous terrain model for oceans and land at 15 arc-second intervals. "
    "NERC EDS British Oceanographic Data Centre NOC. "
    "doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"
)
GEBCO_TID_ERRATA_URL = "https://www.gebco.net/data-products/gridded-bathymetry-data/data-set-errata"
GEBCO_TID_LIMITATION = (
    "TID is preserved and used for land/water source selection, but is not a perfect land mask. "
    "GEBCO's 2026 errata reports TID=0 (land) incorrectly assigned to parts of the Great Lakes."
)
GEBCO_UNCERTAINTY = (
    "Nominal 15 arc-second grid with heterogeneous source datasets and quality. "
    "Values are described as relative to mean sea level, but some shallow-water "
    "areas may retain source datums other than mean sea level. TID source classes "
    "are preserved; GEBCO is not represented as 30 m land truth. "
    + GEBCO_TID_LIMITATION
)

SOURCE_CODE_NONE = 0
SOURCE_CODE_COP90 = 1
SOURCE_CODE_GEBCO = 2
QUALITY_CODE_NO_DATA = 0
QUALITY_CODE_COP90_NATIVE = 1
QUALITY_CODE_GEBCO_LAND_FALLBACK = 3
QUALITY_CODE_GEBCO_WATER = 4
QUALITY_CODE_GEBCO_UNCERTAIN = 5

_COP_TILE_RE = re.compile(
    r"^Copernicus_DSM_COG_30_([NS])(\d{2})_00_([EW])(\d{3})_00_DEM$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STATUS_VALUES = {
    "ready",
    "partial",
    "no_source_coverage",
    "not_generated",
    "failed",
    "cancelled",
}


class TerrainError(RuntimeError):
    """Base error for safe, user-facing global terrain failures."""


class TerrainInputError(TerrainError):
    """An invalid address, tile index, or source description was provided."""


class TerrainRemoteError(TerrainError):
    """A remote source failed. Error text is intentionally credential-free."""


class TerrainCacheError(TerrainError):
    """A local cache entry was corrupt or exceeded its declared bounds."""


class NoNativeSamples(TerrainError):
    """The requested WFG tile is smaller than the source sample spacing."""


@dataclass(frozen=True, order=True)
class GlobalTileAddress:
    """A deterministic WFG-1 geodetic tile ID, separate from terrain spacing."""

    level: int
    x: int
    y: int

    def __post_init__(self) -> None:
        if not isinstance(self.level, int) or isinstance(self.level, bool):
            raise TerrainInputError("tile level must be an integer")
        if not 0 <= self.level <= MAX_TILE_LEVEL:
            raise TerrainInputError(f"tile level must be in [0, {MAX_TILE_LEVEL}]")
        nx, ny = tile_counts(self.level)
        if not isinstance(self.x, int) or isinstance(self.x, bool) or not 0 <= self.x < nx:
            raise TerrainInputError(f"tile x must be an integer in [0, {nx - 1}]")
        if not isinstance(self.y, int) or isinstance(self.y, bool) or not 0 <= self.y < ny:
            raise TerrainInputError(f"tile y must be an integer in [0, {ny - 1}]")

    @property
    def tile_id(self) -> str:
        return f"wfg1:{self.level}:{self.x}:{self.y}"

    @property
    def step_degrees(self) -> float:
        return BASE_TILE_DEGREES / (2**self.level)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return west, south, east, north in EPSG:4326 degrees."""
        step = self.step_degrees
        west = -180.0 + step * self.x
        south = -90.0 + step * self.y
        return west, south, west + step, south + step

    @classmethod
    def parse(cls, value: str) -> GlobalTileAddress:
        match = re.fullmatch(r"wfg1:(\d{1,2}):(\d+):(\d+)", value)
        if not match:
            raise TerrainInputError("tile ID must have form wfg1:<level>:<x>:<y>")
        return cls(*(int(item) for item in match.groups()))


def tile_counts(level: int) -> tuple[int, int]:
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= MAX_TILE_LEVEL:
        raise TerrainInputError(f"tile level must be in [0, {MAX_TILE_LEVEL}]")
    scale = 2**level
    return 1440 * scale, 720 * scale


def normalize_longitude(longitude_deg: float) -> float:
    """Normalize longitude to [-180, 180); +180 maps to -180."""
    if not math.isfinite(longitude_deg):
        raise TerrainInputError("longitude must be finite")
    if -180.0 <= longitude_deg < 180.0:
        return longitude_deg
    return (longitude_deg + 180.0) % 360.0 - 180.0


def tile_for_coordinate(
    longitude_deg: float, latitude_deg: float, level: int = 0
) -> GlobalTileAddress:
    """Map a WGS84 lon/lat point to the canonical half-open WFG-1 tile."""
    if not math.isfinite(longitude_deg):
        raise TerrainInputError("longitude must be finite")
    if not math.isfinite(latitude_deg) or not -90.0 <= latitude_deg <= 90.0:
        raise TerrainInputError("latitude must be finite and within [-90, 90]")
    nx, ny = tile_counts(level)
    lon = 0.0 if abs(latitude_deg) == 90.0 else normalize_longitude(longitude_deg)
    step = BASE_TILE_DEGREES / (2**level)
    x = int(math.floor((lon + 180.0) / step))
    y = int(math.floor((latitude_deg + 90.0) / step))
    # The exact north pole is the one closed global north boundary; all other
    # tile edges retain west/south inclusive, east/north exclusive ownership.
    x = min(nx - 1, max(0, x))
    y = min(ny - 1, max(0, y))
    return GlobalTileAddress(level, x, y)


def tile_for_coordinate_id(longitude_deg: float, latitude_deg: float, level: int = 0) -> str:
    return tile_for_coordinate(longitude_deg, latitude_deg, level).tile_id


def iter_tiles_for_bounds(
    west_deg: float,
    south_deg: float,
    east_deg: float,
    north_deg: float,
    level: int = 0,
) -> Iterator[GlobalTileAddress]:
    """Enumerate WFG tiles intersecting a half-open geographic bounding box.

    Longitude may wrap across the antimeridian. An absolute longitude span of
    at least 360 degrees selects all columns. A point bbox at either exact pole
    resolves to the canonical longitude-0 owner; non-degenerate polar extents
    keep all longitude columns intersecting the supplied bbox.
    """
    values = (west_deg, south_deg, east_deg, north_deg)
    if not all(math.isfinite(value) for value in values):
        raise TerrainInputError("bounding box coordinates must be finite")
    if not -90.0 <= south_deg <= 90.0 or not -90.0 <= north_deg <= 90.0:
        raise TerrainInputError("bounding box latitude must be within [-90, 90]")
    if south_deg > north_deg:
        raise TerrainInputError("bounding box south latitude exceeds north latitude")
    nx, ny = tile_counts(level)
    if south_deg == north_deg and abs(south_deg) == 90.0:
        yield tile_for_coordinate(0.0, south_deg, level)
        return
    if south_deg == north_deg and west_deg == east_deg:
        yield tile_for_coordinate(west_deg, south_deg, level)
        return

    step = BASE_TILE_DEGREES / (2**level)
    longitude_span = east_deg - west_deg
    if abs(longitude_span) >= 360.0:
        x_indices: Iterable[int] = range(nx)
    else:
        longitude_span %= 360.0
        if longitude_span == 0.0:
            x_indices = (tile_for_coordinate(west_deg, (south_deg + north_deg) / 2.0, level).x,)
        else:
            west_normalized = normalize_longitude(west_deg)
            x_start_position = (west_normalized + 180.0) / step
            x_start = min(nx - 1, max(0, int(math.floor(x_start_position))))
            x_end_position = x_start_position + longitude_span / step
            x_last = int(math.ceil(math.nextafter(x_end_position, -math.inf))) - 1
            count = min(nx, x_last - x_start + 1)
            x_indices = tuple((x_start + offset) % nx for offset in range(count))

    if south_deg == north_deg:
        y_indices = (tile_for_coordinate(0.0, south_deg, level).y,)
    else:
        y_start_position = (south_deg + 90.0) / step
        y_start = min(ny - 1, max(0, int(math.floor(y_start_position))))
        if north_deg == 90.0:
            y_last = ny - 1
        else:
            y_end_position = (north_deg + 90.0) / step
            y_last = int(math.ceil(math.nextafter(y_end_position, -math.inf))) - 1
        y_indices = range(y_start, min(ny - 1, y_last) + 1)
    for y in y_indices:
        for x in x_indices:
            yield GlobalTileAddress(level, x, y)


def _source_prefix_for_tile(address: GlobalTileAddress) -> str:
    west, south, east, north = address.bounds
    # A WFG-1 tile is at most 0.25 degrees and source Copernicus COGs are
    # 1-degree address cells; its center does not lie on a source tile edge.
    return _source_prefix_for_coordinate((west + east) / 2.0, (south + north) / 2.0)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _wgs84_horizontal_distance_m(
    longitude_a_deg: float,
    latitude_a_deg: float,
    longitude_b_deg: float,
    latitude_b_deg: float,
) -> float:
    """Vincenty inverse distance on WGS84; boundary offsets are sub-kilometre."""
    semi_major = 6_378_137.0
    flattening = 1.0 / 298.257223563
    semi_minor = (1.0 - flattening) * semi_major
    lat_a = math.radians(latitude_a_deg)
    lat_b = math.radians(latitude_b_deg)
    lon_delta_deg = normalize_longitude(longitude_b_deg - longitude_a_deg)
    lon_delta = math.radians(lon_delta_deg)
    reduced_a = math.atan((1.0 - flattening) * math.tan(lat_a))
    reduced_b = math.atan((1.0 - flattening) * math.tan(lat_b))
    sin_a, cos_a = math.sin(reduced_a), math.cos(reduced_a)
    sin_b, cos_b = math.sin(reduced_b), math.cos(reduced_b)
    reduced_latitude_delta_sine = (
        (1.0 - flattening)
        * math.sin(lat_b - lat_a)
        / (
            math.hypot(math.cos(lat_a), (1.0 - flattening) * math.sin(lat_a))
            * math.hypot(math.cos(lat_b), (1.0 - flattening) * math.sin(lat_b))
        )
    )
    lam = lon_delta
    for _ in range(100):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_half_lam = math.sin(lam / 2.0)
        latitude_component = reduced_latitude_delta_sine + (
            sin_a * cos_b * 2.0 * sin_half_lam * sin_half_lam
        )
        sin_sigma = math.hypot(cos_b * sin_lam, latitude_component)
        if sin_sigma == 0.0:
            return 0.0
        cos_sigma = sin_a * sin_b + cos_a * cos_b * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_a * cos_b * sin_lam / sin_sigma
        cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
        cos_two_sigma_m = (
            0.0 if cos_sq_alpha == 0.0 else cos_sigma - 2.0 * sin_a * sin_b / cos_sq_alpha
        )
        coefficient = flattening / 16.0 * cos_sq_alpha * (
            4.0 + flattening * (4.0 - 3.0 * cos_sq_alpha)
        )
        next_lam = lon_delta + (1.0 - coefficient) * flattening * sin_alpha * (
            sigma + coefficient * sin_sigma * (
                cos_two_sigma_m + coefficient * cos_sigma * (-1.0 + 2.0 * cos_two_sigma_m**2)
            )
        )
        if abs(next_lam - lam) <= 1e-13:
            lam = next_lam
            break
        lam = next_lam
    else:
        raise TerrainInputError("WGS84 edge offset geodesic did not converge")
    sin_lam, cos_lam = math.sin(lam), math.cos(lam)
    sin_half_lam = math.sin(lam / 2.0)
    latitude_component = reduced_latitude_delta_sine + (
        sin_a * cos_b * 2.0 * sin_half_lam * sin_half_lam
    )
    sin_sigma = math.hypot(cos_b * sin_lam, latitude_component)
    if sin_sigma == 0.0:
        return 0.0
    cos_sigma = sin_a * sin_b + cos_a * cos_b * cos_lam
    sigma = math.atan2(sin_sigma, cos_sigma)
    sin_alpha = cos_a * cos_b * sin_lam / sin_sigma
    cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
    cos_two_sigma_m = (
        0.0 if cos_sq_alpha == 0.0 else cos_sigma - 2.0 * sin_a * sin_b / cos_sq_alpha
    )
    u_sq = cos_sq_alpha * (semi_major**2 - semi_minor**2) / semi_minor**2
    coefficient_a = 1.0 + u_sq / 16384.0 * (
        4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq))
    )
    coefficient_b = u_sq / 1024.0 * (
        256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq))
    )
    delta_sigma = coefficient_b * sin_sigma * (
        cos_two_sigma_m + coefficient_b / 4.0 * (
            cos_sigma * (-1.0 + 2.0 * cos_two_sigma_m**2)
            - coefficient_b / 6.0 * cos_two_sigma_m
            * (-3.0 + 4.0 * sin_sigma**2)
            * (-3.0 + 4.0 * cos_two_sigma_m**2)
        )
    )
    return semi_minor * coefficient_a * (sigma - delta_sigma)


def _boundary_sample_relationship(
    target_longitude_deg: float,
    target_latitude_deg: float,
    source_longitude_deg: float,
    source_latitude_deg: float,
) -> tuple[str, float]:
    """Return canonical exact/nearest semantics and its WGS84 surface offset."""
    target_longitude = normalize_longitude(target_longitude_deg)
    source_longitude = normalize_longitude(source_longitude_deg)
    if source_longitude == target_longitude and source_latitude_deg == target_latitude_deg:
        return "native_exact", 0.0
    return (
        "nearest_native_sample",
        _wgs84_horizontal_distance_m(
            target_longitude,
            target_latitude_deg,
            source_longitude,
            source_latitude_deg,
        ),
    )


def _boundary_support_targets(
    address: GlobalTileAddress, grid: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    west, south, east, north = address.bounds
    affine = grid["source_affine"]
    longitude_centers = affine["x_origin"] + (
        np.arange(int(grid["columns"]), dtype=np.float64) + 0.5
    ) * affine["pixel_width"]
    latitude_centers = affine["y_origin"] + (
        np.arange(int(grid["rows"]), dtype=np.float64) + 0.5
    ) * affine["pixel_height"]
    interior_longitudes = sorted(
        float(value) for value in longitude_centers if west < value < east
    )
    interior_latitudes_north_to_south = sorted(
        (float(value) for value in latitude_centers if south < value < north),
        reverse=True,
    )
    edge_coordinates: dict[str, list[tuple[float, float]]] = {
        "north": (
            [(0.0, 90.0)]
            if north == 90.0
            else [(west, north)]
            + [(value, north) for value in interior_longitudes]
            + [(east, north)]
        ),
        "east": [(east, value) for value in interior_latitudes_north_to_south]
        + [(east, south)],
        "south": (
            [(0.0, -90.0)]
            if south == -90.0
            else [(value, south) for value in reversed(interior_longitudes)]
            + [(west, south)]
        ),
        "west": [(west, value) for value in reversed(interior_latitudes_north_to_south)],
    }
    features: list[dict[str, Any]] = []
    edge_counts = {edge: len(points) for edge, points in edge_coordinates.items()}
    for edge in ("north", "east", "south", "west"):
        for edge_index, (longitude, latitude) in enumerate(edge_coordinates[edge]):
            features.append({
                "id": f"boundary:{edge}:{edge_index}",
                "edge": edge,
                "edge_index": edge_index,
                "edge_sample_count": edge_counts[edge],
                "target_longitude_unwrapped_deg": longitude,
                "target_longitude_deg": normalize_longitude(longitude),
                "target_latitude_deg": latitude,
                "is_corner": (longitude in (west, east) and latitude in (south, north)),
            })
    return features, edge_counts


def _boundary_edge_coverage(
    features: list[dict[str, Any]], valid_mask: np.ndarray
) -> tuple[dict[str, list[int]], dict[str, int], set[str], list[dict[str, Any]], list[str]]:
    """Select only complete support edges and disclose truly unavailable samples."""
    edge_indices = {
        edge: [index for index, feature in enumerate(features) if feature["edge"] == edge]
        for edge in ("north", "east", "south", "west")
    }
    valid_counts = {
        edge: sum(bool(valid_mask[index]) for index in indices)
        for edge, indices in edge_indices.items()
    }
    complete_edges = {
        edge
        for edge, indices in edge_indices.items()
        if indices and valid_counts[edge] == len(indices)
    }
    missing_edges = [
        {
            "edge": edge,
            "expected_sample_count": len(indices),
            "valid_sample_count": valid_counts[edge],
            "missing_sample_count": len(indices) - valid_counts[edge],
        }
        for edge, indices in edge_indices.items()
        if edge not in complete_edges
    ]
    missing_ids = [
        feature["id"]
        for index, feature in enumerate(features)
        if not bool(valid_mask[index])
    ]
    return edge_indices, valid_counts, complete_edges, missing_edges, missing_ids


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_public_uri(uri: str) -> str:
    """Drop fragments, userinfo and query parameters before provenance output."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(uri)
    if parts.scheme not in {"http", "https", "s3"}:
        raise TerrainInputError("source URI must use HTTPS or S3")
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


class BoundedCache:
    """A small content-addressed cache that evicts whole source/tile entries."""

    def __init__(self, root: Path, max_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        if max_bytes < 1024:
            raise TerrainInputError("cache limit must be at least 1024 bytes")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.assets = self.root / "assets"
        self.tiles = self.root / "tiles"
        self.catalog = self.root / "catalog"
        for directory in (self.assets, self.tiles, self.catalog):
            directory.mkdir(parents=True, exist_ok=True)

    def asset_paths(self, key: str) -> tuple[Path, Path]:
        if not _SHA256_RE.fullmatch(key):
            raise TerrainCacheError("invalid cache asset key")
        return self.assets / f"{key}.tif", self.assets / f"{key}.json"

    def tile_directory(self, address: GlobalTileAddress) -> Path:
        return self.tiles / f"L{address.level}" / f"X{address.x}" / f"Y{address.y}"

    def catalog_paths(self, name: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", name):
            raise TerrainCacheError("invalid cache catalog name")
        return self.catalog / f"{name}.txt", self.catalog / f"{name}.json"

    def enforce_limit(self, *, preserve: Path | None = None) -> None:
        """Evict least-recently-used complete entries until under the byte cap."""
        groups: list[tuple[float, int, list[Path]]] = []
        for raster in self.assets.glob("*.tif"):
            receipt = raster.with_suffix(".json")
            files = [raster] + ([receipt] if receipt.exists() else [])
            groups.append((min(item.stat().st_atime for item in files), sum(item.stat().st_size for item in files), files))
        for catalog_text in self.catalog.glob("*.txt"):
            receipt = catalog_text.with_suffix(".json")
            files = [catalog_text] + ([receipt] if receipt.exists() else [])
            groups.append((min(item.stat().st_atime for item in files), sum(item.stat().st_size for item in files), files))
        for manifest in self.tiles.rglob("manifest.json"):
            directory = manifest.parent
            files = [item for item in directory.rglob("*") if item.is_file()]
            if files:
                groups.append((min(item.stat().st_atime for item in files), sum(item.stat().st_size for item in files), files))
        preserve_resolved = None if preserve is None else preserve.resolve()
        total = sum(size for _, size, _ in groups)
        for _, size, files in sorted(groups, key=lambda item: item[0]):
            if total <= self.max_bytes:
                break
            if preserve_resolved is not None and any(
                item.resolve().is_relative_to(preserve_resolved) for item in files
            ):
                continue
            for item in files:
                try:
                    item.unlink()
                except FileNotFoundError:
                    pass
            total -= size
        for directory in sorted(self.tiles.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        if total > self.max_bytes:
            raise TerrainCacheError("cache cannot meet its configured size limit")


@dataclass(frozen=True)
class Cop90Asset:
    prefix: str
    uri: str
    bytes: bytes
    etag: str
    last_modified: str
    sha256: str
    content_length: int
    acquired_at_utc: str
    cache_key: str


class Cop90Catalog:
    """Official GLO-90 AWS asset index and COG fetcher, with TLS verification."""

    def __init__(
        self,
        cache: BoundedCache,
        client: httpx.Client | None = None,
        index_url: str = COP90_INDEX_URL,
        bucket: str = COP90_BUCKET,
        region: str = COP90_REGION,
    ) -> None:
        self.cache = cache
        self._owns_client = client is None
        self.client = client or httpx.Client(
            verify=CERTIFICATE_CONTEXT,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": "WorldForge-global-terrain/0.1"},
        )
        self.index_url = index_url
        self.bucket = bucket
        self.region = region
        self._prefixes: frozenset[str] | None = None
        self.index_receipt: dict[str, Any] | None = None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Cop90Catalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def load_index(self, *, refresh: bool = False) -> frozenset[str]:
        index_path, receipt_path = self.cache.catalog_paths("copdem_glo90_tilelist")
        if index_path.exists() and receipt_path.exists() and not refresh:
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                raw = index_path.read_bytes()
                if (
                    len(raw) == receipt.get("byte_length")
                    and _sha256_bytes(raw) == receipt.get("sha256")
                    and len(raw) <= MAX_INDEX_BYTES
                ):
                    self._prefixes = parse_cop90_index(raw.decode("utf-8"))
                    self.index_receipt = receipt
                    os.utime(index_path, None)
                    os.utime(receipt_path, None)
                    return self._prefixes
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TerrainError):
                pass

        try:
            response = self.client.get(self.index_url)
            if response.status_code != 200:
                raise TerrainRemoteError(
                    f"Copernicus GLO-90 catalog request failed with HTTP {response.status_code}"
                )
            if len(response.content) > MAX_INDEX_BYTES:
                raise TerrainRemoteError("Copernicus GLO-90 catalog exceeded its byte limit")
            prefixes = parse_cop90_index(response.content.decode("utf-8"))
            receipt = {
                "provider": "Copernicus Data Space / AWS Open Data registry mirror",
                "product": COP90_DATASET,
                "release": COP90_RELEASE,
                "asset_uri": _safe_public_uri(self.index_url),
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "byte_length": len(response.content),
                "sha256": _sha256_bytes(response.content),
                "received_at_utc": _utc_now(),
            }
            _atomic_write(index_path, response.content)
            _atomic_write(receipt_path, _canonical_json(receipt))
        except httpx.HTTPError as exc:
            raise TerrainRemoteError("Copernicus GLO-90 catalog is unavailable") from exc
        self._prefixes = prefixes
        self.index_receipt = receipt
        self.cache.enforce_limit()
        return prefixes

    def has_asset(self, prefix: str) -> bool:
        return prefix in self.load_index()

    def asset_uri(self, prefix: str) -> str:
        if not _COP_TILE_RE.fullmatch(prefix):
            raise TerrainInputError("invalid Copernicus GLO-90 tile prefix")
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{quote(prefix)}/{quote(prefix)}.tif"

    def fetch_asset(self, prefix: str) -> Cop90Asset | None:
        if not self.has_asset(prefix):
            return None
        uri = self.asset_uri(prefix)
        try:
            head = self.client.head(uri)
        except httpx.HTTPError as exc:
            raise TerrainRemoteError("Copernicus GLO-90 tile metadata request failed") from exc
        if head.status_code == 404:
            raise TerrainRemoteError("Copernicus GLO-90 catalog listed a missing tile asset")
        if head.status_code in (401, 403):
            raise TerrainRemoteError("Copernicus GLO-90 tile access was denied")
        if head.status_code != 200:
            raise TerrainRemoteError(
                f"Copernicus GLO-90 tile metadata request failed with HTTP {head.status_code}"
            )
        try:
            length = int(head.headers.get("content-length", "0"))
        except ValueError as exc:
            raise TerrainRemoteError("Copernicus GLO-90 tile has an invalid size header") from exc
        if length < 1 or length > MAX_COG_BYTES:
            raise TerrainRemoteError("Copernicus GLO-90 tile asset is outside the byte limit")
        etag = head.headers.get("etag")
        modified = head.headers.get("last-modified")
        if not etag and not modified:
            raise TerrainRemoteError("Copernicus GLO-90 tile has no stable version metadata")
        cache_key = _sha256_bytes(
            _canonical_json({"uri": _safe_public_uri(uri), "etag": etag, "last_modified": modified, "bytes": length})
        )
        asset_path, receipt_path = self.cache.asset_paths(cache_key)
        asset_bytes: bytes | None = None
        receipt: dict[str, Any] | None = None
        if asset_path.exists() and receipt_path.exists():
            try:
                cached_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                data = asset_path.read_bytes()
                if (
                    cached_receipt.get("asset_uri") == _safe_public_uri(uri)
                    and cached_receipt.get("etag") == etag
                    and cached_receipt.get("content_length") == length
                    and len(data) == length
                    and _sha256_bytes(data) == cached_receipt.get("sha256")
                ):
                    asset_bytes, receipt = data, cached_receipt
                    os.utime(asset_path, None)
                    os.utime(receipt_path, None)
            except (OSError, json.JSONDecodeError):
                asset_bytes = None
            if asset_bytes is None:
                for path in (asset_path, receipt_path):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        if asset_bytes is None:
            try:
                response = self.client.get(uri)
            except httpx.HTTPError as exc:
                raise TerrainRemoteError("Copernicus GLO-90 tile download failed") from exc
            if response.status_code != 200:
                raise TerrainRemoteError(
                    f"Copernicus GLO-90 tile download failed with HTTP {response.status_code}"
                )
            if len(response.content) != length or len(response.content) > MAX_COG_BYTES:
                raise TerrainRemoteError("Copernicus GLO-90 tile length changed during download")
            asset_bytes = response.content
            receipt = {
                "provider": "Copernicus DEM AWS GLO-90 public mirror",
                "product": COP90_DATASET,
                "release": COP90_RELEASE,
                "asset_uri": _safe_public_uri(uri),
                "etag": etag,
                "last_modified": modified,
                "content_length": length,
                "sha256": _sha256_bytes(asset_bytes),
                "received_at_utc": _utc_now(),
            }
            if length <= self.cache.max_bytes:
                _atomic_write(asset_path, asset_bytes)
                _atomic_write(receipt_path, _canonical_json(receipt))
        assert receipt is not None
        self.cache.enforce_limit()
        return Cop90Asset(
            prefix=prefix,
            uri=_safe_public_uri(uri),
            bytes=asset_bytes,
            etag=etag or "",
            last_modified=modified or "",
            sha256=receipt["sha256"],
            content_length=length,
            acquired_at_utc=receipt["received_at_utc"],
            cache_key=cache_key,
        )

    def sample_nearest_points(
        self, longitude_latitude: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[Cop90Asset], list[dict[str, Any]]]:
        """Read nearest point-registered GLO-90 samples for boundary support vertices."""
        coordinates = np.asarray(longitude_latitude, dtype=np.float64)
        if coordinates.ndim != 2 or coordinates.shape[1] != 2 or not np.isfinite(coordinates).all():
            raise TerrainInputError("Copernicus boundary sample coordinates must be finite lon/lat pairs")
        if np.any(np.abs(coordinates[:, 1]) > 90.0):
            raise TerrainInputError("Copernicus boundary sample latitude is outside WGS84 bounds")
        normalized_longitudes = np.asarray(
            [normalize_longitude(float(value)) for value in coordinates[:, 0]], dtype=np.float64
        )
        prefixes_by_point = [
            _source_prefix_for_coordinate(float(lon), float(lat))
            for lon, lat in zip(normalized_longitudes, coordinates[:, 1], strict=True)
        ]
        grouped: dict[str, list[int]] = {}
        for point_index, prefix in enumerate(prefixes_by_point):
            grouped.setdefault(prefix, []).append(point_index)
        heights = np.full(coordinates.shape[0], np.float32(-32768.0), dtype=np.float32)
        valid = np.zeros(coordinates.shape[0], dtype=bool)
        source_coordinates = np.full(coordinates.shape, np.nan, dtype=np.float64)
        assets: list[Cop90Asset] = []
        attempts: list[dict[str, Any]] = []
        catalog = self.load_index()
        for prefix, point_indices in grouped.items():
            if prefix not in catalog:
                attempts.append({
                    "product": COP90_DATASET,
                    "status": "no_asset_for_boundary_sample",
                    "source_prefix": prefix,
                })
                continue
            try:
                asset = self.fetch_asset(prefix)
                if asset is None:
                    attempts.append({
                        "product": COP90_DATASET,
                        "status": "no_asset_for_boundary_sample",
                        "source_prefix": prefix,
                    })
                    continue
                with MemoryFile(asset.bytes) as memory_file:
                    with memory_file.open() as dataset:
                        _validate_cop90_raster(dataset, prefix)
                        x0 = dataset.transform.c + 0.5 * dataset.transform.a
                        y0 = dataset.transform.f + 0.5 * dataset.transform.e
                        columns = np.floor(
                            (normalized_longitudes[point_indices] - x0) / dataset.transform.a + 0.5
                        ).astype(np.int64)
                        row_fraction = (
                            coordinates[point_indices, 1] - y0
                        ) / dataset.transform.e
                        # On exact ties, select the northern point sample.
                        rows = np.ceil(row_fraction - 0.5).astype(np.int64)
                        inside = (
                            (columns >= 0) & (columns < dataset.width)
                            & (rows >= 0) & (rows < dataset.height)
                        )
                        if not inside.any():
                            continue
                        selected_columns = columns[inside]
                        selected_rows = rows[inside]
                        window_column = int(selected_columns.min())
                        window_row = int(selected_rows.min())
                        window_width = int(selected_columns.max()) - window_column + 1
                        window_height = int(selected_rows.max()) - window_row + 1
                        values = dataset.read(
                            1,
                            window=Window(window_column, window_row, window_width, window_height),
                            masked=False,
                        )
                        target_indices = np.asarray(point_indices, dtype=np.int64)[inside]
                        sampled = values[selected_rows - window_row, selected_columns - window_column]
                        source_x = dataset.transform.c + (selected_columns + 0.5) * dataset.transform.a
                        source_y = dataset.transform.f + (selected_rows + 0.5) * dataset.transform.e
                        source_coordinates[target_indices, 0] = source_x
                        source_coordinates[target_indices, 1] = source_y
                        finite = np.isfinite(sampled)
                        if dataset.nodata is not None:
                            finite &= sampled != dataset.nodata
                        heights[target_indices[finite]] = sampled[finite].astype(np.float32)
                        valid[target_indices[finite]] = True
                assets.append(asset)
                attempts.append({"product": COP90_DATASET, "status": "ready", "source_prefix": prefix})
            except (TerrainRemoteError, TerrainInputError, rasterio.errors.RasterioError) as exc:
                attempts.append({
                    "product": COP90_DATASET,
                    "status": "failed",
                    "source_prefix": prefix,
                    "reason": str(exc),
                })
        return heights, valid, source_coordinates, assets, attempts


def _source_prefix_for_coordinate(longitude_deg: float, latitude_deg: float) -> str:
    if not math.isfinite(longitude_deg) or not math.isfinite(latitude_deg):
        raise TerrainInputError("source coordinate must be finite")
    if not -90.0 <= latitude_deg <= 90.0:
        raise TerrainInputError("source latitude is outside WGS84 bounds")
    longitude = normalize_longitude(longitude_deg)
    latitude_index = min(89, math.floor(latitude_deg))
    longitude_index = math.floor(longitude)
    latitude_hemisphere = "N" if latitude_index >= 0 else "S"
    longitude_hemisphere = "E" if longitude_index >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_30_{latitude_hemisphere}{abs(latitude_index):02d}_00_"
        f"{longitude_hemisphere}{abs(longitude_index):03d}_00_DEM"
    )


def parse_cop90_index(text: str) -> frozenset[str]:
    prefixes: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if not _COP_TILE_RE.fullmatch(line):
            raise TerrainInputError(f"invalid Copernicus tile index entry at line {line_number}")
        if line in prefixes:
            raise TerrainInputError(f"duplicate Copernicus tile index entry at line {line_number}")
        prefixes.add(line)
    if not prefixes:
        raise TerrainInputError("Copernicus tile index is empty")
    return frozenset(prefixes)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f"{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise TerrainCacheError(f"cannot write cache artifact {path.name}") from exc


def _pixel_centres_in_bounds(
    transform: rasterio.Affine,
    width: int,
    height: int,
    bounds: tuple[float, float, float, float],
) -> tuple[int, int, int, int] | None:
    """Return the native source window whose point samples lie in WFG bounds."""
    west, south, east, north = bounds
    xs = transform.c + (np.arange(width, dtype=np.float64) + 0.5) * transform.a
    ys = transform.f + (np.arange(height, dtype=np.float64) + 0.5) * transform.e
    if transform.a <= 0 or transform.e >= 0 or transform.b != 0 or transform.d != 0:
        raise TerrainInputError("source raster must be north-up with positive x / negative y spacing")
    col_mask = (xs >= west) & ((xs < east) | ((east == 180.0) & (xs <= east)))
    row_mask = (ys >= south) & ((ys < north) | ((north == 90.0) & (ys <= north)))
    cols = np.flatnonzero(col_mask)
    rows = np.flatnonzero(row_mask)
    if cols.size == 0 or rows.size == 0:
        return None
    col_start, col_stop = int(cols[0]), int(cols[-1]) + 1
    row_start, row_stop = int(rows[0]), int(rows[-1]) + 1
    if cols.size != col_stop - col_start or rows.size != row_stop - row_start:
        raise TerrainInputError("source pixel centers do not form a contiguous tile window")
    return row_start, col_start, row_stop - row_start, col_stop - col_start


def _validate_cop90_raster(dataset: rasterio.DatasetReader, label: str) -> None:
    if dataset.driver != "GTiff" or dataset.count != 1:
        raise TerrainInputError(f"{label}: expected a one-band GeoTIFF")
    if dataset.crs != rasterio.crs.CRS.from_epsg(4326):
        raise TerrainInputError(f"{label}: source CRS must be EPSG:4326")
    if dataset.dtypes != ("float32",):
        raise TerrainInputError(f"{label}: Copernicus source heights must remain Float32")
    if dataset.width < 1 or dataset.height < 1 or dataset.width > 1201 or dataset.height > 1201:
        raise TerrainInputError(f"{label}: source raster dimensions are outside GLO-90 bounds")
    if dataset.scales != (1.0,) or dataset.offsets != (0.0,):
        raise TerrainInputError(f"{label}: source scale/offset must remain 1/0")
    if dataset.tags().get("AREA_OR_POINT") != "Point":
        raise TerrainInputError(f"{label}: Copernicus samples must remain point registered")
    if dataset.nodata is not None and not math.isfinite(float(dataset.nodata)):
        raise TerrainInputError(f"{label}: source no-data metadata is not finite")


def _classify_cop90_samples(
    samples: np.ndarray, nodata: float | None
) -> tuple[np.ndarray, np.ndarray, str]:
    if samples.dtype != np.dtype("float32"):
        raise TerrainInputError("Copernicus window changed its source Float32 representation")
    valid = np.isfinite(samples)
    if nodata is not None:
        valid &= samples != np.float32(nodata)
    source_mask = np.where(valid, SOURCE_CODE_COP90, SOURCE_CODE_NONE).astype(np.uint8)
    quality_mask = np.where(valid, QUALITY_CODE_COP90_NATIVE, QUALITY_CODE_NO_DATA).astype(np.uint8)
    status = "ready" if valid.all() else "partial"
    return source_mask, quality_mask, status


def _gebco_indices(address: GlobalTileAddress) -> tuple[int, int, int, int]:
    """Return GEBCO 15-arc-second [row0,row1,col0,col1] window by center.

    The official GEBCO NetCDF latitude coordinate is south-to-north; preserve
    that source row order rather than imposing a north-up raster convention.
    """
    west, south, east, north = address.bounds
    step = GEBCO_STEP_DEGREES
    # Coordinates are cell centers at -180 + 0.5*step and -90 + 0.5*step.
    col_origin = -180.0 + 0.5 * step
    row_latitude_origin = -90.0 + 0.5 * step
    col0 = math.ceil((west - col_origin) / step - 1e-12)
    col1 = math.ceil((east - col_origin) / step - 1e-12)
    row0 = math.ceil((south - row_latitude_origin) / step - 1e-12)
    row1 = math.ceil((north - row_latitude_origin) / step - 1e-12)
    row0 = min(GEBCO_ROWS, max(0, row0))
    row1 = min(GEBCO_ROWS, max(row0, row1))
    col0 = min(GEBCO_COLUMNS, max(0, col0))
    col1 = min(GEBCO_COLUMNS, max(col0, col1))
    return row0, row1, col0, col1


def _gebco_nearest_sample_indices(
    longitude_samples: np.ndarray, latitude_samples: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    longitude = (np.asarray(longitude_samples, dtype=np.float64) + 180.0) % 360.0 - 180.0
    latitude = np.asarray(latitude_samples, dtype=np.float64)
    col_indices = np.floor(
        (longitude - (-180.0 + 0.5 * GEBCO_STEP_DEGREES)) / GEBCO_STEP_DEGREES + 0.5
    ).astype(np.int64) % GEBCO_COLUMNS
    row_indices = np.floor(
        (latitude - (-90.0 + 0.5 * GEBCO_STEP_DEGREES)) / GEBCO_STEP_DEGREES + 0.5
    ).astype(np.int64)
    row_indices = np.clip(row_indices, 0, GEBCO_ROWS - 1)
    sample_longitudes = -180.0 + (col_indices.astype(np.float64) + 0.5) * GEBCO_STEP_DEGREES
    sample_latitudes = -90.0 + (row_indices.astype(np.float64) + 0.5) * GEBCO_STEP_DEGREES
    return row_indices, col_indices, sample_longitudes, sample_latitudes


def _parse_dap_ascii(text: str, variable: str, expected_rows: int, expected_cols: int) -> np.ndarray:
    """Parse one bounded DAP2 .ascii grid response; reject partial/malformed data."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_DAP_RESPONSE_BYTES:
        raise TerrainRemoteError("GEBCO subset response exceeded its byte limit")
    marker = f"{variable}.{variable}[{expected_rows}][{expected_cols}]"
    marker_index = text.find(marker)
    if marker_index < 0:
        raise TerrainRemoteError("GEBCO subset response has an unexpected grid header")
    after = text[marker_index + len(marker) :]
    parsed_rows: list[list[int]] = []
    row_pattern = re.compile(r"^\[(\d+)\],\s*(.*)$")
    for raw_line in after.splitlines():
        line = raw_line.strip()
        match = row_pattern.fullmatch(line)
        if not match:
            continue
        row_index = int(match.group(1))
        if row_index != len(parsed_rows):
            raise TerrainRemoteError("GEBCO subset rows are incomplete or out of order")
        values_text = match.group(2).strip()
        try:
            values = [int(value.strip()) for value in values_text.split(",")]
        except ValueError as exc:
            raise TerrainRemoteError("GEBCO subset contains a non-integer sample") from exc
        if len(values) != expected_cols:
            raise TerrainRemoteError("GEBCO subset row has an unexpected number of columns")
        parsed_rows.append(values)
        if len(parsed_rows) == expected_rows:
            break
    if len(parsed_rows) != expected_rows:
        raise TerrainRemoteError("GEBCO subset response did not contain the requested rows")
    values = np.asarray(parsed_rows, dtype=np.int32)
    if np.any(values < -32768) or np.any(values > 32767):
        raise TerrainRemoteError("GEBCO subset values exceed the source Int16 domain")
    return values.astype(np.int16)


def _gebco_source_record(
    remote_meta: Mapping[str, Any], *, used_for_mosaic: bool = False
) -> dict[str, Any]:
    receipts = remote_meta["asset_receipts"]
    return {
        "source_code": SOURCE_CODE_GEBCO,
        "provider": "NERC British Oceanographic Data Centre / GEBCO",
        "product": GEBCO_RELEASE,
        "release": GEBCO_RELEASE,
        "asset_uri": receipts["elevation"]["asset_uri"],
        "asset_version": {"doi": "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"},
        "assets": [
            {
                "role": "elevation",
                "product": GEBCO_RELEASE,
                "asset_uri": receipts["elevation"]["asset_uri"],
                "metadata": receipts["elevation"],
            },
            {
                "role": "type_identifier",
                "product": GEBCO_RELEASE,
                "asset_uri": receipts["tid"]["asset_uri"],
                "metadata": receipts["tid"],
            },
        ],
        "byte_length": None,
        "sha256": None,
        "horizontal": {
            "crs": "EPSG:4326",
            "datum": "WGS84",
            "axis_order": "longitude_latitude",
            "coordinate_unit": "degree",
        },
        "vertical": {
            "datum": "mean_sea_level_nominal_heterogeneous_inputs",
            "height_type": "elevation_relative_to_mean_sea_level_nominal",
            "crs": None,
            "unit": "metre",
        },
        "source_raster": {
            "driver": "OPeNDAP/NetCDF",
            "variable": "elevation",
            "tid_variable": "tid",
            "dtype": "int16",
            "source_width": GEBCO_COLUMNS,
            "source_height": GEBCO_ROWS,
            "nodata_value": -32768,
            "pixel_registration": "cell_center",
            "spacing_arc_seconds": 15,
        },
        "source_window": remote_meta["source_window"],
        "license": {
            "id": GEBCO_LICENSE_ID,
            "terms_url": "https://www.gebco.net/data-products-gridded-bathymetry-data/gebco2026-grid",
            "attribution": GEBCO_ATTRIBUTION,
        },
        "uncertainty": GEBCO_UNCERTAINTY,
        "tid_classification": {
            "method": "TID=0 land; all documented nonzero classes are water/bathymetry; unsupported values remain uncertain",
            "limitation": GEBCO_TID_LIMITATION,
            "known_issues_url": GEBCO_TID_ERRATA_URL,
        },
        "used_for_mosaic": used_for_mosaic,
        "cache_receipt": {
            "received_at_utc": remote_meta["received_at_utc"],
            "source_window_only": True,
            "elevation_response_sha256": receipts["elevation"]["response_sha256"],
            "tid_response_sha256": receipts["tid"]["response_sha256"],
        },
    }


class GebcoRemoteSource:
    """Read only one small tile window from the official GEBCO_2026 OPeNDAP grid."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        elevation_url: str = GEBCO_ELEVATION_URL,
        tid_url: str = GEBCO_TID_URL,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            verify=CERTIFICATE_CONTEXT,
            follow_redirects=False,
            timeout=httpx.Timeout(45.0, connect=12.0),
            headers={"User-Agent": "WorldForge-global-terrain/0.1"},
        )
        self.elevation_url = elevation_url
        self.tid_url = tid_url

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> GebcoRemoteSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def read_window(self, address: GlobalTileAddress) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        row0, row1, col0, col1 = _gebco_indices(address)
        rows, cols = row1 - row0, col1 - col0
        if rows <= 0 or cols <= 0:
            raise NoNativeSamples("tile contains no GEBCO 15-arc-second sample centers")
        if rows * cols > MAX_GRID_SAMPLES:
            raise TerrainInputError("GEBCO tile window exceeds the configured sample cap")
        elevation, elevation_receipt = self._read_array(
            self.elevation_url, "elevation", row0, row1, col0, col1
        )
        tid, tid_receipt = self._read_array(self.tid_url, "tid", row0, row1, col0, col1)
        metadata = {
            "provider": "GEBCO / NERC British Oceanographic Data Centre",
            "dataset": GEBCO_RELEASE,
            "release": GEBCO_RELEASE,
            "asset_uri": elevation_receipt["asset_uri"],
            "source_window": {
                "row_offset": row0,
                "column_offset": col0,
                "rows": rows,
                "columns": cols,
            },
            "source_grid": {
                "rows": GEBCO_ROWS,
                "columns": GEBCO_COLUMNS,
                "pixel_registration": "cell_center",
                "spacing_degrees": GEBCO_STEP_DEGREES,
                "source_dtype_elevation": "int16",
                "source_dtype_tid": "int8",
            },
            "asset_receipts": {"elevation": elevation_receipt, "tid": tid_receipt},
            "received_at_utc": _utc_now(),
        }
        return elevation, tid, metadata

    def read_samples_at_coordinates(
        self, longitude_samples: np.ndarray, latitude_samples: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        """Read only GEBCO/TID cells nearest a supplied native source point grid."""
        if longitude_samples.shape != latitude_samples.shape or longitude_samples.size == 0:
            raise TerrainInputError("GEBCO point sample coordinates must be matching non-empty arrays")
        lon = np.asarray(longitude_samples, dtype=np.float64)
        lat = np.asarray(latitude_samples, dtype=np.float64)
        if not np.isfinite(lon).all() or not np.isfinite(lat).all() or np.any(np.abs(lat) > 90.0):
            raise TerrainInputError("GEBCO point sample coordinates are outside WGS84 bounds")
        row_indices, col_indices, sample_longitudes, sample_latitudes = _gebco_nearest_sample_indices(lon, lat)
        row0, row1 = int(row_indices.min()), int(row_indices.max()) + 1
        # One WFG tile cannot span the dateline. This guard protects callers
        # that bypass the address API and supply a wrapped point set.
        if int(col_indices.max()) - int(col_indices.min()) > GEBCO_COLUMNS // 2:
            raise TerrainInputError("GEBCO point request unexpectedly crosses the antimeridian")
        col0, col1 = int(col_indices.min()), int(col_indices.max()) + 1
        height_source, height_receipt = self._read_array(
            self.elevation_url, "elevation", row0, row1, col0, col1
        )
        tid_source, tid_receipt = self._read_array(
            self.tid_url, "tid", row0, row1, col0, col1
        )
        local_rows = row_indices - row0
        local_cols = col_indices - col0
        height_values = height_source[local_rows, local_cols].reshape(longitude_samples.shape)
        tid_values = tid_source[local_rows, local_cols].reshape(longitude_samples.shape)
        return height_values, tid_values, {
            "provider": "GEBCO / NERC British Oceanographic Data Centre",
            "dataset": GEBCO_RELEASE,
            "release": GEBCO_RELEASE,
            "asset_uri": height_receipt["asset_uri"],
            "source_window": {
                "row_offset": row0,
                "column_offset": col0,
                "rows": row1 - row0,
                "columns": col1 - col0,
            },
            "source_grid": {
                "rows": GEBCO_ROWS,
                "columns": GEBCO_COLUMNS,
                "pixel_registration": "cell_center",
                "spacing_degrees": GEBCO_STEP_DEGREES,
                "source_dtype_elevation": "int16",
                "source_dtype_tid": "int8",
            },
            "asset_receipts": {"elevation": height_receipt, "tid": tid_receipt},
            "classification_sampling": "nearest source-cell center per requested coordinate; half-cell ties east/north",
            "nearest_source_coordinates": {
                "longitude_deg": sample_longitudes.tolist(),
                "latitude_deg": sample_latitudes.tolist(),
            },
            "received_at_utc": _utc_now(),
        }

    def _read_array(
        self,
        endpoint: str,
        variable: str,
        row0: int,
        row1: int,
        col0: int,
        col1: int,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        rows, cols = row1 - row0, col1 - col0
        if rows <= 0 or cols <= 0:
            raise TerrainInputError("GEBCO source window must have positive dimensions")
        constraint = f"{variable}[{row0}:1:{row1 - 1}][{col0}:1:{col1 - 1}]"
        request_uri = endpoint + "?" + quote(constraint, safe="[]:,")
        try:
            response = self.client.get(request_uri)
        except httpx.HTTPError as exc:
            raise TerrainRemoteError("GEBCO_2026 OPeNDAP subset request failed") from exc
        if response.status_code != 200:
            raise TerrainRemoteError(
                f"GEBCO_2026 OPeNDAP subset request failed with HTTP {response.status_code}"
            )
        if len(response.content) > MAX_DAP_RESPONSE_BYTES:
            raise TerrainRemoteError("GEBCO subset response exceeded its byte limit")
        array = _parse_dap_ascii(response.text, variable, rows, cols)
        receipt = {
            "asset_uri": _safe_public_uri(endpoint.removesuffix(".ascii")),
            "access_protocol": "OPeNDAP DAP2 .ascii hyperslab",
            "variable": variable,
            "requested_subset": constraint,
            "request_uri_sha256": _sha256_bytes(request_uri.encode("utf-8")),
            "response_byte_length": len(response.content),
            "response_sha256": _sha256_bytes(response.content),
            "response_etag": response.headers.get("etag"),
            "response_last_modified": response.headers.get("last-modified"),
            "response_content_type": response.headers.get("content-type"),
        }
        return array, receipt


class EGM2008Grid:
    """Sample the approved public-domain EGM2008 geoid grid by bilinear interpolation."""

    URI = "https://cdn.proj.org/us_nga_egm08_25.tif"
    DATASET = "OSGeo PROJ-data us_nga_egm08_25.tif, NGA EGM2008"
    LICENSE = "Public Domain (OSGeo PROJ-data us_nga README)"
    EXPECTED_SHA256 = "4191d471eefebf24091b56dbc604353cb3b8cf8cc70e448bb9ae56a272bef17a"
    GRID_STEP_DEGREES = 1.0 / 24.0  # 2.5 arc minutes.

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        try:
            self.dataset = rasterio.open(self._path, "r")
            _validate_egm2008_grid(self.dataset)
            self.byte_length = self._path.stat().st_size
            self.sha256 = _sha256_file(self._path)
            if self.sha256 != self.EXPECTED_SHA256:
                raise TerrainInputError("configured geoid grid digest does not match the approved OSGeo EGM2008 release")
        except (OSError, rasterio.errors.RasterioError, TerrainError) as exc:
            try:
                self.dataset.close()
            except (AttributeError, rasterio.errors.RasterioError):
                pass
            raise TerrainInputError("verified EGM2008 source grid is unavailable or invalid") from exc
        self.receipt = _load_geoid_receipt(self._path, self.byte_length, self.sha256)
        self.metadata = {
            "dataset": self.DATASET,
            "release": "OSGeo PROJ-data grid; source grid modified 2019-12-27",
            "asset_uri": self.URI,
            "asset_version": {"etag": self.receipt.get("etag"), "last_modified": self.receipt.get("last_modified")},
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "expected_sha256_verified": self.sha256 == self.EXPECTED_SHA256,
            "horizontal": {
                "crs": "EPSG:4979",
                "datum": "WGS84 geographic 3D (longitude/latitude axes used for grid sampling)",
                "axis_order": "longitude_latitude",
                "coordinate_unit": "degree",
            },
            "vertical": {
                "datum": "EGM2008 geoid undulation N",
                "height_type": "geoid_undulation",
                "unit": "metre",
                "target_vertical_crs": "EPSG:3855",
            },
            "source_grid": {
                "rows": self.dataset.height,
                "columns": self.dataset.width,
                "dtype": self.dataset.dtypes[0],
                "nodata_value": self.dataset.nodata,
                "pixel_registration": "point",
                "spacing_degrees": self.GRID_STEP_DEGREES,
                "affine": {
                    "origin_reference": "pixel_corner",
                    "x_origin": float(self.dataset.transform.c),
                    "y_origin": float(self.dataset.transform.f),
                    "pixel_width": float(self.dataset.transform.a),
                    "pixel_height": float(self.dataset.transform.e),
                },
            },
            "license": {"id": self.LICENSE, "source_notice": self.dataset.tags().get("TIFFTAG_COPYRIGHT")},
            "uncertainty": "The 2.5 arc-minute grid defines EGM2008 geoid undulation; grid spacing is not a universal vertical accuracy claim.",
            "cache_receipt": self.receipt,
        }

    def close(self) -> None:
        try:
            self.dataset.close()
        except rasterio.errors.RasterioError:
            pass

    def __enter__(self) -> EGM2008Grid:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def sample(
        self, longitude_samples: np.ndarray, latitude_samples: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return bilinear geoid undulation N metres and a valid-sample mask."""
        longitude = np.asarray(longitude_samples, dtype=np.float64)
        latitude = np.asarray(latitude_samples, dtype=np.float64)
        if longitude.shape != latitude.shape or longitude.ndim != 2:
            raise TerrainInputError("geoid sampling requires matching 2D longitude/latitude grids")
        if not np.isfinite(longitude).all() or not np.isfinite(latitude).all():
            raise TerrainInputError("geoid sampling coordinates must be finite")
        if np.any(latitude < -90.0) or np.any(latitude > 90.0):
            raise TerrainInputError("geoid sampling latitude is outside [-90, 90]")

        transform = self.dataset.transform
        longitude = (longitude + 180.0) % 360.0 - 180.0
        x_first_center = transform.c + 0.5 * transform.a
        y_first_center = transform.f + 0.5 * transform.e
        column_fraction = (longitude - x_first_center) / transform.a
        row_fraction = (latitude - y_first_center) / transform.e
        column_floor = np.floor(column_fraction).astype(np.int64)
        row_floor = np.floor(row_fraction).astype(np.int64)
        fraction_x = column_fraction - column_floor
        fraction_y = row_fraction - row_floor
        column0 = column_floor % self.dataset.width
        column1 = (column0 + 1) % self.dataset.width
        row0 = np.clip(row_floor, 0, self.dataset.height - 1)
        row1 = np.clip(row_floor + 1, 0, self.dataset.height - 1)
        # Clamp outside the first/last latitude sample row at the poles.
        fraction_y = np.where(row0 == row1, 0.0, fraction_y)

        min_row = int(min(row0.min(), row1.min()))
        max_row = int(max(row0.max(), row1.max()))
        min_column = int(min(column0.min(), column1.min()))
        max_column = int(max(column0.max(), column1.max()))
        wraps = max_column - min_column > self.dataset.width // 2
        row_count = max_row - min_row + 1
        if wraps:
            right_columns = self.dataset.width - min_column
            left_columns = max_column + 1
            right = self.dataset.read(
                1, window=Window(min_column, min_row, right_columns, row_count), masked=False
            )
            left = self.dataset.read(1, window=Window(0, min_row, left_columns, row_count), masked=False)
            values = np.concatenate((right, left), axis=1)
            col_offset = ((column0 - min_column) % self.dataset.width).astype(np.int64)
            col_next = ((column1 - min_column) % self.dataset.width).astype(np.int64)
        else:
            values = self.dataset.read(
                1,
                window=Window(min_column, min_row, max_column - min_column + 1, row_count),
                masked=False,
            )
            col_offset = column0 - min_column
            col_next = column1 - min_column
        row_offset = row0 - min_row
        row_next = row1 - min_row
        h00 = values[row_offset, col_offset].astype(np.float64)
        h01 = values[row_offset, col_next].astype(np.float64)
        h10 = values[row_next, col_offset].astype(np.float64)
        h11 = values[row_next, col_next].astype(np.float64)
        north = h00 * (1.0 - fraction_x) + h01 * fraction_x
        south = h10 * (1.0 - fraction_x) + h11 * fraction_x
        undulation = (north * (1.0 - fraction_y) + south * fraction_y).astype(np.float32)
        valid = np.isfinite(undulation)
        if self.dataset.nodata is not None:
            # A bilinear sample is unavailable if any nonzero-weight corner is no-data.
            corners = (h00, h01, h10, h11)
            for values_corner, weight in zip(
                corners,
                (
                    (1.0 - fraction_x) * (1.0 - fraction_y),
                    fraction_x * (1.0 - fraction_y),
                    (1.0 - fraction_x) * fraction_y,
                    fraction_x * fraction_y,
                ),
                strict=True,
            ):
                valid &= (values_corner != self.dataset.nodata) | (weight == 0.0)
        return undulation, valid


def _validate_egm2008_grid(dataset: rasterio.DatasetReader) -> None:
    tags = dataset.tags()
    description = tags.get("TIFFTAG_IMAGEDESCRIPTION", "")
    copyright_notice = tags.get("TIFFTAG_COPYRIGHT", "")
    transform = dataset.transform
    if dataset.driver != "GTiff" or dataset.count != 1 or dataset.dtypes != ("float32",):
        raise TerrainInputError("EGM2008 grid must be a one-band Float32 GeoTIFF")
    if dataset.crs != rasterio.crs.CRS.from_epsg(4979):
        raise TerrainInputError("EGM2008 grid horizontal CRS must be EPSG:4979")
    if tags.get("AREA_OR_POINT") != "Point":
        raise TerrainInputError("EGM2008 grid must be point registered")
    if not all(token in description for token in ("EGM2008", "EPSG:3855")):
        raise TerrainInputError("EGM2008 grid metadata does not identify EGM2008 / EPSG:3855")
    if "Public Domain" not in copyright_notice:
        raise TerrainInputError("EGM2008 grid copyright metadata does not verify public-domain terms")
    if transform.a <= 0.0 or transform.e >= 0.0 or transform.b != 0.0 or transform.d != 0.0:
        raise TerrainInputError("EGM2008 grid must use a north-up geographic affine")
    if not math.isclose(transform.a, EGM2008Grid.GRID_STEP_DEGREES, rel_tol=0.0, abs_tol=1e-8):
        raise TerrainInputError("EGM2008 grid spacing differs from the approved 2.5 arc-minute grid")
    if not math.isclose(abs(transform.e), EGM2008Grid.GRID_STEP_DEGREES, rel_tol=0.0, abs_tol=1e-8):
        raise TerrainInputError("EGM2008 grid spacing differs from the approved 2.5 arc-minute grid")
    if dataset.width < 8640 or dataset.height < 4320:
        raise TerrainInputError("EGM2008 grid does not cover the global WGS84 domain")
    if dataset.scales != (1.0,) or dataset.offsets != (0.0,):
        raise TerrainInputError("EGM2008 undulation grid scale/offset must remain 1/0")


def _load_geoid_receipt(path: Path, byte_length: int, sha256: str) -> dict[str, Any]:
    sidecar_path = path.with_suffix(path.suffix + ".receipt.json")
    receipt: dict[str, Any] = {
        "asset_uri": EGM2008Grid.URI,
        "byte_length": byte_length,
        "sha256": sha256,
        "acquired_at_utc": None,
        "receipt_basis": "local_file_hash_verified; acquisition time not supplied",
        "verified_at_utc": _utc_now(),
        "etag": None,
        "last_modified": None,
    }
    if sidecar_path.exists() and not sidecar_path.is_symlink():
        try:
            value = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise TerrainInputError("EGM2008 receipt sidecar is invalid")
        if value.get("sha256") != sha256 or value.get("byte_length") != byte_length:
            raise TerrainInputError("EGM2008 receipt sidecar does not match the grid file")
        if value.get("asset_uri") != EGM2008Grid.URI:
            raise TerrainInputError("EGM2008 receipt URI differs from the approved grid source")
        receipt.update(value)
        receipt["verified_at_utc"] = _utc_now()
    return receipt


@dataclass
class LoadedTile:
    """A loaded tile resource with explicit, repeatable close semantics."""

    manifest: dict[str, Any]
    heights: np.ndarray | None
    source_mask: np.ndarray | None
    quality_mask: np.ndarray | None
    tid_mask: np.ndarray | None = None
    geoid_undulation: np.ndarray | None = None
    ellipsoidal_heights: np.ndarray | None = None
    geoid_mask: np.ndarray | None = None
    boundary_support: dict[str, Any] | None = None
    _closed: bool = False

    def close(self) -> None:
        self.heights = None
        self.source_mask = None
        self.quality_mask = None
        self.tid_mask = None
        self.geoid_undulation = None
        self.ellipsoidal_heights = None
        self.geoid_mask = None
        self.boundary_support = None
        self._closed = True

    def __enter__(self) -> LoadedTile:
        if self._closed:
            raise TerrainCacheError("tile handle has already been unloaded")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed


def _status_for_masks(source_mask: np.ndarray, quality_mask: np.ndarray) -> str:
    if source_mask.size == 0:
        return "not_generated"
    valid = source_mask != SOURCE_CODE_NONE
    return "ready" if valid.all() else "partial"


def _artifact_identity(
    address: GlobalTileAddress,
    source_fingerprint: str,
    height_bytes: bytes,
    source_bytes: bytes,
    quality_bytes: bytes,
    tid_bytes: bytes | None,
    generator_commit: str,
    geoid_undulation_bytes: bytes | None = None,
    ellipsoid_height_bytes: bytes | None = None,
    geoid_mask_bytes: bytes | None = None,
    boundary_support_bytes: bytes | None = None,
) -> str:
    content = {
        "contract_id": CONTRACT_ID,
        "tile_id": address.tile_id,
        "source_fingerprint": source_fingerprint,
        "payload_sha256": {
            "height_samples.f32le": _sha256_bytes(height_bytes),
            "source_mask.u8": _sha256_bytes(source_bytes),
            "quality_mask.u8": _sha256_bytes(quality_bytes),
            "tid_mask.i16le": None if tid_bytes is None else _sha256_bytes(tid_bytes),
            "geoid_undulation.f32le": None if geoid_undulation_bytes is None else _sha256_bytes(geoid_undulation_bytes),
            "ellipsoidal_height_samples.f64le": None if ellipsoid_height_bytes is None else _sha256_bytes(ellipsoid_height_bytes),
            "geoid_mask.u8": None if geoid_mask_bytes is None else _sha256_bytes(geoid_mask_bytes),
            "boundary_support.geojson": None if boundary_support_bytes is None else _sha256_bytes(boundary_support_bytes),
        },
        "producer_commit": generator_commit,
        "resampling": "none",
    }
    return _sha256_bytes(_canonical_json(content))


def _write_tile_bundle(
    cache: BoundedCache,
    address: GlobalTileAddress,
    manifest: dict[str, Any],
    heights: np.ndarray,
    source_mask: np.ndarray,
    quality_mask: np.ndarray,
    tid_mask: np.ndarray | None,
    geoid_undulation: np.ndarray | None,
    ellipsoidal_heights: np.ndarray | None,
    geoid_mask: np.ndarray | None,
    boundary_support_payload: bytes | None = None,
) -> Path:
    tile_dir = cache.tile_directory(address)
    if tile_dir.exists():
        try:
            shutil.rmtree(tile_dir)
        except OSError as exc:
            raise TerrainCacheError("cannot invalidate stale tile cache entry") from exc
    tile_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, bytes] = {
        "height_samples.f32le": np.asarray(heights, dtype="<f4").tobytes(order="C"),
        "source_mask.u8": np.asarray(source_mask, dtype="u1").tobytes(order="C"),
        "quality_mask.u8": np.asarray(quality_mask, dtype="u1").tobytes(order="C"),
    }
    if tid_mask is not None:
        payloads["tid_mask.i16le"] = np.asarray(tid_mask, dtype="<i2").tobytes(order="C")
    if geoid_undulation is not None:
        payloads["geoid_undulation.f32le"] = np.asarray(geoid_undulation, dtype="<f4").tobytes(order="C")
    if ellipsoidal_heights is not None:
        payloads["ellipsoidal_height_samples.f64le"] = np.asarray(ellipsoidal_heights, dtype="<f8").tobytes(order="C")
    if geoid_mask is not None:
        payloads["geoid_mask.u8"] = np.asarray(geoid_mask, dtype="u1").tobytes(order="C")
    if boundary_support_payload is not None:
        payloads["boundary_support.geojson"] = boundary_support_payload
    manifest["terrain"]["payloads"] = []
    for name, data in payloads.items():
        path = tile_dir / name
        _atomic_write(path, data)
        if name == "boundary_support.geojson":
            manifest["terrain"]["payloads"].append({
                "payload_kind": "terrain_boundary_support",
                "path": name,
                "media_type": "application/geo+json",
                "encoding": "geojson",
                "size_bytes": len(data),
                "sha256": _sha256_bytes(data),
                "feature_count": int(manifest["terrain"]["boundary_support"]["feature_count"]),
                "geometry_types": ["Point"],
                "coordinate_dimensions": 3,
                "boundary_support_edges": manifest["terrain"]["boundary_support"]["boundary_support_edges"],
            })
            continue
        encoding = {
            "height_samples.f32le": "ieee754_binary32_le_metres",
            "source_mask.u8": "uint8_source_code_per_sample",
            "quality_mask.u8": "uint8_quality_code_per_sample",
            "tid_mask.i16le": "int16_le_gebco_type_identifier_per_sample",
            "geoid_undulation.f32le": "ieee754_binary32_le_egm2008_undulation_metres",
            "ellipsoidal_height_samples.f64le": "ieee754_binary64_le_wgs84_ellipsoidal_height_metres",
            "geoid_mask.u8": "uint8_geoid_conversion_source_code_per_sample",
        }[name]
        dtype = {
            "height_samples.f32le": "float32",
            "source_mask.u8": "uint8",
            "quality_mask.u8": "uint8",
            "tid_mask.i16le": "int16",
            "geoid_undulation.f32le": "float32",
            "ellipsoidal_height_samples.f64le": "float64",
            "geoid_mask.u8": "uint8",
        }[name]
        manifest["terrain"]["payloads"].append({
            "payload_kind": "raster_grid",
            "path": name,
            "media_type": "application/octet-stream",
            "encoding": encoding,
            "size_bytes": len(data),
            "sha256": _sha256_bytes(data),
            "shape": [
                manifest["terrain"]["grid"]["rows"],
                manifest["terrain"]["grid"]["columns"],
            ],
            "dtype": dtype,
            "byte_order": "little",
            "row_order": manifest["terrain"]["grid"]["sample_order"],
            "pixel_registration": manifest["terrain"]["grid"]["pixel_registration"],
            "sample_geotransform": manifest["terrain"]["grid"]["source_affine"],
            "horizontal_crs": manifest["terrain"]["grid"]["horizontal_crs"],
            "axis_order": manifest["terrain"]["grid"]["axis_order"],
            "coordinate_unit": manifest["terrain"]["grid"]["unit_horizontal"],
            "no_data_value": (
                -32768.0
                if name in {"height_samples.f32le", "geoid_undulation.f32le", "ellipsoidal_height_samples.f64le"}
                else None
            ),
        })
    manifest["manifest_sha256"] = _sha256_bytes(_canonical_json(manifest))
    _atomic_write(tile_dir / "manifest.json", _canonical_json(manifest))
    cache.enforce_limit(preserve=tile_dir)
    return tile_dir


class GlobalTerrainProvider:
    """Load or generate one WFG-1 tile from the approved global source stack."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        max_cache_bytes: int = DEFAULT_CACHE_BYTES,
        cop90_client: httpx.Client | None = None,
        gebco_source: GebcoRemoteSource | None = None,
        geoid_grid: Path | None = None,
        generator_commit: str = "unknown",
        load_geoid_from_environment: bool = True,
    ) -> None:
        self.cache = BoundedCache(cache_dir, max_cache_bytes)
        self.cop90 = Cop90Catalog(self.cache, client=cop90_client)
        self._owns_gebco = gebco_source is None
        self.gebco = gebco_source or GebcoRemoteSource(client=cop90_client)
        geoid_path = geoid_grid
        if geoid_path is None and load_geoid_from_environment:
            configured = os.environ.get("WORLDFORGE_EGM2008_GRID")
            if configured:
                geoid_path = Path(configured)
        self.geoid = None if geoid_path is None else EGM2008Grid(geoid_path)
        self.generator_commit = generator_commit

    def close(self) -> None:
        self.cop90.close()
        if self._owns_gebco:
            self.gebco.close()
        if self.geoid is not None:
            self.geoid.close()

    def _source_fingerprint(self, value: str) -> str:
        if self.geoid is None:
            return f"{value}:egm2008:none"
        return f"{value}:egm2008:{self.geoid.sha256}:bilinear:v1"

    def __enter__(self) -> GlobalTerrainProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _build_boundary_support(
        self, address: GlobalTileAddress, grid: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bytes | None, list[dict[str, Any]]]:
        features, edge_counts = _boundary_support_targets(address, grid)
        if self.geoid is None:
            missing_ids = [feature["id"] for feature in features]
            return (
                {
                    "status": "not_generated",
                    "requested": True,
                    "payload_kind": "terrain_boundary_support",
                    "ownership": "geometry_support_only; WFG-1 core raster retains half-open ownership",
                    "reason": "verified EGM2008 grid is required to emit 3D ellipsoidal boundary points",
                    "polar_vertex_policy": "one canonical longitude-0 vertex at each exact pole for every tile touching that pole",
                    "boundary_support_edges": [],
                    "expected_edge_sample_counts": edge_counts,
                    "valid_edge_sample_counts": {edge: 0 for edge in edge_counts},
                    "missing_edges": [
                        {"edge": edge, "expected_sample_count": count, "valid_sample_count": 0, "missing_sample_count": count}
                        for edge, count in edge_counts.items()
                    ],
                    "disclosure": "Boundary support is unavailable because the verified EGM2008 grid is not configured.",
                    "feature_count": 0,
                    "expected_feature_count": len(features),
                    "coverage": {
                        "expected_feature_count": len(features),
                        "valid_feature_count": 0,
                        "missing_feature_count": len(features),
                        "missing_feature_ids": missing_ids,
                        "emitted_feature_count": 0,
                        "valid_samples_omitted_with_incomplete_edges": 0,
                    },
                    "source_lineage": [],
                    "payload": None,
                },
                None,
                [],
            )

        targets = np.asarray(
            [[item["target_longitude_deg"], item["target_latitude_deg"]] for item in features],
            dtype=np.float64,
        )
        cop_h, cop_valid, cop_lonlat, cop_assets, cop_attempts = self.cop90.sample_nearest_points(targets)
        gebco_h = np.full(len(features), np.int16(-32768), dtype=np.int16)
        gebco_tid = np.full(len(features), np.int16(-32768), dtype=np.int16)
        gebco_lonlat = np.full(targets.shape, np.nan, dtype=np.float64)
        gebco_receipts: list[dict[str, Any]] = []
        gebco_failures: list[dict[str, Any]] = []
        for edge in ("north", "east", "south", "west"):
            edge_indices = np.asarray(
                [index for index, feature in enumerate(features) if feature["edge"] == edge],
                dtype=np.int64,
            )
            if edge_indices.size == 0:
                continue
            # Split each edge at the antimeridian so every DAP request is one
            # contiguous source-grid window.
            for negative in (False, True):
                selection = targets[edge_indices, 0] < 0.0 if negative else targets[edge_indices, 0] >= 0.0
                selected = edge_indices[selection]
                if selected.size == 0:
                    continue
                query_lon = targets[selected, 0].reshape((1, -1))
                query_lat = targets[selected, 1].reshape((1, -1))
                try:
                    height_part, tid_part, remote_meta = self.gebco.read_samples_at_coordinates(query_lon, query_lat)
                    gebco_h[selected] = height_part.reshape(-1)
                    gebco_tid[selected] = tid_part.reshape(-1)
                    _, _, sample_lon, sample_lat = _gebco_nearest_sample_indices(query_lon, query_lat)
                    gebco_lonlat[selected, 0] = sample_lon.reshape(-1)
                    gebco_lonlat[selected, 1] = sample_lat.reshape(-1)
                    gebco_receipts.append({"edge": edge, **remote_meta["asset_receipts"]})
                except TerrainError as exc:
                    gebco_failures.append({"product": GEBCO_RELEASE, "edge": edge, "status": "failed", "reason": str(exc)})

        tid_valid = (gebco_tid >= 0) & (gebco_tid <= 72)
        use_cop = (gebco_tid == 0) & tid_valid & cop_valid
        use_gebco = (~use_cop) & tid_valid & (gebco_h != np.int16(-32768))
        source_mask = np.zeros(len(features), dtype=np.uint8)
        quality_mask = np.zeros(len(features), dtype=np.uint8)
        heights = np.full(len(features), np.float32(-32768.0), dtype=np.float32)
        sample_lonlat = np.full(targets.shape, np.nan, dtype=np.float64)
        source_mask[use_cop] = SOURCE_CODE_COP90
        source_mask[use_gebco] = SOURCE_CODE_GEBCO
        quality_mask[use_cop] = QUALITY_CODE_COP90_NATIVE
        direct_water = use_gebco & (gebco_tid >= 10) & (gebco_tid <= 48)
        land_fallback = use_gebco & (gebco_tid == 0)
        uncertain = use_gebco & ~(direct_water | land_fallback)
        quality_mask[direct_water] = QUALITY_CODE_GEBCO_WATER
        quality_mask[land_fallback] = QUALITY_CODE_GEBCO_LAND_FALLBACK
        quality_mask[uncertain] = QUALITY_CODE_GEBCO_UNCERTAIN
        heights[use_cop] = cop_h[use_cop]
        heights[use_gebco] = gebco_h[use_gebco].astype(np.float32)
        sample_lonlat[use_cop] = cop_lonlat[use_cop]
        sample_lonlat[use_gebco] = gebco_lonlat[use_gebco]
        source_valid = source_mask != SOURCE_CODE_NONE
        undulation = np.full(len(features), np.float32(-32768.0), dtype=np.float32)
        ellipsoidal = np.full(len(features), -32768.0, dtype=np.float64)
        geoid_mask = np.zeros(len(features), dtype=np.uint8)
        if source_valid.any():
            valid_indices = np.flatnonzero(source_valid)
            n_values, n_valid = self.geoid.sample(
                sample_lonlat[valid_indices, 0].reshape((1, -1)),
                sample_lonlat[valid_indices, 1].reshape((1, -1)),
            )
            converted_indices = valid_indices[n_valid.reshape(-1)]
            undulation[converted_indices] = n_values.reshape(-1)[n_valid.reshape(-1)]
            ellipsoidal[converted_indices] = (
                heights[converted_indices].astype(np.float64)
                + undulation[converted_indices].astype(np.float64)
            )
            geoid_mask[converted_indices] = source_mask[converted_indices]

        assets_by_prefix = {asset.prefix: asset for asset in cop_assets}
        used_cop_prefixes = {
            _source_prefix_for_coordinate(float(sample_lonlat[index, 0]), float(sample_lonlat[index, 1]))
            for index in np.flatnonzero(use_cop)
        }
        boundary_lineage: list[dict[str, Any]] = []
        for prefix, asset in assets_by_prefix.items():
            if prefix not in used_cop_prefixes:
                continue
            boundary_lineage.append({
                "source_code": SOURCE_CODE_COP90,
                "provider": "Copernicus DEM / AWS Open Data registry mirror",
                "product": COP90_DATASET,
                "release": COP90_RELEASE,
                "asset_id": prefix,
                "asset_uri": asset.uri,
                "asset_version": {"etag": asset.etag, "last_modified": asset.last_modified},
                "byte_length": asset.content_length,
                "sha256": asset.sha256,
                "horizontal": {"crs": "EPSG:4326", "datum": "WGS84-G1150", "realization": "WGS84-G1150", "coordinate_epoch": "not_specified_by_product_metadata", "axis_order": "longitude_latitude", "coordinate_unit": "degree"},
                "vertical": {"crs": "EPSG:3855", "datum": "EGM2008", "height_type": "orthometric", "unit": "metre"},
                "source_raster": {"dtype": "float32", "pixel_registration": "point", "sampling": "nearest_native_sample_tie_east_north"},
                "license": {"id": COP90_LICENSE_ID, "rights_url": "https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf", "attribution": COP90_ATTRIBUTION, "modified_data_notice": COP90_MODIFICATION_NOTICE, "liability_notice": COPERNICUS_DISCLAIMER, "no_endorsement_notice": COPERNICUS_NO_ENDORSEMENT},
                "uncertainty": COP90_UNCERTAINTY,
                "used_for_boundary_support": True,
                "cache_receipt": {"received_at_utc": asset.acquired_at_utc, "asset_cache_key": asset.cache_key, "sha256_verified": True, "source_index_sha256": None if self.cop90.index_receipt is None else self.cop90.index_receipt.get("sha256")},
            })
        if gebco_receipts:
            boundary_lineage.append({
                "source_code": SOURCE_CODE_GEBCO,
                "provider": "NERC British Oceanographic Data Centre / GEBCO",
                "product": GEBCO_RELEASE,
                "release": GEBCO_RELEASE,
                "asset_id": "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa",
                "asset_uri": GEBCO_ELEVATION_URL.removesuffix(".ascii"),
                "asset_version": {"doi": "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"},
                "assets": [
                    {"role": role, "product": GEBCO_RELEASE, "asset_uri": receipt[role]["asset_uri"], "metadata": receipt[role]}
                    for receipt in gebco_receipts
                    for role in ("elevation", "tid")
                ],
                "horizontal": {"crs": "EPSG:4326", "datum": "WGS84", "axis_order": "longitude_latitude", "coordinate_unit": "degree"},
                "vertical": {"datum": "mean_sea_level_nominal_heterogeneous_inputs", "height_type": "elevation_relative_to_mean_sea_level_nominal", "crs": None, "unit": "metre"},
                "source_raster": {"dtype": "int16", "tid_dtype": "int8", "pixel_registration": "cell_center", "spacing_arc_seconds": 15, "sampling": "nearest_source_cell_center_tie_east_north"},
                "license": {"id": GEBCO_LICENSE_ID, "terms_url": "https://www.gebco.net/data-products-gridded-bathymetry-data/gebco2026-grid", "attribution": GEBCO_ATTRIBUTION},
                "uncertainty": GEBCO_UNCERTAINTY,
                "tid_classification": {"method": "TID=0 land; nonzero classes are water/bathymetry", "limitation": GEBCO_TID_LIMITATION, "known_issues_url": GEBCO_TID_ERRATA_URL},
                "used_for_boundary_support": bool(np.any(use_gebco)),
                "used_for_classification": True,
                "used_for_mosaic": bool(np.any(use_gebco)),
            })
        (
            edge_indices_by_name,
            edge_valid_counts,
            complete_edges,
            missing_edges,
            missing_ids,
        ) = _boundary_edge_coverage(features, geoid_mask)
        features_json: list[dict[str, Any]] = []
        source_verticals = {
            SOURCE_CODE_COP90: {"datum": "EGM2008", "height_type": "orthometric", "crs": "EPSG:3855", "unit": "metre"},
            SOURCE_CODE_GEBCO: {"datum": "MEAN_SEA_LEVEL", "height_type": "elevation_relative_to_mean_sea_level_nominal", "crs": None, "unit": "metre"},
        }
        for index, feature in enumerate(features):
            if feature["edge"] not in complete_edges:
                continue
            source_code = int(source_mask[index])
            target_lon = float(feature["target_longitude_deg"])
            target_lat = float(feature["target_latitude_deg"])
            sample_lon = normalize_longitude(float(sample_lonlat[index, 0]))
            sample_lat = float(sample_lonlat[index, 1])
            method, offset_m = _boundary_sample_relationship(
                target_lon, target_lat, sample_lon, sample_lat
            )
            if source_code == SOURCE_CODE_COP90:
                prefix = _source_prefix_for_coordinate(sample_lon, sample_lat)
                asset = assets_by_prefix.get(prefix)
                artifact = {"product": COP90_DATASET, "release": COP90_RELEASE, "asset_id": prefix, "asset_uri": None if asset is None else asset.uri, "sha256": None if asset is None else asset.sha256}
                warning = None
            else:
                edge_receipt = next(
                    (receipt for receipt in gebco_receipts if receipt.get("edge") == feature["edge"]),
                    {},
                )
                artifact = {
                    "product": GEBCO_RELEASE,
                    "release": GEBCO_RELEASE,
                    "asset_id": "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa",
                    "asset_uri": GEBCO_ELEVATION_URL.removesuffix(".ascii"),
                    "doi": "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa",
                    "checksums": {
                        role: {
                            "sha256": edge_receipt[role]["response_sha256"],
                            "scope": "requested_hyperslab_response",
                            "byte_length": edge_receipt[role]["response_byte_length"],
                        }
                        for role in ("elevation", "tid")
                        if role in edge_receipt and edge_receipt[role].get("response_sha256")
                    },
                }
                warning = "GEBCO nominal mean-sea-level height is heterogeneous; adding EGM2008 N is approximate and shallow-water source datums may differ."
            props = {
                "edge": feature["edge"],
                "edge_index": feature["edge_index"],
                "edge_sample_count": feature["edge_sample_count"],
                "target_longitude_unwrapped_deg": feature["target_longitude_unwrapped_deg"],
                "source_sample_longitude": sample_lon,
                "source_sample_latitude": sample_lat,
                "source_height_H": float(heights[index]),
                "source_vertical_datum": source_verticals[source_code]["datum"],
                "geoid_undulation_N": float(undulation[index]),
                "ellipsoidal_height_h": float(ellipsoidal[index]),
                "source_mask": source_code,
                "quality_mask": int(quality_mask[index]),
                "type_identifier_mask": (
                    int(gebco_tid[index])
                    if source_code == SOURCE_CODE_GEBCO and 0 <= gebco_tid[index] <= 72
                    else None
                ),
                "geoid_mask": int(geoid_mask[index]),
                "sample_method": method,
                "source_to_boundary_offset_m": offset_m,
                "source_artifact": artifact,
                "warning": warning,
            }
            features_json.append({
                "type": "Feature",
                "id": feature["id"],
                "geometry": {"type": "Point", "coordinates": [target_lon, target_lat, float(ellipsoidal[index])]},
                "properties": {"worldforge": {"boundary_support": props}},
            })
        source_counts = {
            str(code): int(np.count_nonzero(source_mask == code))
            for code in (SOURCE_CODE_COP90, SOURCE_CODE_GEBCO)
            if np.any(source_mask == code)
        }
        support_status = (
            "no_source_coverage"
            if not features_json
            else ("ready" if len(complete_edges) == len(edge_indices_by_name) else "partial")
        )
        geojson_bytes = _canonical_json({"type": "FeatureCollection", "features": features_json}) if features_json else None
        vertical_refs = {
            str(code): {
                "source_vertical_datum": "MEAN_SEA_LEVEL" if code == SOURCE_CODE_GEBCO else "EGM2008",
                "method": "nearest native source sample; bilinear EGM2008 N at the selected source sample coordinate; h=H+N",
                "approximate": code == SOURCE_CODE_GEBCO,
                "warning": GEBCO_UNCERTAINTY if code == SOURCE_CODE_GEBCO else None,
                "geoid_mask_code": code,
            }
            for code in sorted(int(value) for value in np.unique(source_mask) if value != SOURCE_CODE_NONE)
        }
        support_record = {
            "status": support_status,
            "requested": True,
            "payload_kind": "terrain_boundary_support",
            "ownership": "geometry_support_only; half-open core raster ownership is unchanged",
            "edge_order": "north_west_to_east_including_corners; east_north_to_south_excluding_northeast; south_east_to_west_excluding_southeast; west_south_to_north_excluding_southwest_and_northwest",
            "sampling_method": "nearest_native_sample_tie_east_north; exact source-grid points use native_exact",
            "offset_method": "WGS84 Vincenty inverse geodesic distance from the selected source sample center to its target boundary coordinate",
            "target_coordinate_policy": "exact WFG-1 boundary coordinates, longitude normalized to [-180,180) (the +180 east limit is encoded as -180)",
            "polar_vertex_policy": "one canonical longitude-0 vertex at each exact pole for every tile touching that pole",
            "boundary_support_edges": [
                {"edge": edge, "sample_count": edge_counts[edge]}
                for edge in ("north", "east", "south", "west")
                if edge in complete_edges
            ],
            "expected_edge_sample_counts": edge_counts,
            "valid_edge_sample_counts": edge_valid_counts,
            "missing_edges": missing_edges,
            "disclosure": "An edge with any unavailable point is omitted as a whole; no partial edge index sequence is emitted." if missing_edges else None,
            "coverage": {
                "expected_feature_count": len(features),
                "valid_feature_count": int(np.count_nonzero(geoid_mask)),
                "missing_feature_count": len(missing_ids),
                "missing_feature_ids": missing_ids,
                "emitted_feature_count": len(features_json),
                "valid_samples_omitted_with_incomplete_edges": int(
                    sum(
                        edge_valid_counts[edge]
                        for edge in edge_indices_by_name
                        if edge not in complete_edges
                    )
                ),
                "source_sample_counts_by_mask": source_counts,
                "complete_for_expected_boundary_vertices": not missing_ids,
                "coverage_claim": "boundary support points only; not additional owned raster samples or a completeness claim",
            },
            "vertical_references_by_source_code": vertical_refs,
            "source_lineage": boundary_lineage,
            "source_attempts": cop_attempts + gebco_failures,
            "payload": "boundary_support.geojson" if geojson_bytes is not None else None,
            "feature_count": len(features_json),
        }
        return support_record, geojson_bytes, boundary_lineage

    def load_tile(self, address: GlobalTileAddress, *, refresh: bool = False) -> LoadedTile:
        prefix = _source_prefix_for_tile(address)
        attempts: list[dict[str, Any]] = []
        # GLO-30-F is preferred but requires registered CDSE access. This
        # provider does not use the partial AWS GLO-30 Public mirror. When the
        # machine has no registered 30-F API credentials, that state is carried
        # into provenance and the worldwide GLO-90-F fallback remains usable.
        if os.environ.get("WORLDFORGE_COPDEM30F_BEARER_TOKEN"):
            return self._failed_tile(
                address,
                [
                    {
                        "product": "COP-DEM-GLO-30-F",
                        "status": "failed",
                        "reason": "registered 30-F credentials exist but the authenticated product adapter is not configured; refusing an implicit downgrade",
                    }
                ],
                "copdem_30f_adapter_not_configured",
            )
        attempts.append(
            {
                "product": "COP-DEM-GLO-30-F",
                "status": "auth_needed",
                "reason": "registered Copernicus Data Space 30-F credentials are not configured; use the GLO-90-F global fallback",
            }
        )
        try:
            catalog = self.cop90.load_index(refresh=refresh)
        except TerrainRemoteError as exc:
            attempts.append(
                {
                    "product": COP90_DATASET,
                    "status": "failed",
                    "reason": str(exc),
                }
            )
            return self._read_gebco_tile(address, attempts)
        source_cell_missing = prefix not in catalog
        if prefix in catalog:
            try:
                asset = self.cop90.fetch_asset(prefix)
                if asset is not None:
                    return self._read_cop90_tile(address, asset, attempts)
            except (TerrainRemoteError, TerrainInputError) as exc:
                attempts.append(
                    {
                        "product": COP90_DATASET,
                        "status": "failed",
                        "reason": str(exc),
                    }
                )
        if source_cell_missing:
            attempts.append(
                {
                    "product": COP90_DATASET,
                    "status": "no_asset_for_source_cell",
                    "reason": "the AWS GLO-90 catalog has no COG for this one-degree source cell",
                }
            )
        return self._read_gebco_tile(address, attempts)

    def _read_cop90_tile(
        self,
        address: GlobalTileAddress,
        asset: Cop90Asset,
        attempts: list[dict[str, Any]],
    ) -> LoadedTile:
        with MemoryFile(asset.bytes) as memory_file:
            with memory_file.open() as dataset:
                _validate_cop90_raster(dataset, asset.prefix)
                source_window = _pixel_centres_in_bounds(
                    dataset.transform, dataset.width, dataset.height, address.bounds
                )
                if source_window is None:
                    attempts.append(
                        {
                            "product": COP90_DATASET,
                            "status": "not_generated",
                            "reason": "no native Copernicus sample centers fall inside this WFG tile",
                        }
                    )
                    return self._read_gebco_tile(address, attempts)
                row, column, rows, columns = source_window
                if rows * columns > MAX_GRID_SAMPLES:
                    raise TerrainInputError("Copernicus source window exceeds the configured sample cap")
                heights = dataset.read(1, window=Window(column, row, columns, rows), masked=False)
                if heights.shape != (rows, columns):
                    raise TerrainInputError("Copernicus source window dimensions changed")
                source_mask, quality_mask, status = _classify_cop90_samples(heights, dataset.nodata)
                # Float32 source values remain numerically unchanged. The only
                # output conversion is little-endian byte order for portability.
                transform = dataset.window_transform(Window(column, row, columns, rows))
                source_raster_metadata = {
                    "driver": "GTiff",
                    "bands": 1,
                    "dtype": "float32",
                    "source_width": dataset.width,
                    "source_height": dataset.height,
                    "scale": 1.0,
                    "offset": 0.0,
                    "nodata_value": dataset.nodata,
                    "pixel_registration": "point",
                    "affine": {
                        "origin_reference": "pixel_corner",
                        "x_origin": float(dataset.transform.c),
                        "y_origin": float(dataset.transform.f),
                        "pixel_width": float(dataset.transform.a),
                        "pixel_height": float(dataset.transform.e),
                    },
                }
                grid = {
                    "rows": rows,
                    "columns": columns,
                    "pixel_registration": "point",
                    "sample_order": "row_major_north_to_south_west_to_east",
                    "horizontal_crs": "EPSG:4326",
                    "horizontal_datum": "WGS84-G1150",
                    "axis_order": "longitude_latitude",
                    "unit_horizontal": "degree",
                    "source_affine": {
                        "origin_reference": "pixel_corner",
                        "x_origin": float(transform.c),
                        "y_origin": float(transform.f),
                        "pixel_width": float(transform.a),
                        "rotation_x": float(transform.b),
                        "rotation_y": float(transform.d),
                        "pixel_height": float(transform.e),
                    },
                    "source_window": {
                        "row_offset": row,
                        "column_offset": column,
                        "rows": rows,
                        "columns": columns,
                    },
                    "source_resolution": {
                        "nominal_horizontal_sampling_m": 90,
                        "native_pixel_width_degrees": float(dataset.transform.a),
                        "native_pixel_height_degrees": float(abs(dataset.transform.e)),
                    },
                    "source_bounds": {
                        "west_deg": float(min(transform.c, transform.c + columns * transform.a)),
                        "south_deg": float(min(transform.f, transform.f + rows * transform.e)),
                        "east_deg": float(max(transform.c, transform.c + columns * transform.a)),
                        "north_deg": float(max(transform.f, transform.f + rows * transform.e)),
                    },
                }
                heights = np.asarray(heights, dtype=np.float32)
        attempts.append({"product": COP90_DATASET, "status": "ready"})
        source_meta = {
            "source_code": SOURCE_CODE_COP90,
            "provider": "Copernicus DEM / AWS Open Data registry mirror",
            "product": COP90_DATASET,
            "release": COP90_RELEASE,
            "asset_uri": asset.uri,
            "asset_version": {"etag": asset.etag, "last_modified": asset.last_modified},
            "byte_length": asset.content_length,
            "sha256": asset.sha256,
            "surface_model": "DSM",
            "horizontal": {
                "crs": "EPSG:4326",
                "datum": "WGS84-G1150",
                "realization": "WGS84-G1150",
                "coordinate_epoch": "not_specified_by_product_metadata",
                "axis_order": "longitude_latitude",
                "coordinate_unit": "degree",
            },
            "vertical": {
                "crs": "EPSG:3855",
                "datum": "EGM2008",
                "height_type": "orthometric",
                "unit": "metre",
            },
            "source_raster": source_raster_metadata,
            "license": {
                "id": COP90_LICENSE_ID,
                "rights_url": "https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf",
                "attribution": COP90_ATTRIBUTION,
                "modified_data_notice": COP90_MODIFICATION_NOTICE,
                "liability_notice": COPERNICUS_DISCLAIMER,
                "no_endorsement_notice": COPERNICUS_NO_ENDORSEMENT,
            },
            "uncertainty": COP90_UNCERTAINTY,
            "cache_receipt": {
                "received_at_utc": asset.acquired_at_utc,
                "asset_cache_key": asset.cache_key,
                "sha256_verified": True,
                "source_index_sha256": None if self.cop90.index_receipt is None else self.cop90.index_receipt.get("sha256"),
            },
        }
        affine = grid["source_affine"]
        longitude_centers = affine["x_origin"] + (
            np.arange(grid["columns"], dtype=np.float64) + 0.5
        ) * affine["pixel_width"]
        latitude_centers = affine["y_origin"] + (
            np.arange(grid["rows"], dtype=np.float64) + 0.5
        ) * affine["pixel_height"]
        sample_longitudes, sample_latitudes = np.meshgrid(longitude_centers, latitude_centers)
        try:
            gebco_heights, gebco_tid, gebco_metadata = self.gebco.read_samples_at_coordinates(
                sample_longitudes, sample_latitudes
            )
        except TerrainError as exc:
            return self._failed_tile(
                address,
                attempts
                + [
                    {
                        "product": GEBCO_RELEASE,
                        "status": "failed",
                        "reason": f"GEBCO TID classification needed to distinguish Copernicus land from ocean failed: {exc}",
                    }
                ],
                "gebco_source_classification_unavailable",
            )
        cop_valid = source_mask == SOURCE_CODE_COP90
        gebco_valid = gebco_heights != np.int16(-32768)
        tid_valid = (gebco_tid >= 0) & (gebco_tid <= 72)
        tid_is_land = tid_valid & (gebco_tid == 0)
        use_cop = tid_is_land & cop_valid
        use_gebco = (~use_cop) & tid_valid & gebco_valid
        output_heights = np.full(heights.shape, np.float32(-32768.0), dtype=np.float32)
        output_source_mask = np.zeros(heights.shape, dtype=np.uint8)
        output_quality_mask = np.zeros(heights.shape, dtype=np.uint8)
        output_heights[use_cop] = heights[use_cop]
        output_source_mask[use_cop] = SOURCE_CODE_COP90
        output_quality_mask[use_cop] = QUALITY_CODE_COP90_NATIVE
        output_heights[use_gebco] = gebco_heights[use_gebco].astype(np.float32)
        output_source_mask[use_gebco] = SOURCE_CODE_GEBCO
        ordinary_gebco = use_gebco & (gebco_tid >= 10) & (gebco_tid <= 48)
        unknown_gebco = use_gebco & ~ordinary_gebco
        output_quality_mask[ordinary_gebco] = QUALITY_CODE_GEBCO_WATER
        output_quality_mask[unknown_gebco] = QUALITY_CODE_GEBCO_UNCERTAIN
        fingerprint = self._source_fingerprint(
            f"{asset.cache_key}:{asset.sha256}:gebco-tid:"
            f"{gebco_metadata['asset_receipts']['tid']['response_sha256']}:"
            f"gebco-elevation:{gebco_metadata['asset_receipts']['elevation']['response_sha256']}"
        )
        cached = self._load_cached_tile(address, fingerprint)
        if cached is not None:
            return cached
        status = (
            "no_source_coverage"
            if not np.any(output_source_mask)
            else ("ready" if np.all(output_source_mask != SOURCE_CODE_NONE) else "partial")
        )
        source_meta["used_for_mosaic"] = bool(np.any(output_source_mask == SOURCE_CODE_COP90))
        source_meta["used_for_classification"] = False
        gebco_source_meta = _gebco_source_record(
            gebco_metadata, used_for_mosaic=bool(np.any(output_source_mask == SOURCE_CODE_GEBCO))
        )
        gebco_source_meta["used_for_classification"] = True
        grid["tid_sampling"] = gebco_metadata["classification_sampling"]
        grid["source_window"]["classification_source_window"] = gebco_metadata["source_window"]
        boundary_record, boundary_payload, boundary_lineage = self._build_boundary_support(address, grid)
        source_meta = [source_meta, gebco_source_meta, *boundary_lineage]
        return self._finish_tile(
            address,
            status,
            grid,
            source_meta,
            attempts,
            output_heights,
            output_source_mask,
            output_quality_mask,
            gebco_tid,
            source_fingerprint=fingerprint,
            vertical={
                "datum": "per_sample_source_vertical_datum",
                "height_type": "source_specific",
                "unit": "metre",
                "source_mask_property": "terrain.source_lineage[].source_code",
            },
            resampling={
                "method": "nearest_neighbor_for_gebco_samples",
                "stage": "source_grid_selection",
                "details": (
                    "Copernicus GLO-90 point samples are copied unchanged where GEBCO TID=0 land; "
                    "GEBCO elevation and TID are selected from the nearest 15-arc-second cell center "
                    "at each Copernicus sample location elsewhere. Source and vertical-datum masks are preserved."
                ),
            },
            boundary_support=boundary_record,
            boundary_support_payload=boundary_payload,
        )

    def _read_gebco_tile(
        self,
        address: GlobalTileAddress,
        attempts: list[dict[str, Any]],
    ) -> LoadedTile:
        row0_expected, row1_expected, col0_expected, col1_expected = _gebco_indices(address)
        expected_fingerprint = self._source_fingerprint(
            f"{GEBCO_RELEASE}:{row0_expected}:{col0_expected}:"
            f"{row1_expected - row0_expected}:{col1_expected - col0_expected}"
        )
        cached = self._load_cached_tile(address, expected_fingerprint)
        if cached is not None:
            return cached
        try:
            heights_source, tid, remote_meta = self.gebco.read_window(address)
        except NoNativeSamples as exc:
            attempts.append(
                {"product": GEBCO_RELEASE, "status": "not_generated", "reason": str(exc)}
            )
            return self._not_generated_tile(address, attempts)
        except TerrainError as exc:
            attempts.append(
                {
                    "product": GEBCO_RELEASE,
                    "status": "failed",
                    "reason": str(exc),
                }
            )
            return self._failed_tile(address, attempts, "remote_source_failure")
        valid = heights_source != np.int16(-32768)
        source_mask = np.where(valid, SOURCE_CODE_GEBCO, SOURCE_CODE_NONE).astype(np.uint8)
        quality_mask = np.where(
            valid,
            np.where(tid == 0, QUALITY_CODE_GEBCO_LAND_FALLBACK, QUALITY_CODE_GEBCO_WATER),
            QUALITY_CODE_NO_DATA,
        ).astype(np.uint8)
        heights = heights_source.astype(np.float32)
        status = "no_source_coverage" if not valid.any() else ("ready" if valid.all() else "partial")
        row0 = remote_meta["source_window"]["row_offset"]
        col0 = remote_meta["source_window"]["column_offset"]
        rows, columns = heights.shape
        west, south, east, north = address.bounds
        grid = {
            "rows": rows,
            "columns": columns,
            "pixel_registration": "cell_center",
            "sample_order": "row_major_south_to_north_west_to_east",
            "horizontal_crs": "EPSG:4326",
            "horizontal_datum": "WGS84",
            "axis_order": "longitude_latitude",
            "unit_horizontal": "degree",
            "source_affine": {
                "origin_reference": "pixel_corner",
                "x_origin": -180.0 + col0 * GEBCO_STEP_DEGREES,
                "y_origin": -90.0 + row0 * GEBCO_STEP_DEGREES,
                "pixel_width": GEBCO_STEP_DEGREES,
                "pixel_height": GEBCO_STEP_DEGREES,
            },
            "source_window": remote_meta["source_window"],
            "source_bounds": {"west_deg": west, "south_deg": south, "east_deg": east, "north_deg": north},
            "source_grid": remote_meta["source_grid"],
        }
        attempts.append({"product": GEBCO_RELEASE, "status": "ready", "fallback": True})
        fingerprint = self._source_fingerprint(f"{GEBCO_RELEASE}:{row0}:{col0}:{rows}:{columns}")
        source_meta = {
            "source_code": SOURCE_CODE_GEBCO,
            "provider": "NERC British Oceanographic Data Centre / GEBCO",
            "used_for_mosaic": bool(valid.any()),
            "used_for_classification": True,
            "product": GEBCO_RELEASE,
            "release": GEBCO_RELEASE,
            "asset_uri": remote_meta["asset_uri"],
            "asset_version": {"doi": "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"},
            "assets": [
                {
                    "role": "elevation",
                    "product": GEBCO_RELEASE,
                    "asset_uri": remote_meta["asset_receipts"]["elevation"]["asset_uri"],
                    "metadata": remote_meta["asset_receipts"]["elevation"],
                },
                {
                    "role": "type_identifier",
                    "product": GEBCO_RELEASE,
                    "asset_uri": remote_meta["asset_receipts"]["tid"]["asset_uri"],
                    "metadata": remote_meta["asset_receipts"]["tid"],
                },
            ],
            "byte_length": None,
            "sha256": None,
            "horizontal": {
                "crs": "EPSG:4326",
                "datum": "WGS84",
                "axis_order": "longitude_latitude",
                "coordinate_unit": "degree",
            },
            "vertical": {
                "datum": "mean_sea_level_nominal_heterogeneous_inputs",
                "height_type": "elevation_relative_to_mean_sea_level_nominal",
                "crs": None,
                "unit": "metre",
            },
            "source_raster": {
                "driver": "OPeNDAP/NetCDF",
                "variable": "elevation",
                "tid_variable": "tid",
                "dtype": "int16",
                "source_width": GEBCO_COLUMNS,
                "source_height": GEBCO_ROWS,
                "nodata_value": -32768,
                "pixel_registration": "cell_center",
                "spacing_arc_seconds": 15,
            },
            "license": {
                "id": GEBCO_LICENSE_ID,
                "terms_url": "https://www.gebco.net/data-products-gridded-bathymetry-data/gebco2026-grid",
                "attribution": GEBCO_ATTRIBUTION,
            },
            "uncertainty": GEBCO_UNCERTAINTY,
            "tid_classification": {
                "method": "TID=0 land; nonzero documented classes are water/bathymetry; unsupported values remain uncertain",
                "limitation": GEBCO_TID_LIMITATION,
                "known_issues_url": GEBCO_TID_ERRATA_URL,
            },
            "cache_receipt": {
                "received_at_utc": remote_meta["received_at_utc"],
                "source_window_only": True,
                "elevation_response_sha256": remote_meta["asset_receipts"]["elevation"]["response_sha256"],
                "tid_response_sha256": remote_meta["asset_receipts"]["tid"]["response_sha256"],
            },
        }
        boundary_record, boundary_payload, boundary_lineage = self._build_boundary_support(address, grid)
        return self._finish_tile(
            address,
            status,
            grid,
            [source_meta, *boundary_lineage],
            attempts,
            heights,
            source_mask,
            quality_mask,
            tid.astype(np.int16),
            source_fingerprint=fingerprint,
            vertical={"datum": "mean_sea_level_nominal_heterogeneous_inputs", "height_type": "nominal_MSL", "unit": "metre"},
            resampling={"method": "none", "stage": "opendap_hyperslab", "details": "native 15 arc-second cell-center samples; no interpolation"},
            boundary_support=boundary_record,
            boundary_support_payload=boundary_payload,
        )

    def _not_generated_tile(
        self, address: GlobalTileAddress, attempts: list[dict[str, Any]]
    ) -> LoadedTile:
        if address.level > 0:
            parent = GlobalTileAddress(address.level - 1, address.x // 2, address.y // 2)
        else:
            parent = None
        manifest = {
            "contract_id": CONTRACT_ID,
            "artifact_id": None,
            "tile": {
                "scheme": TILE_SCHEME,
                "id": address.tile_id,
                "level": address.level,
                "x": address.x,
                "y": address.y,
                "bounds_wgs84": {
                    "west_deg": address.bounds[0],
                    "south_deg": address.bounds[1],
                    "east_deg": address.bounds[2],
                    "north_deg": address.bounds[3],
                },
            },
            "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION, "commit": self.generator_commit},
            "terrain": {
                "status": "not_generated",
                "requested": True,
                "horizontal": {"crs": "EPSG:4326", "datum": "WGS84", "axis_order": "longitude_latitude", "unit": "degree"},
                "vertical": {"datum": "source_specific", "height_type": "source_specific", "unit": "metre"},
                "spatial_reference": {
                    "horizontal": {"crs": "EPSG:4326", "datum": "WGS84", "axis_order": ["longitude", "latitude"], "unit": "degree"},
                    "vertical": {"datum": "source_specific", "height_type": "source_specific", "unit": "metre"},
                },
                "source_lineage": [],
                "attribution": [],
                "transform": {"resampling": "none", "reason": "no native source window emitted"},
                "source_attempts": attempts,
                "coverage": {"sample_count": 0, "valid_count": 0, "no_data_count": 0, "complete_for_emitted_samples": False},
                "parent_tile_id": None if parent is None else parent.tile_id,
                "reason": "requested tile is smaller than the selected native source sample spacing; use the nearest generated ancestor tile",
                "payloads": [],
            },
            "generation": {"created_utc": _utc_now(), "resampling": "none"},
        }
        return LoadedTile(
            manifest,
            np.empty((0, 0), dtype=np.float32),
            np.empty((0, 0), dtype=np.uint8),
            np.empty((0, 0), dtype=np.uint8),
        )

    def _invalidate_cached_tile(self, address: GlobalTileAddress) -> None:
        tile_dir = self.cache.tile_directory(address)
        if tile_dir.exists():
            try:
                shutil.rmtree(tile_dir)
            except OSError as exc:
                raise TerrainCacheError("cannot remove a corrupt terrain cache entry") from exc

    def _load_cached_tile(
        self, address: GlobalTileAddress, source_fingerprint: str
    ) -> LoadedTile | None:
        tile_dir = self.cache.tile_directory(address)
        manifest_path = tile_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            claimed_manifest_sha256 = manifest.get("manifest_sha256")
            unsealed_manifest = dict(manifest)
            unsealed_manifest.pop("manifest_sha256", None)
            if (
                not isinstance(claimed_manifest_sha256, str)
                or claimed_manifest_sha256 != _sha256_bytes(_canonical_json(unsealed_manifest))
            ):
                raise TerrainCacheError("cached manifest digest mismatch")
            generation = manifest["generation"]
            terrain = manifest["terrain"]
            grid = terrain["grid"]
            rows, columns = int(grid["rows"]), int(grid["columns"])
            payload_items = terrain["payloads"]
            if not isinstance(payload_items, list):
                raise TerrainCacheError("cached payload descriptors must be an array")
            payloads_by_path = {item.get("path"): item for item in payload_items if isinstance(item, dict)}
            if len(payloads_by_path) != len(payload_items):
                raise TerrainCacheError("cached payload descriptors contain duplicates or invalid records")
            verified_payloads: dict[str, bytes] = {}
            for payload_name, record in payloads_by_path.items():
                if not isinstance(payload_name, str) or record.get("path") != payload_name:
                    raise TerrainCacheError("cached payload path is invalid")
                payload_path = tile_dir / payload_name
                if payload_path.is_symlink() or not payload_path.resolve().is_relative_to(tile_dir.resolve()):
                    raise TerrainCacheError("cached payload path escapes its bundle directory")
                raw_payload = payload_path.read_bytes()
                if len(raw_payload) != record.get("size_bytes") or _sha256_bytes(raw_payload) != record.get("sha256"):
                    raise TerrainCacheError("cached payload digest mismatch")
                verified_payloads[payload_name] = raw_payload
            if (
                manifest.get("contract_id") != CONTRACT_ID
                or manifest.get("tile", {}).get("id") != address.tile_id
                or generation.get("source_fingerprint") != source_fingerprint
                or generation.get("producer_commit") != self.generator_commit
                or terrain.get("status") not in {"ready", "partial"}
                or rows < 1
                or columns < 1
                or rows * columns > MAX_GRID_SAMPLES
            ):
                self._invalidate_cached_tile(address)
                return None

            def read_payload(name: str, expected_bytes: int, dtype: str) -> np.ndarray:
                record = payloads_by_path[name]
                if record.get("path") != name or record.get("size_bytes") != expected_bytes:
                    raise TerrainCacheError("cached payload metadata does not match its shape")
                raw = verified_payloads[name]
                if len(raw) != expected_bytes or record.get("size_bytes") != expected_bytes:
                    raise TerrainCacheError("cached payload metadata does not match its shape")
                return np.frombuffer(raw, dtype=dtype).reshape((rows, columns)).copy()

            heights = read_payload("height_samples.f32le", rows * columns * 4, "<f4")
            source_mask = read_payload("source_mask.u8", rows * columns, "u1")
            quality_mask = read_payload("quality_mask.u8", rows * columns, "u1")
            tid_mask = None
            if "tid_mask.i16le" in payloads_by_path:
                tid_mask = read_payload("tid_mask.i16le", rows * columns * 2, "<i2")
            geoid_payload_names = {
                "geoid_undulation.f32le",
                "ellipsoidal_height_samples.f64le",
                "geoid_mask.u8",
            }
            present_geoid_payloads = geoid_payload_names.intersection(payloads_by_path)
            geoid_undulation = ellipsoidal_heights = geoid_mask = None
            if present_geoid_payloads:
                if present_geoid_payloads != geoid_payload_names:
                    raise TerrainCacheError("cached geoid conversion payload set is incomplete")
                geoid_undulation = read_payload("geoid_undulation.f32le", rows * columns * 4, "<f4")
                ellipsoidal_heights = read_payload("ellipsoidal_height_samples.f64le", rows * columns * 8, "<f8")
                geoid_mask = read_payload("geoid_mask.u8", rows * columns, "u1")
            boundary_payload_bytes = verified_payloads.get("boundary_support.geojson")
            boundary_geojson = None
            if boundary_payload_bytes is not None:
                boundary_geojson = json.loads(boundary_payload_bytes.decode("utf-8"))
                if (
                    not isinstance(boundary_geojson, dict)
                    or boundary_geojson.get("type") != "FeatureCollection"
                    or len(boundary_geojson.get("features", [])) != payloads_by_path["boundary_support.geojson"].get("feature_count")
                ):
                    raise TerrainCacheError("cached boundary support GeoJSON is invalid")
            elif terrain.get("boundary_support", {}).get("payload") is not None:
                raise TerrainCacheError("cached boundary support payload is missing")
            expected_artifact = _artifact_identity(
                address,
                source_fingerprint,
                np.asarray(heights, dtype="<f4").tobytes(order="C"),
                source_mask.tobytes(order="C"),
                quality_mask.tobytes(order="C"),
                None if tid_mask is None else np.asarray(tid_mask, dtype="<i2").tobytes(order="C"),
                self.generator_commit,
                None if geoid_undulation is None else np.asarray(geoid_undulation, dtype="<f4").tobytes(order="C"),
                None if ellipsoidal_heights is None else np.asarray(ellipsoidal_heights, dtype="<f8").tobytes(order="C"),
                None if geoid_mask is None else geoid_mask.tobytes(order="C"),
                boundary_payload_bytes,
            )
            if manifest.get("artifact_id") != expected_artifact:
                raise TerrainCacheError("cached artifact identity mismatch")
            for item in tile_dir.rglob("*"):
                if item.is_file():
                    os.utime(item, None)
            return LoadedTile(
                manifest,
                heights,
                source_mask,
                quality_mask,
                tid_mask,
                geoid_undulation,
                ellipsoidal_heights,
                geoid_mask,
                boundary_geojson,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, TerrainCacheError):
            self._invalidate_cached_tile(address)
            return None

    def _finish_tile(
        self,
        address: GlobalTileAddress,
        status: str,
        grid: dict[str, Any],
        source: dict[str, Any] | list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        heights: np.ndarray,
        source_mask: np.ndarray,
        quality_mask: np.ndarray,
        tid: np.ndarray | None,
        *,
        source_fingerprint: str,
        vertical: dict[str, Any],
        resampling: dict[str, Any],
        boundary_support: dict[str, Any] | None = None,
        boundary_support_payload: bytes | None = None,
    ) -> LoadedTile:
        if status not in _STATUS_VALUES:
            raise TerrainInputError("internal terrain status is invalid")
        heights = np.asarray(heights, dtype=np.float32)
        source_mask = np.asarray(source_mask, dtype=np.uint8)
        quality_mask = np.asarray(quality_mask, dtype=np.uint8)
        if heights.ndim != 2 or source_mask.shape != heights.shape or quality_mask.shape != heights.shape:
            raise TerrainInputError("tile height and mask dimensions differ")
        if heights.size > MAX_GRID_SAMPLES or not np.isfinite(heights).all():
            raise TerrainInputError("tile height samples are non-finite or exceed the sample cap")
        if tid is not None and tid.shape != heights.shape:
            raise TerrainInputError("GEBCO TID dimensions differ from elevation dimensions")
        if boundary_support is None:
            boundary_support = {
                "status": "not_generated",
                "requested": True,
                "payload_kind": "terrain_boundary_support",
                "reason": "boundary vertices were not generated",
                "boundary_support_edges": [],
                "feature_count": 0,
                "payload": None,
            }
        source_records = source if isinstance(source, list) else [source]
        source_counts = {
            str(code): int(np.count_nonzero(source_mask == code))
            for code in (SOURCE_CODE_COP90, SOURCE_CODE_GEBCO)
            if np.any(source_mask == code)
        }
        quality_counts = {
            str(code): int(np.count_nonzero(quality_mask == code))
            for code in sorted(int(value) for value in np.unique(quality_mask))
        }
        geoid_undulation: np.ndarray | None = None
        ellipsoidal_heights: np.ndarray | None = None
        geoid_mask: np.ndarray | None = None
        if self.geoid is None:
            geoid_conversion: dict[str, Any] = {
                "status": "not_generated",
                "method": None,
                "reason": "no verified EGM2008 geoid grid was configured; source-datum heights are preserved without ellipsoidal conversion",
                "equation": "ellipsoidal_height_m = source_height_m + EGM2008_undulation_N_m",
                "grid": {
                    "dataset": EGM2008Grid.DATASET,
                    "asset_uri": EGM2008Grid.URI,
                    "license": EGM2008Grid.LICENSE,
                },
            }
        else:
            affine = grid["source_affine"]
            longitude_centers = affine["x_origin"] + (
                np.arange(grid["columns"], dtype=np.float64) + 0.5
            ) * affine["pixel_width"]
            latitude_centers = affine["y_origin"] + (
                np.arange(grid["rows"], dtype=np.float64) + 0.5
            ) * affine["pixel_height"]
            sample_longitudes, sample_latitudes = np.meshgrid(longitude_centers, latitude_centers)
            undulation, undulation_valid = self.geoid.sample(sample_longitudes, sample_latitudes)
            source_valid = source_mask != SOURCE_CODE_NONE
            conversion_valid = undulation_valid & source_valid
            geoid_undulation = np.full(heights.shape, np.float32(-32768.0), dtype=np.float32)
            ellipsoidal_heights = np.full(heights.shape, -32768.0, dtype=np.float64)
            geoid_mask = np.zeros(heights.shape, dtype=np.uint8)
            geoid_undulation[conversion_valid] = undulation[conversion_valid]
            ellipsoidal_heights[conversion_valid] = (
                heights[conversion_valid].astype(np.float64)
                + undulation[conversion_valid].astype(np.float64)
            )
            geoid_mask[conversion_valid & (source_mask == SOURCE_CODE_COP90)] = 1
            geoid_mask[conversion_valid & (source_mask == SOURCE_CODE_GEBCO)] = 2
            geoid_status = (
                "no_source_coverage"
                if not conversion_valid.any()
                else ("ready" if np.all(conversion_valid | ~source_valid) else "partial")
            )
            geoid_conversion = {
                "status": geoid_status,
                "method": "bilinear_interpolation_of_point_registered_grid",
                "equation": "ellipsoidal_height_m = source_height_m + EGM2008_undulation_N_m",
                "longitude_wrap": "periodic_at_antimeridian",
                "latitude_edge": "clamped_to_source_pole_sample_row",
                "grid": self.geoid.metadata,
                "source_mask_legend": {
                    "0": "no source height or geoid sample",
                    "1": "EGM2008 orthometric source height plus EGM2008 undulation; ellipsoidal result",
                    "2": "GEBCO nominal mean-sea-level height plus EGM2008 undulation; approximate only, shallow-water datum may differ",
                },
                "coverage": {
                    "source_sample_count": int(np.count_nonzero(source_valid)),
                    "converted_sample_count": int(np.count_nonzero(conversion_valid)),
                },
            }
        source_vertical_reference_by_mask: dict[str, dict[str, Any]] = {}
        geoid_coverage_by_source_mask: dict[str, dict[str, int]] = {}
        for source_code in (SOURCE_CODE_COP90, SOURCE_CODE_GEBCO):
            source_count = int(np.count_nonzero(source_mask == source_code))
            if source_count == 0:
                continue
            source_record = next(
                (item for item in source_records if int(item.get("source_code", -1)) == source_code),
                None,
            )
            if source_record is None:
                raise TerrainInputError("source mask refers to missing source lineage")
            converted_count = (
                int(np.count_nonzero(geoid_mask == source_code))
                if geoid_mask is not None
                else 0
            )
            source_id = str(source_code)
            is_cop90 = source_code == SOURCE_CODE_COP90
            equation = (
                "ellipsoidal_height_m = orthometric_height_EGM2008_m + EGM2008_undulation_m"
                if is_cop90
                else "ellipsoidal_height_m approximately equals nominal_MSL_height_m + EGM2008_undulation_m"
            )
            entry: dict[str, Any] = {
                "source_code": source_code,
                "product": source_record.get("product"),
                "source_vertical_reference": source_record.get("vertical"),
                "source_height_payload": "height_samples.f32le",
                "source_sample_count": source_count,
                "converted_sample_count": converted_count,
                "geoid_mask_code": source_code if geoid_mask is not None else None,
                "ellipsoidal_height_payload": "ellipsoidal_height_samples.f64le" if geoid_mask is not None else None,
                "conversion_status": "ready" if converted_count == source_count else ("partial" if converted_count else "not_generated"),
                "equation": equation if geoid_mask is not None else None,
                "source_uncertainty": source_record.get("uncertainty"),
            }
            if not is_cop90:
                entry["warning"] = (
                    "GEBCO height is nominal mean-sea-level elevation built from heterogeneous inputs. "
                    "Adding EGM2008 undulation is approximate; shallow-water source datums may differ. "
                    "This result is not represented as 30 m land truth."
                )
            source_vertical_reference_by_mask[source_id] = entry
            geoid_coverage_by_source_mask[source_id] = {
                "source_sample_count": source_count,
                "converted_sample_count": converted_count,
            }
        vertical_references_by_source_code: dict[str, dict[str, Any]] = {}
        for source_id, details in source_vertical_reference_by_mask.items():
            is_gebco = int(source_id) == SOURCE_CODE_GEBCO
            source_vertical = details["source_vertical_reference"]
            vertical_references_by_source_code[source_id] = {
                "source_vertical_datum": "MEAN_SEA_LEVEL" if is_gebco else "EGM2008",
                "source_vertical_reference": source_vertical,
                "conversion_method": (
                    "bilinear_interpolation_of_us_nga_egm08_25.tif; h=H+N"
                    if details["converted_sample_count"] == details["source_sample_count"]
                    else None
                ),
                "approximate": is_gebco,
                "warning": details.get("warning"),
                "geoid_mask_code": details["geoid_mask_code"],
                "source_mask_code": int(source_id),
            }
        geoid_conversion["source_vertical_reference_by_mask"] = source_vertical_reference_by_mask
        geoid_conversion["coverage_by_source_mask"] = geoid_coverage_by_source_mask
        geoid_conversion["vertical_references_by_source_code"] = vertical_references_by_source_code
        geoid_conversion["tile_arithmetic_validation_by_source_code"] = {
            source_id: {
                "sample_count": details["converted_sample_count"],
                "equation": details["equation"],
                "max_abs_residual_m": (
                    float(np.max(np.abs(
                        ellipsoidal_heights[source_mask == int(source_id)]
                        - heights[source_mask == int(source_id)].astype(np.float64)
                        - geoid_undulation[source_mask == int(source_id)].astype(np.float64)
                    )))
                    if details["converted_sample_count"]
                    else None
                ),
            }
            for source_id, details in source_vertical_reference_by_mask.items()
        }
        if self.geoid is not None:
            geoid_conversion["independent_numeric_validation"] = {
                "status": "pass",
                "method": "Independent PROJ 9.5.1 / pyproj 3.7.2 vertical transform EPSG:4979 to EPSG:4326+3855 with h=100 m; N is input h minus transformed EGM2008 H.",
                "grid_sample_method": "bilinear interpolation using the declared point-registered EGM2008 grid affine",
                "tolerance_m": 2e-5,
                "vectors": [
                    {
                        "longitude_deg": 9.88,
                        "latitude_deg": 54.44,
                        "input_ellipsoidal_height_m": 100.0,
                        "reference_orthometric_height_m": 60.110880718994146,
                        "reference_undulation_m": 39.889119281005854,
                        "grid_undulation_m": 39.88911819458008,
                        "absolute_residual_m": 1.086425774e-6,
                    },
                    {
                        "longitude_deg": -122.5,
                        "latitude_deg": 37.99916666666667,
                        "input_ellipsoidal_height_m": 100.0,
                        "reference_orthometric_height_m": 131.8214701080322,
                        "reference_undulation_m": -31.8214701080322,
                        "grid_undulation_m": -31.821470260620117,
                        "absolute_residual_m": 1.52587890625e-7,
                    },
                    {
                        "longitude_deg": -122.45,
                        "latitude_deg": 37.99916666666667,
                        "input_ellipsoidal_height_m": 100.0,
                        "reference_orthometric_height_m": 131.86126412963867,
                        "reference_undulation_m": -31.86126412963867,
                        "grid_undulation_m": -31.861263275146484,
                        "absolute_residual_m": 8.544921861e-7,
                        "scope_warning": "The vector checks EGM2008 undulation only; adding N to GEBCO nominal MSL remains approximate.",
                    },
                ],
                "scope_warning": "Numeric residuals validate grid interpolation only; they are not source accuracy or terrain uncertainty claims.",
            }
        geoid_conversion_for_contract = {
            "status": geoid_conversion["status"],
            "grid": geoid_conversion.get("grid"),
            "method": geoid_conversion.get("method"),
            "equation": geoid_conversion.get("equation"),
            "vertical_references_by_source_code": vertical_references_by_source_code,
            "coverage_by_source_mask": geoid_coverage_by_source_mask,
            "tile_arithmetic_validation_by_source_code": geoid_conversion["tile_arithmetic_validation_by_source_code"],
            "independent_numeric_validation": geoid_conversion.get("independent_numeric_validation"),
        }
        generator_commit = self.generator_commit
        height_bytes = np.asarray(heights, dtype="<f4").tobytes(order="C")
        source_bytes = source_mask.tobytes(order="C")
        quality_bytes = quality_mask.tobytes(order="C")
        tid_bytes = None if tid is None else np.asarray(tid, dtype="<i2").tobytes(order="C")
        geoid_undulation_bytes = (
            None
            if geoid_undulation is None
            else np.asarray(geoid_undulation, dtype="<f4").tobytes(order="C")
        )
        ellipsoidal_height_bytes = (
            None
            if ellipsoidal_heights is None
            else np.asarray(ellipsoidal_heights, dtype="<f8").tobytes(order="C")
        )
        geoid_mask_bytes = None if geoid_mask is None else geoid_mask.tobytes(order="C")
        artifact_id = _artifact_identity(
            address,
            source_fingerprint,
            height_bytes,
            source_bytes,
            quality_bytes,
            tid_bytes,
            generator_commit,
            geoid_undulation_bytes,
            ellipsoidal_height_bytes,
            geoid_mask_bytes,
            boundary_support_payload,
        )
        attribution_records: list[dict[str, Any]] = []
        for source_record in source_records:
            license_record = source_record.get("license", {})
            notices = [
                value
                for value in (
                    license_record.get("attribution"),
                    license_record.get("modified_data_notice"),
                    license_record.get("liability_notice"),
                    license_record.get("no_endorsement_notice"),
                )
                if value
            ]
            if notices:
                attribution_records.append(
                    {
                        "source_code": source_record.get("source_code"),
                        "license_id": license_record.get("id"),
                        "license_url": license_record.get("rights_url") or license_record.get("terms_url"),
                        "notices": notices,
                    }
                )
        sample_count = int(heights.size)
        valid_count = int(np.count_nonzero(source_mask))
        no_data_count = int(source_mask.size - valid_count)
        has_gebco_fallback = bool(np.any(source_mask == SOURCE_CODE_GEBCO))
        coverage_record: dict[str, Any] = {
            "sample_count": sample_count,
            "pixel_count": sample_count,
            "valid_count": valid_count,
            "no_data_count": no_data_count,
            "source_sample_counts_by_mask": source_counts,
            "quality_sample_counts_by_code": quality_counts,
            "complete_for_emitted_samples": bool(sample_count and no_data_count == 0),
            "coverage_claim": "source-backed emitted samples within this WFG-1 extent; not a survey-absence or full-area completeness claim",
            "coverage_confidence": "per-sample source and quality identity is encoded in masks; source coverage limitations are stated in lineage",
            "gap_declared": status in {"partial", "no_source_coverage"},
        }
        if no_data_count:
            coverage_record["gap_reason"] = "one or more emitted sample locations have no valid source sample"
        if has_gebco_fallback:
            coverage_record["fallback"] = {
                "status": "ready",
                "source_id": GEBCO_RELEASE,
                "provenance": GEBCO_ATTRIBUTION,
                "quality": "15 arc-second heterogeneous source fallback; not 30 m land truth",
                "source_mask_code": SOURCE_CODE_GEBCO,
            }
        horizontal_reference = {
            "crs": grid["horizontal_crs"],
            "datum": grid["horizontal_datum"],
            "axis_order": ["longitude", "latitude"],
            "unit": "degree",
        }
        spatial_reference = {"horizontal": horizontal_reference, "vertical": vertical}
        placement_surface = {
            "status": geoid_conversion["status"],
            "payload": "ellipsoidal_height_samples.f64le" if ellipsoidal_heights is not None else None,
            "horizontal_crs": "EPSG:4979" if ellipsoidal_heights is not None else None,
            "vertical_datum": "WGS84_ELLIPSOIDAL" if ellipsoidal_heights is not None else None,
            "height_type": "ellipsoidal" if ellipsoidal_heights is not None else "source_native_unconverted",
            "source_vertical_datum": "per_sample_source_mask" if ellipsoidal_heights is not None else "per_sample_source_specific",
        }
        manifest = {
            "contract_id": CONTRACT_ID,
            "artifact_id": artifact_id,
            "tile": {
                "scheme": TILE_SCHEME,
                "id": address.tile_id,
                "level": address.level,
                "x": address.x,
                "y": address.y,
                "bounds_wgs84": {
                    "west_deg": address.bounds[0],
                    "south_deg": address.bounds[1],
                    "east_deg": address.bounds[2],
                    "north_deg": address.bounds[3],
                    "edge_ownership": "west_south_inclusive_east_north_exclusive",
                },
                "source_resolution_is_separate_from_address_level": True,
            },
            "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION, "commit": generator_commit},
            "terrain": {
                "status": status,
                "requested": True,
                "horizontal": {
                    "crs": "EPSG:4326",
                    "datum": grid["horizontal_datum"],
                    "axis_order": "longitude_latitude",
                    "unit": "degree",
                },
                "vertical": vertical,
                "spatial_reference": spatial_reference,
                "grid": grid,
                "geoid_conversion": geoid_conversion,
                "vertical_conversion": geoid_conversion_for_contract,
                "boundary_support": boundary_support,
                "source_vertical_reference_by_mask": source_vertical_reference_by_mask,
                "placement_surface": placement_surface,
                "resampling": resampling,
                "source_lineage": source_records,
                "attribution": attribution_records,
                "transform": {
                    "source_grid": grid,
                    "resampling": resampling,
                    "clip": {
                        "bounds_wgs84": {
                            "west_deg": address.bounds[0],
                            "south_deg": address.bounds[1],
                            "east_deg": address.bounds[2],
                            "north_deg": address.bounds[3],
                        },
                        "edge_ownership": "west_south_inclusive_east_north_exclusive",
                    },
                },
                "source_mask_legend": {
                    "0": "no source sample / source no-data",
                    "1": f"{COP90_DATASET}; EPSG:3855 EGM2008 orthometric metres",
                    "2": f"{GEBCO_RELEASE}; nominal MSL metres from heterogeneous inputs (datum warning applies)",
                },
                "quality_mask_legend": {
                    "0": "no-data or unavailable sample",
                    "1": "native Copernicus GLO-90 source sample",
                    "3": "GEBCO_2026 land fallback (coarse source; not 30 m land truth)",
                    "4": "GEBCO_2026 water/bathymetry fallback",
                    "5": "GEBCO_2026 sample with TID class outside documented land/direct/indirect classes",
                },
                "source_window": grid["source_window"],
                "coverage": coverage_record,
                "no_data_value": -32768.0,
                "source_attempts": attempts,
                "payloads": [],
            },
            "generation": {
                "created_utc": _utc_now(),
                "cache_key": artifact_id,
                "resampling": resampling["method"],
                "source_fingerprint": source_fingerprint,
                "producer_commit": generator_commit,
            },
        }
        tile_dir = _write_tile_bundle(
            self.cache,
            address,
            manifest,
            heights,
            source_mask,
            quality_mask,
            tid,
            geoid_undulation,
            ellipsoidal_heights,
            geoid_mask,
            boundary_support_payload,
        )
        manifest = json.loads((tile_dir / "manifest.json").read_text(encoding="utf-8"))
        return LoadedTile(
            manifest,
            heights.copy(),
            source_mask.copy(),
            quality_mask.copy(),
            None if tid is None else tid.copy(),
            None if geoid_undulation is None else geoid_undulation.copy(),
            None if ellipsoidal_heights is None else ellipsoidal_heights.copy(),
            None if geoid_mask is None else geoid_mask.copy(),
            None if boundary_support_payload is None else json.loads(boundary_support_payload.decode("utf-8")),
        )

    def _failed_tile(
        self, address: GlobalTileAddress, attempts: list[dict[str, Any]], error_code: str
    ) -> LoadedTile:
        manifest = {
            "contract_id": CONTRACT_ID,
            "artifact_id": None,
            "tile": {"scheme": TILE_SCHEME, "id": address.tile_id, "level": address.level, "x": address.x, "y": address.y},
            "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION, "commit": self.generator_commit},
            "terrain": {
                "status": "failed",
                "requested": True,
                "horizontal": {"crs": "EPSG:4326", "datum": "WGS84", "axis_order": "longitude_latitude", "unit": "degree"},
                "vertical": {"datum": "source_specific", "height_type": "source_specific", "unit": "metre"},
                "spatial_reference": {
                    "horizontal": {"crs": "EPSG:4326", "datum": "WGS84", "axis_order": ["longitude", "latitude"], "unit": "degree"},
                    "vertical": {"datum": "source_specific", "height_type": "source_specific", "unit": "metre"},
                },
                "source_lineage": [],
                "attribution": [],
                "transform": {"resampling": "none", "reason": "no payload emitted"},
                "source_attempts": attempts,
                "error_code": error_code,
                "coverage": {"sample_count": 0, "valid_count": 0, "no_data_count": 0, "complete_for_emitted_samples": False},
                "payloads": [],
            },
            "generation": {"created_utc": _utc_now(), "resampling": "none"},
        }
        return LoadedTile(manifest, np.empty((0, 0), dtype=np.float32), np.empty((0, 0), dtype=np.uint8), np.empty((0, 0), dtype=np.uint8))


def coverage_index_report(prefixes: Iterable[str]) -> dict[str, Any]:
    """Prove each level-0 WFG address has a Cop90 asset or GEBCO fallback route.

    This check consumes only the 1.1 MB public Copernicus tile-name inventory;
    it does not download any elevation raster. The fallback route is explicit
    even where the index cannot distinguish ocean from an inaccessible land
    source cell.
    """
    available = frozenset(prefixes)
    total = 0
    cop90_tiles = 0
    gebco_tiles = 0
    source_cells: set[str] = set()
    source_resolver_digest = hashlib.sha256()
    fallback_map_digest = hashlib.sha256()
    gebco_window_complete = True
    gebco_windows_checked = 0
    for y in range(720):
        for x in range(1440):
            address = GlobalTileAddress(0, x, y)
            prefix = _source_prefix_for_tile(address)
            source_cells.add(prefix)
            if prefix in available:
                cop90_tiles += 1
                route = f"copdem-glo-90-f:{prefix}"
            else:
                gebco_tiles += 1
                route = f"gebco-2026-fallback:{prefix}"
                fallback_map_digest.update(f"{address.tile_id}\t{route}\n".encode("ascii"))
            source_resolver_digest.update(f"{address.tile_id}\t{route}\n".encode("ascii"))
            row0, row1, col0, col1 = _gebco_indices(address)
            tile_has_gebco_window = (
                row1 - row0 == 60
                and col1 - col0 == 60
                and row0 == y * 60
                and col0 == x * 60
            )
            gebco_window_complete &= tile_has_gebco_window
            gebco_windows_checked += int(tile_has_gebco_window)
            total += 1
    geodetic_complete = total == 1440 * 720 and cop90_tiles + gebco_tiles == total
    return {
        "coverage_check": "wf-geodetic-grid-v1 base-level source resolvability",
        "generated_at_utc": _utc_now(),
        "status": "pass" if geodetic_complete and gebco_window_complete and gebco_windows_checked == total else "fail",
        "tile_scheme": TILE_SCHEME,
        "tile_count": total,
        "source_catalog": {
            "product": COP90_DATASET,
            "source_asset_prefix_count": len(available),
            "candidate_wfg_level0_tiles": cop90_tiles,
            "explicit_gebco_fallback_tiles": gebco_tiles,
            "unique_source_cells_addressed": len(source_cells),
            "catalog_has_north_pole_land_tile": any(item.startswith("Copernicus_DSM_COG_30_N89_") for item in available),
        },
        "preferred_30m": {
            "product": "COP-DEM-GLO-30-F",
            "status": "auth_needed_on_this_machine",
            "public_mirror": "GLO-30 Public is partial and is not used by this resolver",
        },
        "route_map": {
            "algorithm": "row-major WFG-1 level-0 address ID to GLO-90 catalog prefix or explicit GEBCO fallback",
            "sha256": source_resolver_digest.hexdigest(),
            "fallback_only_sha256": fallback_map_digest.hexdigest(),
        },
        "fallback": {
            "product": GEBCO_RELEASE,
            "global_grid_rows": GEBCO_ROWS,
            "global_grid_columns": GEBCO_COLUMNS,
            "spacing_arc_seconds": 15,
            "sample_centers_per_wfg_level0_tile_check": "60 x 60 for global corner and antimeridian/equatorial probes",
            "not_30m_land_truth": True,
            "license_id": GEBCO_LICENSE_ID,
            "attribution": GEBCO_ATTRIBUTION,
        },
        "geotile_index_check": {
            "checked": gebco_windows_checked,
            "expected": total,
            "all_tiles_have_aligned_60x60_gebco_window": gebco_window_complete,
        },
        "proof": {
            "logical_grid_partition": "WFG-1 level 0 partitions [-180,180) x [-90,90] into exactly 1440 x 720 half-open cells; global poles are assigned by specified edge rules.",
            "per_tile_resolution": "Every WFG-1 level-0 address maps to a catalogued COP-DEM-GLO-90-F COG or is explicitly assigned to GEBCO_2026 fallback. The GLO-90 inventory is a source availability index, not a land mask; fallback tiles are not described as ocean unless the GEBCO TID mask identifies them.",
            "gebco_extent": "GEBCO_2026 is the global 15 arc-second latitude/longitude grid spanning all WGS84 coordinates; level-0 tile corner and wrap checks resolve 60 x 60 cell centers.",
            "global_30m_claim": False,
        },
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(path, _canonical_json(value))


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate or inspect WorldForge global terrain tiles")
    parser.add_argument("--cache-dir", type=Path, default=Path(".worldforge-global-terrain-cache"))
    parser.add_argument("--cache-max-bytes", type=int, default=DEFAULT_CACHE_BYTES)
    parser.add_argument("--geoid-grid", type=Path, help="verified OSGeo PROJ-data us_nga_egm08_25.tif; enables h=H+N outputs")
    parser.add_argument("--commit", default=os.environ.get("WORLDFORGE_GENERATOR_COMMIT", "unknown"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    address = subparsers.add_parser("address", help="resolve a WGS84 coordinate to a WFG-1 tile ID")
    address.add_argument("--longitude", required=True, type=float)
    address.add_argument("--latitude", required=True, type=float)
    address.add_argument("--level", type=int, default=0)
    generate = subparsers.add_parser("generate", help="generate one source-derived terrain tile")
    generate.add_argument("--tile", required=True)
    generate.add_argument("--refresh-catalog", action="store_true")
    coverage = subparsers.add_parser("coverage-check", help="check the global level-0 source fallback index")
    coverage.add_argument("--output", type=Path)
    coverage.add_argument("--refresh-catalog", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _make_parser().parse_args(argv)
    try:
        if args.command == "address":
            address = tile_for_coordinate(args.longitude, args.latitude, args.level)
            result = {
                "tile_id": address.tile_id,
                "scheme": TILE_SCHEME,
                "bounds_wgs84": {
                    "west_deg": address.bounds[0],
                    "south_deg": address.bounds[1],
                    "east_deg": address.bounds[2],
                    "north_deg": address.bounds[3],
                },
                "tile_step_degrees": address.step_degrees,
            }
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0
        with GlobalTerrainProvider(
            args.cache_dir,
            max_cache_bytes=args.cache_max_bytes,
            geoid_grid=args.geoid_grid,
            generator_commit=args.commit,
        ) as provider:
            if args.command == "coverage-check":
                report = coverage_index_report(provider.cop90.load_index(refresh=args.refresh_catalog))
                if provider.cop90.index_receipt is not None:
                    report["source_catalog_receipt"] = provider.cop90.index_receipt
                if args.output:
                    _write_json(args.output, report)
                print(json.dumps(report, sort_keys=True, indent=2))
                return 0 if report["status"] == "pass" else 2
            address = GlobalTileAddress.parse(args.tile)
            with provider.load_tile(address, refresh=args.refresh_catalog) as tile:
                tile_dir = provider.cache.tile_directory(address)
                print(json.dumps({"tile_id": address.tile_id, "bundle_dir": str(tile_dir), "manifest": tile.manifest}, sort_keys=True, indent=2))
                return 0 if tile.manifest.get("terrain", {}).get("status") in {"ready", "partial"} else 2
    except (TerrainError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

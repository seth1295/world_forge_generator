# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""On-demand reads from ESA WorldCover 2021 v200 COG tiles."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .addressing import TileAddress
from .models import CancellationToken, CancelledError, SourceError

WORLDCOVER_PRODUCT = "ESA WorldCover 10 m 2021 v200"
WORLDCOVER_RELEASE = "2021-v200"
WORLDCOVER_LICENSE = "CC-BY-4.0"
WORLDCOVER_PIXEL_REGISTRATION = "area"
WORLDCOVER_PIXEL_CENTER_FORMULA = (
    "For zero-based window column c and row r, use pixel center (c+0.5, r+0.5): "
    "longitude = a*(c+0.5) + b*(r+0.5) + x0; "
    "latitude = d*(c+0.5) + e*(r+0.5) + y0; "
    "affine coefficients are [a,b,x0,d,e,y0]."
)
WORLDCOVER_BUCKET = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
WORLDCOVER_PRODUCT_PREFIX = "v200/2021/map"
WORLDCOVER_CITATION = "https://doi.org/10.5281/zenodo.7254221"
WORLDCOVER_TERMS_URL = "https://esa-worldcover.org/en/data-access"
WORLDCOVER_ATTRIBUTION = "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium"
CLASS_LABELS: dict[int, str] = {
    0: "NoData",
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare / sparse vegetation",
    70: "Snow and ice",
    80: "Permanent water bodies",
    90: "Herbaceous wetland",
    95: "Mangroves",
    100: "Moss and lichen",
}
CLASS_VALUES = {value for value in CLASS_LABELS if value != 0}
REMOTE_TIMEOUT_SECONDS = 30
MAX_COG_OBJECT_BYTES = 256 * 1024 * 1024
MAX_COG_CACHE_BYTES = 512 * 1024 * 1024
COG_CACHE_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class WorldCoverWindow:
    data: bytes
    width: int
    height: int
    affine: tuple[float, float, float, float, float, float]
    pixel_registration: str
    nodata: int
    source_tile: str
    source_url: str
    source_etag: str | None
    source_size_bytes: int
    source_sha256: str
    source_download_bytes: int
    source_cache_hit: bool
    source_window: tuple[int, int, int, int]
    source_pixel_spacing_degrees: tuple[float, float]
    class_counts: dict[int, int]
    valid_pixel_count: int

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    def as_metadata(self) -> dict[str, object]:
        return {
            "release": WORLDCOVER_RELEASE,
            "product": WORLDCOVER_PRODUCT,
            "source_tile": self.source_tile,
            "source_url": self.source_url,
            "source_etag": self.source_etag,
            "source_object_size_bytes": self.source_size_bytes,
            "source_sha256": self.source_sha256,
            "source_download_bytes_this_request": self.source_download_bytes,
            "source_cache_hit": self.source_cache_hit,
            "source_window_row_col_height_width": list(self.source_window),
            "width": self.width,
            "height": self.height,
            "pixel_count": self.pixel_count,
            "affine_wgs84_lon_lat": list(self.affine),
            "origin_reference": "pixel_corner",
            "pixel_registration": self.pixel_registration,
            "source_area_or_point_tag": "Area",
            "row_column_order": "row-major; rows north-to-south, columns west-to-east",
            "pixel_center_formula": WORLDCOVER_PIXEL_CENTER_FORMULA,
            "crs": "EPSG:4326",
            "axis_order": ["longitude", "latitude"],
            "pixel_spacing_degrees_lon_lat": list(self.source_pixel_spacing_degrees),
            "sample_method": "source-grid pixel-center ownership; direct COG window read; no reprojection or resampling",
            "nodata": self.nodata,
            "class_labels": {str(key): value for key, value in CLASS_LABELS.items()},
            "class_counts": {
                str(key): count for key, count in sorted(self.class_counts.items())
            },
            "valid_pixel_count": self.valid_pixel_count,
            "semantic_warning": "land-cover classification, not an inventory of individual trees or vegetation instances",
        }


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError as error:
        raise SourceError(
            "missing_dependency", "certifi is required for verified HTTPS"
        ) from error
    return ssl.create_default_context(cafile=certifi.where())


def worldcover_tile_name(longitude: float, latitude: float) -> str:
    """Return the 3-degree lower-left WorldCover map tile containing a point."""

    if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
        raise ValueError("longitude must be finite and in [-180, 180]")
    if not math.isfinite(latitude) or not -90.0 <= latitude <= 90.0:
        raise ValueError("latitude must be finite and in [-90, 90]")
    if longitude == 180.0:
        longitude = -180.0
    longitude_origin = max(-180, min(177, math.floor(longitude / 3.0) * 3))
    latitude_origin = max(-90, min(87, math.floor(latitude / 3.0) * 3))
    lat_direction = "N" if latitude_origin >= 0 else "S"
    lon_direction = "E" if longitude_origin >= 0 else "W"
    lat_token = f"{lat_direction}{abs(latitude_origin):02d}"
    lon_token = f"{lon_direction}{abs(longitude_origin):03d}"
    return f"{lat_token}{lon_token}"


def _worldcover_url(tile_name: str) -> str:
    if not re.fullmatch(r"[NS]\d{2}[EW]\d{3}", tile_name):
        raise ValueError("invalid WorldCover tile identifier")
    filename = f"ESA_WorldCover_10m_2021_v200_{tile_name}_Map.tif"
    return f"{WORLDCOVER_BUCKET}/{WORLDCOVER_PRODUCT_PREFIX}/{filename}"


def _pixel_registration_from_tags(tags: dict[str, str]) -> str:
    """Translate the authoritative GeoTIFF tag into the manifest enum."""

    if tags.get("AREA_OR_POINT") != "Area":
        raise SourceError(
            "worldcover_pixel_registration_invalid",
            "WorldCover COG must declare AREA_OR_POINT=Area",
        )
    return WORLDCOVER_PIXEL_REGISTRATION


def worldcover_source_for_tile(address: TileAddress) -> tuple[str, str]:
    """Return the pinned ESA 3° source COG identity and HTTPS URL for a WFG tile."""

    bounds = address.bounds
    tile_name = worldcover_tile_name(bounds.west, bounds.south)
    return tile_name, _worldcover_url(tile_name)


def _head_cog(
    url: str, *, token: CancellationToken, timeout_seconds: float
) -> tuple[int, str | None]:
    context = _ssl_context()
    request = urllib.request.Request(
        url, headers={"User-Agent": "WorldForge-global-features/1.0"}, method="HEAD"
    )
    token.raise_if_cancelled()
    try:
        with urllib.request.urlopen(
            request, context=context, timeout=timeout_seconds
        ) as response:
            if response.status != 200:
                raise SourceError(
                    "worldcover_http_status",
                    f"COG HEAD returned HTTP {response.status}",
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is None or not raw_length.isdecimal():
                raise SourceError(
                    "worldcover_size_invalid", "COG has no valid Content-Length"
                )
            size = int(raw_length)
            if size <= 0 or size > MAX_COG_OBJECT_BYTES:
                raise SourceError(
                    "worldcover_size_invalid", "COG object exceeds its safety bound"
                )
            etag = response.headers.get("ETag")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SourceError(
                "worldcover_tile_missing",
                "WorldCover has no COG at this 3-degree tile key",
            ) from error
        raise SourceError(
            "worldcover_http_status", f"COG HEAD returned HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SourceError("worldcover_network_error", type(error).__name__) from error
    return size, etag


class _WorldCoverCogCache:
    """Small, checksum-verified local cache for individual 3-degree ESA COGs."""

    def __init__(
        self, root: Path, *, max_cache_bytes: int, timeout_seconds: float
    ) -> None:
        if max_cache_bytes <= 0:
            raise ValueError("max_cache_bytes must be positive")
        self.root = Path(root)
        self.max_cache_bytes = max_cache_bytes
        self.timeout_seconds = timeout_seconds

    def get(
        self,
        *,
        tile_name: str,
        url: str,
        object_size: int,
        etag: str | None,
        token: CancellationToken,
    ) -> tuple[Path, str, int, bool]:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise SourceError(
                "worldcover_cache_invalid", "COG cache root must not be a symlink"
            )
        root = self.root.resolve(strict=True)
        key = hashlib.sha256(
            f"{WORLDCOVER_RELEASE}\0{tile_name}\0{url}\0{etag or ''}\0{object_size}".encode()
        ).hexdigest()[:24]
        file_name = f"{tile_name}-{key}.tif"
        path = root / file_name
        metadata_path = root / f"{file_name}.json"
        self._check_child(root, path)
        self._check_child(root, metadata_path)
        if path.exists() or metadata_path.exists():
            if path.is_symlink() or metadata_path.is_symlink():
                raise SourceError(
                    "worldcover_cache_invalid", "symbolic link found in COG cache"
                )
            if (path.exists() and not path.is_file()) or (
                metadata_path.exists() and not metadata_path.is_file()
            ):
                raise SourceError(
                    "worldcover_cache_invalid", "non-file entry found in COG cache"
                )
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                digest, size = _file_sha256(path)
                valid = (
                    metadata.get("release") == WORLDCOVER_RELEASE
                    and metadata.get("url") == url
                    and metadata.get("etag") == etag
                    and metadata.get("size_bytes") == object_size == size
                    and metadata.get("sha256") == digest
                )
            except (OSError, ValueError, json.JSONDecodeError):
                valid = False
                digest = ""
            if valid:
                os.utime(path, None)
                self._trim(active_path=path)
                return path, digest, 0, True
            if path.exists():
                path.unlink()
            if metadata_path.exists():
                metadata_path.unlink()
        if object_size > self.max_cache_bytes:
            raise SourceError(
                "worldcover_cache_limit",
                "one COG exceeds the configured source-cache bound",
            )
        suffix = uuid.uuid4().hex
        path_part = root / f".{file_name}.{suffix}.part"
        metadata_part = root / f".{file_name}.{suffix}.json.part"
        self._check_child(root, path_part)
        context = _ssl_context()
        headers = {"User-Agent": "WorldForge-global-features/1.0"}
        if etag:
            headers["If-Match"] = etag
        request = urllib.request.Request(url, headers=headers)
        token.raise_if_cancelled()
        downloaded = 0
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(
                request, context=context, timeout=self.timeout_seconds
            ) as response:
                if response.status != 200:
                    raise SourceError(
                        "worldcover_http_status",
                        f"COG GET returned HTTP {response.status}",
                    )
                if response.headers.get("Content-Length") != str(object_size):
                    raise SourceError(
                        "worldcover_size_invalid",
                        "COG GET length differs from its verified HEAD",
                    )
                response_etag = response.headers.get("ETag")
                if etag and response_etag != etag:
                    raise SourceError(
                        "worldcover_object_changed",
                        "COG ETag changed between HEAD and GET",
                    )
                with path_part.open("xb") as output:
                    while True:
                        token.raise_if_cancelled()
                        chunk = response.read(COG_CACHE_CHUNK_BYTES)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if (
                            downloaded > object_size
                            or downloaded > MAX_COG_OBJECT_BYTES
                        ):
                            raise SourceError(
                                "worldcover_size_invalid",
                                "COG response exceeded its size limit",
                            )
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if downloaded != object_size:
                raise SourceError(
                    "worldcover_size_invalid",
                    "COG response ended before Content-Length",
                )
            content_sha256 = digest.hexdigest()
            os.replace(path_part, path)
            metadata = {
                "release": WORLDCOVER_RELEASE,
                "tile": tile_name,
                "url": url,
                "etag": etag,
                "size_bytes": object_size,
                "sha256": content_sha256,
            }
            with metadata_part.open("x", encoding="utf-8") as metadata_stream:
                metadata_stream.write(
                    json.dumps(metadata, sort_keys=True, separators=(",", ":"))
                )
                metadata_stream.flush()
                os.fsync(metadata_stream.fileno())
            os.replace(metadata_part, metadata_path)
            self._trim(active_path=path)
            return path, content_sha256, downloaded, False
        except CancelledError:
            path_part.unlink(missing_ok=True)
            metadata_part.unlink(missing_ok=True)
            raise
        except SourceError:
            path_part.unlink(missing_ok=True)
            metadata_part.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as error:
            path_part.unlink(missing_ok=True)
            metadata_part.unlink(missing_ok=True)
            if error.code == 412:
                raise SourceError(
                    "worldcover_object_changed", "COG ETag precondition failed"
                ) from error
            if error.code == 404:
                raise SourceError(
                    "worldcover_tile_missing", "WorldCover COG disappeared"
                ) from error
            raise SourceError(
                "worldcover_http_status", f"COG GET returned HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            path_part.unlink(missing_ok=True)
            metadata_part.unlink(missing_ok=True)
            raise SourceError(
                "worldcover_network_error", type(error).__name__
            ) from error

    def _trim(self, *, active_path: Path) -> None:
        entries: list[tuple[float, Path, int]] = []
        total = 0
        pattern = re.compile(r"^[NS]\d{2}[EW]\d{3}-[0-9a-f]{24}\.tif$")
        for candidate in self.root.iterdir():
            if candidate.is_symlink():
                raise SourceError(
                    "worldcover_cache_invalid", "symbolic link found in COG cache"
                )
            if not candidate.is_file() or not pattern.fullmatch(candidate.name):
                continue
            if candidate.resolve(strict=True).parent != self.root.resolve(strict=True):
                raise SourceError(
                    "worldcover_cache_invalid", "COG cache entry escaped the cache root"
                )
            size = candidate.stat().st_size
            total += size
            entries.append((candidate.stat().st_mtime, candidate, size))
        if total <= self.max_cache_bytes:
            return
        for _, candidate, size in sorted(
            entries, key=lambda row: (row[0], row[1].name)
        ):
            if candidate == active_path:
                continue
            metadata = candidate.with_name(candidate.name + ".json")
            candidate.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            total -= size
            if total <= self.max_cache_bytes:
                return
        raise SourceError(
            "worldcover_cache_limit", "source COG cache quota cannot be met"
        )

    @staticmethod
    def _check_child(root: Path, path: Path) -> None:
        if path.parent.resolve(strict=True) != root.resolve(strict=True):
            raise SourceError(
                "worldcover_cache_invalid", "cache path is outside the configured root"
            )


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _window_indices(
    transform: Any,
    width: int,
    height: int,
    west: float,
    south: float,
    east: float,
    north: float,
) -> tuple[int, int, int, int]:
    """Choose source pixels by their centers with WFG-1 half-open ownership."""

    dx = float(transform.a)
    dy = float(-transform.e)
    if dx <= 0.0 or dy <= 0.0 or transform.b != 0.0 or transform.d != 0.0:
        raise SourceError(
            "worldcover_grid_invalid", "source is not a north-up geographic grid"
        )
    x0 = float(transform.c)
    top = float(transform.f)
    # Pixel-center comparisons use a tiny source-grid epsilon to absorb affine roundoff.
    epsilon = 1e-8
    col_start = math.ceil((west - x0) / dx - 0.5 - epsilon)
    col_stop = math.ceil((east - x0) / dx - 0.5 - epsilon)
    row_start = math.floor((top - north) / dy - 0.5 + epsilon) + 1
    row_stop = math.floor((top - south) / dy - 0.5 + epsilon) + 1
    col_start = min(width, max(0, col_start))
    col_stop = min(width, max(col_start, col_stop))
    row_start = min(height, max(0, row_start))
    row_stop = min(height, max(row_start, row_stop))
    return row_start, col_start, row_stop - row_start, col_stop - col_start


class WorldCoverProvider:
    """Fetch one source COG on demand, cache it within a strict quota and read one window."""

    def __init__(
        self,
        *,
        timeout_seconds: float = REMOTE_TIMEOUT_SECONDS,
        cache_dir: str | Path | None = None,
        max_cache_bytes: int = MAX_COG_CACHE_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        if cache_dir is None:
            cache_dir = (
                Path(tempfile.gettempdir())
                / "WorldForge"
                / "global-feature-source-cache"
                / "worldcover"
            )
        self.cog_cache = _WorldCoverCogCache(
            Path(cache_dir),
            max_cache_bytes=max_cache_bytes,
            timeout_seconds=timeout_seconds,
        )

    def read_tile(
        self, address: TileAddress, token: CancellationToken, *, max_pixels: int
    ) -> WorldCoverWindow:
        token.raise_if_cancelled()
        bounds = address.bounds
        tile_name, url = worldcover_source_for_tile(address)
        source_size, etag = _head_cog(
            url, token=token, timeout_seconds=self.timeout_seconds
        )
        local_path, source_sha256, source_download_bytes, source_cache_hit = (
            self.cog_cache.get(
                tile_name=tile_name,
                url=url,
                object_size=source_size,
                etag=etag,
                token=token,
            )
        )
        try:
            import numpy as np
            import rasterio
            from rasterio.windows import Window
        except ImportError as error:
            raise SourceError(
                "missing_dependency", "rasterio and numpy are required"
            ) from error
        token.raise_if_cancelled()
        try:
            with rasterio.open(local_path) as source:
                if source.crs is None or source.crs.to_epsg() != 4326:
                    raise SourceError(
                        "worldcover_crs_invalid", "WorldCover COG is not EPSG:4326"
                    )
                if source.count != 1 or source.dtypes[0] != "uint8":
                    raise SourceError(
                        "worldcover_band_invalid",
                        "WorldCover map must be one uint8 band",
                    )
                pixel_registration = _pixel_registration_from_tags(source.tags())
                if (
                    source.width <= 0
                    or source.height <= 0
                    or source.width * source.height > 4_000_000_000
                ):
                    raise SourceError(
                        "worldcover_dimensions_invalid",
                        "WorldCover COG dimensions are invalid",
                    )
                if source.nodata is None or int(source.nodata) != 0:
                    raise SourceError(
                        "worldcover_nodata_invalid",
                        "WorldCover map must declare nodata 0",
                    )
                source_transform = source.transform
                row, col, height, width = _window_indices(
                    source_transform,
                    source.width,
                    source.height,
                    bounds.west,
                    bounds.south,
                    bounds.east,
                    bounds.north,
                )
                if width * height > max_pixels:
                    raise SourceError(
                        "worldcover_window_limit",
                        "requested raster window exceeds its pixel bound",
                    )
                token.raise_if_cancelled()
                if width == 0 or height == 0:
                    values = np.empty((height, width), dtype=np.uint8)
                else:
                    values = source.read(
                        1, window=Window(col, row, width, height), out_dtype="uint8"
                    )
        except SourceError:
            raise
        except Exception as error:
            raise SourceError("worldcover_cog_invalid", type(error).__name__) from error
        token.raise_if_cancelled()
        if values.shape != (height, width) or values.dtype != np.uint8:
            raise SourceError(
                "worldcover_window_invalid",
                "decoded COG window does not match the requested size",
            )
        observed = {int(item) for item in np.unique(values)}
        if observed - set(CLASS_LABELS):
            raise SourceError(
                "worldcover_class_invalid",
                "COG window contains unknown classification codes",
            )
        class_counts = {code: int((values == code).sum()) for code in sorted(observed)}
        valid_pixel_count = sum(
            count for code, count in class_counts.items() if code != 0
        )
        affine = source_transform
        window_transform = (
            float(affine.a),
            float(affine.b),
            float(affine.c + col * affine.a + row * affine.b),
            float(affine.d),
            float(affine.e),
            float(affine.f + col * affine.d + row * affine.e),
        )
        return WorldCoverWindow(
            data=values.tobytes(order="C"),
            width=width,
            height=height,
            affine=window_transform,
            pixel_registration=pixel_registration,
            nodata=0,
            source_tile=tile_name,
            source_url=url,
            source_etag=etag,
            source_size_bytes=source_size,
            source_sha256=source_sha256,
            source_download_bytes=source_download_bytes,
            source_cache_hit=source_cache_hit,
            source_window=(row, col, height, width),
            source_pixel_spacing_degrees=(float(affine.a), float(-affine.e)),
            class_counts=class_counts,
            valid_pixel_count=valid_pixel_count,
        )

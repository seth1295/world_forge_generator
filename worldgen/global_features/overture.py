# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Pinned, AOI-only Overture GeoParquet resolver with verified HTTPS range reads."""

from __future__ import annotations

import hashlib
import io
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from shapely.errors import ShapelyError

from .addressing import TileAddress, TileBounds
from .geometry import (
    apply_half_open_line_ownership,
    feature_to_geojson,
    json_safe,
    remove_empty_coordinate_parts,
)
from .models import OVERTURE_LAYERS, CancellationToken, FeatureLayerSpec, SourceError

OVERTURE_RELEASE = "2026-10-21.0"
OVERTURE_RELEASE_NOTES = "https://docs.overturemaps.org/blog/2026/10/21/release-notes/"
OVERTURE_ATTRIBUTION_URL = "https://docs.overturemaps.org/attribution/"
OVERTURE_CATALOG_BASE = "https://stac.overturemaps.org"
OVERTURE_S3_HOST = "overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
OVERTURE_AZURE_HOST = "overturemapswestus2.blob.core.windows.net"
CATALOG_MAX_BYTES = 16 * 1024 * 1024
RANGE_BLOCK_BYTES = 4 * 1024 * 1024
RANGE_CACHE_BYTES = 16 * 1024 * 1024
USER_AGENT = "WorldForge-global-features/1.0"


@dataclass(frozen=True)
class SourceFileRef:
    url: str
    bbox: tuple[float, float, float, float]
    num_rows: int
    size_bytes: int | None
    theme: str
    feature_type: str


@dataclass
class QueryAudit:
    catalog_url: str
    catalog_sha256: str
    catalog_bytes: int
    catalog_candidate_file_count: int = 0
    selected_file_count: int = 0
    row_group_count_total: int = 0
    row_group_count_selected: int = 0
    parquet_rows_scanned: int = 0
    feature_bbox_matches: int = 0
    invalid_features: int = 0
    duplicate_fragments: int = 0
    remote_bytes_read: int = 0
    files: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "catalog_url": self.catalog_url,
            "catalog_sha256": self.catalog_sha256,
            "catalog_bytes": self.catalog_bytes,
            "catalog_candidate_file_count": self.catalog_candidate_file_count,
            "selected_file_count": self.selected_file_count,
            "row_group_count_total": self.row_group_count_total,
            "row_group_count_selected": self.row_group_count_selected,
            "parquet_rows_scanned": self.parquet_rows_scanned,
            "feature_bbox_matches": self.feature_bbox_matches,
            "invalid_features": self.invalid_features,
            "duplicate_fragments": self.duplicate_fragments,
            "remote_bytes_read": self.remote_bytes_read,
            "files": list(self.files),
        }


class ByteBudget:
    """Aggregate byte ceiling shared by all remote objects in a tile-layer query."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def consume(self, byte_count: int) -> None:
        if byte_count < 0 or self.used + byte_count > self.limit:
            raise SourceError("source_read_limit", "remote byte budget reached")
        self.used += byte_count


class HttpRangeReader(io.RawIOBase):
    """Seekable file-like object backed by bounded, TLS-verified HTTP Range reads."""

    def __init__(
        self,
        source: SourceFileRef,
        *,
        token: CancellationToken,
        timeout_seconds: float,
        byte_budget: ByteBudget,
        expected_host: str | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.byte_budget = byte_budget
        self.context = _ssl_context()
        self.position = 0
        self.blocks: OrderedDict[int, bytes] = OrderedDict()
        self.cached_bytes = 0
        self.total_bytes_read = 0
        self.url = _validate_source_url(
            source.url, source.theme, source.feature_type, expected_host
        )
        self.size_bytes, self.etag = self._head()
        if source.size_bytes is not None and source.size_bytes != self.size_bytes:
            raise SourceError(
                "source_object_changed", "catalog and object lengths differ"
            )

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_CUR:
            offset += self.position
        elif whence == io.SEEK_END:
            offset += self.size_bytes
        elif whence != io.SEEK_SET:
            raise ValueError("invalid seek mode")
        if offset < 0:
            raise ValueError("negative seek offset")
        self.position = min(int(offset), self.size_bytes)
        return self.position

    def read(self, size: int = -1) -> bytes:
        self.token.raise_if_cancelled()
        if size is None or size < 0:
            size = self.size_bytes - self.position
        size = min(size, self.size_bytes - self.position)
        if size <= 0:
            return b""
        result = bytearray()
        while len(result) < size:
            self.token.raise_if_cancelled()
            block_index = self.position // RANGE_BLOCK_BYTES
            block = self._read_block(block_index)
            within = self.position - block_index * RANGE_BLOCK_BYTES
            take = min(size - len(result), len(block) - within)
            if take <= 0:
                break
            result.extend(block[within : within + take])
            self.position += take
        return bytes(result)

    def readinto(self, buffer: bytearray | memoryview) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def _head(self) -> tuple[int, str | None]:
        request = urllib.request.Request(
            self.url,
            headers={"User-Agent": USER_AGENT},
            method="HEAD",
        )
        self.token.raise_if_cancelled()
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=self.timeout_seconds
            ) as response:
                if response.status != 200:
                    raise SourceError(
                        "source_http_status",
                        f"unexpected HEAD status {response.status}",
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is None or not content_length.isdecimal():
                    raise SourceError(
                        "source_size_missing",
                        "remote object has no valid Content-Length",
                    )
                if response.headers.get("Accept-Ranges", "").lower() not in {
                    "bytes",
                    "none",
                    "",
                }:
                    raise SourceError(
                        "source_range_invalid", "unexpected range support metadata"
                    )
                return int(content_length), response.headers.get("ETag")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise SourceError(
                    "source_object_missing", "catalog object no longer exists"
                ) from error
            raise SourceError(
                "source_http_status", f"HEAD failed with HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SourceError("source_network_error", type(error).__name__) from error

    def _read_block(self, block_index: int) -> bytes:
        cached = self.blocks.get(block_index)
        if cached is not None:
            self.blocks.move_to_end(block_index)
            return cached
        self.token.raise_if_cancelled()
        start = block_index * RANGE_BLOCK_BYTES
        if start >= self.size_bytes:
            return b""
        end = min(self.size_bytes - 1, start + RANGE_BLOCK_BYTES - 1)
        headers = {"Range": f"bytes={start}-{end}", "User-Agent": USER_AGENT}
        if self.etag:
            headers["If-Match"] = self.etag
        request = urllib.request.Request(self.url, headers=headers)
        expected_length = end - start + 1
        for attempt in range(2):
            self.token.raise_if_cancelled()
            try:
                with urllib.request.urlopen(
                    request, context=self.context, timeout=self.timeout_seconds
                ) as response:
                    if response.status != 206:
                        raise SourceError(
                            "source_range_unsupported",
                            "provider did not honor HTTP Range",
                        )
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if (
                        match is None
                        or int(match.group(1)) != start
                        or int(match.group(2)) != end
                        or int(match.group(3)) != self.size_bytes
                    ):
                        raise SourceError(
                            "source_range_malformed",
                            "provider returned an inconsistent range",
                        )
                    data = response.read(expected_length + 1)
                if len(data) != expected_length:
                    raise SourceError(
                        "source_range_malformed", "provider returned a short range"
                    )
                self.byte_budget.consume(len(data))
                self.total_bytes_read += len(data)
                self._store_block(block_index, data)
                return data
            except SourceError:
                raise
            except urllib.error.HTTPError as error:
                if error.code == 412:
                    raise SourceError(
                        "source_object_changed", "ETag precondition failed"
                    ) from error
                if error.code == 404:
                    raise SourceError(
                        "source_object_missing", "catalog object no longer exists"
                    ) from error
                if attempt == 1 or error.code < 500:
                    raise SourceError(
                        "source_http_status",
                        f"range read failed with HTTP {error.code}",
                    ) from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == 1:
                    raise SourceError(
                        "source_network_error", type(error).__name__
                    ) from error
                time.sleep(0.1)
        raise SourceError("source_network_error", "range retry exhausted")

    def _store_block(self, index: int, data: bytes) -> None:
        while self.cached_bytes + len(data) > RANGE_CACHE_BYTES and self.blocks:
            _, evicted = self.blocks.popitem(last=False)
            self.cached_bytes -= len(evicted)
        if len(data) <= RANGE_CACHE_BYTES:
            self.blocks[index] = data
            self.cached_bytes += len(data)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError as error:
        raise SourceError(
            "missing_dependency", "certifi is required for verified HTTPS"
        ) from error
    return ssl.create_default_context(cafile=certifi.where())


def _validate_source_url(
    url: str,
    theme: str,
    feature_type: str,
    expected_host: str | None = None,
) -> str:
    parsed = urlparse(url)
    allowed_hosts = {OVERTURE_S3_HOST, OVERTURE_AZURE_HOST}
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SourceError("source_url_invalid", "catalog supplied an unsafe URL")
    if parsed.hostname not in allowed_hosts or (
        expected_host and parsed.hostname != expected_host
    ):
        raise SourceError("source_url_invalid", "catalog supplied a non-Overture host")
    expected_prefix = f"/release/{OVERTURE_RELEASE}/theme={theme}/type={feature_type}/"
    if not parsed.path.startswith(expected_prefix) or ".." in parsed.path.split("/"):
        raise SourceError(
            "source_url_invalid",
            "catalog path does not match the pinned release/theme/type",
        )
    return url


def _read_catalog(
    url: str, token: CancellationToken, timeout_seconds: float
) -> tuple[bytes, str]:
    token.raise_if_cancelled()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, context=_ssl_context(), timeout=timeout_seconds
        ) as response:
            if response.status != 200:
                raise SourceError(
                    "catalog_http_status", f"catalog returned HTTP {response.status}"
                )
            length = response.headers.get("Content-Length")
            if (
                length is None
                or not length.isdecimal()
                or int(length) > CATALOG_MAX_BYTES
            ):
                raise SourceError(
                    "catalog_size_invalid",
                    "catalog exceeds its size limit or has no length",
                )
            data = response.read(CATALOG_MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise SourceError(
            "catalog_http_status", f"catalog returned HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SourceError("catalog_network_error", type(error).__name__) from error
    if len(data) > CATALOG_MAX_BYTES or len(data) != int(length):
        raise SourceError(
            "catalog_malformed", "catalog size did not match its declared length"
        )
    return data, hashlib.sha256(data).hexdigest()


def _stac_file_refs(
    table_bytes: bytes,
    *,
    spec: FeatureLayerSpec,
    bounds: TileBounds,
) -> list[SourceFileRef]:
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(
            io.BytesIO(table_bytes),
            columns=["collection", "type", "bbox", "assets", "num_rows"],
        )
        rows = table.to_pylist()
    except Exception as error:
        raise SourceError("catalog_malformed", type(error).__name__) from error
    refs: list[SourceFileRef] = []
    for row in rows:
        bbox = row.get("bbox")
        if (
            row.get("collection") != spec.overture_type
            or row.get("type") != "Feature"
            or not bbox
        ):
            continue
        if (
            float(bbox["xmin"]) > bounds.east
            or float(bbox["xmax"]) < bounds.west
            or float(bbox["ymin"]) > bounds.north
            or float(bbox["ymax"]) < bounds.south
        ):
            continue
        assets = row.get("assets") or {}
        asset = assets.get("aws") or assets.get("azure")
        if not asset or not asset.get("href"):
            raise SourceError(
                "catalog_malformed", "matching STAC row has no public GeoParquet asset"
            )
        url = asset["href"]
        _validate_source_url(url, spec.overture_theme, spec.overture_type)
        size = asset.get("file:size")
        refs.append(
            SourceFileRef(
                url=url,
                bbox=(
                    float(bbox["xmin"]),
                    float(bbox["ymin"]),
                    float(bbox["xmax"]),
                    float(bbox["ymax"]),
                ),
                num_rows=int(row.get("num_rows") or 0),
                size_bytes=int(size) if size is not None else None,
                theme=spec.overture_theme,
                feature_type=spec.overture_type,
            )
        )
    return sorted(refs, key=lambda item: item.url)


def _stat_range(
    parquet_file: Any, row_group_index: int, column_path: str
) -> tuple[float, float] | None:
    row_group = parquet_file.metadata.row_group(row_group_index)
    for column_index in range(row_group.num_columns):
        column = row_group.column(column_index)
        if column.path_in_schema != column_path:
            continue
        statistics = column.statistics
        if statistics is None or not statistics.has_min_max:
            return None
        try:
            return float(statistics.min), float(statistics.max)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _row_group_overlaps(parquet_file: Any, index: int, bounds: TileBounds) -> bool:
    xmin = _stat_range(parquet_file, index, "bbox.xmin")
    xmax = _stat_range(parquet_file, index, "bbox.xmax")
    ymin = _stat_range(parquet_file, index, "bbox.ymin")
    ymax = _stat_range(parquet_file, index, "bbox.ymax")
    if any(item is None for item in (xmin, xmax, ymin, ymax)):
        return True
    assert (
        xmin is not None and xmax is not None and ymin is not None and ymax is not None
    )
    # The group is excluded only when every bbox is on one side of the AOI.
    return not (
        xmax[1] < bounds.west
        or xmin[0] > bounds.east
        or ymax[1] < bounds.south
        or ymin[0] > bounds.north
    )


def _geometry_parts(geometry: Any, *, dimension: int) -> list[Any]:
    if geometry.is_empty:
        return []
    geometry_type = geometry.geom_type
    if dimension == 2 and geometry_type in {"LineString", "LinearRing"}:
        return [geometry]
    if dimension == 3 and geometry_type == "Polygon":
        return [geometry]
    if geometry_type in {"MultiLineString", "MultiPolygon", "GeometryCollection"}:
        result: list[Any] = []
        for child in geometry.geoms:
            result.extend(_geometry_parts(child, dimension=dimension))
        return result
    return []


def _clip_feature(
    row: dict[str, Any],
    spec: FeatureLayerSpec,
    address: TileAddress,
) -> dict[str, Any] | None:
    from shapely import from_wkb
    from shapely.geometry import (
        LineString,
        MultiLineString,
        MultiPolygon,
        box,
        mapping,
    )

    source_id = row.get("id")
    wkb = row.get("geometry")
    if (
        not isinstance(source_id, str)
        or not source_id
        or not isinstance(wkb, (bytes, bytearray, memoryview))
    ):
        raise ValueError("feature lacks a stable source ID or WKB geometry")
    source_geometry = from_wkb(bytes(wkb), on_invalid="raise")
    if source_geometry.is_empty or not source_geometry.is_valid:
        raise ValueError("source geometry is empty or invalid")
    bounds = address.bounds
    clipped = source_geometry.intersection(
        box(bounds.west, bounds.south, bounds.east, bounds.north)
    )
    parts = [
        part
        for part in _geometry_parts(clipped, dimension=spec.geometry_dimension)
        if not part.is_empty
    ]
    if not parts:
        return None
    if spec.geometry_dimension == 2:
        lines: list[Any] = []
        for part in parts:
            if float(part.length) <= 0.0:
                continue
            line_mapping = mapping(part)
            owned = apply_half_open_line_ownership(line_mapping, bounds)
            if owned is None:
                continue
            # The edge helper may split a line; rebuild its normalized parts.
            if owned["type"] == "LineString":
                lines.append(LineString(owned["coordinates"]))
            else:
                lines.extend(LineString(coords) for coords in owned["coordinates"])
        if not lines:
            return None
        output_geometry: Any = lines[0] if len(lines) == 1 else MultiLineString(lines)
    else:
        polygons = [part for part in parts if float(part.area) > 0.0]
        if not polygons:
            return None
        output_geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    geometry_json = json_safe(mapping(output_geometry))
    cleaned = remove_empty_coordinate_parts(geometry_json)
    if cleaned is None:
        return None
    source_properties = {key: value for key, value in row.items() if key != "geometry"}
    return feature_to_geojson(
        source_id,
        cleaned,
        source_properties,
        address=address,
        theme=spec.overture_theme,
        feature_type=spec.overture_type,
        version=row.get("version"),
        sources=row.get("sources"),
        license_expression=spec.license_expression,
    )


class OvertureProvider:
    """Read only the release-pinned GeoParquet row groups intersecting a WFG tile."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_remote_bytes_per_layer: int = 512 * 1024 * 1024,
    ) -> None:
        if timeout_seconds <= 0 or max_remote_bytes_per_layer <= 0:
            raise ValueError("timeouts and byte limits must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_remote_bytes_per_layer = max_remote_bytes_per_layer
        self._catalog_cache: tuple[bytes, str] | None = None

    def open_tile_query(
        self,
        layer_id: str,
        address: TileAddress,
        token: CancellationToken,
    ) -> tuple[Iterator[dict[str, Any]], QueryAudit]:
        """Open a lazy feature iterator plus a mutable, auditable query summary."""

        if layer_id not in OVERTURE_LAYERS:
            raise ValueError(f"unsupported Overture layer: {layer_id}")
        token.raise_if_cancelled()
        spec = OVERTURE_LAYERS[layer_id]
        bounds = address.bounds
        catalog_url = f"{OVERTURE_CATALOG_BASE}/{OVERTURE_RELEASE}/collections.parquet"
        if self._catalog_cache is None:
            self._catalog_cache = _read_catalog(
                catalog_url, token, self.timeout_seconds
            )
        catalog_bytes, catalog_sha256 = self._catalog_cache
        refs = _stac_file_refs(catalog_bytes, spec=spec, bounds=bounds)
        audit = QueryAudit(
            catalog_url=catalog_url,
            catalog_sha256=catalog_sha256,
            catalog_bytes=len(catalog_bytes),
            catalog_candidate_file_count=len(refs),
        )

        def features() -> Iterator[dict[str, Any]]:
            if not refs:
                return
            budget = ByteBudget(self.max_remote_bytes_per_layer)
            for source_file in refs:
                token.raise_if_cancelled()
                range_reader: HttpRangeReader | None = None
                file_audit: dict[str, object] = {
                    "url": source_file.url,
                    "catalog_bbox_wgs84": [
                        source_file.bbox[0],
                        source_file.bbox[1],
                        source_file.bbox[2],
                        source_file.bbox[3],
                    ],
                    "catalog_row_count": source_file.num_rows,
                    "catalog_file_size_bytes": source_file.size_bytes,
                }
                try:
                    range_reader = HttpRangeReader(
                        source_file,
                        token=token,
                        timeout_seconds=self.timeout_seconds,
                        byte_budget=budget,
                    )
                    import pyarrow.parquet as pq

                    parquet_file = pq.ParquetFile(range_reader)
                    selected_groups = [
                        index
                        for index in range(parquet_file.metadata.num_row_groups)
                        if _row_group_overlaps(parquet_file, index, bounds)
                    ]
                    audit.selected_file_count += 1
                    audit.row_group_count_total += parquet_file.metadata.num_row_groups
                    audit.row_group_count_selected += len(selected_groups)
                    file_audit["etag"] = range_reader.etag
                    file_audit["remote_object_size_bytes"] = range_reader.size_bytes
                    file_audit["row_groups_total"] = (
                        parquet_file.metadata.num_row_groups
                    )
                    file_audit["row_groups_selected"] = selected_groups
                    for batch in parquet_file.iter_batches(
                        batch_size=512, row_groups=selected_groups
                    ):
                        token.raise_if_cancelled()
                        for row in batch.to_pylist():
                            audit.parquet_rows_scanned += 1
                            feature_bbox = row.get("bbox") or {}
                            if feature_bbox and (
                                float(feature_bbox["xmin"]) > bounds.east
                                or float(feature_bbox["xmax"]) < bounds.west
                                or float(feature_bbox["ymin"]) > bounds.north
                                or float(feature_bbox["ymax"]) < bounds.south
                            ):
                                continue
                            audit.feature_bbox_matches += 1
                            try:
                                clipped = _clip_feature(row, spec, address)
                            except (TypeError, ValueError, OverflowError, ShapelyError):
                                audit.invalid_features += 1
                                continue
                            if clipped is not None:
                                yield clipped
                    file_audit["remote_bytes_read"] = range_reader.total_bytes_read
                except SourceError:
                    raise
                except Exception as error:
                    raise SourceError(
                        "remote_parquet_malformed", type(error).__name__
                    ) from error
                finally:
                    if range_reader is not None:
                        file_audit.setdefault(
                            "remote_bytes_read", range_reader.total_bytes_read
                        )
                        audit.remote_bytes_read += range_reader.total_bytes_read
                    audit.files.append(file_audit)
            if audit.invalid_features:
                # Consumers see this count and the outer producer marks the layer partial.
                return

        return features(), audit

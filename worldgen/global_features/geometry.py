# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Engine-neutral GeoJSON fragment identity and half-open boundary helpers."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

from .addressing import TileAddress, TileBounds


def canonical_json(value: object) -> str:
    """Stable compact JSON encoding used for content hashes and shard bytes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def fragment_id(
    source_feature_id: str, address: TileAddress, geometry: dict[str, Any]
) -> str:
    payload = canonical_json(
        {
            "tile_id": address.tile_id,
            "source_id": source_feature_id,
            "geometry": geometry,
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"wff:{digest[:32]}"


def _on_east_edge(point: list[float], bounds: TileBounds) -> bool:
    return len(point) >= 2 and float(point[0]) == bounds.east


def _on_north_edge(point: list[float], bounds: TileBounds) -> bool:
    return len(point) >= 2 and float(point[1]) == bounds.north


def _excluded_edge_segment(
    first: list[float], second: list[float], bounds: TileBounds
) -> bool:
    """Internal east/north edges belong to the adjacent half-open tile."""

    return (_on_east_edge(first, bounds) and _on_east_edge(second, bounds)) or (
        _on_north_edge(first, bounds) and _on_north_edge(second, bounds)
    )


def _split_line_at_excluded_edges(
    coordinates: list[list[float]], bounds: TileBounds
) -> list[list[list[float]]]:
    if len(coordinates) < 2:
        return []
    parts: list[list[list[float]]] = []
    current: list[list[float]] = []
    for index in range(len(coordinates) - 1):
        first = coordinates[index]
        second = coordinates[index + 1]
        if _excluded_edge_segment(first, second, bounds):
            if len(current) >= 2:
                parts.append(current)
            current = []
            continue
        if not current:
            current = [first, second]
        elif current[-1] == first:
            current.append(second)
        else:
            if len(current) >= 2:
                parts.append(current)
            current = [first, second]
    if len(current) >= 2:
        parts.append(current)
    return parts


def apply_half_open_line_ownership(
    geometry: dict[str, Any], bounds: TileBounds
) -> dict[str, Any] | None:
    """Drop road segments that lie wholly on this tile's excluded east/north edge.

    Clipped fragments can share endpoints, which have zero measure. Segments
    exactly on an internal east/north boundary are assigned to the adjacent
    cell; west/south-boundary segments remain owned here. Coordinate values
    are compared exactly because clipping uses the exact WFG-1 tile bounds.
    """

    kind = geometry.get("type")
    if kind == "LineString":
        parts = _split_line_at_excluded_edges(geometry.get("coordinates", []), bounds)
        if not parts:
            return None
        if len(parts) == 1:
            return {"type": "LineString", "coordinates": parts[0]}
        return {"type": "MultiLineString", "coordinates": parts}
    if kind == "MultiLineString":
        lines: list[list[list[float]]] = []
        for coordinates in geometry.get("coordinates", []):
            lines.extend(_split_line_at_excluded_edges(coordinates, bounds))
        if not lines:
            return None
        if len(lines) == 1:
            return {"type": "LineString", "coordinates": lines[0]}
        return {"type": "MultiLineString", "coordinates": lines}
    raise ValueError(f"expected a clipped line geometry, got {kind!r}")


def remove_empty_coordinate_parts(geometry: dict[str, Any]) -> dict[str, Any] | None:
    """Reject empty/null vector intersections emitted by a geometry engine."""

    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "LineString":
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return None
        return geometry
    if kind == "MultiLineString":
        lines = [
            part
            for part in coordinates or []
            if isinstance(part, list) and len(part) >= 2
        ]
        if not lines:
            return None
        if len(lines) == 1:
            return {"type": "LineString", "coordinates": lines[0]}
        return {"type": "MultiLineString", "coordinates": lines}
    if kind == "Polygon":
        rings = coordinates or []
        if not rings or not rings[0] or len(rings[0]) < 4:
            return None
        return geometry
    if kind == "MultiPolygon":
        polygons = [
            part for part in coordinates or [] if part and part[0] and len(part[0]) >= 4
        ]
        if not polygons:
            return None
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}
        return {"type": "MultiPolygon", "coordinates": polygons}
    raise ValueError(f"unexpected clipped geometry type {kind!r}")


def _json_safe(value: Any) -> Any:
    """Convert DuckDB nested values to JSON-safe values without dropping lineage."""

    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite source properties are not valid GeoJSON")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(
                "non-finite decimal source properties are not valid GeoJSON"
            )
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "hex", "value": bytes(value).hex()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "as_py"):
        return _json_safe(value.as_py())
    if hasattr(value, "item"):
        return _json_safe(value.item())
    # Decimal and DuckDB scalar classes stringify without executing metadata.
    return str(value)


def json_safe(value: Any) -> Any:
    return _json_safe(value)


def feature_to_geojson(
    source_feature_id: str,
    geometry: dict[str, Any],
    source_properties: dict[str, Any],
    *,
    address: TileAddress,
    theme: str,
    feature_type: str,
    version: Any,
    sources: Any,
    license_expression: str,
) -> dict[str, Any]:
    """Wrap source properties with source-preserving WorldForge tile metadata."""

    clean_geometry = remove_empty_coordinate_parts(geometry)
    if clean_geometry is None:
        raise ValueError("cannot emit an empty geometry")
    safe_sources = json_safe(sources)
    safe_version = json_safe(version)
    properties = json_safe(source_properties)
    if not isinstance(properties, dict):
        raise TypeError("source properties did not serialize to a JSON object")
    properties["worldforge"] = {
        "address_scheme": "WFG-1",
        "tile_id": address.tile_id,
        "source_provider": "Overture Maps",
        "source_release": "2026-10-21.0",
        "source_theme": theme,
        "source_type": feature_type,
        "source_version": safe_version,
        "source_feature_id": source_feature_id,
        "source_records": safe_sources,
        "source_theme_license": license_expression,
        "fragment_id": fragment_id(source_feature_id, address, clean_geometry),
        "geometry_operation": "ST_Intersection with the WFG-1 tile rectangle; no simplification or reprojection",
    }
    return {
        "type": "Feature",
        "id": source_feature_id,
        "geometry": clean_geometry,
        "properties": properties,
    }

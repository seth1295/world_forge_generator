# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""WFG-1 geographic tile addressing and bbox decomposition."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

ADDRESS_SCHEME = "WFG-1"
ADDRESS_PREFIX = "wfg1"
MAX_LEVEL = 16
_TILE_ID_RE = re.compile(r"^wfg1:(0|[1-9][0-9]*):(0|[1-9][0-9]*):(0|[1-9][0-9]*)$")


def _finite(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def normalize_longitude(longitude: float) -> float:
    """Normalize a valid WGS84 longitude to [-180, 180); +180 becomes -180."""

    longitude = _finite(longitude, "longitude")
    if longitude < -180.0 or longitude > 180.0:
        raise ValueError("longitude must be in [-180, 180]")
    if longitude == 180.0:
        return -180.0
    return longitude


@dataclass(frozen=True, order=True)
class TileBounds:
    """Half-open WGS84 bbox, in longitude/latitude degrees."""

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        values = (
            _finite(self.west, "west"),
            _finite(self.south, "south"),
            _finite(self.east, "east"),
            _finite(self.north, "north"),
        )
        if values[0] < -180.0 or values[2] > 180.0 or values[0] >= values[2]:
            raise ValueError("bounds must have -180 <= west < east <= 180")
        if values[1] < -90.0 or values[3] > 90.0 or values[1] >= values[3]:
            raise ValueError("bounds must have -90 <= south < north <= 90")

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


@dataclass(frozen=True, order=True)
class TileAddress:
    """Canonical WFG-1 tile at a level from 0 through 16."""

    level: int
    x: int
    y: int

    def __post_init__(self) -> None:
        if not isinstance(self.level, int) or isinstance(self.level, bool):
            raise TypeError("level must be an integer")
        if self.level < 0 or self.level > MAX_LEVEL:
            raise ValueError(f"level must be in [0, {MAX_LEVEL}]")
        x_count, y_count = self.x_count, self.y_count
        if (
            not isinstance(self.x, int)
            or isinstance(self.x, bool)
            or not 0 <= self.x < x_count
        ):
            raise ValueError(
                f"x must be an integer in [0, {x_count}) at level {self.level}"
            )
        if (
            not isinstance(self.y, int)
            or isinstance(self.y, bool)
            or not 0 <= self.y < y_count
        ):
            raise ValueError(
                f"y must be an integer in [0, {y_count}) at level {self.level}"
            )

    @property
    def x_count(self) -> int:
        return 1440 * (1 << self.level)

    @property
    def y_count(self) -> int:
        return 720 * (1 << self.level)

    @property
    def step_degrees(self) -> float:
        return 0.25 / (1 << self.level)

    @property
    def tile_id(self) -> str:
        return f"{ADDRESS_PREFIX}:{self.level}:{self.x}:{self.y}"

    @property
    def bounds(self) -> TileBounds:
        step = self.step_degrees
        west = -180.0 + self.x * step
        south = -90.0 + self.y * step
        return TileBounds(west, south, west + step, south + step)

    @classmethod
    def parse(cls, tile_id: str) -> TileAddress:
        match = _TILE_ID_RE.fullmatch(tile_id)
        if match is None:
            raise ValueError("tile ID must use canonical form wfg1:<level>:<x>:<y>")
        level, x, y = (int(part) for part in match.groups())
        return cls(level=level, x=x, y=y)


def point_to_tile(longitude: float, latitude: float, level: int = 0) -> TileAddress:
    """Return the half-open WFG-1 owner for a WGS84 point."""

    longitude = normalize_longitude(longitude)
    latitude = _finite(latitude, "latitude")
    if latitude < -90.0 or latitude > 90.0:
        raise ValueError("latitude must be in [-90, 90]")
    step = TileAddress(level, 0, 0).step_degrees
    x_count = 1440 * (1 << level)
    y_count = 720 * (1 << level)
    if abs(latitude) == 90.0:
        # Longitude is not physically distinguished at either pole.
        x = x_count // 2
    else:
        x = min(x_count - 1, max(0, math.floor((longitude + 180.0) / step)))
    # The north pole is the one closed global maximum; its owner is the final row.
    y = min(y_count - 1, max(0, math.floor((latitude + 90.0) / step)))
    return TileAddress(level=level, x=x, y=y)


def _axis_indices(
    low: float, high: float, minimum: float, maximum: float, step: float, count: int
) -> range:
    if low == high:
        index = min(count - 1, max(0, math.floor((low - minimum) / step)))
        return range(index, index + 1)
    start = min(count - 1, max(0, math.floor((low - minimum) / step)))
    # Subtracting one after ceil gives a half-open maximum at internal grid edges.
    end = min(count - 1, max(0, math.ceil((high - minimum) / step) - 1))
    return range(start, end + 1)


def split_bbox_antimeridian(
    west: float, south: float, east: float, north: float
) -> tuple[tuple[float, float, float, float], ...]:
    """Validate and split a WGS84 bbox; west > east means it crosses the dateline."""

    west = _finite(west, "west")
    east = _finite(east, "east")
    south = _finite(south, "south")
    north = _finite(north, "north")
    if west < -180.0 or west > 180.0 or east < -180.0 or east > 180.0:
        raise ValueError("bbox longitudes must be in [-180, 180]")
    if south < -90.0 or north > 90.0 or south > north:
        raise ValueError(
            "bbox latitude interval must satisfy -90 <= south <= north <= 90"
        )
    if west == -180.0 and east == 180.0:
        return ((-180.0, south, 180.0, north),)
    if west <= east:
        if west == east:
            point_lon = normalize_longitude(west)
            # Preserve a point request as a degenerate bbox for tile ownership.
            return ((point_lon, south, point_lon, north),)
        return ((west, south, east, north),)
    return ((west, south, 180.0, north), (-180.0, south, east, north))


def tiles_for_bbox(
    west: float,
    south: float,
    east: float,
    north: float,
    level: int = 0,
    *,
    max_tiles: int = 256,
) -> list[TileAddress]:
    """Enumerate unique owner tiles intersecting a bbox, sorted by (y, x).

    A wrapped bbox is split at the antimeridian. The tile cap keeps a request
    bounded and prevents accidental whole-planet acquisition.
    """

    if max_tiles < 1:
        raise ValueError("max_tiles must be positive")
    # Constructing this validates level before arithmetic uses its dimensions.
    TileAddress(level, 0, 0)
    step = 0.25 / (1 << level)
    x_count = 1440 * (1 << level)
    y_count = 720 * (1 << level)
    y_values = _axis_indices(south, north, -90.0, 90.0, step, y_count)
    bbox_parts = split_bbox_antimeridian(west, south, east, north)
    if south == north and abs(south) == 90.0:
        # A degenerate bbox at a pole names one physical point regardless of input longitude.
        x_values = {x_count // 2}
    else:
        x_values: set[int] = set()
        for part_west, _, part_east, _ in bbox_parts:
            x_values.update(
                _axis_indices(part_west, part_east, -180.0, 180.0, step, x_count)
            )
    count = len(x_values) * len(y_values)
    if count > max_tiles:
        raise ValueError(
            f"bbox resolves to {count} tiles, exceeding max_tiles={max_tiles}"
        )
    return [TileAddress(level, x, y) for y in y_values for x in sorted(x_values)]


def tiles_for_bboxes(
    bboxes: Iterable[tuple[float, float, float, float]],
    level: int = 0,
    *,
    max_tiles: int = 256,
) -> list[TileAddress]:
    """Return the stable unique union of tiles for multiple bbox parts."""

    addresses: set[TileAddress] = set()
    for west, south, east, north in bboxes:
        addresses.update(
            tiles_for_bbox(west, south, east, north, level, max_tiles=max_tiles)
        )
        if len(addresses) > max_tiles:
            raise ValueError(f"request exceeds max_tiles={max_tiles}")
    return sorted(addresses, key=lambda tile: (tile.y, tile.x))

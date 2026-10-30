# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""ESA WorldCover source tile naming and native-pixel window ownership tests."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from worldgen.global_features.addressing import TileAddress
from worldgen.global_features.models import CancellationToken, SourceError
from worldgen.global_features.worldcover import (
    _head_cog,
    _pixel_registration_from_tags,
    _window_indices,
    _WorldCoverCogCache,
    worldcover_source_for_tile,
    worldcover_tile_name,
)


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            result, self._body = self._body, b""
            return result
        result, self._body = self._body[:size], self._body[size:]
        return result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class WorldCoverTests(unittest.TestCase):
    def test_pixel_registration_requires_the_official_area_tag(self) -> None:
        self.assertEqual(
            _pixel_registration_from_tags({"AREA_OR_POINT": "Area"}), "area"
        )
        with self.assertRaisesRegex(SourceError, "AREA_OR_POINT=Area"):
            _pixel_registration_from_tags({"AREA_OR_POINT": "Point"})

    def test_three_degree_cog_names_include_signed_edges_and_poles(self) -> None:
        self.assertEqual(worldcover_tile_name(3.0, 51.0), "N51E003")
        self.assertEqual(worldcover_tile_name(-0.1, -0.1), "S03W003")
        self.assertEqual(worldcover_tile_name(180.0, 90.0), "N87W180")
        self.assertEqual(worldcover_tile_name(-180.0, -90.0), "S90W180")
        self.assertEqual(
            worldcover_source_for_tile(TileAddress.parse("wfg1:0:1324:224"))[0],
            "S36E150",
        )

    def test_adjacent_wfg_cells_select_disjoint_native_pixel_centers(self) -> None:
        resolution = 1.0 / 12_000.0
        transform = SimpleNamespace(
            a=resolution, b=0.0, c=0.0, d=0.0, e=-resolution, f=3.0
        )
        source_width = 36_000
        source_height = 36_000
        west_tile = TileAddress.parse("wfg1:0:720:360").bounds
        east_tile = TileAddress.parse("wfg1:0:721:360").bounds
        north_tile = TileAddress.parse("wfg1:0:720:361").bounds
        west_row, west_col, west_height, west_width = _window_indices(
            transform,
            source_width,
            source_height,
            west_tile.west,
            west_tile.south,
            west_tile.east,
            west_tile.north,
        )
        _east_row, east_col, east_height, east_width = _window_indices(
            transform,
            source_width,
            source_height,
            east_tile.west,
            east_tile.south,
            east_tile.east,
            east_tile.north,
        )
        north_row, _north_col, north_height, north_width = _window_indices(
            transform,
            source_width,
            source_height,
            north_tile.west,
            north_tile.south,
            north_tile.east,
            north_tile.north,
        )
        self.assertEqual((west_height, west_width), (3000, 3000))
        self.assertEqual((east_height, east_width), (3000, 3000))
        self.assertEqual((north_height, north_width), (3000, 3000))
        self.assertEqual(west_col + west_width, east_col)
        self.assertEqual(north_row + north_height, west_row)

    def test_finer_logical_tile_can_have_no_source_pixel_center(self) -> None:
        resolution = 1.0 / 12_000.0
        transform = SimpleNamespace(
            a=resolution, b=0.0, c=0.0, d=0.0, e=-resolution, f=3.0
        )
        address = TileAddress(level=16, x=720 * (1 << 16), y=360 * (1 << 16))
        bounds = address.bounds
        row, col, height, width = _window_indices(
            transform,
            36_000,
            36_000,
            bounds.west,
            bounds.south,
            bounds.east,
            bounds.north,
        )
        self.assertEqual((height, width), (0, 0))
        self.assertGreaterEqual(row, 0)
        self.assertGreaterEqual(col, 0)

    def test_cog_source_cache_verifies_digest_reuses_and_refreshes_corruption(
        self,
    ) -> None:
        body = b"small-cog-fixture"
        etag = '"fixture-etag"'
        url = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/test.tif"

        def urlopen(request, *, context, timeout):
            if request.get_method() == "HEAD":
                return FakeResponse(
                    200, {"Content-Length": str(len(body)), "ETag": etag}
                )
            return FakeResponse(
                200, {"Content-Length": str(len(body)), "ETag": etag}, body
            )

        with (
            tempfile.TemporaryDirectory() as cache_root,
            patch("urllib.request.urlopen", side_effect=urlopen) as mocked,
        ):
            cache = _WorldCoverCogCache(
                Path(cache_root), max_cache_bytes=1024, timeout_seconds=2
            )
            first = cache.get(
                tile_name="N00E000",
                url=url,
                object_size=len(body),
                etag=etag,
                token=CancellationToken(),
            )
            self.assertEqual(first[1], hashlib.sha256(body).hexdigest())
            self.assertEqual(first[2:], (len(body), False))
            second = cache.get(
                tile_name="N00E000",
                url=url,
                object_size=len(body),
                etag=etag,
                token=CancellationToken(),
            )
            self.assertEqual(second[2:], (0, True))
            self.assertEqual(mocked.call_count, 1)
            first[0].write_bytes(b"corrupt")
            third = cache.get(
                tile_name="N00E000",
                url=url,
                object_size=len(body),
                etag=etag,
                token=CancellationToken(),
            )
            self.assertEqual(third[1], hashlib.sha256(body).hexdigest())
            self.assertFalse(third[3])
            self.assertEqual(mocked.call_count, 2)

    def test_cog_object_size_limit_and_missing_tile_are_explicit(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=FakeResponse(
                200, {"Content-Length": str(512 * 1024 * 1024), "ETag": '"large"'}
            ),
        ):
            with self.assertRaises(SourceError) as caught:
                _head_cog(
                    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/x.tif",
                    token=CancellationToken(),
                    timeout_seconds=1,
                )
            self.assertEqual(caught.exception.code, "worldcover_size_invalid")
        with patch(
            "urllib.request.urlopen",
            side_effect=HTTPError(
                "https://esa-worldcover.s3.eu-central-1.amazonaws.com/missing.tif",
                404,
                "missing",
                {},
                None,
            ),
        ):
            with self.assertRaises(SourceError) as caught:
                _head_cog(
                    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/missing.tif",
                    token=CancellationToken(),
                    timeout_seconds=1,
                )
            self.assertEqual(caught.exception.code, "worldcover_tile_missing")


if __name__ == "__main__":
    unittest.main()

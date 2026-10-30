# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Pinned Overture catalog, range safety and source failure tests."""

from __future__ import annotations

import hashlib
import io
import re
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from worldgen.global_features.addressing import TileAddress
from worldgen.global_features.models import (
    OVERTURE_LAYERS,
    CancellationToken,
    CancelledError,
    SourceError,
)
from worldgen.global_features.overture import (
    OVERTURE_RELEASE,
    ByteBudget,
    HttpRangeReader,
    OvertureProvider,
    SourceFileRef,
    _stac_file_refs,
    _validate_source_url,
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


def make_catalog(rows: list[dict[str, object]]) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buffer)
    return buffer.getvalue()


class OvertureProviderTests(unittest.TestCase):
    def test_release_path_and_source_urls_are_pinned_and_validated(self) -> None:
        self.assertEqual(OVERTURE_RELEASE, "2026-10-21.0")
        url = (
            "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
            "release/2026-10-21.0/theme=transportation/type=segment/part-00000.parquet"
        )
        self.assertEqual(_validate_source_url(url, "transportation", "segment"), url)
        bad_urls = (
            url.replace("https://", "http://"),
            url.replace("2026-10-21.0", "latest"),
            url.replace("theme=transportation", "theme=places"),
            "https://evil.invalid/release/2026-10-21.0/theme=transportation/type=segment/x.parquet",
            url.replace("part-00000.parquet", "../part-00000.parquet"),
        )
        for bad_url in bad_urls:
            with self.subTest(url=bad_url), self.assertRaises(SourceError):
                _validate_source_url(bad_url, "transportation", "segment")

    def test_stac_index_selects_only_matching_pinned_aoi_files(self) -> None:
        in_bounds = {
            "collection": "segment",
            "type": "Feature",
            "bbox": {"xmin": 2.0, "ymin": 48.0, "xmax": 3.0, "ymax": 49.0},
            "assets": {
                "aws": {
                    "href": (
                        "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
                        "release/2026-10-21.0/theme=transportation/type=segment/a.parquet"
                    ),
                    "file:size": 100,
                }
            },
            "num_rows": 7,
        }
        outside = in_bounds | {
            "bbox": {"xmin": 80.0, "ymin": 0.0, "xmax": 81.0, "ymax": 1.0}
        }
        wrong_type = in_bounds | {"collection": "building"}
        bounds = TileAddress.parse("wfg1:0:729:555").bounds
        refs = _stac_file_refs(
            make_catalog([outside, in_bounds, wrong_type]),
            spec=OVERTURE_LAYERS["roads"],
            bounds=bounds,
        )
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].num_rows, 7)
        self.assertEqual(refs[0].size_bytes, 100)

    def test_catalog_empty_result_is_distinct_from_catalog_failure(self) -> None:
        bounds = TileAddress.parse("wfg1:0:729:555").bounds
        catalog = make_catalog(
            [
                {
                    "collection": "building",
                    "type": "Feature",
                    "bbox": {"xmin": 10.0, "ymin": 10.0, "xmax": 11.0, "ymax": 11.0},
                    "assets": {
                        "aws": {
                            "href": (
                                "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
                                "release/2026-10-21.0/theme=buildings/type=building/unrelated.parquet"
                            ),
                            "file:size": 100,
                        }
                    },
                    "num_rows": 0,
                }
            ]
        )
        provider = OvertureProvider()
        provider._catalog_cache = (catalog, hashlib.sha256(catalog).hexdigest())
        stream, audit = provider.open_tile_query(
            "roads", TileAddress.parse("wfg1:0:729:555"), CancellationToken()
        )
        self.assertEqual(list(stream), [])
        self.assertEqual(audit.catalog_candidate_file_count, 0)
        with self.assertRaises(SourceError) as caught:
            _stac_file_refs(
                b"malformed parquet", spec=OVERTURE_LAYERS["roads"], bounds=bounds
            )
        self.assertEqual(caught.exception.code, "catalog_malformed")

    def test_http_range_reader_requires_exact_206_and_reuses_cached_block(self) -> None:
        content = b"abcdefgh"
        source = SourceFileRef(
            url=(
                "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
                "release/2026-10-21.0/theme=transportation/type=segment/test.parquet"
            ),
            bbox=(0.0, 0.0, 1.0, 1.0),
            num_rows=1,
            size_bytes=len(content),
            theme="transportation",
            feature_type="segment",
        )

        def urlopen(request, *, context, timeout):
            if request.get_method() == "HEAD":
                return FakeResponse(
                    200,
                    {
                        "Content-Length": str(len(content)),
                        "ETag": '"pin"',
                        "Accept-Ranges": "bytes",
                    },
                )
            range_value = request.get_header("Range")
            start, end = (
                int(item)
                for item in re.fullmatch(r"bytes=(\d+)-(\d+)", range_value).groups()
            )
            return FakeResponse(
                206,
                {"Content-Range": f"bytes {start}-{end}/{len(content)}"},
                content[start : end + 1],
            )

        with patch("urllib.request.urlopen", side_effect=urlopen) as mocked:
            reader = HttpRangeReader(
                source,
                token=CancellationToken(),
                timeout_seconds=2,
                byte_budget=ByteBudget(8),
            )
            self.assertEqual(reader.read(4), b"abcd")
            reader.seek(0)
            self.assertEqual(reader.read(8), content)
            self.assertEqual(reader.total_bytes_read, 8)
            self.assertEqual(
                mocked.call_count, 2
            )  # HEAD plus one cached-byte range request.

    def test_http_range_reader_rejects_server_that_ignores_range(self) -> None:
        source = SourceFileRef(
            url=(
                "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
                "release/2026-10-21.0/theme=transportation/type=segment/test.parquet"
            ),
            bbox=(0.0, 0.0, 1.0, 1.0),
            num_rows=1,
            size_bytes=8,
            theme="transportation",
            feature_type="segment",
        )

        def urlopen(request, *, context, timeout):
            if request.get_method() == "HEAD":
                return FakeResponse(200, {"Content-Length": "8"})
            return FakeResponse(200, {"Content-Length": "8"}, b"abcdefgh")

        with patch("urllib.request.urlopen", side_effect=urlopen):
            reader = HttpRangeReader(
                source,
                token=CancellationToken(),
                timeout_seconds=2,
                byte_budget=ByteBudget(8),
            )
            with self.assertRaisesRegex(SourceError, "Range"):
                reader.read(1)

    def test_http_range_reader_timeout_and_cancellation_are_explicit(self) -> None:
        source = SourceFileRef(
            url=(
                "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
                "release/2026-10-21.0/theme=transportation/type=segment/test.parquet"
            ),
            bbox=(0.0, 0.0, 1.0, 1.0),
            num_rows=1,
            size_bytes=8,
            theme="transportation",
            feature_type="segment",
        )
        with patch(
            "urllib.request.urlopen", side_effect=TimeoutError("request timed out")
        ):
            with self.assertRaises(SourceError) as caught:
                HttpRangeReader(
                    source,
                    token=CancellationToken(),
                    timeout_seconds=0.01,
                    byte_budget=ByteBudget(8),
                )
            self.assertEqual(caught.exception.code, "source_network_error")

        def urlopen(request, *, context, timeout):
            if request.get_method() == "HEAD":
                return FakeResponse(200, {"Content-Length": "8"})
            return FakeResponse(206, {"Content-Range": "bytes 0-7/8"}, b"abcdefgh")

        with patch("urllib.request.urlopen", side_effect=urlopen):
            token = CancellationToken()
            reader = HttpRangeReader(
                source, token=token, timeout_seconds=2, byte_budget=ByteBudget(8)
            )
            token.cancel()
            with self.assertRaises(CancelledError):
                reader.read(1)

    def test_http_range_reader_rejects_wrong_content_range_and_byte_budget(
        self,
    ) -> None:
        source = SourceFileRef(
            url=(
                "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
                "release/2026-10-21.0/theme=transportation/type=segment/test.parquet"
            ),
            bbox=(0.0, 0.0, 1.0, 1.0),
            num_rows=1,
            size_bytes=8,
            theme="transportation",
            feature_type="segment",
        )

        def bad_range(request, *, context, timeout):
            if request.get_method() == "HEAD":
                return FakeResponse(200, {"Content-Length": "8"})
            return FakeResponse(206, {"Content-Range": "bytes 1-7/8"}, b"bcdefgh")

        with patch("urllib.request.urlopen", side_effect=bad_range):
            reader = HttpRangeReader(
                source,
                token=CancellationToken(),
                timeout_seconds=2,
                byte_budget=ByteBudget(8),
            )
            with self.assertRaises(SourceError) as caught:
                reader.read(1)
            self.assertEqual(caught.exception.code, "source_range_malformed")

        def good_range(request, *, context, timeout):
            if request.get_method() == "HEAD":
                return FakeResponse(200, {"Content-Length": "8"})
            return FakeResponse(206, {"Content-Range": "bytes 0-7/8"}, b"abcdefgh")

        with patch("urllib.request.urlopen", side_effect=good_range):
            reader = HttpRangeReader(
                source,
                token=CancellationToken(),
                timeout_seconds=2,
                byte_budget=ByteBudget(4),
            )
            with self.assertRaises(SourceError) as caught:
                reader.read(1)
            self.assertEqual(caught.exception.code, "source_read_limit")


if __name__ == "__main__":
    unittest.main()

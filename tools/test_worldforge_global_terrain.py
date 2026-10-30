"""WorldForge global terrain address, provider, cache, seam and failure tests."""

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

import httpx
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import Affine

import worldforge_global_terrain as terrain


COP90_PREFIX = "Copernicus_DSM_COG_30_N00_00_E000_00_DEM"
COP90_INDEX = f"{COP90_PREFIX}\n"
FIXED_RECEIPT = "2026-10-30T00:00:00Z"
# Live run artifacts use the supplied revision. The zero hash is fixture-only
# for local unit tests that do not publish or claim producer provenance.
PRODUCER_COMMIT = os.environ.get("WORLDFORGE_TEST_PRODUCER_COMMIT", "0" * 40)


def _fake_cog(values: np.ndarray | None = None) -> bytes:
    if values is None:
        values = np.arange(64, dtype=np.float32).reshape((8, 8)) + 100.0
    mem = MemoryFile()
    with mem.open(
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=Affine(0.125, 0.0, 0.0, 0.0, -0.125, 1.0),
        nodata=None,
    ) as dataset:
        dataset.write(values.astype(np.float32), 1)
        dataset.update_tags(AREA_OR_POINT="Point")
    result = mem.read()
    mem.close()
    return result


class FakeCop90Transport:
    def __init__(self, *, cog: bytes | None = None) -> None:
        self.cog = _fake_cog() if cog is None else cog
        self.etag = '"fixture-v1"'
        self.last_modified = "Thu, 29 Oct 2026 00:00:00 GMT"
        self.index_body = COP90_INDEX.encode("ascii")
        self.index_status = 200
        self.head_status = 200
        self.get_status = 200
        self.index_requests = 0
        self.head_requests = 0
        self.tile_requests = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tileList.txt"):
            self.index_requests += 1
            return httpx.Response(
                self.index_status,
                content=self.index_body if self.index_status == 200 else b"source unavailable",
                headers={"etag": '"index-v1"', "last-modified": self.last_modified},
                request=request,
            )
        if request.url.path.endswith(f"/{COP90_PREFIX}.tif"):
            if request.method == "HEAD":
                self.head_requests += 1
                return httpx.Response(
                    self.head_status,
                    headers={
                        "content-length": str(len(self.cog)),
                        "etag": self.etag,
                        "last-modified": self.last_modified,
                    },
                    request=request,
                )
            self.tile_requests += 1
            return httpx.Response(
                self.get_status,
                content=self.cog if self.get_status == 200 else b"source unavailable",
                request=request,
            )
        return httpx.Response(404, request=request)


class FakeGebcoSource:
    def __init__(self, *, all_water: bool = False, no_data: bool = False) -> None:
        self.all_water = all_water
        self.no_data = no_data
        self.window_requests = 0
        self.point_requests = 0

    @staticmethod
    def _receipt(variable: str, digest: str) -> dict[str, str | int | None]:
        name = "GEBCO_2026.nc" if variable == "elevation" else "gebco_2026_tid.nc"
        return {
            "asset_uri": f"https://dap.ceda.ac.uk/{name}",
            "access_protocol": "fixture DAP2",
            "variable": variable,
            "requested_subset": "fixture window",
            "request_uri_sha256": hashlib.sha256(variable.encode()).hexdigest(),
            "response_byte_length": 16,
            "response_sha256": digest,
            "response_etag": '"fixture"',
            "response_last_modified": FIXED_RECEIPT,
            "response_content_type": "text/plain",
        }

    @classmethod
    def metadata(cls, row0: int, col0: int, rows: int, columns: int) -> dict:
        return {
            "provider": "GEBCO test fixture",
            "dataset": "GEBCO_2026",
            "release": "GEBCO_2026",
            "asset_uri": "https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/ice_surface_elevation/netcdf/GEBCO_2026.nc",
            "source_window": {
                "row_offset": row0,
                "column_offset": col0,
                "rows": rows,
                "columns": columns,
            },
            "source_grid": {
                "rows": terrain.GEBCO_ROWS,
                "columns": terrain.GEBCO_COLUMNS,
                "pixel_registration": "cell_center",
                "spacing_degrees": terrain.GEBCO_STEP_DEGREES,
                "source_dtype_elevation": "int16",
                "source_dtype_tid": "int8",
            },
            "asset_receipts": {
                "elevation": cls._receipt("elevation", hashlib.sha256(b"elevation fixture").hexdigest()),
                "tid": cls._receipt("tid", hashlib.sha256(b"tid fixture").hexdigest()),
            },
            "classification_sampling": "test fixture nearest cell",
            "received_at_utc": FIXED_RECEIPT,
        }

    def read_samples_at_coordinates(self, longitude_samples, latitude_samples):
        self.point_requests += 1
        if self.all_water:
            tid = np.full(longitude_samples.shape, 11, dtype=np.int16)
        else:
            tid = np.where(longitude_samples < 0.125, 0, 11).astype(np.int16)
        heights = np.full(longitude_samples.shape, -30, dtype=np.int16)
        metadata = self.metadata(10, 20, 1, 1)
        return heights, tid, metadata

    def read_window(self, address):
        self.window_requests += 1
        row0, row1, col0, col1 = terrain._gebco_indices(address)
        rows, columns = row1 - row0, col1 - col0
        if self.no_data:
            heights = np.full((rows, columns), -32768, dtype=np.int16)
        else:
            heights = np.full((rows, columns), -120, dtype=np.int16)
        if self.all_water:
            tid = np.full((rows, columns), 11, dtype=np.int16)
        else:
            tid = np.zeros((rows, columns), dtype=np.int16)
        return heights, tid, self.metadata(row0, col0, rows, columns)


class GlobalTerrainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path(os.environ.get("WORLDFORGE_TEST_TEMP", tempfile.gettempdir()))
        self.test_dir = test_root / f"worldforge-global-terrain-test-{uuid.uuid4().hex}"
        self.test_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def client(self, fixture: FakeCop90Transport) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(fixture.handle))

    def provider(
        self,
        fixture: FakeCop90Transport | None = None,
        gebco: FakeGebcoSource | None = None,
        *,
        geoid_grid: Path | None = None,
    ) -> tuple[terrain.GlobalTerrainProvider, httpx.Client | None, FakeCop90Transport | None]:
        fixture = fixture or FakeCop90Transport()
        client = self.client(fixture)
        provider = terrain.GlobalTerrainProvider(
            self.test_dir / "cache",
            cop90_client=client,
            gebco_source=gebco or FakeGebcoSource(),
            geoid_grid=geoid_grid,
            generator_commit=PRODUCER_COMMIT,
            load_geoid_from_environment=geoid_grid is not None,
        )
        return provider, client, fixture


class AddressAndCoverageTests(GlobalTerrainTestCase):
    def test_longitude_wrap_poles_and_half_open_boundaries(self) -> None:
        self.assertEqual(terrain.tile_for_coordinate(180.0, 0.0).tile_id, "wfg1:0:0:360")
        self.assertEqual(terrain.tile_for_coordinate(-180.0, 0.0).tile_id, "wfg1:0:0:360")
        self.assertEqual(terrain.tile_for_coordinate(0.0, 90.0).tile_id, "wfg1:0:720:719")
        self.assertEqual(terrain.tile_for_coordinate(0.0, -90.0).tile_id, "wfg1:0:720:0")
        for level in (0, 5, 16):
            nx, ny = terrain.tile_counts(level)
            for longitude in (-1080.0, -180.0, -73.2, 0.0, 127.8, 180.0, 1080.0):
                self.assertEqual(terrain.tile_for_coordinate(longitude, 90.0, level), terrain.GlobalTileAddress(level, nx // 2, ny - 1))
                self.assertEqual(terrain.tile_for_coordinate(longitude, -90.0, level), terrain.GlobalTileAddress(level, nx // 2, 0))
        self.assertEqual(terrain.tile_for_coordinate(0.249999, 0.249999).tile_id, "wfg1:0:720:360")
        self.assertEqual(terrain.tile_for_coordinate(0.25, 0.25).tile_id, "wfg1:0:721:361")
        self.assertEqual(terrain.tile_for_coordinate(151.2, -33.87, 16).level, 16)
        with self.assertRaises(terrain.TerrainInputError):
            terrain.tile_for_coordinate(0.0, 90.00001)
        with self.assertRaises(terrain.TerrainInputError):
            terrain.tile_for_coordinate(float("nan"), 0.0)
        with self.assertRaises(terrain.TerrainInputError):
            terrain.tile_for_coordinate(float("nan"), 90.0)

    def test_bbox_tile_enumeration_wrap_and_pole_ownership(self) -> None:
        wrapped = list(terrain.iter_tiles_for_bounds(179.75, -0.25, -179.75, 0.25))
        self.assertEqual({(tile.x, tile.y) for tile in wrapped}, {(1439, 359), (0, 359), (1439, 360), (0, 360)})
        north_point = list(terrain.iter_tiles_for_bounds(-10.0, 90.0, 10.0, 90.0))
        south_point = list(terrain.iter_tiles_for_bounds(-180.0, -90.0, 180.0, -90.0))
        self.assertEqual(north_point, [terrain.GlobalTileAddress(0, 720, 719)])
        self.assertEqual(south_point, [terrain.GlobalTileAddress(0, 720, 0)])
        north_extent = list(terrain.iter_tiles_for_bounds(-1.0, 89.75, 1.0, 90.0))
        self.assertEqual({tile.x for tile in north_extent}, set(range(716, 724)))
        self.assertTrue(all(tile.y == 719 for tile in north_extent))
        whole_world_pole_touch = list(terrain.iter_tiles_for_bounds(-180.0, 89.75, 180.0, 90.0))
        self.assertEqual(len(whole_world_pole_touch), 1440)
        self.assertEqual({tile.y for tile in whole_world_pole_touch}, {719})

    def test_address_parse_and_exact_bounds(self) -> None:
        address = terrain.GlobalTileAddress.parse("wfg1:0:720:360")
        self.assertEqual(address.bounds, (0.0, 0.0, 0.25, 0.25))
        self.assertEqual(address.tile_id, "wfg1:0:720:360")
        with self.assertRaises(terrain.TerrainInputError):
            terrain.GlobalTileAddress.parse("wfg1:17:0:0")
        with self.assertRaises(terrain.TerrainInputError):
            terrain.GlobalTileAddress.parse("wfg1:0:1440:0")

    def test_source_cell_names_wrap_and_cover_exact_poles(self) -> None:
        self.assertTrue(
            terrain._source_prefix_for_tile(terrain.tile_for_coordinate(180.0, -86.0)).endswith("W180_00_DEM")
        )
        self.assertIn("S90_00_E000_00", terrain._source_prefix_for_tile(terrain.tile_for_coordinate(0, -90)))
        self.assertIn("N89_00_E000_00", terrain._source_prefix_for_tile(terrain.tile_for_coordinate(0, 90)))

    def test_boundary_poles_have_one_shared_longitude_zero_vertex(self) -> None:
        grid = {
            "source_affine": {
                "x_origin": -180.0,
                "pixel_width": 0.25,
                "y_origin": -90.0,
                "pixel_height": 0.25,
            },
            "columns": 1,
            "rows": 1,
        }
        nx, ny = terrain.tile_counts(0)
        for y, edge, target in ((ny - 1, "north", (0.0, 90.0)), (0, "south", (0.0, -90.0))):
            canonical_vertices = []
            for x in (0, nx // 2, nx - 1):
                address = terrain.GlobalTileAddress(0, x, y)
                features, counts = terrain._boundary_support_targets(address, grid)
                pole_features = [feature for feature in features if feature["edge"] == edge]
                self.assertEqual(len(pole_features), 1)
                self.assertEqual(pole_features[0]["id"], f"boundary:{edge}:0")
                self.assertEqual(pole_features[0]["edge_sample_count"], 1)
                self.assertEqual(
                    (pole_features[0]["target_longitude_deg"], pole_features[0]["target_latitude_deg"]),
                    target,
                )
                self.assertEqual(counts[edge], 1)
                canonical_vertices.append(target)
            self.assertEqual(canonical_vertices, [target, target, target])

    def test_boundary_exactness_uses_coordinate_equality_and_zero_offset(self) -> None:
        self.assertEqual(
            terrain._boundary_sample_relationship(2.5, 49.0, 2.5, 49.0),
            ("native_exact", 0.0),
        )
        method, offset = terrain._boundary_sample_relationship(2.5, 49.0, 2.500000000000001, 49.0)
        self.assertEqual(method, "nearest_native_sample")
        self.assertGreater(offset, 0.0)
        method, offset = terrain._boundary_sample_relationship(2.5, 49.0, 2.5, 49.00000000000001)
        self.assertEqual(method, "nearest_native_sample")
        self.assertGreater(offset, 0.0)

    def test_incomplete_boundary_edge_is_omitted_as_a_whole(self) -> None:
        grid = {
            "source_affine": {
                "x_origin": 2.25,
                "pixel_width": 1.0 / 1200.0,
                "y_origin": 49.0,
                "pixel_height": -1.0 / 1200.0,
            },
            "columns": 300,
            "rows": 300,
        }
        features, _ = terrain._boundary_support_targets(terrain.GlobalTileAddress(0, 729, 555), grid)
        valid = np.ones(len(features), dtype=np.uint8)
        failed_index = next(index for index, feature in enumerate(features) if feature["edge"] == "east")
        valid[failed_index] = 0
        edge_indices, valid_counts, complete, missing_edges, missing_ids = terrain._boundary_edge_coverage(features, valid)
        self.assertIn("north", complete)
        self.assertNotIn("east", complete)
        self.assertEqual(valid_counts["east"], len(edge_indices["east"]) - 1)
        self.assertEqual({record["edge"] for record in missing_edges}, {"east"})
        self.assertEqual(missing_ids, [features[failed_index]["id"]])
        self.assertTrue(all(features[index]["edge"] == "east" for index in edge_indices["east"]))

    def test_gebco_source_row_order_is_south_to_north(self) -> None:
        south = terrain._gebco_indices(terrain.GlobalTileAddress(0, 0, 0))
        north = terrain._gebco_indices(terrain.GlobalTileAddress(0, 0, 719))
        self.assertEqual(south, (0, 60, 0, 60))
        self.assertEqual(north, (43140, 43200, 0, 60))
        self.assertEqual(terrain._gebco_indices(terrain.GlobalTileAddress(0, 1439, 719))[3], 86400)

    def test_coverage_report_routes_every_global_base_tile(self) -> None:
        report = terrain.coverage_index_report([COP90_PREFIX])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["tile_count"], 1440 * 720)
        source = report["source_catalog"]
        self.assertEqual(source["candidate_wfg_level0_tiles"] + source["explicit_gebco_fallback_tiles"], 1440 * 720)
        self.assertGreater(source["explicit_gebco_fallback_tiles"], 0)
        self.assertFalse(report["proof"]["global_30m_claim"])


class ParsingTests(GlobalTerrainTestCase):
    def test_copernicus_attribution_notices_are_not_mojibaked(self) -> None:
        self.assertIn(chr(0x00a9) + " DLR e.V.", terrain.COP90_ATTRIBUTION)
        self.assertIn(chr(0x00a9) + " Airbus Defence and Space GmbH", terrain.COP90_ATTRIBUTION)
        self.assertIn("WorldDEM" + chr(0x2122) + "-90", terrain.COP90_MODIFICATION_NOTICE)
        self.assertNotIn(chr(0xfffd), terrain.COP90_ATTRIBUTION + terrain.COP90_MODIFICATION_NOTICE)
        self.assertIn("do not incur any liability", terrain.COPERNICUS_DISCLAIMER)
        self.assertIn("not officially endorsed", terrain.COPERNICUS_NO_ENDORSEMENT)

    def test_rejects_invalid_egm2008_grid(self) -> None:
        path = self.test_dir / "invalid_egm2008.tif"
        path.write_bytes(b"not a GeoTIFF")
        with self.assertRaises(terrain.TerrainInputError):
            terrain.EGM2008Grid(path)

    def test_cop90_tile_index_rejects_malformed_and_duplicate_records(self) -> None:
        self.assertIn(COP90_PREFIX, terrain.parse_cop90_index(COP90_INDEX))
        with self.assertRaises(terrain.TerrainInputError):
            terrain.parse_cop90_index(COP90_INDEX + COP90_INDEX)
        with self.assertRaises(terrain.TerrainInputError):
            terrain.parse_cop90_index("Copernicus_DSM_COG_10_N00_00_E000_00_DEM\n")

    def test_dap_ascii_is_bounded_and_preserves_rows(self) -> None:
        body = (
            "Dataset { Grid { ARRAY: Int16 elevation[lat = 2][lon = 2]; } } x;\n"
            "elevation.elevation[2][2]\n[0], -12, -11\n[1], -10, -9\n"
        )
        self.assertEqual(
            terrain._parse_dap_ascii(body, "elevation", 2, 2).tolist(),
            [[-12, -11], [-10, -9]],
        )
        with self.assertRaises(terrain.TerrainRemoteError):
            terrain._parse_dap_ascii(body, "elevation", 3, 2)
        with self.assertRaises(terrain.TerrainRemoteError):
            terrain._parse_dap_ascii("not a DAP response", "elevation", 1, 1)


class ProviderTests(GlobalTerrainTestCase):
    def test_mixed_land_water_tile_has_per_sample_source_and_quality_masks(self) -> None:
        cop_fixture = FakeCop90Transport()
        gebco = FakeGebcoSource()
        provider, client, _ = self.provider(cop_fixture, gebco)
        try:
            address = terrain.GlobalTileAddress(0, 720, 360)
            with provider.load_tile(address) as tile:
                self.assertEqual(tile.manifest["terrain"]["status"], "ready")
                self.assertEqual(tile.heights.shape, (2, 2))
                self.assertEqual(tile.heights.tolist(), [[148.0, -30.0], [156.0, -30.0]])
                self.assertEqual(tile.source_mask.tolist(), [[1, 2], [1, 2]])
                self.assertEqual(tile.quality_mask.tolist(), [[1, 4], [1, 4]])
                self.assertEqual(tile.tid_mask.tolist(), [[0, 11], [0, 11]])
                manifest_terrain = tile.manifest["terrain"]
                self.assertEqual(
                    [record["source_code"] for record in manifest_terrain["source_lineage"]],
                    [1, 2],
                )
                self.assertEqual(manifest_terrain["vertical"]["datum"], "per_sample_source_vertical_datum")
                self.assertTrue(manifest_terrain["coverage"]["fallback"])
                payloads = {item["path"]: item for item in manifest_terrain["payloads"]}
                self.assertIn("height_samples.f32le", payloads)
                self.assertEqual(payloads["height_samples.f32le"]["shape"], [2, 2])
                self.assertEqual(payloads["height_samples.f32le"]["dtype"], "float32")
        finally:
            provider.close()
            client.close()

    def test_same_source_adjacent_tiles_share_an_exact_source_edge(self) -> None:
        fixture = FakeCop90Transport()
        provider, client, _ = self.provider(fixture, FakeGebcoSource())
        try:
            west_address = terrain.GlobalTileAddress(0, 720, 360)
            east_address = terrain.GlobalTileAddress(0, 721, 360)
            with provider.load_tile(west_address) as west_tile, provider.load_tile(east_address) as east_tile:
                west_grid = west_tile.manifest["terrain"]["grid"]
                east_grid = east_tile.manifest["terrain"]["grid"]
                self.assertEqual(west_grid["source_window"]["column_offset"] + west_grid["columns"], east_grid["source_window"]["column_offset"])
                west_edge = west_grid["source_affine"]["x_origin"] + west_grid["columns"] * west_grid["source_affine"]["pixel_width"]
                east_edge = east_grid["source_affine"]["x_origin"]
                self.assertEqual(west_edge, east_edge)
                self.assertEqual(west_tile.manifest["terrain"]["source_lineage"][0]["sha256"], east_tile.manifest["terrain"]["source_lineage"][0]["sha256"])
        finally:
            provider.close()
            client.close()

    def test_missing_cop90_asset_uses_explicit_gebco_fallback(self) -> None:
        fixture = FakeCop90Transport()
        fixture.index_body = b"Copernicus_DSM_COG_30_N00_00_E001_00_DEM\n"
        gebco = FakeGebcoSource()
        provider, client, _ = self.provider(fixture, gebco)
        try:
            with provider.load_tile(terrain.GlobalTileAddress(0, 720, 360)) as tile:
                self.assertEqual(tile.manifest["terrain"]["status"], "ready")
                self.assertEqual(tile.manifest["terrain"]["source_lineage"][0]["product"], "GEBCO_2026")
                self.assertIn("no_asset_for_source_cell", [item["status"] for item in tile.manifest["terrain"]["source_attempts"]])
                self.assertTrue(np.all(tile.source_mask == terrain.SOURCE_CODE_GEBCO))
        finally:
            provider.close()
            client.close()

    def test_remote_cop90_failure_falls_back_without_mislabeling_absence(self) -> None:
        fixture = FakeCop90Transport()
        fixture.head_status = 503
        gebco = FakeGebcoSource()
        provider, client, _ = self.provider(fixture, gebco)
        try:
            with provider.load_tile(terrain.GlobalTileAddress(0, 720, 360)) as tile:
                attempts = tile.manifest["terrain"]["source_attempts"]
                self.assertIn("failed", [item["status"] for item in attempts])
                self.assertNotIn("no_asset_for_source_cell", [item["status"] for item in attempts])
                self.assertEqual(tile.manifest["terrain"]["source_lineage"][0]["product"], "GEBCO_2026")
        finally:
            provider.close()
            client.close()

    def test_catalog_remote_failure_keeps_fallback_explicit(self) -> None:
        fixture = FakeCop90Transport()
        fixture.index_status = 503
        provider, client, _ = self.provider(fixture, FakeGebcoSource())
        try:
            with provider.load_tile(terrain.GlobalTileAddress(0, 720, 360)) as tile:
                attempts = tile.manifest["terrain"]["source_attempts"]
                self.assertEqual(attempts[0]["status"], "auth_needed")
                self.assertEqual(attempts[1]["status"], "failed")
                self.assertEqual(tile.manifest["terrain"]["source_lineage"][0]["product"], "GEBCO_2026")
        finally:
            provider.close()
            client.close()

    def test_gebo_no_data_is_not_emitted_as_ready_zero_height(self) -> None:
        fixture = FakeCop90Transport()
        fixture.index_body = b"Copernicus_DSM_COG_30_N00_00_E001_00_DEM\n"
        provider, client, _ = self.provider(fixture, FakeGebcoSource(no_data=True))
        try:
            with provider.load_tile(terrain.GlobalTileAddress(0, 720, 360)) as tile:
                self.assertEqual(tile.manifest["terrain"]["status"], "no_source_coverage")
                self.assertTrue(np.all(tile.source_mask == 0))
                self.assertTrue(np.all(tile.heights == -32768.0))
                self.assertEqual(tile.manifest["terrain"]["coverage"]["gap_declared"], True)
        finally:
            provider.close()
            client.close()

    def test_cache_repeat_unload_corruption_and_source_etag_invalidation(self) -> None:
        fixture = FakeCop90Transport()
        provider, client, _ = self.provider(fixture, FakeGebcoSource())
        try:
            address = terrain.GlobalTileAddress(0, 720, 360)
            first = provider.load_tile(address)
            first_id = first.manifest["artifact_id"]
            bundle_path = provider.cache.tile_directory(address)
            corrupt_path = bundle_path / "height_samples.f32le"
            first.close()
            self.assertTrue(first.closed)
            self.assertIsNone(first.heights)

            reopened = provider.load_tile(address)
            self.assertEqual(reopened.manifest["artifact_id"], first_id)
            reopened.close()

            manifest_path = bundle_path / "manifest.json"
            tampered_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tampered_manifest["terrain"]["source_lineage"][0]["provider"] = "tampered source"
            manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
            repaired_manifest = provider.load_tile(address)
            self.assertEqual(repaired_manifest.manifest["artifact_id"], first_id)
            self.assertNotEqual(repaired_manifest.manifest["terrain"]["source_lineage"][0]["provider"], "tampered source")
            repaired_manifest.close()

            corrupt_path.write_bytes(b"corrupt cache bytes")
            repaired = provider.load_tile(address)
            self.assertEqual(repaired.manifest["artifact_id"], first_id)
            repaired.close()

            fixture.etag = '"fixture-v2"'
            fixture.cog = _fake_cog(np.arange(64, dtype=np.float32).reshape((8, 8)) + 200.0)
            invalidated = provider.load_tile(address)
            self.assertNotEqual(invalidated.manifest["artifact_id"], first_id)
            self.assertEqual(float(invalidated.heights[0, 0]), 248.0)
            invalidated.close()
        finally:
            provider.close()
            client.close()

    def test_auth_credentials_fail_closed_until_30f_adapter_exists(self) -> None:
        fixture = FakeCop90Transport()
        provider, client, _ = self.provider(fixture, FakeGebcoSource())
        previous = os.environ.get("WORLDFORGE_COPDEM30F_BEARER_TOKEN")
        os.environ["WORLDFORGE_COPDEM30F_BEARER_TOKEN"] = "test-only-sentinel"
        try:
            with provider.load_tile(terrain.GlobalTileAddress(0, 720, 360)) as tile:
                self.assertEqual(tile.manifest["terrain"]["status"], "failed")
                self.assertEqual(tile.manifest["terrain"]["error_code"], "copdem_30f_adapter_not_configured")
                self.assertNotIn("test-only-sentinel", json.dumps(tile.manifest))
        finally:
            if previous is None:
                os.environ.pop("WORLDFORGE_COPDEM30F_BEARER_TOKEN", None)
            else:
                os.environ["WORLDFORGE_COPDEM30F_BEARER_TOKEN"] = previous
            provider.close()
            client.close()


class ExternalSourceIntegrationTests(GlobalTerrainTestCase):
    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION") == "1" or "--integration" in os.sys.argv,
        "set --integration or WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION=1 for live-source checks",
    )
    def test_live_global_sources_addresses_seams_and_reload(self) -> None:
        points = [
            (151.2, -33.87),  # Sydney
            (-21.94, 64.15),  # Reykjavík
            (179.9, -86.0),  # high-latitude Antarctic tile east of the dateline
            (-180.0, -86.0),  # the same wrapped edge on the west side
            (0.0, 90.0),  # GEBCO ocean at the North Pole
        ]
        with terrain.GlobalTerrainProvider(
            self.test_dir / "live-cache", generator_commit=PRODUCER_COMMIT
        ) as provider:
            tiles = []
            for longitude, latitude in points:
                address = terrain.tile_for_coordinate(longitude, latitude)
                tile = provider.load_tile(address)
                tiles.append(tile)
                record = tile.manifest["terrain"]
                self.assertIn(record["status"], {"ready", "partial"})
                self.assertTrue(record["source_lineage"])
                self.assertEqual(record["coverage"]["valid_count"], int(np.count_nonzero(tile.source_mask)))
            self.assertEqual(tiles[0].manifest["terrain"]["source_lineage"][0]["product"], "COP-DEM-GLO-90-F")
            self.assertEqual(tiles[1].manifest["terrain"]["source_lineage"][0]["product"], "COP-DEM-GLO-90-F")
            self.assertEqual(tiles[2].manifest["tile"]["id"], "wfg1:0:1439:16")
            self.assertEqual(tiles[3].manifest["tile"]["id"], "wfg1:0:0:16")
            self.assertEqual(tiles[4].manifest["terrain"]["source_lineage"][0]["product"], "GEBCO_2026")
            east, west = tiles[2], tiles[3]
            east_grid, west_grid = east.manifest["terrain"]["grid"], west.manifest["terrain"]["grid"]
            east_last = east_grid["source_affine"]["x_origin"] + (east_grid["columns"] - 0.5) * east_grid["source_affine"]["pixel_width"]
            west_first = west_grid["source_affine"]["x_origin"] + 0.5 * west_grid["source_affine"]["pixel_width"]
            step = east_grid["source_affine"]["pixel_width"]
            self.assertAlmostEqual(((west_first - east_last + 180.0) % 360.0) - 180.0, step, places=8)
            repeat = provider.load_tile(terrain.tile_for_coordinate(*points[0]))
            self.assertEqual(repeat.manifest["artifact_id"], tiles[0].manifest["artifact_id"])
            repeat.close()
            for tile in tiles:
                tile.close()

    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION") == "1" or "--integration" in os.sys.argv,
        "set --integration or WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION=1 for live-source checks",
    )
    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_EGM2008_GRID") or "--geoid-grid" in os.sys.argv,
        "set --geoid-grid to verify real source-specific height conversion",
    )
    def test_live_mixed_coastal_tile_tracks_each_source_vertical_reference(self) -> None:
        geoid_path = Path(os.environ["WORLDFORGE_EGM2008_GRID"])
        address = terrain.GlobalTileAddress(0, 230, 511)  # San Francisco Bay, mixed coast/ocean.
        with terrain.GlobalTerrainProvider(
            self.test_dir / "mixed-coast-cache",
            geoid_grid=geoid_path,
            generator_commit=PRODUCER_COMMIT,
        ) as provider:
            tile = provider.load_tile(address)
            record = tile.manifest["terrain"]
            self.assertEqual(record["status"], "ready")
            self.assertEqual(record["grid"]["sample_order"], "row_major_north_to_south_west_to_east")
            self.assertEqual(record["grid"]["source_affine"]["origin_reference"], "pixel_corner")
            source_counts = {str(code): int(np.count_nonzero(tile.source_mask == code)) for code in (1, 2)}
            self.assertGreater(source_counts["1"], 0)
            self.assertGreater(source_counts["2"], 0)
            self.assertEqual(record["coverage"]["source_sample_counts_by_mask"], source_counts)
            self.assertEqual(record["geoid_conversion"]["coverage_by_source_mask"]["1"]["source_sample_count"], source_counts["1"])
            self.assertEqual(record["geoid_conversion"]["coverage_by_source_mask"]["2"]["source_sample_count"], source_counts["2"])
            np.testing.assert_array_equal(tile.geoid_mask[tile.source_mask != 0], tile.source_mask[tile.source_mask != 0])
            self.assertEqual(record["geoid_conversion"]["status"], "ready")
            self.assertEqual(record["geoid_conversion"]["grid"]["sha256"], "4191d471eefebf24091b56dbc604353cb3b8cf8cc70e448bb9ae56a272bef17a")

            source_map = record["source_vertical_reference_by_mask"]
            self.assertEqual(set(source_map), {"1", "2"})
            self.assertEqual(source_map["1"]["product"], "COP-DEM-GLO-90-F")
            self.assertEqual(source_map["1"]["source_vertical_reference"]["datum"], "EGM2008")
            self.assertIn("orthometric_height_EGM2008_m + EGM2008_undulation_m", source_map["1"]["equation"])
            self.assertEqual(source_map["2"]["product"], "GEBCO_2026")
            self.assertEqual(source_map["2"]["source_vertical_reference"]["datum"], "mean_sea_level_nominal_heterogeneous_inputs")
            self.assertIn("approximate", source_map["2"]["warning"])
            self.assertIn("shallow-water", source_map["2"]["warning"])
            self.assertIn("not represented as 30 m land truth", source_map["2"]["warning"])
            contract_map = record["vertical_conversion"]["vertical_references_by_source_code"]
            self.assertEqual(set(contract_map), {"1", "2"})
            self.assertEqual(contract_map["1"]["source_vertical_datum"], "EGM2008")
            self.assertFalse(contract_map["1"]["approximate"])
            self.assertEqual(contract_map["1"]["geoid_mask_code"], 1)
            self.assertEqual(contract_map["2"]["source_vertical_datum"], "MEAN_SEA_LEVEL")
            self.assertTrue(contract_map["2"]["approximate"])
            self.assertEqual(contract_map["2"]["geoid_mask_code"], 2)
            self.assertEqual(record["vertical_conversion"]["independent_numeric_validation"]["status"], "pass")
            self.assertEqual(
                record["vertical_conversion"]["tile_arithmetic_validation_by_source_code"]["1"]["max_abs_residual_m"],
                0.0,
            )
            self.assertEqual(
                record["vertical_conversion"]["tile_arithmetic_validation_by_source_code"]["2"]["max_abs_residual_m"],
                0.0,
            )

            source_meta = {str(item["source_code"]): item for item in record["source_lineage"]}
            self.assertEqual(source_meta["1"]["asset_id"], "Copernicus_DSM_COG_30_N37_00_W123_00_DEM")
            self.assertTrue(source_meta["1"]["asset_uri"].endswith("/Copernicus_DSM_COG_30_N37_00_W123_00_DEM.tif"))
            self.assertEqual(source_meta["2"]["product"], "GEBCO_2026")
            self.assertEqual(source_meta["2"]["asset_id"], "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa")
            self.assertTrue(source_meta["2"]["used_for_mosaic"])
            self.assertTrue(source_meta["2"]["used_for_classification"])
            self.assertTrue(source_meta["2"]["assets"][0]["metadata"]["response_sha256"])

            # Independent PROJ 9.5.1 / pyproj 3.7.2 references, transforming
            # EPSG:4979 h=100 m to EPSG:4326+3855 and taking N=100-H.
            # GEBCO code 2 still carries the nominal-MSL datum warning above;
            # the PROJ vector verifies only the EGM2008 undulation grid value.
            references = {
                1: ((0, 0), -31.8214701080322),
                2: ((0, 60), -31.86126412963867),
            }
            for code, ((row, column), expected_n) in references.items():
                self.assertEqual(int(tile.source_mask[row, column]), code)
                self.assertEqual(int(tile.geoid_mask[row, column]), code)
                self.assertAlmostEqual(float(tile.geoid_undulation[row, column]), expected_n, delta=2e-5)
                expected_h = float(tile.heights[row, column]) + float(tile.geoid_undulation[row, column])
                self.assertEqual(float(tile.ellipsoidal_heights[row, column]), expected_h)
            converted = tile.source_mask != 0
            np.testing.assert_array_equal(
                tile.ellipsoidal_heights[converted],
                tile.heights[converted].astype(np.float64) + tile.geoid_undulation[converted].astype(np.float64),
            )

            for payload in record["payloads"]:
                if payload.get("payload_kind") != "raster_grid":
                    payload_bytes = (provider.cache.tile_directory(address) / payload["path"]).read_bytes()
                    self.assertEqual(len(payload_bytes), payload["size_bytes"])
                    self.assertEqual(hashlib.sha256(payload_bytes).hexdigest(), payload["sha256"])
                    continue
                self.assertEqual(payload["row_order"], record["grid"]["sample_order"])
                self.assertEqual(payload["pixel_registration"], record["grid"]["pixel_registration"])
                self.assertEqual(payload["sample_geotransform"]["origin_reference"], "pixel_corner")
                payload_bytes = (provider.cache.tile_directory(address) / payload["path"]).read_bytes()
                self.assertEqual(len(payload_bytes), payload["size_bytes"])
                self.assertEqual(hashlib.sha256(payload_bytes).hexdigest(), payload["sha256"])
            artifact_id = tile.manifest["artifact_id"]
            tile.close()
            tile.close()
            reopened = provider.load_tile(address)
            self.assertEqual(
                reopened.manifest["artifact_id"],
                artifact_id,
                json.dumps(reopened.manifest.get("terrain", {}), sort_keys=True),
            )
            reopened.close()

    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION") == "1" or "--integration" in os.sys.argv,
        "set --integration or WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION=1 for live-source checks",
    )
    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_EGM2008_GRID") or "--geoid-grid" in os.sys.argv,
        "set --geoid-grid to verify shared-edge ellipsoidal samples",
    )
    def test_live_paris_boundary_support_matches_east_north_and_corner_core_samples(self) -> None:
        geoid_path = Path(os.environ["WORLDFORGE_EGM2008_GRID"])
        addresses = {
            "paris": terrain.GlobalTileAddress(0, 729, 555),
            "east": terrain.GlobalTileAddress(0, 730, 555),
            "north": terrain.GlobalTileAddress(0, 729, 556),
            "northeast": terrain.GlobalTileAddress(0, 730, 556),
        }
        with terrain.GlobalTerrainProvider(
            self.test_dir / "paris-boundary-cache",
            geoid_grid=geoid_path,
            generator_commit=PRODUCER_COMMIT,
        ) as provider:
            tiles = {name: provider.load_tile(address) for name, address in addresses.items()}
            try:
                paris = tiles["paris"]
                support = paris.boundary_support
                self.assertIsNotNone(support)
                self.assertEqual(support["type"], "FeatureCollection")
                terrain_record = paris.manifest["terrain"]["boundary_support"]
                self.assertEqual(terrain_record["status"], "ready")
                self.assertEqual(terrain_record["coverage"]["missing_feature_count"], 0)
                descriptor = next(
                    payload for payload in paris.manifest["terrain"]["payloads"]
                    if payload.get("payload_kind") == "terrain_boundary_support"
                )
                self.assertEqual(descriptor["feature_count"], len(support["features"]))
                self.assertEqual(descriptor["boundary_support_edges"], terrain_record["boundary_support_edges"])
                for feature in support["features"]:
                    props = feature["properties"]["worldforge"]["boundary_support"]
                    artifact = props["source_artifact"]
                    self.assertTrue(artifact["product"])
                    self.assertTrue(artifact["release"])
                    self.assertTrue(artifact["asset_id"])
                    self.assertEqual(
                        props["ellipsoidal_height_h"],
                        props["source_height_H"] + props["geoid_undulation_N"],
                    )
                    if props["source_mask"] == terrain.SOURCE_CODE_COP90:
                        self.assertIsNone(props["type_identifier_mask"])
                        self.assertIsNone(props["warning"])
                    else:
                        self.assertIsInstance(props["type_identifier_mask"], int)
                        self.assertTrue(props["warning"])
                        self.assertEqual(
                            set(artifact["checksums"]),
                            {"elevation", "tid"},
                        )
                    if props["sample_method"] == "native_exact":
                        self.assertEqual(
                            (props["source_sample_longitude"], props["source_sample_latitude"]),
                            tuple(feature["geometry"]["coordinates"][:2]),
                        )
                        self.assertEqual(props["source_to_boundary_offset_m"], 0.0)
                    else:
                        self.assertGreater(
                            props["source_to_boundary_offset_m"],
                            0.0,
                            (feature["id"], props["source_sample_longitude"], props["source_sample_latitude"], feature["geometry"]["coordinates"][:2]),
                        )

                def coordinate_index(tile: terrain.LoadedTile) -> dict[tuple[float, float], dict[str, Any]]:
                    assert tile.boundary_support is not None
                    return {
                        (float(feature["geometry"]["coordinates"][0]), float(feature["geometry"]["coordinates"][1])): feature
                        for feature in tile.boundary_support["features"]
                    }

                tile_edges = {name: coordinate_index(tile) for name, tile in tiles.items()}
                for edge, neighbor_name in (("east", "east"), ("north", "north")):
                    edge_features = [
                        feature for feature in support["features"]
                        if feature["properties"]["worldforge"]["boundary_support"]["edge"] == edge
                    ]
                    self.assertGreaterEqual(len(edge_features), 2)
                    for feature in edge_features:
                        coordinates = feature["geometry"]["coordinates"]
                        key = (float(coordinates[0]), float(coordinates[1]))
                        neighbor_feature = tile_edges[neighbor_name].get(key)
                        if neighbor_feature is None and edge == "north" and math.isclose(key[0], 2.5, abs_tol=1e-11) and math.isclose(key[1], 49.0, abs_tol=1e-11):
                            neighbor_feature = tile_edges["northeast"].get(key)
                        self.assertIsNotNone(neighbor_feature, (edge, key))
                        neighbor_coordinates = neighbor_feature["geometry"]["coordinates"]
                        self.assertEqual(coordinates[:2], neighbor_coordinates[:2])
                        self.assertEqual(coordinates[2], neighbor_coordinates[2])
                        props = feature["properties"]["worldforge"]["boundary_support"]
                        neighbor_props = neighbor_feature["properties"]["worldforge"]["boundary_support"]
                        for field in (
                            "source_height_H",
                            "geoid_undulation_N",
                            "ellipsoidal_height_h",
                            "source_mask",
                            "quality_mask",
                            "type_identifier_mask",
                            "geoid_mask",
                        ):
                            self.assertEqual(props[field], neighbor_props[field], (edge, key, field))
            finally:
                for tile in tiles.values():
                    tile.close()

    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION") == "1" or "--integration" in os.sys.argv,
        "set --integration or WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION=1 for live-source checks",
    )
    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_EGM2008_GRID") or "--geoid-grid" in os.sys.argv,
        "set --geoid-grid to verify canonical pole support",
    )
    def test_live_pole_support_is_one_identical_vertex_for_all_longitude_tiles(self) -> None:
        geoid_path = Path(os.environ["WORLDFORGE_EGM2008_GRID"])
        addresses = [
            terrain.GlobalTileAddress(0, x, 719)
            for x in (0, 720, 1439)
        ]
        with terrain.GlobalTerrainProvider(
            self.test_dir / "north-pole-boundary-cache",
            geoid_grid=geoid_path,
            generator_commit=PRODUCER_COMMIT,
        ) as provider:
            tiles = [provider.load_tile(address) for address in addresses]
            try:
                canonical_features = []
                for tile in tiles:
                    self.assertIsNotNone(tile.boundary_support)
                    north = [
                        feature for feature in tile.boundary_support["features"]
                        if feature["properties"]["worldforge"]["boundary_support"]["edge"] == "north"
                    ]
                    self.assertEqual(len(north), 1)
                    feature = north[0]
                    self.assertEqual(feature["id"], "boundary:north:0")
                    self.assertEqual(
                        feature["properties"]["worldforge"]["boundary_support"]["edge_sample_count"],
                        1,
                    )
                    self.assertEqual(feature["geometry"]["coordinates"][:2], [0.0, 90.0])
                    canonical_features.append(feature)
                first_coords = canonical_features[0]["geometry"]["coordinates"]
                first_props = canonical_features[0]["properties"]["worldforge"]["boundary_support"]
                for feature in canonical_features[1:]:
                    self.assertEqual(feature["geometry"]["coordinates"], first_coords)
                    props = feature["properties"]["worldforge"]["boundary_support"]
                    for key in (
                        "source_height_H",
                        "geoid_undulation_N",
                        "ellipsoidal_height_h",
                        "source_mask",
                        "quality_mask",
                        "type_identifier_mask",
                        "geoid_mask",
                    ):
                        self.assertEqual(props[key], first_props[key])
            finally:
                for tile in tiles:
                    tile.close()

    @unittest.skipUnless(
        os.environ.get("WORLDFORGE_EGM2008_GRID") or "--geoid-grid" in os.sys.argv,
        "set --geoid-grid to verify the real geoid grid",
    )
    def test_independent_proj_reference_for_real_egm2008_grid(self) -> None:
        geoid_path = Path(os.environ["WORLDFORGE_EGM2008_GRID"])
        with terrain.EGM2008Grid(geoid_path) as geoid:
            undulation, valid = geoid.sample(
                np.asarray([[9.88, 180.0, -180.0]], dtype=np.float64),
                np.asarray([[54.44, 10.0, 10.0]], dtype=np.float64),
            )
            self.assertTrue(valid.all())
            # Independent PROJ 9.5.1 / pyproj 3.7.2 transform, WGS84 ellipsoid
            # EPSG:4979 (h=100 m) -> EPSG:4326+3855 at (9.88E, 54.44N) gave
            # H=60.110880718994146 m, hence N=39.889119281005854 m.
            self.assertAlmostEqual(float(undulation[0, 0]), 39.889119281005854, delta=2e-5)
            self.assertEqual(float(undulation[0, 1]), float(undulation[0, 2]))


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--geoid-grid", type=Path)
    args, unittest_args = parser.parse_known_args()
    if args.integration:
        os.environ["WORLDFORGE_RUN_GLOBAL_TERRAIN_INTEGRATION"] = "1"
    if args.geoid_grid:
        os.environ["WORLDFORGE_EGM2008_GRID"] = str(args.geoid_grid)
    unittest.main(argv=[__file__, *unittest_args], verbosity=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

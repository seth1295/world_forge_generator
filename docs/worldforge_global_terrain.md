# WorldForge global terrain producer

This module adds a standalone global terrain route to the legacy generator repository. `tools/worldforge_global_terrain.py` imports no BeamNG server, Lua hook, game asset, or BeamNG schema. It reads individual source windows on demand and writes source-backed terrain bundles to a bounded local cache. It does not download the global source grids.

The producer emits a terrain-only manifest (`urn:worldforge:contract:global-terrain-tile:1.0.0`). The parent repository owns `world_tile_bundle_v1`; an adapter must place this record under `layers.terrain`, preserve its payload descriptors and lineage, add the common tile origin and other requested layers, and validate the parent schema. The producer does not edit `terrain_grid_v1`, which remains a regional contract.

## WFG-1 addressing

The shared address scheme is `wf-geodetic-grid-v1`, with IDs `wfg1:<level>:<x>:<y>`. Level 0 has 1,440 columns and 720 rows of 0.25 degrees cells. At level `L` (0 through 16), each cell is `0.25 degrees / 2^L`; the address level is independent of sample spacing. Longitudes normalize to `[-180, 180)`, so +180 degrees becomes -180 degrees. Cells own west/south edges and exclude east/north edges, with the global limits handled explicitly. At either exact pole, point requests use longitude 0 and the midpoint x column. `iter_tiles_for_bounds` handles wrapped longitude intervals; a point/degenerate pole bbox returns the canonical owner, while a non-degenerate extent touching the pole retains its intersecting columns.

For example, Paris at (2.375 degrees E, 48.875 degrees N) resolves to `wfg1:0:729:555`, whose bounds are [2.25, 2.50) degrees x [48.75, 49.00) degrees. The tile key does not imply a 30 m output; its payload is native source sampling with explicit row order and geotransform.

## Source stack and limits

| Role | Provider/release | Native semantics | Use and limitation |
|---|---|---|---|
| Preferred global land surface | COP-DEM-GLO-30-F through Copernicus Data Space | 30 m DSM; WGS84-G1150 / EPSG:4326; EGM2008 orthometric metres / EPSG:3855 | The preferred 30-F authenticated adapter is not configured in this generator. Requests record `auth_needed` when registered credentials are absent, then use the approved GLO-90-F fallback. If a 30-F bearer token is configured before its adapter exists, the provider fails closed. |
| Always-available global land fallback | COP-DEM-GLO-90-F, AWS public COG mirror, 2021 release | 90 m DSM; WGS84-G1150 / EPSG:4326; EGM2008 orthometric metres / EPSG:3855 | Individual one-degree COGs are fetched on demand and cropped to source-native point samples. No AWS ocean assets are expected. |
| Global ocean and explicit lower-resolution fallback | GEBCO_2026 plus its TID grid, official CEDA OPeNDAP service | WGS84 longitude/latitude, 15 arc-seconds, pixel-center registered; H nominally relative to MSL; elevation is Int16 and TID is Int8 | Native GEBCO cell values are preserved as Float32 output, with TID, source and quality masks. Inputs are heterogeneous and GEBCO is not 30 m land truth. Some shallow-water values may use a datum other than MSL. |
| Optional ellipsoidal placement conversion | OSGeo PROJ-data `us_nga_egm08_25.tif`, derived from NGA EGM2008 | Worldwide 2.5 arc-minute geoid undulation grid, 8,640 x 4,321, Float32 point samples | Public Domain; the accepted source asset is pinned by SHA-256 `4191d471eefebf24091b56dbc604353cb3b8cf8cc70e448bb9ae56a272bef17a`. Configure with `--geoid-grid` or `WORLDFORGE_EGM2008_GRID`. A digest mismatch fails closed. If no verified grid is configured, native H remains available and ellipsoidal `h` is explicitly `not_generated`; no `H=h` assumption is made. |

The AWS GLO-30 Public mirror is partial and is never used. The resolver does not use paid sources, restricted data, or public OSM tile APIs. GLO-90 catalog membership is only an asset-availability index; it is not a land/water mask. The generated coverage report sends every WFG-1 base cell to a GLO-90 asset prefix or to an explicit GEBCO route. At sample time, Copernicus H is selected only where GEBCO TID classifies the cell as land and a valid Cop90 sample exists; other valid samples use GEBCO with its own source code. Missing, invalid or inaccessible Cop90 data routes to the GEBCO fallback. No fabricated ground detail is inserted.

GEBCO's published errata identifies TID=0 (land) mislabeling in parts of the Great Lakes. The TID grid is retained and its known limitation is carried in the source lineage; it must not be interpreted as an infallible land mask. Copernicus handbook-wide statistical accuracy claims are recorded as context only and are not local guarantees. Their validation excludes Greenland and Antarctica; Antarctic output retains its GEBCO/TID provenance and is not presented as validated Copernicus land data.

## Height and raster semantics

For Copernicus, `height_samples.f32le` stores source-native orthometric `H` unchanged. The source mask uses 1 for COP-DEM-GLO-90-F and 2 for GEBCO_2026; 0 is no data. The quality mask keeps native Cop90 land, GEBCO land fallback, GEBCO water/bathymetry and uncertain TID classes distinct. GEBCO's TID values remain in `tid_mask.i16le`. Source masks, quality masks, TID, H, N and h are aligned row-major arrays.

When the verified EGM2008 grid is configured, `geoid_undulation.f32le` stores bilinearly interpolated `N`, and `ellipsoidal_height_samples.f64le` stores `h = H + N`. The original H remains in the bundle. Cop90's EGM2008 orthometric-to-ellipsoidal conversion is directly compatible with this grid. For GEBCO, the layer also emits `h approximately equals H_MSL + N`, marked approximate with a warning that heterogeneous shallow-water datums may differ. It is only a placement estimate and is not labelled surveyed ellipsoidal truth. `vertical_conversion.vertical_references_by_source_code` maps each source-mask code to its source vertical datum, conversion, approximation flag, warning and geoid-mask code. Tile-level equation residuals and independent PROJ reference vectors are included in the manifest; these validate numerical interpolation/arithmetic, not source accuracy.

Raster metadata carries horizontal CRS, datum/realization when supplied, axis order, point/cell-center registration, source affine and whether its origin is a pixel corner, row direction, source window, native spacing, no-data convention and the exact selection/resampling method. Copernicus samples are copied without elevation interpolation. GEBCO elevation and TID are read from nearest native 15-arc-second cell centers at Copernicus sample centers. GEBCO-only level-0 output uses its native 60 x 60 cell-center window. Same-source shared-edge values remain identical; no cross-source smoothing hides a source boundary.

When the verified EGM2008 grid is configured, `boundary_support.geojson` carries separately sampled 3D support vertices for mesh edges omitted by the half-open raster window. Its stable IDs use unpadded decimal indices (`boundary:north:0`). The perimeter order is north west-to-east including corners; east north-to-south excluding northeast; south east-to-west excluding southeast; west south-to-north excluding southwest and northwest. The support ring does not change raster ownership, counts, or source spacing. A boundary whose any sample lacks a valid source or geoid value is omitted as a whole; expected, valid, missing and emitted counts and unavailable IDs remain in the manifest. `source_to_boundary_offset_m` is a WGS84 Vincenty inverse geodesic distance from the chosen native source sample to the exact target boundary point. The method is `native_exact` only when normalized source coordinates exactly equal the target and the recorded offset is exactly zero; otherwise it is `nearest_native_sample` with deterministic east-then-north tie ownership. Each feature carries source H, geoid N, h, source/quality/TID/geoid masks and the source artifact URI/checksum receipt where available. GEBCO request checksums are explicitly scoped to the fetched hyperslab response, not represented as whole-file checksums.

At either exact pole, each touching tile emits one canonical support point at longitude 0 and latitude +90 or -90, with `boundary:north:0` or `boundary:south:0` and `edge_sample_count: 1`. Every polar longitude tile queries that same canonical source coordinate so the shared pole vertex and ellipsoidal h are identical. This special vertex is not a sequence of per-longitude boundary samples. When no verified geoid grid is configured, the support layer is marked `not_generated` and no 3D boundary payload is emitted.

Layer `status` values are `ready`, `partial`, `no_source_coverage`, `not_generated`, `failed` and `cancelled`. `ready` means the requested source window completed; it does not assert geographic completeness. Smaller address cells with no native source centers return `not_generated` and identify the nearest generated ancestor rather than inventing finer detail.

## Provenance, cache and attribution

Each source lineage record includes provider, product/release, exact source URI or hyperslab, asset ID/version, response/asset size and digest where available, original CRS and vertical reference, source quality, license/terms URL, acquisition/cache receipt, and the operation used to select or convert values. The GEBCO elevation and TID OPeNDAP requests are separate hashed hyperslabs, not downloads of the 7 GB elevation grid or 3.5 GB TID grid. The Copernicus catalog itself is about 1.1 MB; a DEM COG is fetched only for a requested cell.

The default cache is bounded to 512 MiB. Asset COGs, tile payloads, request receipts and manifests are SHA-256 checked; the cached manifest checksum binds source lineage and transform metadata so edits invalidate the bundle. Source ETags/version metadata participate in cache identity; source changes invalidate affected bundles. Corrupt payloads or manifests are discarded and regenerated. Repeated load/unload releases in-memory arrays and a reload returns the validated artifact. Python HTTP clients use the installed `certifi` CA bundle and retain TLS verification. No credentials are written into manifests or error messages. This Windows environment's native SChannel client did fail on the public S3 host with `SEC_E_NO_CREDENTIALS`; the Python/certifi HTTPS path succeeded without disabling TLS.

The Copernicus general-public GLO-90 terms grant worldwide, time-unlimited, free rights to reproduce, distribute, communicate, adapt and combine the data, subject to acceptance and required downstream notices. Those notices are emitted in each tile's source lineage and `attribution` array. Preserve them in any redistributed product:

- `© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.`
- For modified data: `produced using Copernicus WorldDEM™-90 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.`
- Liability notice: `The organisations in charge of the Copernicus programme by law or by delegation do not incur any liability for any use of the Copernicus WorldDEM™-90.`
- Do not imply endorsement by the provider, licensor, EU, ESA, DLR or Airbus. The producer carries a no-endorsement notice in its attribution records.

GEBCO_2026 is placed in the public domain. Its source acknowledgement is requested and required by its terms; do not imply GEBCO, IHO or IOC endorsement. The OSGeo PROJ-data EGM2008 grid is marked Public Domain in the official repository README. Source version, product, license and hash are recorded per output tile; versions are not interchangeable.

## Reproduction

Install the small runtime dependencies:

```powershell
py -m pip install -r tools/requirements-worldforge-global-terrain.txt
```

Resolve a WFG-1 address:

```powershell
py tools/worldforge_global_terrain.py address --longitude 2.375 --latitude 48.875
```

Generate a Paris tile into a temporary bounded cache with a locally downloaded and SHA-256-verified PROJ-data geoid grid:

```powershell
py tools/worldforge_global_terrain.py `
  --cache-dir $env:TEMP\worldforge-global-terrain-cache `
  --geoid-grid C:\data\us_nga_egm08_25.tif `
  --commit <generator-commit> `
  generate --tile wfg1:0:729:555
```

To enable the independently verified real-source checks, run:

```powershell
py tools/test_worldforge_global_terrain.py `
  --integration `
  --geoid-grid C:\data\us_nga_egm08_25.tif
```

The integration suite acquires geographically separated live data at Sydney, Reykjavik, Paris/San Francisco Bay, Antarctica on both sides of the dateline, and the North Pole (GEBCO ocean). It checks source receipts and arrays, shared-edge identity, longitude wrap, source masks, EGM2008 numerical references, repeated load/unload and cache reload. Fixtures exercise corrupt inputs, source/index failure, missing assets, no-data and invalidation. Fixture results are unit-test evidence only; the geographic integrations use live source responses.

Regenerate the public level-0 coverage/index report without fetching elevation rasters:

```powershell
py tools/worldforge_global_terrain.py `
  --cache-dir $env:TEMP\worldforge-global-terrain-cache `
  coverage-check --output docs\global_terrain_coverage_v1.json
```

`docs/global_terrain_coverage_v1.json` was generated from the live GLO-90 index on 2026-10-30. It records 26,475 available one-degree GLO-90 prefixes, routing of all 1,036,800 WFG-1 level-0 addresses (423,600 through a catalogued GLO-90 source prefix; 613,200 explicit GEBCO routes), and aligned 60 x 60 GEBCO windows for all addresses. Its route-map SHA-256 is `1e6d63bcf5f4c515d8c60cebabc847433c6e259a2ff883a2b5f5e5cc47987ac5`. These are address/source-route checks, not a terrain accuracy, actual land mask, or 30 m coverage claim.

## Official source references checked 2026-10-30

- [Copernicus DEM product and technical information](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM): DSM definition, resolutions, global coverage, registered access and product context.
- [Copernicus 2025 mission-specific licence PDF](https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf): GLO-90 general-public use rights and required source/modified-data/liability/non-endorsement notices.
- [AWS Open Data registry for Copernicus DEM](https://registry.opendata.aws/copernicus-dem/): GLO-90 worldwide COG source, no ocean tiles, AWS GLO-30 Public partial-coverage warning and license reference.
- [GEBCO_2026 grid and terms](https://www.gebco.net/data-products-gridded-bathymetry-data/gebco2026-grid): 15-arc-second center registration, WGS84/MSL caveats, TID, public-domain terms, attribution and uncertainty.
- [GEBCO grid errata](https://www.gebco.net/data-products/gridded-bathymetry-data/data-set-errata): known current grid/TID issues, including Great Lakes TID=0 errors.
- [OSGeo PROJ-data NGA README](https://github.com/OSGeo/PROJ-data/blob/master/us_nga/us_nga_README.txt): EGM2008 2.5-minute global grid and Public Domain terms.
- [NGA Earth-info](https://earth-info.nga.mil/): authoritative EGM2008 model source.
- The live endpoints used by the producer are the AWS GLO-90 `tileList.txt` and per-asset COG URLs, the CEDA GEBCO_2026 `ice_surface_elevation/netcdf/GEBCO_2026.nc.ascii` and `gebco_2026_tid/netcdf/gebco_2026_tid.nc.ascii` DAP variables, and [OSGeo's EGM2008 GeoTIFF](https://cdn.proj.org/us_nga_egm08_25.tif). Each generated manifest carries the exact request/asset receipt and bytes/hash where available.

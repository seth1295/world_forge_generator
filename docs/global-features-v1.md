# WorldForge global geographic features v1

**Status:** generator-owned engine-neutral producer proposal. The parent product repository owns the consumer schema and may map these layer objects into its `world_tile_bundle_v1` contract. This module does not change `terrain_grid_v1` or require BeamNG.

## Purpose and scope

`worldgen/global_features/` resolves bounded WFG-1 tile requests into reproducible, source-aware layer bundles. It handles roads, building footprints, mapped water features, ESA WorldCover land-cover classes, and a class-preserving vegetation-placement support mask. It is an independent Python producer; it does not import or launch the inherited BeamNG server, Lua hooks, terrain producer, or proprietary game assets.

The source data remains remote. A request addresses one tile or a bounded WGS84 bbox, downloads only the selected Overture GeoParquet row groups, and reads one ESA WorldCover COG source tile on demand. The output cache is capped at 8 GiB by default. The separate local WorldCover COG cache is capped at 512 MiB and holds only a few verified 3° source files. Neither cache seeds or downloads the planet.

## Addressing

The module uses the Main Lead's canonical **WFG-1** scheme:

- Tile IDs are `wfg1:<level>:<x>:<y>`; levels are 0 through 16.
- Level 0 uses 0.25° × 0.25° cells, with 1,440 columns and 720 rows. At level `L`, each level-0 cell is subdivided by `2^L` in each axis.
- Longitude is normalized to `[-180, 180)`; exactly +180° maps to −180°. Latitude is valid from −90° to +90°; the exact north pole belongs to the final row. Both exact poles use the longitude-0 column for point and degenerate-bbox ownership because longitude is physically undefined there. A non-degenerate polar-area bbox below a pole keeps each requested x cell in the polar row.
- Tiles own west and south edges and exclude internal east and north edges. Bboxes with west > east split at the antimeridian. A bbox resolver rejects requests that exceed its tile cap instead of scheduling a globe-sized fetch.
- Logical tile level is separate from source sample spacing. WorldCover pixels remain on their native raster grid.

`addressing.py` exposes point addressing, canonical ID parsing, bbox decomposition and duplicate-free antimeridian tile enumeration.

## Sources, terms and attribution

### Overture Maps vectors

The reproducible baseline is Overture release **`2026-10-21.0`**, with GeoParquet accessed from the official release catalog and its release-pinned `collections.parquet` STAC index. The queried feature types are:

| Output layer | Overture theme/type | Theme terms |
| --- | --- | --- |
| `roads` | `transportation/segment` | ODbL-1.0 |
| `buildings` | `buildings/building` | ODbL-1.0 |
| `water` | `base/water` | ODbL-1.0 |

The release archive and release notes are documented by the [2026-10-21 release notes](https://docs.overturemaps.org/blog/2026/10/21/release-notes/). Official [GeoParquet access guidance](https://docs.overturemaps.org/getting-data/) describes cloud-native AOI querying and the release-partitioned storage layout. The [attribution and licensing page](https://docs.overturemaps.org/attribution/) lists ODbL for the transportation, buildings and base themes, and documents theme source data including OpenStreetMap, TomTom, Esri Community Maps, Global ML Building Footprints, Google Open Buildings, USGS, Daylight, ESA WorldCover, ETOPO1 and GLOBathy.

The theme-level source inventory is recorded with each layer as a source catalogue reference, not as a claim that every listed provider contributed to that tile. Each emitted feature preserves its Overture feature ID and original `sources[]` array, including any available property pointer, dataset, SPDX license, source record ID, update time, confidence, provider, resource, version and linearly referenced range. The source item record is retained exactly as received; a missing source-item license is not inferred. Layer manifests also identify the ODbL theme terms and the attribution page. Where Overture features derive from OpenStreetMap, the required attribution is `© OpenStreetMap contributors, Overture Maps Foundation`.

ODbL is compatible with commercial use subject to its attribution and share-alike database obligations. Keep Overture-derived feature databases separable and review those duties for each public use. Overture theme-level ODbL does not replace the source records and any source-specific obligations carried by individual features. The generator does not include source data in the public repository.

### ESA WorldCover

Land cover is **ESA WorldCover 10 m 2021 v200**, consumed from its 3°×3° AWS COG objects. The official [data access page](https://esa-worldcover.org/en/data-access) identifies its AWS source, COG product, terms and required acknowledgement; the product citation is [Zanaga et al. 2022](https://doi.org/10.5281/zenodo.7254221). The data license is **CC BY 4.0**. Every WorldCover-derived layer includes the attribution:

> © ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium

WorldCover is a land-cover **classification**, not an inventory of individual trees, shrubs or grasses. The classes are retained as the source's categorical values: `10` tree cover, `20` shrubland, `30` grassland, `40` cropland, `50` built-up, `60` bare/sparse vegetation, `70` snow/ice, `80` permanent water, `90` herbaceous wetland, `95` mangroves and `100` moss/lichen; `0` is NoData.

On Windows, GDAL/Rasterio's SChannel HTTP backend failed with `SEC_E_NO_CREDENTIALS` in the checked environment. This producer verifies HTTPS with `certifi`, downloads the single required 3° COG into its bounded source cache, verifies declared length/ETag and a local SHA-256, then reads only the requested pixel window. The COG is never copied into the generated feature bundle. COG reads are limited to a 256 MiB source object and 512 MiB source cache; a source object beyond those bounds is reported as unavailable/failure rather than silently skipped.

## Bundle and layer representation proposal

The output directory is content-addressed by a SHA-256 cache key that includes the bundle schema, producer version and source revision, WFG-1 tile ID, requested layers, Overture release/themes/types, WorldCover release, vegetation support algorithm, geometry/raster methods and generation limits. The manifest records the exact producer Git revision. From a Git checkout, a dirty worktree is explicitly marked `<commit>+dirty`; deployments without Git can set `WORLDFORGE_SOURCE_REVISION` to their immutable build revision. `manifest.json` is written last and the directory is published atomically. A cache hit verifies every payload size and digest. A source release, source revision, producer or transformation-version change yields a new key; a corrupted cache fails verification rather than being rendered as current data.

Each manifest contains:

- Schema ID/version, producer, bundle outcome, creation time and content-addressed bundle ID.
- Canonical WFG-1 address, exact WGS84 tile bounds, CRS, axis order, edge ownership and the distinction between logical tile level and source sample spacing.
- Per-layer standard outcome (`ready`, `partial`, `no_source_coverage`, `not_generated`, `failed`, `cancelled`), feature/pixel counts, source query extent, coverage claim/confidence, attribution, transform/method, source lineage, error code and SHA-256 payload descriptors.
- Source release and theme/type, per-feature lineage policy, license, attribution URLs, raster class labels, raw raster dimensions/affine/NoData, and known gaps.
- A hard warning that a successful empty vector query is only an empty query result, not proof of complete data or absence of real-world features.

| Layer | Payload | Semantics |
| --- | --- | --- |
| `roads` | Deterministic shards at `roads/shard-00000.geojson`, etc. | RFC 7946/WGS84 2D source centerline fragments. Root GeoJSON `id` preserves Overture `id`; `properties.worldforge.fragment_id` is deterministic per tile. Road class, subtype, subclass, surface, width/level/access/speed/routing fields and original `sources[]` remain in properties. No road width, height, bridge clearance or surveyed accuracy is invented. |
| `buildings` | Deterministic GeoJSON shards under `buildings/` | Positive-area clipped building footprints with stable Overture ID, source semantics and exact source records. Heights/levels/roof data appear only if Overture provides them; no footprint extrusion is implied by this producer. |
| `water` | Deterministic GeoJSON shards under `water/` | Positive-area Overture Base water polygons with source class/subtype and provenance. A missing or empty result does not imply dry land. |
| `landcover` | `landcover/classes.u8` | Raw uint8 categorical values in row-major order (rows north-to-south, columns west-to-east). `width`, `height`, affine, CRS, axis order, pixel registration, NoData, class labels/counts and source window are in both transform and layer metadata. The six affine coefficients `[a,b,x0,d,e,y0]` locate the upper-left pixel **corner**; source metadata declares `AREA_OR_POINT=Area`. For zero-based column `c` and row `r`, the pixel center is `lon=a*(c+0.5)+b*(r+0.5)+x0`, `lat=d*(c+0.5)+e*(r+0.5)+y0`. No resampling or reprojection is done. |
| `vegetation` | `vegetation/placement_support.u8` | A support mask that retains the source class IDs `10`, `20`, `30`, `40`, `60`, `90`, `95` and `100` verbatim, and writes `0` for NoData, built-up, snow/ice, water and unknown values. It shares the land-cover source window, affine and declared pixel registration; `width`, `height`, corner origin, row/column order, and pixel-center formula appear in transform and layer metadata. It is keyed by the WFG-1 tile and `worldforge_procedural_vegetation_support_v2`; a consumer derives its stable 64-bit seed as the first eight SHA-256 bytes, big-endian, of UTF-8 `algorithm_id + NUL + canonical_tile_id`. It contains no individual plants, locations, species, or tree inventory. |

Vector rows are selected through the pinned STAC file bbox index, then row-group bbox statistics, then an exact feature-bbox check and a GEOS intersection against the WFG tile rectangle. GeoJSON coordinate order remains `[longitude, latitude]` and geometries are not reprojected or simplified. The stable source ID is retained across fragments. Within one tile, duplicate source-fragment IDs are deduplicated; across tiles, adjacent fragments intentionally share source IDs and have distinct fragment IDs. Line segments that lie entirely on an internal east or north edge belong only to the adjacent half-open tile. Polygon intersections with zero area are omitted.

Each vector payload is a compact GeoJSON FeatureCollection, sorted by source ID and fragment ID and split under configured feature/byte caps. Defaults are a maximum of 1,000,000 features and 512 MiB serialized payload per vector layer, 10,000 features and 16 MiB per shard, an 8 MiB individual feature, and a 2 GiB total bundle. If a configured feature, byte, range-read or source-validation bound is reached, **the entire layer fails atomically and no staged subset is published**. Its failure metadata records the exact configured limit, source rows scanned, remote bytes read, staged feature count/bytes and, for an oversized next feature, its serialized size. Rerun with a deliberately higher per-run bound if the tile requires it; cached results never contain a cap-truncated feature layer. `partial` is reserved for a genuine data-coverage/fallback condition, not an internally truncated dataset.

### Road-terrain alignment handoff

Road output remains 2D. The `roads` layer includes a `worldforge_terrain_sampler_v1` request description, and `terrain.py` defines an injected sampler interface. A ready sample must declare **WGS 84 ellipsoidal height (EPSG:4979, metres)** because the planetary runtime consumes ellipsoidal heights for ECEF placement. Each sample trace carries its source height/reference, geoid undulation when used, resulting ellipsoidal `h`, terrain artifact ID and SHA-256, sampler method and warnings. For an orthometric source such as EGM2008, a conversion trace such as `h = H + N` must retain both source `H` and geoid undulation `N`; any approximate-mean-sea-level caveat remains in warnings. The drape helper rejects an orthometric or undocumented ready result. An unavailable or cancelled sampler returns no draped geometry; this feature producer never guesses height or converts a vertical datum itself.

### Coverage states

- Overture catalog has no intersecting parquet object: `no_source_coverage` with a catalog/file gap note.
- Overture file(s) exist and a query completes with zero features: `ready`, `feature_count=0`, query audit recorded, `coverage_claim=no_completeness_claim`.
- WorldCover COG object is absent for a requested 3° source tile: `no_source_coverage`.
- Malformed catalog/GeoParquet/COG, wrong CRS/dtype/NoData/class, integrity mismatch, incomplete network/read, or a configured feature/shard/size bound: the affected layer is `failed` atomically with no staged subset published. Its audit records the source rows/bytes read and staged feature count/bytes discarded; empty source responses are not substituted.
- `partial` is reserved for a genuine, explicit source-coverage or fallback condition with its provenance and known gap recorded; it never means an internal cap-truncated dataset.
- Cancellation: current requests stop between bounded reads and the incomplete bundle is not published to the output cache.

All network calls use verified HTTPS, finite request timeouts, bounded response/object sizes and cancellation checks. Overture GeoParquet rows are read through verified Python HTTP Range requests; only STAC-listed objects intersecting the tile and their overlapping row groups are fetched. Range responses must be `206` with an exact matching `Content-Range`; a server that ignores the range is rejected. COG cache writes are temporary, verified and atomically renamed. Cache paths are restricted to module-owned roots and payloads reject symlinks/traversal.

## Dependencies

`tools/requirements-global-features.txt` pins Python dependencies and their package SPDX identifiers. PyArrow (Apache-2.0) reads GeoParquet metadata/row groups; Shapely (BSD-3-Clause) validates/intersects vector geometry; Rasterio (BSD-3-Clause) reads local COG windows; NumPy (BSD-3-Clause and bundled permissive notices) handles uint8 windows; Certifi (MPL-2.0) supplies verified TLS roots in this environment; tzdata (Apache-2.0) supplies Windows time-zone metadata. No credentials or paid APIs are used.

## Reproduction

From the generator repository root in PowerShell:

```powershell
python -m pip install -r tools/requirements-global-features.txt

# Urban Paris: WFG-1 tile [2.25, 48.75, 2.5, 49.0]
python -m worldgen.global_features resolve-tile `
  --tile-id wfg1:0:729:555 `
  --cache-root "$env:TEMP\WorldForge\global-feature-cache"

# Antimeridian-crossing high-latitude bbox; WFG-1 splits it on both sides of ±180°.
python -m worldgen.global_features resolve-bbox `
  --bbox 179.9 70.1 -179.9 70.4 `
  --level 0 `
  --layers roads water landcover vegetation `
  --cache-root "$env:TEMP\WorldForge\global-feature-cache"
```

Use the smaller source window or just the layer needed for a probe:

```powershell
python -m worldgen.global_features resolve-tile `
  --tile-id wfg1:0:1324:224 `
  --layers roads water landcover vegetation `
  --cache-root "$env:TEMP\WorldForge\global-feature-cache"
```

`--layers` accepts `roads`, `buildings`, `water`, `landcover`, `vegetation`. A bbox is expanded to owned WFG-1 tiles with a default 64-tile request cap. Increase limits only deliberately through `GenerationLimits`; do not use a globe-sized request as a way to prefetch all data.

For a dense tile that exceeds default layer caps, raise the bounded run configuration explicitly, for example:

```powershell
python -m worldgen.global_features resolve-tile `
  --tile-id wfg1:0:729:555 `
  --cache-root "$env:TEMP\WorldForge\global-feature-cache" `
  --max-features-per-layer 2000000 `
  --max-payload-bytes-per-layer 2147483648 `
  --max-bundle-bytes 4294967296 `
  --max-cache-bytes 8589934592
```

Those settings keep individual GeoJSON shards bounded; they do not prefetch neighboring tiles or the whole release.

## Verified boundary

The producer emits source-derived map features and classification/policy metadata. It does not make a global coverage-completeness claim, invent feature accuracy, infer building height, drape roads without a real terrain sampler, or output individual vegetation instances. The WorldCover placement-support mask gives the Godot consumer a deterministic class field; actual procedural plant proxies and their terrain alignment are downstream runtime work. Source coverage can be absent or incomplete and must remain visible in manifests.

# Adjacent Golden Tiles for Terrain Grid v1

This document records the producer's real-data Golden tile layout. The output
remains two ordinary Terrain Grid v1 bundles; the parent schema is unchanged.
The producer is still source-specific and does not claim to be a general DEM
tiler.

## Command and compatibility

The standalone producer adds `--region golden-tiles`. It writes two new
subdirectories, `golden-west/` and `golden-east/`, each containing the fixed v1
files `manifest.json`, `horizontal_xy.f64le`, and `height_samples.f32le`.
`--region both`, `--region golden`, and `--region baltimore` keep their previous
selection and output behavior. The generator validates each bundle against the
unchanged parent Terrain Grid v1 JSON Schema and validates the seam before
reporting success.

Tile identity is carried by the output directory and the v1 `grid.source_window`
fields. Both manifests retain `grid.region_id = wf-vs01:golden-front-range` and
the exact same `frame` object. The frame hash is the same; `artifact_id` differs
because the source window and payloads differ. The bundle names and source
windows make the two outputs unambiguous without adding a schema property.

For a local Windows PowerShell checkout, run from the generator repository:

```powershell
$env:WF_USGS_SOURCE_ROOT = '<local-only directory containing the acquired USGS snapshots>'
$outputRoot = '<new output directory>'
$parentRoot = '<local world_forge checkout>'

python tools/terrain_grid_v1.py `
  --source-dir $env:WF_USGS_SOURCE_ROOT `
  --ledger "$parentRoot/docs/preproduction/vertical-slice.md" `
  --schema "$parentRoot/contracts/terrain_grid_v1.schema.json" `
  --output-root $outputRoot `
  --region golden-tiles
```

The source directory and output root are local inputs. The producer hashes the
original TIFF, XML sidecar, and saved TNM product record against the parent
ledger, reads the two tile windows from that original raster, and refuses to
replace existing bundle directories. No BeamNG process, assets, Lua hooks, or
network request is used.

To reproduce the focused producer suite after creating both existing pilot
bundles and the adjacent Golden bundles in the same output root:

```powershell
$env:WF_TEST_TEMP_ROOT = '<existing task-scoped writable test directory>'

python tools/terrain_grid_v1.py `
  --source-dir $env:WF_USGS_SOURCE_ROOT `
  --ledger "$parentRoot/docs/preproduction/vertical-slice.md" `
  --schema "$parentRoot/contracts/terrain_grid_v1.schema.json" `
  --output-root $outputRoot `
  --region both

python tools/terrain_grid_v1.py `
  --source-dir $env:WF_USGS_SOURCE_ROOT `
  --ledger "$parentRoot/docs/preproduction/vertical-slice.md" `
  --schema "$parentRoot/contracts/terrain_grid_v1.schema.json" `
  --output-root $outputRoot `
  --region golden-tiles

python tools/test_terrain_grid_v1.py `
  --schema "$parentRoot/contracts/terrain_grid_v1.schema.json" `
  --example "$parentRoot/contracts/examples/terrain_grid_v1_minimal" `
  --bundles $outputRoot `
  --source-dir $env:WF_USGS_SOURCE_ROOT
```

`WF_TEST_TEMP_ROOT` is optional in ordinary environments. It lets the test
runner place temporary cases in an existing task-scoped writable directory
when the machine's default temporary folder is restricted. Each test removes
the case directory it creates.

The tested Windows environment used Python 3.12.10, Rasterio 1.5.2, GDAL
3.12.2, PROJ 9.7.1, NumPy 2.5.3, and jsonschema 4.26.0. These versions were
already available; the task installed no package or runtime dependency.

## Source data and rights

The original sample data is the USGS 1/3 Arc Second product `USGS 1/3 Arc
Second n40w106 20260630` for the Front Range near Golden, Colorado. The public
product records are:

| Artifact | Public source | Acquired bytes | SHA-256 |
|---|---|---:|---|
| GeoTIFF | [USGS 3DEP download](https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/historical/n40w106/USGS_13_n40w106_20260630.tif) | 413,480,184 | `2a2ac1c4e0e571e5734bafff2b47f0b5cd864ddcb45b69085f0fbad86d44b4ba` |
| XML metadata | [USGS metadata sidecar](https://thor-f5.er.usgs.gov/ngtoc/metadata/waf/elevation/1-3_arc-second/UT_COUTAML_D24/USGS_13_n40w106_20260630.xml) | 12,962 | `a43163a50c480a0ac50573a9fe4b9d50e6469ed317edf5399d4bf04ea2de28c7` |
| TNM product record | [ScienceBase product record](https://www.sciencebase.gov/catalog/item/6a471a0d1ba49bcdf785e0fa) | 2,707 | `9cbbb36df1333b8636521eedfdab35b2e8040bff216ae774dd0e2627a5e92195f` |

The local snapshot timestamp recorded in the parent ledger is
`2026-10-29T13:41:19Z`. It matches the source TIFF's Windows `CreationTimeUtc`,
and the manifest labels its basis `filesystem_creation_time`. It is not a
download receipt, product publication date, pixel survey date, or an accuracy
measurement. The product was published 2026-07-01 and its TNM record was last
updated 2026-07-02.

The source raster is a 10,812 by 10,812 single-band Float32 GeoTIFF in
`EPSG:4269`, with `AREA_OR_POINT=Area`, scale 1, offset 0, approximately
`9.25925927e-05` degree pixel spacing, and no-data value `-999999`. The XML
identifies NAD83 geographic coordinates and NAVD88 orthometric metres. It gives
a 1 m vertical resolution but disclaims an accuracy warranty and does not give
a tile-specific accuracy bound. The metadata does not identify a NAD83
realization or epoch.

USGS identifies 3DEP products as public-domain U.S. Government data. Preserve
the attribution **U.S. Geological Survey (USGS), USGS 1/3 Arc Second n40w106
20260630**. This does not clear other datasets or software. The raw source
files remain outside the repository; the source raster itself is not added to
this repository.

## Tile windows and seam semantics

The approved Golden crop in the original raster is `(row=2544, column=8214,
rows=216, columns=432)`. Row and column indices are zero-based absolute source
indices. V1 stores sample centres, with rows north-to-south and columns
west-to-east. The split is:

| Output directory | `source_window` (row, column, rows, columns) | Absolute source columns | Relative crop columns |
|---|---|---|---|
| `golden-west/` | `(2544, 8214, 216, 217)` | 8214 through 8430 | 0 through 216 |
| `golden-east/` | `(2544, 8430, 216, 216)` | 8430 through 8645 | 216 through 431 |

Source column 8430 is the one deliberately shared boundary sample. Both tiles
read that sample from the original Float32 raster and project the same pixel
centre in the same frame; the producer requires exact byte equality for every
shared Float64 XY pair and NAVD88 Float32 height. The west mesh owns the
sample-centre intervals from crop columns 0 to 216; the east mesh owns intervals
216 to 431. They share boundary vertices but no cell strip. This layout carries
all 432 original columns exactly once after the duplicated seam sample is
removed.

Because the input uses `AREA_OR_POINT=Area`, each manifest's
`source_pixel_footprint_bounds` reports the outer edges of its source pixels.
Those metadata bounds overlap by the width of the shared source pixel at column
8430. This describes source-pixel coverage; it does not mean the sample-centre
mesh cell strips overlap. The v1 consumer builds cells between sample centres,
so it should use the exact shared edge and sample-window adjacency when assigning
mesh cells rather than treating the two outer pixel-footprint bounds as mesh
bounds.

No resampling, height conversion, or interpolation is performed. Each tile
must remain inside the original parent window, use the complete row range, and
meet the other tile at exactly one column. The layout validator rejects a gap,
more than one overlapping column, a window outside the parent crop, mismatched
row ranges, a different frame/source identity, or a missing shared anchor.

## Shared regional frame and limits

Both manifests use `wf-vs01:golden-grs80-aeqd`. The origin is the original
source sample at row 2652, column 8430: longitude
`-105.21995370310027°`, latitude `39.754953704278655°`, and
`H0=1729.523193359375 m` NAVD88. The horizontal operation is a direct
`EPSG:4269` to GRS80 ellipsoidal AEQD projection; the generated WKT2 and PROJ
pipeline are stored in each v1 manifest. Horizontal values are Float64 East and
North metres. Height values retain source Float32 NAVD88 orthometric metres;
the consumer's local vertical expression remains `H-H0`.

The Main Lead set a crop-specific horizontal chart criterion of at most
`0.0011 m`: a conservative `0.000070245 m` curvature comparison bound for the
maximum 2,042.063294 m anchor radius, plus a 1 mm independent numerical
allowance. The functional test checks a 13 by 13 point grid against independent
GRS80 Vincenty vectors and all pairwise distances. This criterion covers
projection-scale and numerical comparison for this crop only. It is not a
survey-accuracy or source-accuracy bound; the USGS source has no tile-specific
accuracy bound. It is not a global tolerance.

The two tile frames cannot be combined with Baltimore or treated as ENU,
ellipsoidal height, WGS84, ECEF, or a global frame. V1 does not encode LOD,
cross-frame transforms, or tile cell ownership. This producer emits only the
selected Golden split and does not prove full-Earth coverage, source accuracy,
consumer install behavior, LOD behavior, or rendered seam continuity.

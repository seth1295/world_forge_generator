# WF Terrain Grid v1 Producer Pilot

The standalone `tools/terrain_grid_v1.py` command builds the two approved WF-VS-01
USGS 3DEP source windows and can additionally emit the adjacent Golden tiles,
without importing or launching the legacy BeamNG server, game, Lua hooks, or
BeamNG asset formats. It is a source-specific pre-production pilot, not a
general DEM tiler. The source windows and their public acquisition records are
selected by the Main Lead and recorded in the WorldForge integration
repository's `docs/preproduction/vertical-slice.md`. The Golden split and seam
semantics are documented in [terrain-grid-v1-adjacent-golden.md](terrain-grid-v1-adjacent-golden.md).

## Reproduction

Use a local-only directory containing the two original USGS TIFFs, XML metadata
sidecars, and saved TNM product records. Supply the integration repository's
vertical-slice ledger and v1 JSON Schema. The command verifies all six source
hashes and lengths from the ledger, checks the TNM record and XML identity,
validates the full raster metadata, reads only the selected crop windows, and
refuses to overwrite existing bundle directories.

Before hashing a source file, it validates the ledger byte count against the
Terrain Grid v1 file-size ceiling and compares that value with the file's
current length. Hashing is then bounded by the validated expected length. The
acquisition timestamp comes from the explicit UTC snapshot receipt recorded in
the supplied ledger row; the current machine's filesystem creation time is not
used. The manifest uses the v1 `recorded_download_receipt` basis value for that
receipt.

`--region both` treats the selected bundle outputs as one command result. If a
later region fails, the command removes complete bundles created earlier by
that invocation and prints an error without reporting partial success. An
existing region directory is never overwritten or included in rollback. Each
bundle also removes only files it successfully opened when a write or
validation fails; a partial file is tracked as soon as exclusive creation
succeeds. If cleanup fails or an output changed after creation, the error names
the preserved region directory so it can be inspected and recovered.

```powershell
python tools/terrain_grid_v1.py `
  --source-dir <local-USGS-snapshot-directory> `
  --ledger <world_forge/docs/preproduction/vertical-slice.md> `
  --schema <world_forge/contracts/terrain_grid_v1.schema.json> `
  --output-root <new-output-directory> `
  --region both
```

The output root receives `baltimore/` and `golden/` bundle directories, each
containing exactly `manifest.json`, `horizontal_xy.f64le`, and
`height_samples.f32le`. The manifest contains public URLs and source hashes, but
no local source or output paths. Source rasters remain local-only.

The functional test command validates the parent's synthetic cross-language
hash vector and, when `--bundles` and `--source-dir` are supplied, both real
bundles against their source Float32 windows and independent GRS80 geodesic
checks. It also uses small task-scoped temporary fixtures to exercise bundle
generation through the CLI, parser and corrupted-output rejection, existing
output preservation, dirty-worktree provenance, bounded source-size checks,
later-region rollback, partial-write cleanup, and the declared direct AEQD
operation. These fixture tests use local files only and do not contact a
network:

```powershell
python tools/test_terrain_grid_v1.py `
  --schema <world_forge/contracts/terrain_grid_v1.schema.json> `
  --example <world_forge/contracts/examples/terrain_grid_v1_minimal> `
  --bundles <new-output-directory> `
  --source-dir <local-USGS-snapshot-directory>
```

Install only the pinned packages listed in `tools/requirements-terrain-grid-v1.txt`
in an isolated environment if the required libraries are not already available.
The producer was exercised with Python 3.12.10, Rasterio 1.5.2, GDAL 3.12.2,
PROJ 9.7.1, NumPy 2.5.3, and jsonschema 4.26.0. Package metadata identifies
Rasterio as BSD-3-Clause, NumPy as BSD-3-Clause with its bundled-component
notices, and jsonschema as MIT. No package installation is part of the pilot.

## Spatial and data semantics

For each region the producer applies the source EPSG:4269 longitude/latitude
coordinates directly to an ellipsoidal AEQD projection on GRS80 centered on the
recorded source pixel anchor. It does not insert a WGS84 intermediate, apply a
datum shift, or request a ballpark operation. The exact resulting WKT2 and the
explicit operation pipeline are recorded in the manifest. The source horizontal
datum realization and epoch remain unspecified; the output is not a global,
ECEF, WGS84, or cross-region frame.

The raster window is read at stride 1 and is not resampled. Height bytes retain
the source Float32 NAVD88 orthometric metres and no-data value. Any no-data,
NaN, infinity, source height outside ±12,000 m, H0 outside ±12,000 m, or relative
height outside ±24,000 m aborts production. Consumers use `H-H0` only as a
regional chart's local up value. The output projection coordinates are
Float64 little-endian East/North pairs and are checked against the v1 ±100 km
component limit.

The frame and artifact SHA-256 functions implement the byte recipes in the
parent contract. The synthetic minimal example's published digest vector is a
cross-language conformance check. `artifact_id` excludes creation timestamps;
it is stable for identical source, frame, window, and payload content.

## Verification boundary

The independent GRS80 Vincenty checks use a 3×3 point sample in each crop plus
three asymmetric cross-crop point pairs. On the pinned Windows environment, the
test recorded these sampled forward, non-radial, and inverse/direct results:

| Region | Radial/vector difference, max | Cross-pair chart/geodesic distance difference, max | Inverse/direct round-trip, max |
|---|---:|---:|---:|
| Baltimore | 9.91829228645e-09 m | 1.82540020432e-06 m | 7.55076544561e-09 m |
| Golden | 3.91186343217e-08 m | 5.28600003236e-06 m | 3.45580740957e-08 m |

These are measured samples, not bounds over the crop or source accuracy
statements. The functional test uses 1e-4 m and 1e-5 m checks for the sampled
radial and inverse/direct numerical residuals; these are arithmetic sanity
checks, not global fidelity budgets. The cross-pair values report non-radial
chart distortion and have no pass threshold here. The Main Lead owns any
acceptance tolerance. This pilot does not claim a survey-accuracy bound, global
placement, NAVD88-to-ellipsoid conversion, full Earth coverage, generator-wide
BeamNG removal, Godot consumption, or rendered terrain. Those claims require
separate evidence and review.

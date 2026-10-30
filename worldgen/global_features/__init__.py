"""Source-aware, on-demand global feature bundles for WorldForge."""

from .addressing import TileAddress, TileBounds, point_to_tile, tiles_for_bbox
from .pipeline import GlobalFeatureGenerator
from .terrain import (
    TERRAIN_SAMPLER_INTERFACE_ID,
    HeightReference,
    TerrainSampler,
    TerrainSamples,
    TerrainSampleTrace,
    drape_road_centerline,
)

__all__ = [
    "TERRAIN_SAMPLER_INTERFACE_ID",
    "GlobalFeatureGenerator",
    "HeightReference",
    "TerrainSampleTrace",
    "TerrainSampler",
    "TerrainSamples",
    "TileAddress",
    "TileBounds",
    "drape_road_centerline",
    "point_to_tile",
    "tiles_for_bbox",
]

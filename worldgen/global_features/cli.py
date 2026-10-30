# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Command line entry point for globally addressable feature tiles."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .addressing import MAX_LEVEL, TileAddress
from .models import GenerationLimits
from .pipeline import DEFAULT_LAYERS, GlobalFeatureGenerator


def _default_cache_root() -> Path:
    return Path(tempfile.gettempdir()) / "WorldForge" / "global-feature-cache"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or resolve WorldForge global geographic content tiles."
    )
    parser.add_argument(
        "command",
        choices=("resolve-tile", "resolve-bbox"),
        help="resolve one canonical WFG-1 tile or a bounded bbox AOI",
    )
    parser.add_argument("--cache-root", type=Path, default=_default_cache_root())
    parser.add_argument("--tile-id", help="canonical WFG-1 ID: wfg1:<level>:<x>:<y>")
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH")
    )
    parser.add_argument("--level", type=int, default=0, choices=range(MAX_LEVEL + 1))
    parser.add_argument(
        "--layers", nargs="+", choices=DEFAULT_LAYERS, default=list(DEFAULT_LAYERS)
    )
    parser.add_argument("--max-request-tiles", type=_positive_int)
    parser.add_argument("--max-features-per-layer", type=_positive_int)
    parser.add_argument("--max-payload-bytes-per-layer", type=_positive_int)
    parser.add_argument("--max-shard-features", type=_positive_int)
    parser.add_argument("--max-shard-bytes", type=_positive_int)
    parser.add_argument("--max-feature-bytes", type=_positive_int)
    parser.add_argument("--max-raster-pixels", type=_positive_int)
    parser.add_argument("--max-bundle-bytes", type=_positive_int)
    parser.add_argument("--max-cache-bytes", type=_positive_int)
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "resolve-tile" and (not args.tile_id or args.bbox):
        raise SystemExit("resolve-tile requires --tile-id and does not accept --bbox")
    if args.command == "resolve-bbox" and (not args.bbox or args.tile_id):
        raise SystemExit("resolve-bbox requires --bbox and does not accept --tile-id")
    defaults = GenerationLimits()
    limits = GenerationLimits(
        max_request_tiles=args.max_request_tiles or defaults.max_request_tiles,
        max_features_per_layer=args.max_features_per_layer
        or defaults.max_features_per_layer,
        max_payload_bytes_per_layer=args.max_payload_bytes_per_layer
        or defaults.max_payload_bytes_per_layer,
        max_shard_features=args.max_shard_features or defaults.max_shard_features,
        max_shard_bytes=args.max_shard_bytes or defaults.max_shard_bytes,
        max_feature_bytes=args.max_feature_bytes or defaults.max_feature_bytes,
        max_raster_pixels=args.max_raster_pixels or defaults.max_raster_pixels,
        max_bundle_bytes=args.max_bundle_bytes or defaults.max_bundle_bytes,
        max_cache_bytes=args.max_cache_bytes or defaults.max_cache_bytes,
    )
    generator = GlobalFeatureGenerator(args.cache_root, limits=limits)
    try:
        if args.command == "resolve-tile":
            results = [
                generator.resolve_tile(
                    TileAddress.parse(args.tile_id), layers=args.layers
                )
            ]
        else:
            results = generator.resolve_bbox(
                *args.bbox, level=args.level, layers=args.layers
            )
    except KeyboardInterrupt:
        return 130
    document = [
        {
            "bundle_id": result.bundle_id,
            "tile_id": result.address.tile_id,
            "outcome": result.outcome.value,
            "cache_hit": result.cache_hit,
            "bundle_path": str(result.path) if result.path is not None else None,
            "layers": [
                {
                    "layer_id": layer["layer_id"],
                    "outcome": layer["outcome"],
                    "feature_count": layer.get("feature_count"),
                    "pixel_count": layer.get("pixel_count"),
                }
                for layer in result.manifest.get("layers", [])
            ],
        }
        for result in results
    ]
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

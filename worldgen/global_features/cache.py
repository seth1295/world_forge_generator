# SPDX-License-Identifier: MIT
# Copyright (c) 2026 WorldForge contributors
"""Content-addressed, integrity-checked and size-bounded local tile cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_ID = "urn:worldforge:global-feature-bundle:1.0.0"
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_MAX_BYTES = 16 * 1024 * 1024


class CacheError(RuntimeError):
    """Raised for an invalid, busy, or over-quota global feature cache."""


class CacheStore:
    def __init__(self, root: str | Path, *, max_cache_bytes: int) -> None:
        if max_cache_bytes <= 0:
            raise ValueError("max_cache_bytes must be positive")
        self.root = Path(root)
        self.max_cache_bytes = max_cache_bytes
        self.bundles_dir = self.root / "worldforge_global_features" / "v1"

    def prepare(self) -> None:
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        if self.bundles_dir.is_symlink():
            raise CacheError("cache bundle directory must not be a symlink")
        self._assert_inside_root(self.bundles_dir)

    def path_for_key(self, key: str) -> Path:
        if not _KEY_RE.fullmatch(key):
            raise ValueError("cache key must be lowercase SHA-256 hex")
        self.prepare()
        path = self.bundles_dir / key
        self._assert_inside_root(path)
        return path

    def existing(self, key: str) -> Path | None:
        path = self.path_for_key(key)
        if not path.exists():
            return None
        self.verify(path, expected_key=key)
        return path

    def acquire(self, key: str) -> Path:
        self.prepare()
        lock = self.bundles_dir / f".{key}.lock"
        self._assert_inside_root(lock)
        try:
            with lock.open("x", encoding="ascii") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as error:
            raise CacheError("tile bundle generation is already in progress") from error
        return lock

    def create_stage(self, key: str) -> Path:
        self.prepare()
        stage = Path(
            tempfile.mkdtemp(prefix=f".stage-{key[:12]}-", dir=self.bundles_dir)
        )
        self._assert_inside_root(stage)
        (stage / ".wfg-staging").write_text(key, encoding="ascii")
        return stage

    def publish(self, key: str, stage: Path) -> Path:
        destination = self.path_for_key(key)
        self._assert_inside_root(stage)
        if (
            stage.parent.resolve() != self.bundles_dir.resolve()
            or not stage.name.startswith(f".stage-{key[:12]}-")
        ):
            raise CacheError("staging directory is outside the cache's owned path")
        if destination.exists():
            self.verify(destination, expected_key=key)
            self.cleanup_stage(stage, key)
            return destination
        (stage / ".wfg-staging").unlink(missing_ok=True)
        os.replace(stage, destination)
        self.verify(destination, expected_key=key)
        self.trim(active_key=key)
        return destination

    def cleanup_stage(self, stage: Path, key: str) -> None:
        self._assert_inside_root(stage)
        if (
            stage.parent.resolve() != self.bundles_dir.resolve()
            or not stage.name.startswith(f".stage-{key[:12]}-")
        ):
            raise CacheError("refusing to remove a non-owned staging path")
        marker = stage / ".wfg-staging"
        if marker.exists() and marker.read_text(encoding="ascii") != key:
            raise CacheError("staging ownership marker does not match")
        shutil.rmtree(stage)

    def release(self, lock: Path) -> None:
        self._assert_inside_root(lock)
        if (
            lock.parent.resolve() != self.bundles_dir.resolve()
            or not lock.name.endswith(".lock")
        ):
            raise CacheError("refusing to remove a non-owned lock path")
        lock.unlink(missing_ok=True)

    def verify(
        self, bundle_path: Path, *, expected_key: str | None = None
    ) -> dict[str, Any]:
        resolved = bundle_path.resolve(strict=True)
        bundles_root = self.bundles_dir.resolve(strict=True)
        if not resolved.is_relative_to(bundles_root) or bundle_path.is_symlink():
            raise CacheError("bundle path escaped the cache root")
        manifest_path = resolved / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise CacheError("bundle has no regular manifest.json")
        if manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
            raise CacheError("bundle manifest exceeds the size limit")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CacheError("bundle manifest is corrupt") from error
        if manifest.get("schema_id") != SCHEMA_ID:
            raise CacheError("bundle manifest schema is not recognized")
        key = manifest.get("bundle_id")
        if expected_key is not None and key != expected_key:
            raise CacheError("bundle ID does not match its cache key")
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise CacheError("bundle manifest has an invalid cache key")
        for layer in manifest.get("layers", []):
            for payload in layer.get("payloads", []):
                relative = payload.get("path")
                if not isinstance(relative, str):
                    raise CacheError("payload path is malformed")
                candidate = self._safe_payload_path(resolved, relative)
                try:
                    digest, size = _sha256_file(candidate)
                except OSError as error:
                    raise CacheError(
                        "bundle payload is missing or unreadable"
                    ) from error
                if size != payload.get("size_bytes") or digest != payload.get("sha256"):
                    raise CacheError("bundle payload checksum mismatch")
        return manifest

    def trim(self, *, active_key: str) -> None:
        self.prepare()
        bundles: list[tuple[float, Path, int]] = []
        total = 0
        for child in self.bundles_dir.iterdir():
            if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
                continue
            if not _KEY_RE.fullmatch(child.name):
                continue
            self._assert_inside_root(child)
            try:
                manifest = self.verify(child, expected_key=child.name)
            except CacheError:
                # Corrupt or foreign entries are preserved for diagnosis, never followed or evicted.
                continue
            if manifest.get("schema_id") != SCHEMA_ID:
                continue
            size = _directory_size(child)
            total += size
            bundles.append((child.stat().st_mtime, child, size))
        if total <= self.max_cache_bytes:
            return
        for _, child, size in sorted(bundles, key=lambda row: (row[0], row[1].name)):
            if child.name == active_key:
                continue
            self._assert_inside_root(child)
            shutil.rmtree(child)
            total -= size
            if total <= self.max_cache_bytes:
                return
        raise CacheError("cache quota cannot be met without evicting the active bundle")

    def _safe_payload_path(self, bundle_path: Path, relative: str) -> Path:
        if "\\" in relative:
            raise CacheError("payload paths must use forward slashes")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise CacheError("payload path is not a safe relative path")
        candidate = bundle_path.joinpath(*pure.parts)
        if candidate.is_symlink():
            raise CacheError("symbolic links are not permitted in bundles")
        resolved = candidate.resolve(strict=True)
        if (
            not resolved.is_relative_to(bundle_path.resolve(strict=True))
            or not resolved.is_file()
        ):
            raise CacheError("payload path escaped the bundle directory")
        return resolved

    def _assert_inside_root(self, path: Path) -> None:
        root = self.bundles_dir.resolve(strict=False)
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise CacheError("cache path escaped its owned root")


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _directory_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise CacheError("symbolic links are not permitted in cache bundles")
        if path.is_file():
            total += path.stat().st_size
    return total

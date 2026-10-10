# -*- coding: utf-8 -*-
"""Stage SAR products from a local archive into a workdir ``slc/`` tree.

Keeps a central image library on disk and places per-stack working copies
(symlink by default, or hardlink / copy) instead of re-downloading from ASF.

Archive layout is free-form: flat, nested by path/frame, or mixed. Products
are matched by ASF ``fileName``, ``.SAFE`` directory name, or bare
``sceneName`` (with ``.zip`` / ``.SAFE`` suffixes tried in that order).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVE_MODES = ("symlink", "hardlink", "copy")

# Product suffixes we index. Directories ending in .SAFE are treated as products.
_FILE_SUFFIXES = (".zip", ".tif", ".tiff", ".h5", ".EOF", ".eof")


@dataclass
class StageResult:
    """Outcome of staging one product (or a batch)."""

    staged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.failed


def normalize_archive_mode(mode: str | None) -> str:
    m = (mode or "symlink").strip().lower()
    if m not in ARCHIVE_MODES:
        raise ValueError(
            f"archive_mode must be one of {ARCHIVE_MODES}, got {mode!r}"
        )
    return m


def build_archive_index(archive_dir: Path | str) -> dict[str, Path]:
    """Walk *archive_dir* and map lookup keys → product path.

    Keys include the basename (``S1A_….zip``), the stem, and for ``.SAFE``
    directories the directory name. First hit wins; nested duplicates are ignored.
    """
    root = Path(archive_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"local_archive is not a directory: {root}")

    index: dict[str, Path] = {}

    def _add(key: str, path: Path) -> None:
        if key and key not in index:
            index[key] = path

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        base = Path(dirpath)
        # Index .SAFE directories before descending into them (avoid indexing
        # every annotation tiff inside as a top-level product).
        safe_dirs = [d for d in dirnames if d.upper().endswith(".SAFE")]
        for name in safe_dirs:
            path = base / name
            _add(name, path)
            _add(name.upper(), path)
            stem = name[:-5] if name.upper().endswith(".SAFE") else name
            _add(stem, path)
            _add(stem.upper(), path)
        # Do not walk into .SAFE trees
        dirnames[:] = [d for d in dirnames if not d.upper().endswith(".SAFE")]

        for name in filenames:
            path = base / name
            lower = name.lower()
            if not any(lower.endswith(suf.lower()) for suf in _FILE_SUFFIXES):
                continue
            _add(name, path)
            _add(name.upper(), path)
            stem = Path(name).stem
            # S1 zips are often SceneName.zip — stem == sceneName
            _add(stem, path)
            _add(stem.upper(), path)
            # Double extensions like .SAFE.zip
            if lower.endswith(".safe.zip"):
                stem2 = name[:-9] if len(name) > 9 else stem
                _add(stem2, path)
                _add(f"{stem2}.SAFE", path)
                _add(f"{stem2}.SAFE".upper(), path)

    return index


def find_in_archive(
    index: dict[str, Path],
    *,
    file_name: str | None = None,
    scene_name: str | None = None,
) -> Path | None:
    """Resolve a product path from an archive index."""
    candidates: list[str] = []
    if file_name:
        candidates.append(file_name)
        candidates.append(file_name.upper())
        candidates.append(Path(file_name).stem)
    if scene_name:
        candidates.append(scene_name)
        candidates.append(scene_name.upper())
        for suffix in (".zip", ".ZIP", ".SAFE", ".safe", ".SAFE.zip"):
            candidates.append(f"{scene_name}{suffix}")

    for key in candidates:
        hit = index.get(key)
        if hit is not None and (hit.exists() or hit.is_symlink()):
            return hit
    return None


def find_orbit_in_archive(
    index: dict[str, Path],
    acquisition_time: str,
) -> Path | None:
    """Find an ``.EOF`` whose validity window covers *acquisition_time*.

    *acquisition_time* is the compact UTC stamp from a Sentinel-1 scene name
    (``YYYYMMDDTHHMMSS``), same convention as ``S1_SLC.download_orbit``.
    """
    acq = acquisition_time.strip()
    if not acq:
        return None
    for key, path in index.items():
        if not key.upper().endswith(".EOF"):
            continue
        parts = path.stem.split("_V")
        if len(parts) != 2:
            continue
        validity = parts[1].split("_")
        if len(validity) == 2 and validity[0] <= acq <= validity[1]:
            if path.exists() or path.is_symlink():
                return path
    return None


def stage_path(
    src: Path,
    dest_dir: Path,
    mode: str = "symlink",
    *,
    dest_name: str | None = None,
) -> str:
    """Place *src* into *dest_dir* using *mode*.

    Returns one of ``staged``, ``skipped``.
    """
    mode = normalize_archive_mode(mode)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (dest_name or src.name)

    if dest.exists() or dest.is_symlink():
        # Already present — leave alone (same policy as download skip).
        return "skipped"

    src = src.resolve()
    if mode == "symlink":
        dest.symlink_to(src)
    elif mode == "hardlink":
        if src.is_dir():
            # Hardlink trees are not portable; fall back to symlink for dirs.
            dest.symlink_to(src)
        else:
            os.link(src, dest)
    else:  # copy
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)
    return "staged"


def stage_scenes(
    scene_names: list[str],
    archive_dir: Path | str,
    dest_dir: Path | str,
    mode: str = "symlink",
    *,
    index: dict[str, Path] | None = None,
    require_all: bool = False,
    stage_orbits: bool = True,
) -> StageResult:
    """Stage each scene (and optionally a covering orbit) into *dest_dir*."""
    index = index if index is not None else build_archive_index(archive_dir)
    mode = normalize_archive_mode(mode)
    dest = Path(dest_dir)
    result = StageResult()

    for scene in scene_names:
        scene = str(scene).strip()
        if not scene:
            continue
        src = find_in_archive(index, scene_name=scene)
        if src is None:
            result.missing.append(scene)
            continue
        try:
            status = stage_path(src, dest, mode=mode)
            (result.skipped if status == "skipped" else result.staged).append(
                f"{scene} → {src.name}"
            )
        except Exception as exc:  # noqa: BLE001 — report per-scene, keep going
            result.failed.append((scene, str(exc)))
            continue

        if stage_orbits:
            # Acquisition time token is field 5 of the SAFE/scene name.
            parts = scene.replace("__", "_").split("_")
            acq = parts[4] if len(parts) >= 5 else ""
            eof = find_orbit_in_archive(index, acq) if acq else None
            if eof is not None:
                try:
                    status = stage_path(eof, dest, mode=mode)
                    bucket = result.skipped if status == "skipped" else result.staged
                    bucket.append(f"orbit:{eof.name}")
                except Exception as exc:  # noqa: BLE001
                    result.failed.append((eof.name, str(exc)))

    if require_all and result.missing:
        raise FileNotFoundError(
            f"{len(result.missing)} scene(s) missing from local_archive "
            f"{Path(archive_dir)}: {result.missing[:5]}"
            + ("…" if len(result.missing) > 5 else "")
        )
    return result


def scenes_from_stack_file(stack_path: Path | str) -> list[str]:
    """Read scene names from a ``stack_p*_f*.json`` file."""
    data = json.loads(Path(stack_path).read_text())
    scenes = data.get("scenes") or []
    if scenes:
        return [str(s) for s in scenes]
    # Fall back to unique names in pairs
    names: list[str] = []
    seen: set[str] = set()
    for pair in data.get("pairs") or []:
        for item in pair[:2]:
            s = str(item)
            if s not in seen:
                seen.add(s)
                names.append(s)
    return names


def stage_from_stack_file(
    stack_path: Path | str,
    archive_dir: Path | str,
    dest_dir: Path | str | None = None,
    mode: str = "symlink",
    *,
    require_all: bool = False,
    stage_orbits: bool = True,
) -> StageResult:
    """Stage every scene listed in a stack JSON into *dest_dir*.

    When *dest_dir* is omitted, uses ``<stack_dir>/slc``.
    """
    stack_path = Path(stack_path)
    if dest_dir is None:
        dest_dir = stack_path.parent / "slc"
    scenes = scenes_from_stack_file(stack_path)
    return stage_scenes(
        scenes,
        archive_dir,
        dest_dir,
        mode=mode,
        require_all=require_all,
        stage_orbits=stage_orbits,
    )


def maybe_stage_for_processor(config, pairs: list) -> StageResult | None:
    """If *config.local_archive* is set, stage pair scenes into ``slc_dir``.

    Returns ``None`` when no archive is configured. Raises ``FileNotFoundError``
    when any scene is missing from the archive.
    """
    archive = getattr(config, "local_archive", None)
    if not archive:
        return None
    slc_dir = getattr(config, "slc_dir", None)
    if slc_dir is None:
        workdir = getattr(config, "workdir", None) or Path.cwd()
        slc_dir = Path(workdir) / "slc"
    mode = getattr(config, "archive_mode", "symlink") or "symlink"
    result = stage_from_pairs(
        pairs, archive, slc_dir, mode=mode, require_all=True, stage_orbits=True,
    )
    print(
        f"Local archive → {slc_dir}: "
        f"{len(result.staged)} staged, {len(result.skipped)} skipped"
    )
    return result


def stage_from_pairs(
    pairs: list,
    archive_dir: Path | str,
    dest_dir: Path | str,
    mode: str = "symlink",
    *,
    require_all: bool = False,
    stage_orbits: bool = True,
) -> StageResult:
    """Stage unique reference/secondary scene names from a pairs list."""
    names: list[str] = []
    seen: set[str] = set()
    for pair in pairs:
        if not pair:
            continue
        for item in pair[:2]:
            # Strip .SAFE / path prefixes if a 4-tuple GMTSAR pair leaked in
            s = Path(str(item)).name
            if s.upper().endswith(".SAFE"):
                s = s[:-5]
            if s.lower().endswith(".zip"):
                s = s[:-4]
            if s not in seen:
                seen.add(s)
                names.append(s)
    return stage_scenes(
        names,
        archive_dir,
        dest_dir,
        mode=mode,
        require_all=require_all,
        stage_orbits=stage_orbits,
    )

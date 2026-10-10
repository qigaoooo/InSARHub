"""Tier 2 — local SAR archive staging (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insarhub.config.defaultconfig import ASF_Base_Config, ISCE2_S1_Config
from insarhub.utils.local_archive import (
    build_archive_index,
    find_in_archive,
    find_orbit_in_archive,
    maybe_stage_for_processor,
    stage_from_stack_file,
    stage_path,
    stage_scenes,
)


SCENE = "S1A_IW_SLC__1SDV_20200101T000000_20200101T000030_030000_036ABC_1234"
SCENE2 = "S1A_IW_SLC__1SDV_20200113T000000_20200113T000030_030200_036DEF_5678"
EOF_NAME = "S1A_OPER_AUX_POEORB_OPOD_20200110T120000_V20200101T000000_20200103T000000.EOF"


def _touch(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_build_index_finds_nested_zip_and_safe(tmp_path: Path):
    zip_path = _touch(tmp_path / "p100_f466" / f"{SCENE}.zip")
    safe_dir = tmp_path / "other" / f"{SCENE2}.SAFE"
    safe_dir.mkdir(parents=True)
    (safe_dir / "manifest.safe").write_text("x")

    index = build_archive_index(tmp_path)
    assert find_in_archive(index, file_name=f"{SCENE}.zip") == zip_path.resolve()
    assert find_in_archive(index, scene_name=SCENE) == zip_path.resolve()
    assert find_in_archive(index, scene_name=SCENE2) == safe_dir.resolve()
    assert find_in_archive(index, file_name=f"{SCENE2}.SAFE") == safe_dir.resolve()


def test_stage_symlink_and_skip(tmp_path: Path):
    archive = tmp_path / "archive"
    dest = tmp_path / "slc"
    src = _touch(archive / f"{SCENE}.zip")

    assert stage_path(src, dest, mode="symlink") == "staged"
    link = dest / f"{SCENE}.zip"
    assert link.is_symlink()
    assert link.resolve() == src.resolve()
    assert stage_path(src, dest, mode="symlink") == "skipped"


def test_stage_scenes_with_orbit(tmp_path: Path):
    archive = tmp_path / "archive"
    dest = tmp_path / "slc"
    _touch(archive / f"{SCENE}.zip")
    _touch(archive / "orbits" / EOF_NAME)

    result = stage_scenes([SCENE], archive, dest, mode="symlink", stage_orbits=True)
    assert result.ok
    assert (dest / f"{SCENE}.zip").is_symlink()
    assert (dest / EOF_NAME).is_symlink()
    assert not result.missing


def test_stage_from_stack_file(tmp_path: Path):
    archive = tmp_path / "archive"
    work = tmp_path / "p100_f466"
    work.mkdir()
    _touch(archive / f"{SCENE}.zip")
    _touch(archive / f"{SCENE2}.zip")
    stack = work / "stack_p100_f466.json"
    stack.write_text(json.dumps({
        "pairs": [[SCENE, SCENE2]],
        "scenes": [SCENE, SCENE2],
        "baselines": {},
    }))

    result = stage_from_stack_file(stack, archive, mode="symlink", require_all=True)
    assert result.ok
    assert (work / "slc" / f"{SCENE}.zip").exists()
    assert (work / "slc" / f"{SCENE2}.zip").exists()


def test_require_all_raises_on_missing(tmp_path: Path):
    archive = tmp_path / "archive"
    archive.mkdir()
    with pytest.raises(FileNotFoundError):
        stage_scenes(
            [SCENE], archive, tmp_path / "slc",
            mode="symlink", require_all=True,
        )


def test_asf_config_resolves_local_archive(tmp_path: Path):
    cfg = ASF_Base_Config(local_archive=str(tmp_path), archive_mode="copy", archive_only=True)
    assert cfg.local_archive == tmp_path.resolve()
    assert cfg.archive_mode == "copy"
    assert cfg.archive_only is True
    assert "local_archive" in ASF_Base_Config._ui_fields


def test_asf_config_rejects_bad_mode():
    with pytest.raises(ValueError, match="archive_mode"):
        ASF_Base_Config(archive_mode="move")


def test_maybe_stage_for_processor(tmp_path: Path):
    archive = tmp_path / "archive"
    work = tmp_path / "job"
    _touch(archive / f"{SCENE}.zip")
    _touch(archive / f"{SCENE2}.zip")
    cfg = ISCE2_S1_Config(
        workdir=str(work),
        local_archive=str(archive),
        archive_mode="symlink",
    )
    result = maybe_stage_for_processor(cfg, [(SCENE, SCENE2)])
    assert result is not None
    assert result.ok
    assert (cfg.slc_dir / f"{SCENE}.zip").is_symlink()


def test_find_orbit_by_validity_window(tmp_path: Path):
    archive = tmp_path / "archive"
    eof = _touch(archive / EOF_NAME)
    index = build_archive_index(archive)
    assert find_orbit_in_archive(index, "20200101T120000") == eof.resolve()
    assert find_orbit_in_archive(index, "20190101T120000") is None


def test_non_search_fields_include_archive_keys():
    from insarhub.downloader.asf_base import ASF_Base_Downloader
    for key in ("local_archive", "archive_mode", "archive_only"):
        assert key in ASF_Base_Downloader._NON_SEARCH_FIELDS

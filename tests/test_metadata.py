"""Phase 2: DatasetMetadata 与工具版本检测测试。

覆盖：
  - OpenVSP 版本检测（目录名 / CHANGELOG.md / 未知回退）
  - XFOIL 版本检测（运行 banner，本机有 XFOIL 时验证）
  - DatasetMetadata to_dict/from_dict 往返
  - build_metadata（版本检测 + mission/geometry id 回填）
  - SQLite dataset_metadata 表读写往返
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database.sqlite_manager import SQLiteManager
from src.dataset.metadata import (PLATFORM_VERSION, DatasetMetadata, build_metadata,
                                  detect_openvsp_version, detect_vspaero_version,
                                  detect_xfoil_version)

XFOIL_EXE = Path("C:/Xfoil/xfoil.exe")
requires_xfoil = pytest.mark.skipif(not XFOIL_EXE.exists(), reason="本机未安装 XFOIL")


# ---------------- OpenVSP 版本检测 ----------------
def test_detect_openvsp_from_dirname():
    # 仅依赖目录名解析，不要求文件真实存在
    ver = detect_openvsp_version("C:/OpenVSP-3.55.0-win64/vspscript.exe")
    assert ver == "3.55.0"


def test_detect_openvsp_underscore_dirname():
    ver = detect_openvsp_version("/opt/OpenVSP_4.2.1/vspscript.exe")
    assert ver == "4.2.1"


def test_detect_openvsp_from_changelog(tmp_path):
    d = tmp_path / "OpenVSP"
    d.mkdir()
    (d / "CHANGELOG.md").write_text(
        "# [OpenVSP 3.55.0](https://github.com/OpenVSP/OpenVSP/releases)\n",
        encoding="utf-8")
    ver = detect_openvsp_version(d / "vspscript.exe")
    assert ver == "3.55.0"


def test_detect_openvsp_unknown(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    ver = detect_openvsp_version(d / "vspscript.exe")
    assert ver == "unknown"


def test_detect_vspaero_matches_openvsp():
    ver = detect_vspaero_version("C:/OpenVSP-3.55.0-win64/vspscript.exe")
    assert ver == detect_openvsp_version("C:/OpenVSP-3.55.0-win64/vspscript.exe")


# ---------------- XFOIL 版本检测 ----------------
@requires_xfoil
def test_detect_xfoil_version_real():
    ver = detect_xfoil_version(XFOIL_EXE)
    assert ver != "unknown"
    # 版本号形如 6.99
    assert ver.replace(".", "").isdigit()


def test_detect_xfoil_missing_file():
    ver = detect_xfoil_version("C:/no_such_dir/xfoil.exe")
    assert ver == "unknown"


# ---------------- DatasetMetadata ----------------
def test_metadata_roundtrip():
    m = DatasetMetadata(
        run_id="run_001", random_seed=42, sample_method="lhs",
        openvsp_version="3.55.0", xfoil_version="6.99",
        dataset_id="ds_001", extra={"note": "x"},
    )
    d = m.to_dict()
    assert d["run_id"] == "run_001"
    assert d["random_seed"] == 42
    assert d["generated_at"] != ""
    m2 = DatasetMetadata.from_dict(d)
    assert m2.run_id == m.run_id
    assert m2.random_seed == 42
    assert m2.extra == {"note": "x"}


def test_metadata_defaults_generated_at():
    m = DatasetMetadata(run_id="r")
    assert m.generated_at != ""
    assert m.platform_version == PLATFORM_VERSION
    assert m.openvsp_version == "unknown"


def test_build_metadata_detects_versions_and_backfills_ids():
    tools = {
        "openvsp_script": "C:/OpenVSP-3.55.0-win64/vspscript.exe",
        "xfoil": str(XFOIL_EXE),
    }
    m = build_metadata("run_9", dataset_id="ds", random_seed=7,
                       sample_method="random", tools=tools)
    assert m.run_id == "run_9"
    assert m.mission_id == "run_9"          # 缺省回填 run_id
    assert m.geometry_id == "run_9"
    assert m.dataset_id == "ds"
    assert m.random_seed == 7
    assert m.openvsp_version == "3.55.0"    # 目录名解析
    assert m.vspaero_version == "3.55.0"


def test_build_metadata_explicit_ids():
    m = build_metadata("run", mission_id="m1", geometry_id="g1")
    assert m.mission_id == "m1"
    assert m.geometry_id == "g1"


# ---------------- SQLite dataset_metadata ----------------
def test_sqlite_metadata_roundtrip(tmp_path):
    db_path = tmp_path / "meta_test.sqlite"
    db = SQLiteManager(db_path)
    try:
        meta = DatasetMetadata(
            run_id="run_001", dataset_id="ds_001", random_seed=42,
            sample_method="lhs", openvsp_version="3.55.0",
            xfoil_version="6.99", vspaero_version="3.55.0",
            mission_id="run_001", geometry_id="run_001",
        )
        db.save_metadata(meta)

        got = db.get_metadata("run_001")
        assert got is not None
        assert got["run_id"] == "run_001"
        assert got["random_seed"] == 42
        assert got["openvsp_version"] == "3.55.0"
        assert got["xfoil_version"] == "6.99"

        assert db.count_metadata() == 1
        assert db.count_metadata("ds_001") == 1
        assert db.count_metadata("no_such_ds") == 0

        rows = db.list_metadata()
        assert len(rows) == 1
        assert rows[0]["sample_method"] == "lhs"

        # 不存在返回 None
        assert db.get_metadata("run_missing") is None

        # 覆盖更新
        db.save_metadata({"run_id": "run_001", "random_seed": 99,
                          "generated_at": "2026-11-25T00:00:00"})
        got2 = db.get_metadata("run_001")
        assert got2["random_seed"] == 99
        assert db.count_metadata() == 1   # 仍只有一条
    finally:
        db.close()


def test_sqlite_metadata_schema_created_on_existing_db(tmp_path):
    """旧库重连也应自动建 dataset_metadata 表（IF NOT EXISTS）。"""
    db_path = tmp_path / "schema.sqlite"
    db = SQLiteManager(db_path)
    db.close()
    # 再次打开，表应仍在（SCHEMA 幂等）
    db2 = SQLiteManager(db_path)
    try:
        assert db2.count_metadata() == 0
    finally:
        db2.close()
